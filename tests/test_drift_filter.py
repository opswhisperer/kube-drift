"""GET /api/drift filters: narrow `components` to what the summary counts, and nothing else."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from app import main
from app.scan import Scanner
from app.store import Store
from tests.fake_k8s import FakeK8s

STATUSES = ["outdated", "current", "unknown", "error"]
IGNORED = "workload/apps/web/nginx"  # a top-level outdated component, ignored directly
RELEASE = "helm/gitea/gitea"  # outdated, ignored, so its images are ignored through it


class Offline(Scanner):
    """The fixture cluster with made-up statuses instead of upstream lookups, so `run` sorts and
    counts the way a real scan does without the network."""

    def probe(self, comps):
        pass

    def resolve(self, comp):
        outdated = comp.get("release") or comp["id"] in (IGNORED, RELEASE)
        return {**comp, "status": "outdated" if outdated else STATUSES[len(comp["id"]) % 4]}


class DriftFilter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (main.ORPHANS, main.STATE.result)
        main.ORPHANS = main.OrphanState(Store(Path(self.tmp.name)))
        main.STATE.result = Offline(FakeK8s(), {}).run()
        ig = {"latest": None, "note": "n", "until": "2999-01-01T00:00:00+00:00", "kube_version": None,
              "until_newer": False, "latest_any": None, "digest": None}
        main.ORPHANS.store.ignore_updates({IGNORED: ig, RELEASE: ig})
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), main.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.all = self.get("/api/drift")[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        main.ORPHANS, main.STATE.result = self.saved
        self.tmp.cleanup()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as r:
                body = r.read().decode()
                return r.status, json.loads(body) if r.headers.get_content_type() == "application/json" else body
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def ids(self, path):
        status, d = self.get(path)
        self.assertEqual(status, 200, d)
        return [c["id"] for c in d["components"]]

    def test_fixture_has_what_the_filters_drop(self):
        comps = {c["id"]: c for c in self.all["components"]}
        self.assertEqual(set(STATUSES), {c["status"] for c in comps.values()})
        self.assertIn("ignored", comps[IGNORED])
        self.assertTrue([c for c in comps.values() if c.get("release") == RELEASE and c["ignored"]["via"] == RELEASE])
        self.assertTrue([c for c in comps.values() if c.get("release") and not c.get("ignored")])

    def test_no_parameters_unchanged(self):
        snap = main.STATE.snapshot()
        for path in ("/api/drift", "/api/drift?"):
            d = self.get(path)[1]
            self.assertEqual({k: v for k, v in d.items() if k != "generated_at"},
                             {k: v for k, v in snap.items() if k != "generated_at"})
        self.assertEqual(self.get("/api/drift.csv")[1], main.to_csv(snap))

    def test_status(self):
        comps = self.all["components"]
        self.assertEqual(self.ids("/api/drift?status=outdated"), [c["id"] for c in comps if c["status"] == "outdated"])
        either = [c["id"] for c in comps if c["status"] in ("unknown", "error")]
        self.assertEqual(self.ids("/api/drift?status=unknown,error"), either)
        self.assertEqual(self.ids("/api/drift?status=error&status=unknown"), either)

    def test_ignored(self):
        comps = self.all["components"]
        self.assertEqual(self.ids("/api/drift?ignored=false"), [c["id"] for c in comps if not c.get("ignored")])
        self.assertEqual(self.ids("/api/drift?ignored=true"), [c["id"] for c in comps if c.get("ignored")])

    def test_top(self):
        comps = self.all["components"]
        self.assertEqual(self.ids("/api/drift?top=true"), [c["id"] for c in comps if not c.get("release")])
        self.assertEqual(self.ids("/api/drift?top=false"), [c["id"] for c in comps if c.get("release")])

    def test_actionable_matches_summary(self):
        status, d = self.get("/api/drift?status=outdated&ignored=false&top=true")
        self.assertEqual(status, 200)
        self.assertGreater(self.all["summary"]["outdated"], 0)
        self.assertEqual(len(d["components"]), self.all["summary"]["outdated"])
        self.assertEqual({k: v for k, v in d.items() if k not in ("components", "generated_at")},
                         {k: v for k, v in self.all.items() if k not in ("components", "generated_at")})
        ids = [c["id"] for c in d["components"]]
        self.assertEqual(ids, [c["id"] for c in self.all["components"] if c["id"] in set(ids)])  # same order
        self.assertNotIn(IGNORED, ids)
        self.assertNotIn(RELEASE, ids)
        self.assertFalse([c for c in d["components"] if c.get("release") or c.get("ignored") or c["status"] != "outdated"])

    def test_csv_takes_the_same_filters(self):
        csv = self.get("/api/drift.csv?status=outdated&ignored=false&top=true")[1]
        self.assertEqual(len(csv.strip().splitlines()) - 1, self.all["summary"]["outdated"])

    def test_bad_input_is_400(self):
        for q in ("state=outdated", "status=", "status=old", "status=outdated,", "ignored=no", "ignored=",
                  "top=1", "top=true&top=false", "ignored", "status=outdated&limit=5"):
            for path in ("/api/drift", "/api/drift.csv"):
                status, d = self.get(f"{path}?{q}")
                self.assertEqual(status, 400, q)
                self.assertEqual(set(d), {"error"}, q)


if __name__ == "__main__":
    unittest.main()
