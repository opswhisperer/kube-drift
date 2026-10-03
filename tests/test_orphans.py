import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from app import main
from app.orphans import OrphanScanner, apply_ignores, canonical_kind, commands, object_path, parse_kor, validate_rule
from app.store import Store
from tests.fake_orphans import FakeKor, FakeOrphanK8s

CFG = {"orphans": {
    "ignore": [{"kind": "ConfigMap", "name_regex": "^istio-ca-(root-cert|crl)$", "why": "istiod"}],
    "protect": [{"kind": "Secret", "namespace": "cert-manager", "why": "cert-manager keys"}],
}}


def scan():
    return OrphanScanner(FakeOrphanK8s(), CFG, FakeKor(), self_namespace="apps").run()


class Parse(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(canonical_kind("Pvc"), "PersistentVolumeClaim")
        self.assertEqual(canonical_kind("crd"), "CustomResourceDefinition")
        self.assertEqual(canonical_kind("ConfigMap"), "ConfigMap")
        self.assertIsNone(canonical_kind("Widget"))
        self.assertEqual(object_path("ConfigMap", "apps", "x"), "/api/v1/namespaces/apps/configmaps/x")
        self.assertEqual(object_path("ClusterRoleBinding", "", "kubeadm:a b"),
                         "/apis/rbac.authorization.k8s.io/v1/clusterrolebindings/kubeadm:a%20b")

    def test_parse_handles_nulls_and_strings(self):
        found = parse_kor({"Pdb": None, "Service": {"apps": None}, "Pvc": {"media": ["a"]},
                           "Widget": {"": [{"name": "w", "reason": "r"}]}})
        self.assertEqual(found, [{"kind": "PersistentVolumeClaim", "namespace": "media", "name": "a", "reason": ""},
                                 {"kind": "Widget", "namespace": "", "name": "w", "reason": "r"}])


class Classify(unittest.TestCase):
    def setUp(self):
        self.res = scan()
        self.by = {i["id"]: i for i in self.res["items"]}

    def test_statuses(self):
        st = lambda k: self.by[k]["status"]  # noqa: E731
        self.assertEqual(st("ClusterRoleBinding//kubeadm:apiserver-kubelet-client"), "protected")
        self.assertEqual(st("Secret/cert-manager/letsencrypt"), "protected")
        self.assertEqual(st("ClusterRoleBinding//legacy-dashboard"), "orphan")  # its Helm release is gone
        self.assertIn({"type": "leftover", "source": "helm-leftover", "text": "from Helm release old-dashboard/legacy-dashboard, which is no longer installed"},
                      self.by["ClusterRoleBinding//legacy-dashboard"]["hints"])
        self.assertEqual(st("ConfigMap/web/web-assets-25thdt57cm"), "orphan")
        self.assertEqual(st("PersistentVolumeClaim/media/old-uploads"), "orphan")
        # used by a Gateway, which kor doesn't look at: a kor false positive
        self.assertEqual(st("Secret/apps/web-tls"), "in-use")
        self.assertEqual(self.by["Secret/apps/web-tls"]["hints"], [
            {"type": "in-use", "source": "gateway", "text": "TLS certificate for Gateway apps/web (kor doesn't check Gateways)"}])
        # an installed Helm release owns it: deleting it would be undone
        self.assertEqual(st("Secret/sandbox/demo-db-credentials"), "managed")
        self.assertEqual(self.by["Secret/sandbox/demo-db-credentials"]["hints"], [
            {"type": "recreated", "source": "helm", "text": "Helm release sandbox/demo puts it back on its next upgrade"}])
        # in use wins over recreated; evidence lists the in-use reason first
        tls = self.by["Secret/apps/kube-drift-tls"]["hints"]
        self.assertEqual([h["type"] for h in tls], ["in-use", "recreated"])
        self.assertIn("Certificate kube-drift-tls", tls[1]["text"])

    def test_builtin_protect_covers_self(self):
        self.assertEqual(self.by["Secret/apps/kube-drift-tls"]["status"], "protected")

    def test_config_ignore_and_metadata(self):
        self.assertEqual(self.by["StorageClass//longhorn-static"]["created"], "2026-04-01T10:00:00Z")
        self.assertEqual(self.res["kor_version"], "v0.6.9")
        self.assertEqual(len(self.res["items"]), 13)
        self.assertEqual(self.res["warnings"], [])  # 404 on issuers is not a warning


class Rules(unittest.TestCase):
    def setUp(self):
        self.items = scan()["items"]

    def ignored(self, per_item=None, user=(), config=()):
        items, rules = apply_ignores(self.items, per_item or {}, list(user), list(config))
        return {i["id"]: i.get("ignored") for i in items}, {r["id"]: r for r in rules}

    def test_source_rule(self):
        rule = {"id": "r1", "kind": "Secret", "source": "cert-manager", "note": "certs", "at": "t"}
        ign, rules = self.ignored(user=[rule])
        self.assertEqual(ign["Secret/apps/kube-drift-tls"], {"by": "rule", "rule": "r1", "note": "certs", "at": "t"})
        self.assertIsNone(ign["Secret/apps/web-tls"])  # Gateway-referenced, but no cert-manager annotation
        self.assertEqual(rules["r1"]["matches"], 1)
        self.assertEqual(rules["r1"]["from"], "dashboard")

    def test_precedence_and_config(self):
        cfg = CFG["orphans"]["ignore"]
        per = {"ConfigMap/apps/istio-ca-crl": {"note": "mine", "at": "t"}}
        ign, rules = self.ignored(per_item=per, config=cfg)
        self.assertEqual(ign["ConfigMap/apps/istio-ca-crl"]["by"], "dashboard")  # one resource beats a rule
        self.assertEqual(rules["config-0"]["matches"], 1)  # still counted
        ign, _ = self.ignored(config=cfg)
        self.assertEqual(ign["ConfigMap/apps/istio-ca-crl"], {"by": "config", "rule": "config-0", "note": "istiod", "at": None})

    def test_namespace_and_regex(self):
        ign, rules = self.ignored(user=[{"id": "r", "namespace": "apps", "name_regex": "-tls$"}])
        self.assertEqual(rules["r"]["matches"], 2)
        self.assertEqual(ign["Secret/apps/web-tls"]["by"], "rule")

    def test_validate(self):
        self.assertEqual(validate_rule({"kind": "secret", "source": "cert-manager", "bogus": "x", "namespace": " "}),
                         {"kind": "Secret", "source": "cert-manager"})
        for bad in ({}, {"namespace": ""}, {"name_regex": "("}, {"source": "x" * 300}):
            with self.assertRaises(ValueError):
                validate_rule(bad)


class Persisted(unittest.TestCase):
    def test_other_format_is_discarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = {"scanned_at": "2026-10-03T18:49:56+00:00", "items": [{"id": "Secret/a/b", "hints": ["referenced: x"]}]}
            (Path(tmp) / "orphans.json").write_text(json.dumps(old))
            self.assertIsNone(main.OrphanState(Store(Path(tmp))).result)
            (Path(tmp) / "orphans.json").write_text(json.dumps(scan()))
            st = main.OrphanState(Store(Path(tmp)), config_rules=CFG["orphans"]["ignore"])
            self.assertEqual(st.result["format"], 2)
            self.assertEqual(st.snapshot()["summary"]["ignored"], 1)


class Commands(unittest.TestCase):
    def setUp(self):
        self.items = {i["id"]: i for i in scan()["items"]}

    def pick(self, *ids):
        return [self.items[i] for i in ids]

    def test_delete_script(self):
        sh = commands(self.pick("ConfigMap/web/web-assets-25thdt57cm", "ClusterRoleBinding//legacy-dashboard",
                                "ClusterRoleBinding//kubeadm:apiserver-kubelet-client"), "delete", "demo")
        lines = sh.splitlines()
        self.assertEqual(lines[0], "# kube-drift: back up and delete 2 orphaned resources on demo")
        self.assertIn("set -euo pipefail; umask 077", lines)
        # backup first, delete right after, both with the context
        i = lines.index('kubectl --context demo -n web get configmap web-assets-25thdt57cm -o json '
                        '| jq "$strip" > "$d/ConfigMap_web_web-assets-25thdt57cm.json"')
        self.assertEqual(lines[i + 1], "kubectl --context demo -n web delete configmap web-assets-25thdt57cm")
        self.assertIn("kubectl --context demo delete clusterrolebinding legacy-dashboard", lines)
        self.assertNotIn("kubeadm", sh)  # protected: never in a delete script
        self.assertEqual((lines[2], lines[-1]), ("(", ")"))  # subshell: set -e can't close the terminal

    def test_export_includes_protected_and_no_delete(self):
        sh = commands(self.pick("Secret/cert-manager/letsencrypt"), "export")
        self.assertIn("-n cert-manager get secret letsencrypt", sh)
        self.assertNotIn("delete", sh)
        self.assertNotIn("--context", sh)

    def test_kind_specific_strip_only_when_needed(self):
        self.assertNotIn('"Job"', commands(self.pick("ClusterRoleBinding//legacy-dashboard"), "export"))
        self.assertIn('.spec.selector', commands(self.pick("Job/db/backup-verify"), "export"))

    def test_quoting(self):
        odd = {"kind": "ConfigMap", "namespace": "a", "name": "it's $(x)", "known_kind": True, "status": "orphan"}
        self.assertIn("""delete configmap 'it'\\''s $(x)'""", commands([odd], "delete"))

    def test_jq_filter_restores_cleanly(self):
        import shutil
        import subprocess
        if not shutil.which("jq"):
            self.skipTest("jq not installed")
        sh = commands(self.pick("Job/db/backup-verify"), "export")
        strip = next(ln for ln in sh.splitlines() if ln.startswith("strip="))[len("strip='"):-1]
        job = FakeOrphanK8s().get(object_path("Job", "db", "backup-verify"))
        out = json.loads(subprocess.run(["jq", strip], input=json.dumps(job), capture_output=True, text=True, check=True).stdout)
        self.assertNotIn("status", out)
        self.assertNotIn("selector", out["spec"])
        self.assertEqual(set(out["metadata"]), {"name", "namespace", "labels"})
        self.assertEqual(out["metadata"]["labels"], {"job-name": "backup-verify"})


class Store_(unittest.TestCase):
    def test_ignore_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            it = {"kind": "ClusterRoleBinding", "namespace": "", "name": "legacy-dashboard"}
            Store(Path(tmp)).ignore([it], "left over from legacy-dashboard")
            again = Store(Path(tmp))
            self.assertEqual(again.ignored()["ClusterRoleBinding//legacy-dashboard"]["note"], "left over from legacy-dashboard")
            self.assertEqual(again.unignore([it]), 1)
            self.assertEqual(Store(Path(tmp)).ignored(), {})


class Endpoints(unittest.TestCase):
    """The HTTP layer: token gate, scope checks, delete removes from the result."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.k = FakeOrphanK8s()
        self.saved = main.ORPHANS
        main.ORPHANS = main.OrphanState(Store(Path(self.tmp.name)), "demo", CFG["orphans"]["ignore"])
        main.ORPHANS.result = OrphanScanner(self.k, CFG, FakeKor(), self_namespace="apps").run()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), main.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        main.ORPHANS = self.saved
        self.tmp.cleanup()

    def post(self, path, body, ctype="application/json"):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read().decode()

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as r:
            return json.loads(r.read())

    def test_json_only(self):
        # a cross-site form post can't ignore things
        self.assertEqual(self.post("/api/orphans/ignore", {"ids": ["ClusterRoleBinding//legacy-dashboard"]}, "text/plain")[0], 415)

    def test_ignore_flow(self):
        code, _ = self.post("/api/orphans/ignore", {"ids": ["ClusterRoleBinding//legacy-dashboard"], "note": "n"})
        self.assertEqual(code, 200)
        snap = self.get("/api/orphans")
        dev = next(i for i in snap["items"] if i["id"] == "ClusterRoleBinding//legacy-dashboard")
        self.assertEqual(dev["ignored"]["by"], "dashboard")
        self.assertEqual(snap["summary"]["ignored"], 2)  # + istio-ca-crl via config
        self.post("/api/orphans/unignore", {"ids": ["ClusterRoleBinding//legacy-dashboard"]})
        self.assertEqual(self.get("/api/orphans")["summary"]["ignored"], 1)

    def test_rules_flow(self):
        code, body = self.post("/api/orphans/rules", {"rule": {"kind": "Secret", "source": "cert-manager"}, "note": "certs"})
        self.assertEqual(code, 200)
        rid = json.loads(body)["rule"]["id"]
        snap = self.get("/api/orphans")
        self.assertEqual(next(r for r in snap["rules"] if r["id"] == rid)["matches"], 1)
        self.assertEqual(snap["summary"]["ignored"], 2)  # + istio-ca-crl via config
        self.assertEqual(self.post("/api/orphans/rules", {"rule": {"name_regex": "("}})[0], 400)
        self.assertEqual(self.post("/api/orphans/rules/delete", {"id": rid})[0], 200)
        self.assertEqual(self.post("/api/orphans/rules/delete", {"id": rid})[0], 404)
        self.assertEqual(self.get("/api/orphans")["summary"]["ignored"], 1)
        # survives a restart
        self.post("/api/orphans/rules", {"rule": {"namespace": "media"}})
        self.assertEqual(len(Store(Path(self.tmp.name)).rules()), 1)

    def test_commands(self):
        code, body = self.post("/api/orphans/commands", {"action": "delete", "ids": [
            "ClusterRoleBinding//legacy-dashboard", "ClusterRoleBinding//kubeadm:apiserver-kubelet-client", "Secret/kube-system/x"]})
        res = json.loads(body)
        self.assertEqual(code, 200)
        self.assertIn("kubectl --context demo delete clusterrolebinding legacy-dashboard", res["script"])
        self.assertNotIn("kubeadm", res["script"])
        self.assertNotIn("kube-system", res["script"])  # not in the scan → never scripted
        why = {x["id"]: x["why"] for x in res["skipped"]}
        self.assertTrue(why["ClusterRoleBinding//kubeadm:apiserver-kubelet-client"].startswith("protected"))
        self.assertEqual(why["Secret/kube-system/x"], "not in the current orphan scan")
        self.assertEqual(self.post("/api/orphans/commands", {"action": "nuke", "ids": ["ClusterRoleBinding//legacy-dashboard"]})[0], 400)
        # nothing was deleted — the app has no way to
        self.assertIn("ClusterRoleBinding//legacy-dashboard", {i["id"] for i in self.get("/api/orphans")["items"]})

    def test_no_write_endpoints(self):
        for path in ("/api/orphans/delete", "/api/orphans/export"):
            self.assertEqual(self.post(path, {"ids": ["ClusterRoleBinding//legacy-dashboard"], "confirm": True})[0], 404)

if __name__ == "__main__":
    unittest.main()
