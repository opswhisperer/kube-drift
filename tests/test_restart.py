"""Restart now: a row's workload restarted through Argo CD's action. All data is invented."""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from app import argocd, http, main
from app.scan import Scanner
from tests.fake_k8s import FakeK8s

RES = {"application": "shop", "app_namespace": "argocd", "project": "default", "group": "apps", "version": "v1",
       "kind": "Deployment", "namespace": "shop", "name": "web"}
CFG = {"enabled": True, "url": "https://argo.example/"}
ROW = {"id": "workload/shop/web/web", "category": "manifest", "name": "web", "namespace": "shop", "status": "outdated",
       "restart_target": "deployment/web", "restart_command": "kubectl -n shop rollout restart deployment/web",
       "argocd_resource": RES}


class Configured(unittest.TestCase):
    def test_it_takes_a_token_and_an_address(self):
        with mock.patch.dict(os.environ, {"ARGOCD_TOKEN": "t"}):
            self.assertTrue(argocd.can_restart(CFG))
            self.assertFalse(argocd.can_restart({"enabled": True}))
            self.assertFalse(argocd.can_restart({"url": "https://argo.example"}))
        with mock.patch.dict(os.environ, clear=True):
            self.assertFalse(argocd.can_restart(CFG))

    def test_restart_url_overrides_the_ui_address(self):
        cfg = {**CFG, "restart": {"url": "http://argocd-server.argocd.svc/"}}
        self.assertEqual(argocd.restart_cfg(cfg)["url"], "http://argocd-server.argocd.svc")


class Restart(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, {"ARGOCD_TOKEN": "t"})
        p.start()
        self.addCleanup(p.stop)

    def test_it_sends_the_v2_action(self):
        with mock.patch.object(http, "request") as req:
            out = argocd.restart(CFG, RES)
        self.assertEqual(out, {"status": "restarting", "application": "shop"})
        url, kw = req.call_args.args[0], req.call_args.kwargs
        self.assertEqual(url, "https://argo.example/api/v1/applications/shop/resource/actions/v2")
        self.assertEqual(kw["headers"]["Authorization"], "Bearer t")
        self.assertEqual(json.loads(kw["data"]), {
            "name": "shop", "appNamespace": "argocd", "project": "default", "namespace": "shop", "resourceName": "web",
            "group": "apps", "version": "v1", "kind": "Deployment", "action": "restart"})

    def test_a_refusal_says_what_to_check_and_hides_the_token(self):
        with mock.patch.object(http, "request", side_effect=http.HttpError(403, "u")):
            with self.assertRaises(argocd.RestartError) as e:
                argocd.restart(CFG, RES)
        self.assertEqual(e.exception.status, 502)
        self.assertIn("restart action", str(e.exception))
        self.assertNotIn("Bearer", str(e.exception))

    def test_argo_cd_unreachable(self):
        with mock.patch.object(http, "request", side_effect=OSError("no route")):
            with self.assertRaises(argocd.RestartError) as e:
                argocd.restart(CFG, RES)
        self.assertEqual(e.exception.status, 502)


class Scan(unittest.TestCase):
    def test_a_workload_in_an_application_is_named_as_argo_cd_names_it(self):
        class K(FakeK8s):
            def workloads(self):
                return [{"kind": "Deployment", "metadata": {"name": "web", "namespace": "shop"},
                         "spec": {"strategy": {"type": "Recreate"}, "template": {"spec": {"containers": [
                             {"name": "c", "image": "acme/web:1.0.0", "imagePullPolicy": "Always"}]}}}}]

            def pods(self, namespace=None):
                return []

        sc = Scanner(K(), {})
        sc.argo_restart[("Deployment", "shop", "web")] = RES
        comp = next(c for c in sc._workloads() if c["image_repo"] == "acme/web")
        self.assertEqual((comp["argocd_resource"], comp["update_strategy"]), (RES, "Recreate"))


class Endpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (main.AUTH, dict(main.CONFIG), main.STATE.result)
        main.AUTH = None
        main.CONFIG.clear()
        main.CONFIG["argocd"] = CFG
        main.STATE.result = {"scanned_at": None, "summary": {}, "components": [
            ROW, {**ROW, "id": "workload/shop/old/old", "argocd_resource": None},
            {**ROW, "id": "workload/shop/ok/ok", "restart_command": None}]}
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), main.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        p = mock.patch.dict(os.environ, {"ARGOCD_TOKEN": "t"})
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        main.AUTH, cfg, main.STATE.result = self.saved
        main.CONFIG.clear()
        main.CONFIG.update(cfg)
        self.tmp.cleanup()

    def post(self, obj):
        req = urllib.request.Request(self.base + "/api/drift/restart", data=json.dumps(obj).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_a_row_is_restarted_from_what_the_scan_saw(self):
        with mock.patch.object(argocd, "restart", return_value={"status": "restarting", "application": "shop"}) as r:
            code, res = self.post({"id": ROW["id"], "application": "other", "name": "other"})
        self.assertEqual((code, res["status"]), (200, "restarting"))
        self.assertEqual(r.call_args.args[1], RES)   # nothing the caller sent but the id

    def test_unknown_and_ineligible_rows_never_reach_argo_cd(self):
        with mock.patch.object(argocd, "restart") as r:
            self.assertEqual(self.post({"id": "nope"})[0], 404)
            self.assertEqual(self.post({"id": "workload/shop/old/old"})[0], 409)
            self.assertEqual(self.post({"id": "workload/shop/ok/ok"})[0], 409)
        r.assert_not_called()

    def test_an_api_key_cannot_restart(self):
        main.AUTH = mock.Mock()
        main.AUTH.identify.return_value = {"via": "api-key", "key": {"name": "ci"}}
        with mock.patch.object(argocd, "restart") as r:
            self.assertEqual(self.post({"id": ROW["id"]})[0], 403)
        r.assert_not_called()

    def test_not_configured(self):
        main.CONFIG["argocd"] = {"enabled": True}
        self.assertEqual(self.post({"id": ROW["id"]})[0], 503)

    def test_the_dashboard_is_told(self):
        with urllib.request.urlopen(self.base + "/api/drift") as r:
            self.assertIs(json.loads(r.read())["can_restart"], True)
