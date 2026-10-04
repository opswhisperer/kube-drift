import json
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from app import main
from app.openapi import SPEC
from app.orphans import OrphanScanner
from app.scan import Scanner
from app.store import Store
from tests.fake_k8s import FakeK8s
from tests.fake_orphans import FakeKor, FakeOrphanK8s
from tests.test_orphans import CFG

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = SPEC["components"]["schemas"]
NOT_API = {"/", "/index.html", "/docs"}  # pages, not API operations


def props(name: str) -> set[str]:
    """All property names a schema allows, following allOf and $ref."""
    s = SCHEMAS[name]
    out = set(s.get("properties", {}))
    for part in s.get("allOf", []):
        out |= props(part["$ref"].rsplit("/", 1)[1]) if "$ref" in part else set(part.get("properties", {}))
    return out


class Spec(unittest.TestCase):
    def test_every_route_is_documented(self):
        src = (ROOT / "app" / "main.py").read_text()
        routes = set(re.findall(r'"(/(?:api|healthz|readyz|openapi)[^"?]*)"', src)) - {"/api/"}
        self.assertEqual(routes - set(SPEC["paths"]), set())
        self.assertEqual(set(SPEC["paths"]) - routes - NOT_API, set())

    def test_refs_resolve(self):
        refs = set(re.findall(r'"\$ref": "#/components/schemas/([^"]+)"', json.dumps(SPEC)))
        self.assertTrue(refs)
        self.assertEqual(refs - set(SCHEMAS), set())

    def test_operation_ids_unique(self):
        ids = [op["operationId"] for p in SPEC["paths"].values() for op in p.values()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_swagger_version_matches_dockerfile(self):
        m = re.search(r"ARG SWAGGER_UI_VERSION=(\S+)", (ROOT / "Dockerfile").read_text())
        self.assertEqual(m.group(1), main.SWAGGER_UI_VERSION)

    def test_validates_if_validator_installed(self):
        try:
            from openapi_spec_validator import validate
        except ImportError:
            self.skipTest("openapi-spec-validator not installed")
        validate(SPEC)


class Live(unittest.TestCase):
    """Responses only use fields the spec documents, so the spec can't silently drift."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (main.ORPHANS, main.STATE.result)
        main.ORPHANS = main.OrphanState(Store(Path(self.tmp.name)), "demo", CFG["orphans"]["ignore"])
        main.ORPHANS.result = OrphanScanner(FakeOrphanK8s(), CFG, FakeKor(), self_namespace="apps").run()
        main.ORPHANS.store.add_rule({"kind": "Secret", "source": "cert-manager"}, "certs")
        main.ORPHANS.store.ignore([{"kind": "StorageClass", "namespace": "", "name": "longhorn-static"}], "n")
        main.STATE.result = {"scanned_at": "2026-10-03T00:00:00+00:00", "duration_s": 1.0, "summary": {"total": 1},
                             "components": Scanner(FakeK8s(), {}).inventory()}
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), main.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        main.ORPHANS, main.STATE.result = self.saved
        self.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as r:
            return r.status, dict(r.headers), r.read()

    def test_orphans_fields(self):
        _, headers, body = self.get("/api/orphans")
        d = json.loads(body)
        self.assertIn('rel="service-desc"', headers["Link"])
        self.assertEqual(set(d) - props("OrphanScan"), set())
        self.assertEqual({k for i in d["items"] for k in i} - props("OrphanItem"), set())
        self.assertEqual({k for i in d["items"] for h in i["hints"] for k in h} - props("Hint"), set())
        ignored = [i["ignored"] for i in d["items"] if i.get("ignored")]
        self.assertEqual({i["by"] for i in ignored}, {"dashboard", "rule", "config"})
        self.assertEqual({k for i in ignored for k in i} - props("Ignored"), set())
        self.assertEqual({k for r in d["rules"] for k in r} - props("Rule"), set())
        self.assertEqual(set(d["summary"]) - set(SCHEMAS["OrphanScan"]["allOf"][1]["properties"]["summary"]["properties"]), set())

    def test_drift_fields(self):
        _, _, body = self.get("/api/drift")
        d = json.loads(body)
        self.assertEqual(set(d) - props("VersionScan"), set())
        self.assertEqual({k for c in d["components"] for k in c} - props("Component"), set())

    def test_openapi_and_docs(self):
        status, headers, body = self.get("/openapi.json")
        self.assertEqual(json.loads(body)["openapi"], "3.1.0")
        status, headers, body = self.get("/docs")
        self.assertIn(b"/docs/swagger-ui-bundle.js", body)
        self.assertIn(b"url: '/openapi.json'", body)

    def test_swagger_assets(self):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **kw):
                return None
        opener = urllib.request.build_opener(NoRedirect)
        try:
            r = opener.open(self.base + "/docs/swagger-ui-bundle.js")
            self.assertEqual(r.status, 200)  # bundled (inside the image)
        except urllib.error.HTTPError as e:  # from source: pinned CDN copy
            with e:
                self.assertEqual(e.code, 302)
                self.assertEqual(e.headers["Location"],
                                 f"https://cdn.jsdelivr.net/npm/swagger-ui-dist@{main.SWAGGER_UI_VERSION}/swagger-ui-bundle.js")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/docs/../index.html")
        cm.exception.close()


if __name__ == "__main__":
    unittest.main()
