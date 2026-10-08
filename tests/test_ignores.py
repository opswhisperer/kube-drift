"""Ignoring a component's update until a date, a Kubernetes change, or a newer version."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from app import main
from app.scan import apply_ignores
from app.store import Store

NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)


def scan(kube="v1.35.9", latest="1.12.4", latest_any=None, digest="sha256:a"):
    return {"scanned_at": "2026-10-08T00:00:00+00:00", "summary": {"total": 3, "outdated": 2, "current": 1}, "components": [
        {"id": "cluster/kubernetes", "category": "cluster", "installed": kube, "status": "current"},
        {"id": "helm/longhorn-system/longhorn", "category": "helm", "installed": "1.10.1", "latest": latest,
         "latest_app": "v" + latest, "latest_any": latest_any, "status": "outdated"},
        {"id": "workload/apps/web/web", "category": "manifest", "installed": "latest", "latest": None,
         "remote_digest": digest, "status": "outdated"},
        {"id": "helm/apps/widget", "category": "helm", "installed": "2.0.0", "latest": "2.1.0", "status": "outdated"},
        {"id": "workload/longhorn-system/manager/longhorn-manager", "category": "helm-workload",
         "release": "helm/longhorn-system/longhorn", "status": "outdated"},
    ]}


class ApplyIgnores(unittest.TestCase):
    def ignored(self, ig, **kw):
        res, ended = apply_ignores(scan(**kw), {"helm/longhorn-system/longhorn": ig}, NOW)
        return next(c for c in res["components"] if c["id"] == "helm/longhorn-system/longhorn").get("ignored"), ended, res

    def ignored_image(self, ig, **kw):
        res, _ = apply_ignores(scan(**kw), {"workload/apps/web/web": ig}, NOW)
        return next(c for c in res["components"] if c["id"] == "workload/apps/web/web").get("ignored")

    def test_summary_moves_to_ignored(self):
        ig, ended, res = self.ignored({"until": "2026-11-01T00:00:00+00:00"})
        self.assertTrue(ig)
        self.assertEqual(ended, set())
        self.assertEqual(res["summary"], {"total": 2, "outdated": 1, "current": 1, "ignored": 1})

    def test_ends_at_date(self):
        ig, ended, res = self.ignored({"until": "2026-10-01T00:00:00+00:00"})
        self.assertIsNone(ig)
        self.assertEqual(ended, {"helm/longhorn-system/longhorn"})
        self.assertEqual(res["summary"]["outdated"], 2)

    def test_ends_when_kubernetes_changes(self):
        self.assertTrue(self.ignored({"kube_version": "v1.35.9"})[0])
        self.assertIsNone(self.ignored({"kube_version": "v1.35.9"}, kube="v1.36.0")[0])

    def test_ends_at_the_next_version(self):
        ig = {"until_newer": True, "latest": "1.12.4", "latest_any": "2.0.0"}
        self.assertTrue(self.ignored(ig, latest_any="2.0.0")[0])  # an update outside the track was already there
        self.assertIsNone(self.ignored(ig, latest="1.12.5", latest_any="2.0.0")[0])
        self.assertIsNone(self.ignored(ig, latest_any="2.1.0")[0])

    def test_floating_tag_ends_when_its_image_changes(self):
        ig = {"until_newer": True, "latest": None, "digest": "sha256:a"}
        self.assertTrue(self.ignored_image(ig))
        self.assertIsNone(self.ignored_image(ig, digest="sha256:b"))

    def test_first_condition_wins(self):
        ig = {"until": "2027-01-01T00:00:00+00:00", "until_newer": True, "latest": "1.12.4", "kube_version": "v1.35.9"}
        self.assertTrue(self.ignored(ig)[0])
        self.assertIsNone(self.ignored(ig, latest="1.13.0")[0])
        self.assertIsNone(self.ignored(ig, kube="v1.36.0")[0])


class Api(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (main.ORPHANS, main.STATE.result)
        main.ORPHANS = main.OrphanState(Store(Path(self.tmp.name)))
        main.STATE.result = scan()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), main.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        main.ORPHANS, main.STATE.result = self.saved
        self.tmp.cleanup()

    def post(self, path, body):
        req = urllib.request.Request(self.base + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def drift(self):
        with urllib.request.urlopen(self.base + "/api/drift") as r:
            return {c["id"]: c for c in json.loads(r.read())["components"]}

    def test_ignore_and_unignore(self):
        lh = "helm/longhorn-system/longhorn"
        status, res = self.post("/api/drift/ignore", {"ids": [lh, "nope"], "days": 14, "until_kube_change": True,
                                                      "until_newer": True, "note": "waiting for 1.13"})
        self.assertEqual((status, res["ignored"], res["missing"]), (200, 1, ["nope"]))
        ig = self.drift()[lh]["ignored"]
        self.assertEqual((ig["latest"], ig["kube_version"], ig["until_newer"], ig["note"]), ("1.12.4", "v1.35.9", True, "waiting for 1.13"))
        self.assertGreater(ig["until"], "2026-10-08")
        self.assertTrue(json.loads((Path(self.tmp.name) / "update-ignores.json").read_text())[lh])
        self.assertEqual(self.post("/api/drift/unignore", {"ids": [lh]}), (200, {"unignored": 1}))
        self.assertNotIn("ignored", self.drift()[lh])

    def test_rejects_bad_conditions(self):
        lh = "helm/longhorn-system/longhorn"
        for body in ({"ids": [lh]}, {"ids": [lh], "days": 0}, {"ids": [lh], "days": "7"}, {"ids": []}):
            self.assertEqual(self.post("/api/drift/ignore", body)[0], 400, body)
        status, res = self.post("/api/drift/ignore", {"ids": ["cluster/kubernetes"], "until_newer": True})
        self.assertEqual((status, res["ignored"], res["skipped"]),
                         (200, 0, [{"id": "cluster/kubernetes", "why": "no version or image digest to wait past"}]))
        self.assertEqual(self.post("/api/drift/ignore", {"ids": ["workload/apps/web/web"], "until_newer": True})[1]["ignored"], 1)
        self.assertEqual(self.drift()["workload/apps/web/web"]["ignored"]["digest"], "sha256:a")


if __name__ == "__main__":
    unittest.main()
