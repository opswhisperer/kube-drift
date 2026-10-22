"""Image and Helm rules made in the dashboard, and the config view (GET /api/config)."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import yaml

from app import main, overrides
from app.overrides import effective, redact, validate_key, validate_rule, view
from app.scan import Scanner
from app.store import Store
from tests.fake_k8s import FakeK8s

FILE_CFG = {
    "cluster_name": "demo",
    "images": {"mongo": {"track": "major"}, "re:^quay.io/tigera/": {"source": {"type": "none"}}},
    "helm": {"eg": {"source": {"type": "oci", "ref": "docker.io/envoyproxy/gateway-helm"}}},
    "auth": {"oidc": {"issuer": "https://auth.example.com/", "client_id": "kd", "client_secret": "s3cret",
                      "client_secret_env": "OIDC_CLIENT_SECRET"}},
    "probes": [{"namespace": "n8n", "name": "n8n", "url": "http://bob:pw@n8n.n8n.svc:5678/rest/settings",
                "headers": {"Authorization": "Bearer abc123"}}],
    "registries": {"docker.io": {"auth_env": "DOCKERHUB_AUTH"}},
}


class Validate(unittest.TestCase):
    def test_image_rule(self):
        self.assertEqual(validate_rule("images", {"track": "minor", "source": {"type": "none"}, "ref": "https://x.example/r",
                                                  "name": " Mongo ", "note": "", "loose_suffix": False}),
                         {"track": "minor", "source": {"type": "none"}, "ref": "https://x.example/r", "name": "Mongo", "loose_suffix": False})
        self.assertEqual(validate_rule("images", {"track": "any"}), {})  # defaults aren't stored

    def test_helm_sources(self):
        self.assertEqual(validate_rule("helm", {"source": {"type": "oci", "ref": "oci://ghcr.io/org/charts/app"}}),
                         {"source": {"type": "oci", "ref": "ghcr.io/org/charts/app"}})
        self.assertEqual(validate_rule("helm", {"source": {"type": "artifacthub", "repo": "bitnami", "chart": "postgresql"}}),
                         {"source": {"type": "artifacthub", "repo": "bitnami", "chart": "postgresql"}})
        self.assertEqual(validate_rule("helm", {"source": {"type": "github", "repo": "n8n-io/n8n", "version_regex": r"n8n@(\d+\.\d+\.\d+)"}}),
                         {"source": {"type": "github", "repo": "n8n-io/n8n", "version_regex": r"n8n@(\d+\.\d+\.\d+)"}})

    def test_rejects(self):
        bad = [
            ("images", {"track": "patch"}, "track"),
            ("images", {"ref": "javascript:alert(1)"}, "ref"),  # rendered as a link
            ("images", {"ref": "https://"}, "ref"),
            ("images", {"colour": "red"}, "unknown field colour"),
            ("helm", {"name": "x"}, "unknown field name"),  # images only
            ("images", {"source": {"type": "oci", "ref": "ghcr.io/x/y"}}, "source.type"),  # charts only
            ("images", {"source": {"type": "github", "repo": "a/b/c"}}, "source.repo"),
            ("images", {"source": {"type": "github", "repo": "a/b", "version_regex": r"\d+"}}, "group"),
            ("images", {"source": {"type": "github", "repo": "a/b", "version_regex": "("}}, "bad regex"),
            ("images", {"source": {"type": "none", "repo": "a/b"}}, "unknown field repo"),
            ("helm", {"source": {"type": "artifacthub", "chart": "x"}}, "source.repo"),
            ("helm", {"source": {"type": "oci", "ref": "https://ghcr.io/x"}}, "source.ref"),
            ("images", {"loose_suffix": "yes"}, "loose_suffix"),
            ("images", "track: major", "rule"),
        ]
        for sec, rule, msg in bad:
            with self.subTest(rule=rule), self.assertRaisesRegex(ValueError, msg):
                validate_rule(sec, rule)

    def test_key(self):
        self.assertEqual(validate_key("images", " ghcr.io/org/app "), "ghcr.io/org/app")
        self.assertEqual(validate_key("helm", "re:^db-"), "re:^db-")
        for sec, key, msg in [("images", "", "key"), ("images", "a b", "key"), ("images", "re:(", "bad regex"), ("probes", "x", "section")]:
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, msg):
                validate_key(sec, key)


class Effective(unittest.TestCase):
    def test_dashboard_rules_replace_in_place_and_add(self):
        rules = {"images": {"mongo": {"rule": {"track": "minor"}}, "redis": {"rule": {"track": "major"}}}}
        eff = effective(FILE_CFG, rules)
        self.assertEqual(list(eff["images"]), ["mongo", "re:^quay.io/tigera/", "redis"])  # re: keys keep their order
        self.assertEqual(eff["images"]["mongo"], {"track": "minor"})
        self.assertEqual(eff["helm"], FILE_CFG["helm"])
        self.assertEqual(FILE_CFG["images"]["mongo"], {"track": "major"})  # the file's config isn't changed
        self.assertIs(effective(FILE_CFG, {})["images"], FILE_CFG["images"])

    def test_scanner_uses_them_and_records_the_key(self):
        rules = {"images": {"re:^ghcr.io/kube-vip/": {"rule": {"track": "minor"}}}, "helm": {"cnpg": {"rule": {"source": {"type": "none"}}}}}
        comps = {c["id"]: c for c in Scanner(FakeK8s(), effective(FILE_CFG, rules)).inventory()}
        mongo = comps["workload/db/mongo/mongo"]
        self.assertEqual((mongo["config_key"], mongo["track"]), ("mongo", "major"))
        typha = comps["workload/calico-system/calico-typha/typha"]
        self.assertEqual((typha["config_key"], typha["source"]), ("re:^quay.io/tigera/", {"type": "none"}))
        self.assertEqual(comps["workload/kube-system/kube-vip-cloud-provider/kube-vip-cloud-provider"]["config_key"], "re:^ghcr.io/kube-vip/")
        self.assertEqual((comps["helm/cnpg-system/cnpg"]["config_key"], comps["helm/cnpg-system/cnpg"]["source"]), ("cnpg", {"type": "none"}))
        self.assertEqual(comps["helm/envoy-gateway-system/eg"]["config_key"], "eg")
        self.assertNotIn("config_key", comps["workload/apps/whoami/whoami"])
        self.assertNotIn("config_key", comps["helm/gitea/gitea"])


class View(unittest.TestCase):
    def test_redact(self):
        r = redact(FILE_CFG)
        oidc = r["auth"]["oidc"]
        self.assertEqual((oidc["client_secret"], oidc["client_secret_env"]), ("(hidden)", "OIDC_CLIENT_SECRET"))
        self.assertEqual(r["probes"][0]["headers"], {"Authorization": "(hidden)"})
        self.assertEqual(r["probes"][0]["url"], "http://(hidden)@n8n.n8n.svc:5678/rest/settings")
        self.assertEqual(r["registries"], {"docker.io": {"auth_env": "DOCKERHUB_AUTH"}})
        text = json.dumps(r)
        for secret in ("s3cret", "abc123", "bob:pw"):
            self.assertNotIn(secret, text)

    def test_view(self):
        rules = {"images": {"mongo": {"rule": {"track": "minor"}, "at": "2026-10-22T00:00:00+00:00", "by": "ann@example.com"},
                            "redis": {"rule": {"source": {"type": "none"}}, "at": None, "by": ""}}}
        comps = [{"id": "workload/db/mongo/mongo", "category": "manifest", "config_key": "mongo"},
                 {"id": "helm/envoy-gateway-system/eg", "category": "helm", "config_key": "eg"},
                 {"id": "workload/apps/web/web", "category": "manifest"}]
        v = view(FILE_CFG, rules, comps)
        imgs = {e["key"]: e for e in v["images"]}
        self.assertEqual([e["key"] for e in v["images"]], ["mongo", "re:^quay.io/tigera/", "redis"])
        self.assertEqual(imgs["mongo"], {"key": "mongo", "from": "dashboard", "rule": {"track": "minor"}, "replaces": {"track": "major"},
                                         "components": ["workload/db/mongo/mongo"], "at": "2026-10-22T00:00:00+00:00", "by": "ann@example.com"})
        self.assertEqual((imgs["re:^quay.io/tigera/"]["from"], imgs["redis"]["from"]), ("config", "dashboard"))
        self.assertNotIn("replaces", imgs["redis"])
        self.assertEqual(v["helm"][0]["components"], ["helm/envoy-gateway-system/eg"])
        self.assertEqual(set(v["settings"]), {"cluster_name", "auth", "probes", "registries"})
        self.assertNotIn("s3cret", json.dumps(v))
        self.assertEqual(yaml.safe_load(v["dashboard_yaml"]), {"images": {"mongo": {"track": "minor"}, "redis": {"source": {"type": "none"}}}})
        for k, text in v["settings_yaml"].items():
            self.assertEqual(yaml.safe_load(text), {k: v["settings"][k]})
        self.assertEqual(view({}, {}, [])["dashboard_yaml"], "")

    def test_yaml_round_trips(self):
        cases = [{"cluster_name": "demo"}, {"n": None}, {"e": {}}, {"l": []}, {"s": "a: b\nc"},
                 {"images": {"mongo": {"track": "major"}, "re:^quay.io/": {"source": {"type": "none"}, "note": "x: y"}}},
                 {"orphans": {"enabled": False, "ignore": [{"kind": "ConfigMap", "name": "a"}], "kor_args": ["--older-than=1h"]}},
                 {"probes": [{"name": "n8n", "headers": {"A": "x"}, "source": {"type": "github", "repo": "a/b"}}]},
                 {"auth": {"oidc": {"issuer": "https://x", "allowed_groups": ["a"], "deep": {"er": {"x": 1}}}}}, {"x": [[1, [2]]]}]
        for c in cases:
            with self.subTest(c=c):
                self.assertEqual(yaml.safe_load(overrides._yaml(c)), c)
        self.assertEqual(overrides._yaml({"images": {"mongo": {"track": "major"}}}), "images:\n  mongo: {track: major}\n")


class StoreRules(unittest.TestCase):
    def test_persisted(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d))
            e = s.set_config_rule("images", "mongo", {"track": "major"}, "ann@example.com")
            self.assertEqual((e["rule"], e["by"]), ({"track": "major"}, "ann@example.com"))
            s.set_config_rule("helm", "eg", {"source": {"type": "none"}})
            self.assertEqual(Store(Path(d)).config_rules()["images"]["mongo"]["rule"], {"track": "major"})
            self.assertTrue(s.remove_config_rule("images", "mongo"))
            self.assertFalse(s.remove_config_rule("images", "mongo"))
            self.assertEqual(Store(Path(d)).config_rules(), {"images": {}, "helm": {"eg": s.config_rules()["helm"]["eg"]}})


class Http(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (main.ORPHANS, main.STATE.result, dict(main.CONFIG))
        main.ORPHANS = main.OrphanState(Store(Path(self.tmp.name)))
        main.STATE.result = {"scanned_at": "2026-10-22T00:00:00+00:00", "summary": {}, "components": [
            {"id": "workload/db/mongo/mongo", "category": "manifest", "name": "mongo", "config_key": "mongo"}]}
        main.CONFIG.clear()
        main.CONFIG.update(FILE_CFG)
        main.STATE.wake.clear()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), main.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        main.ORPHANS, main.STATE.result, cfg = self.saved
        main.CONFIG.clear()
        main.CONFIG.update(cfg)
        main.STATE.wake.clear()
        self.tmp.cleanup()

    def call(self, path, body=None):
        req = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_get_config(self):
        status, d = self.call("/api/config")
        self.assertEqual(status, 200)
        self.assertTrue(d["editable"])
        self.assertEqual(d["images"][0], {"key": "mongo", "from": "config", "rule": {"track": "major"}, "components": ["workload/db/mongo/mongo"]})
        text = json.dumps(d)
        for secret in ("s3cret", "abc123", "bob:pw"):
            self.assertNotIn(secret, text)

    def test_set_and_remove(self):
        status, d = self.call("/api/config/rules", {"section": "images", "key": "mongo", "rule": {"track": "minor"}})
        self.assertEqual((status, d["key"], d["rule"]), (200, "mongo", {"track": "minor"}))
        self.assertTrue(main.STATE.wake.is_set())  # rescans with it
        _, d = self.call("/api/config")
        self.assertEqual((d["images"][0]["from"], d["images"][0]["replaces"]), ("dashboard", {"track": "major"}))
        self.assertEqual(effective(main.CONFIG, main.ORPHANS.store.config_rules())["images"]["mongo"], {"track": "minor"})
        self.assertEqual(self.call("/api/config/rules/delete", {"section": "images", "key": "mongo"}), (200, {"removed": True}))
        self.assertEqual(self.call("/api/config/rules/delete", {"section": "images", "key": "mongo"}), (404, {"removed": False}))
        _, d = self.call("/api/config")
        self.assertEqual(d["images"][0]["from"], "config")

    def test_bad_rule(self):
        for body in ({"section": "images", "key": "x", "rule": {"ref": "javascript:alert(1)"}},
                     {"section": "probes", "key": "x", "rule": {}}, {"section": "helm", "key": "", "rule": {}}):
            with self.subTest(body=body):
                status, d = self.call("/api/config/rules", body)
                self.assertEqual(status, 400)
                self.assertIn("error", d)
        self.assertEqual(main.ORPHANS.store.config_rules(), {})

    def test_not_writable(self):
        main.ORPHANS.store.writable = False
        status, _ = self.call("/api/config/rules", {"section": "images", "key": "x", "rule": {}})
        self.assertEqual(status, 503)
        self.assertFalse(self.call("/api/config")[1]["editable"])


if __name__ == "__main__":
    unittest.main()
