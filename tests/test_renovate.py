"""Renovate support: the report in, rows matched, approvals ticked on a fake forge. All data is invented."""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app import main, renovate
from tests.test_openapi import props


def dep(name, current, datasource="docker", file="deploy.yaml", package=None):
    return {"depName": name, "packageName": package or name, "currentValue": current, "datasource": datasource}


def report():
    """Renovate's report shape (reportType: file), two repositories."""
    return {"problems": [], "repositories": {
        "acme/shop": {
            "packageFiles": {"kubernetes": [{"packageFile": "deploy.yaml", "deps": [
                dep("example/web", "1.4.0"), dep("python", "3.12-alpine"), dep("example/queue", "2.0.0")]}],
                "helm-values": [{"packageFile": "values.yaml", "deps": [dep("example/vault", "1.8.0")]}]},
            "branches": [
                {"branchName": "renovate/example-web-1.x", "prTitle": "Update example/web to v1.5.0", "result": "needs-approval",
                 "upgrades": [{"depName": "example/web", "packageName": "example/web", "packageFile": "deploy.yaml",
                               "currentValue": "1.4.0", "newValue": "1.5.0", "updateType": "minor", "datasource": "docker"}]},
                {"branchName": "renovate/python-3.x", "prTitle": "Update python to v3.13", "result": "needs-approval",
                 "upgrades": [{"depName": "python", "packageName": "python", "packageFile": "deploy.yaml",
                               "currentValue": "3.12-alpine", "newValue": "3.13-alpine", "updateType": "minor", "datasource": "docker"}]},
                {"branchName": "renovate/example-queue-2.x", "prTitle": "Update example/queue to v2.1.0", "result": "done", "prNo": 7,
                 "upgrades": [{"depName": "example/queue", "packageName": "example/queue", "packageFile": "deploy.yaml",
                               "currentValue": "2.0.0", "newValue": "2.1.0", "updateType": "minor", "datasource": "docker"}]},
            ]},
        "acme/platform": {
            "packageFiles": {"regex": [{"packageFile": "apps.yaml", "deps": [
                dep("podinfo", "6.7.1", "helm"), dep("python", "3.12-alpine")]}]},
            "branches": [
                {"branchName": "renovate/podinfo-6.x", "prTitle": "Update Helm chart podinfo to v6.8.0", "result": "needs-approval",
                 "upgrades": [{"depName": "podinfo", "packageName": "podinfo", "packageFile": "apps.yaml",
                               "currentValue": "6.7.1", "newValue": "6.8.0", "updateType": "minor", "datasource": "helm"}]},
                {"branchName": "renovate/python-3.x", "prTitle": "Update python to v3.13", "result": "needs-approval",
                 "upgrades": [{"depName": "python", "packageName": "python", "packageFile": "apps.yaml",
                               "currentValue": "3.12-alpine", "newValue": "3.13-alpine", "updateType": "minor", "datasource": "docker"}]},
            ]},
    }}


def image_row(ns, name, host, repo, tag, category="manifest"):
    return {"id": f"workload/{ns}/{name}/{repo.split('/')[-1]}", "category": category, "namespace": ns, "name": name,
            "image": f"{host}/{repo}:{tag}", "image_host": host, "image_repo": repo, "tag": tag, "installed": tag}


ROWS = [
    image_row("shop", "web", "docker.io", "example/web", "1.4.0"),
    image_row("shop", "vault", "quay.io", "example/vault", "1.8.0", "helm-workload"),   # values omit the registry
    image_row("shop", "queue", "docker.io", "example/queue", "2.0.0"),
    image_row("shop", "jobs", "docker.io", "library/python", "3.12-alpine"),
    image_row("platform", "jobs", "docker.io", "library/python", "3.12-alpine"),
    image_row("shop", "cache", "docker.io", "example/cache", "7.0.0"),
    {"id": "helm/shop/podinfo", "category": "helm", "namespace": "shop", "name": "podinfo", "image": "chart podinfo",
     "installed": "6.7.1"},
    {"id": "node/n1/kubelet", "category": "node", "namespace": "-", "name": "n1 · kubelet", "installed": "v1.33.0"},
]
NS_REPOS = {"shop": ["https://git.example.com/acme/shop.git"], "platform": ["https://git.example.com/acme/platform.git"]}
CFG = {"enabled": True, "forge": {"type": "forgejo", "url": "https://git.example.com", "token_env": "TEST_RENOVATE_TOKEN"}}


class Report(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = renovate.Store(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_condense_and_merge_per_repository(self):
        res = self.store.ingest(report())
        self.assertEqual((res["repositories"], res["dependencies"], res["awaiting_approval"]), (2, 6, 4))
        one = {"repositories": {"acme/shop": {"packageFiles": {}, "branches": []}}}
        self.store.ingest(one)   # a run for one repository replaces just that one
        doc = renovate.Store(Path(self.tmp.name)).snapshot()   # and it's on disk
        self.assertEqual(doc["repos"]["acme/shop"]["deps"], [])
        self.assertEqual(len(doc["repos"]["acme/platform"]["deps"]), 2)

    def test_not_a_report(self):
        with self.assertRaises(ValueError):
            self.store.ingest({"hello": "world"})


class Annotate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = renovate.Store(Path(self.tmp.name))
        self.store.ingest(report())
        self.rows = {c["id"] + "@" + c["namespace"]: c for c in renovate.annotate(ROWS, self.store.snapshot(), CFG, NS_REPOS)}

    def tearDown(self):
        self.tmp.cleanup()

    def r(self, cid, ns):
        return self.rows[f"{cid}@{ns}"].get("renovate")

    def test_states(self):
        web = self.r("workload/shop/web/web", "shop")
        self.assertEqual((web["state"], web["repo"], web["branch"], web["new"]),
                         ("approve", "acme/shop", "renovate/example-web-1.x", "1.5.0"))
        queue = self.r("workload/shop/queue/queue", "shop")
        self.assertEqual((queue["state"], queue["pr"], queue["pr_url"]), ("pr", 7, "https://git.example.com/acme/shop/pulls/7"))
        self.assertEqual(self.r("workload/shop/vault/vault", "shop")["state"], "tracked")   # quay.io/example/vault = example/vault
        self.assertEqual(self.r("workload/shop/cache/cache", "shop"), {"state": "untracked"})
        chart = self.r("helm/shop/podinfo", "shop")
        self.assertEqual((chart["state"], chart["repo"], chart["new"]), ("approve", "acme/platform", "6.8.0"))
        self.assertNotIn("renovate", self.rows["node/n1/kubelet@-"])

    def test_same_image_in_two_repositories_goes_by_namespace(self):
        self.assertEqual(self.r("workload/shop/jobs/python", "shop")["repo"], "acme/shop")
        self.assertEqual(self.r("workload/platform/jobs/python", "platform")["repo"], "acme/platform")
        # without Argo CD's namespace → repository map, the row still says where else it is
        rows = renovate.annotate(ROWS, self.store.snapshot(), CFG, None)
        jobs = next(c for c in rows if c["id"] == "workload/shop/jobs/python")["renovate"]
        self.assertEqual(len(jobs["also_in"]), 1)

    def test_no_report_changes_nothing(self):
        self.assertIs(renovate.annotate(ROWS, {"received_at": None, "repos": {}}, CFG, NS_REPOS), ROWS)


DASHBOARD = """This issue lists Renovate updates.

## Pending Approval

 - [ ] <!-- approve-branch=renovate/example-web-1.x -->Update example/web to v1.5.0
 - [x] <!-- approve-branch=renovate/python-3.x -->Update python to v3.13
 - [ ] <!-- approve-all-pending-prs -->Create all pending approval PRs at once
"""


class FakeForge(BaseHTTPRequestHandler):
    issues: list = []
    patched: list = []
    seen_auth: list = []
    gets: list = []

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        FakeForge.seen_auth.append(self.headers.get("Authorization"))
        FakeForge.gets.append(self.path)
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        size = int((qs.get("limit") or qs.get("per_page") or ["30"])[0])
        page = int((qs.get("page") or ["1"])[0])
        self._send(200, FakeForge.issues[(page - 1) * size:page * size])

    def do_PATCH(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeForge.patched.append((self.path, body["body"]))
        self._send(201, {})


class Approve(unittest.TestCase):
    def setUp(self):
        FakeForge.issues = [{"number": 3, "title": "Some bug", "body": "", "html_url": "x"},
                            {"number": 1, "title": "Renovate: dependency dashboard", "body": DASHBOARD,
                             "html_url": "https://git.example.com/acme/shop/issues/1"}]
        FakeForge.patched, FakeForge.seen_auth, FakeForge.gets = [], [], []
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeForge)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.cfg = {"forge": {"type": "forgejo", "url": f"http://127.0.0.1:{self.srv.server_address[1]}",
                              "token_env": "TEST_RENOVATE_TOKEN"}, "dashboard_title": "Renovate: dependency dashboard"}
        os.environ["TEST_RENOVATE_TOKEN"] = "t0ken"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        os.environ.pop("TEST_RENOVATE_TOKEN", None)

    def test_ticks_the_box(self):
        res = renovate.approve(self.cfg, "acme/shop", "renovate/example-web-1.x")
        self.assertEqual(res["status"], "approved")
        path, body = FakeForge.patched[0]
        self.assertEqual(path, "/api/v1/repos/acme/shop/issues/1")
        self.assertIn("- [x] <!-- approve-branch=renovate/example-web-1.x -->", body)
        self.assertIn("- [ ] <!-- approve-all-pending-prs -->", body)   # nothing else changed
        self.assertEqual(body.replace("- [x] <!-- approve-branch=renovate/example-web-1.x", "- [ ] <!-- approve-branch=renovate/example-web-1.x"), DASHBOARD)
        self.assertEqual(FakeForge.seen_auth[0], "token t0ken")

    def test_forgejo_issues_carry_pull_request_null(self):
        # Forgejo/Gitea send "pull_request": null on every plain issue (GitHub omits the key)
        FakeForge.issues = [dict(i, pull_request=None) for i in FakeForge.issues]
        FakeForge.issues.append({"number": 8, "title": "Renovate: dependency dashboard", "body": "",
                                 "pull_request": {"merged": False}})
        self.assertEqual(renovate.approve(self.cfg, "acme/shop", "renovate/example-web-1.x")["status"], "approved")
        self.assertEqual(FakeForge.patched[0][0], "/api/v1/repos/acme/shop/issues/1")

    def test_dashboard_on_a_later_page_without_search(self):
        # Forgejo's q= search comes from its issue indexer, which can miss the dashboard: list and page instead
        FakeForge.issues = [{"number": n, "title": f"bug {n}", "body": ""} for n in range(10, 70)] + FakeForge.issues[1:]
        self.assertEqual(renovate.approve(self.cfg, "acme/shop", "renovate/example-web-1.x")["status"], "approved")
        self.assertEqual([urllib.parse.parse_qs(urllib.parse.urlsplit(g).query)["page"] for g in FakeForge.gets], [["1"], ["2"]])
        self.assertFalse(any("q=" in g for g in FakeForge.gets))
        self.assertEqual(FakeForge.patched[0][0], "/api/v1/repos/acme/shop/issues/1")

    def test_already_approved_and_not_waiting(self):
        self.assertEqual(renovate.approve(self.cfg, "acme/shop", "renovate/python-3.x")["status"], "already approved")
        with self.assertRaises(renovate.ApproveError) as e:
            renovate.approve(self.cfg, "acme/shop", "renovate/nothing-1.x")
        self.assertEqual(e.exception.status, 409)
        self.assertEqual(FakeForge.patched, [])

    def test_no_dashboard_no_token_bad_input(self):
        FakeForge.issues = [{"number": 3, "title": "Some bug", "body": ""}]
        with self.assertRaises(renovate.ApproveError) as e:
            renovate.approve(self.cfg, "acme/shop", "renovate/example-web-1.x")
        self.assertEqual(e.exception.status, 404)
        with self.assertRaises(renovate.ApproveError) as e:
            renovate.approve(self.cfg, "acme/shop; rm", "renovate/x")
        self.assertEqual(e.exception.status, 400)
        os.environ.pop("TEST_RENOVATE_TOKEN")
        with self.assertRaises(renovate.ApproveError) as e:
            renovate.approve(self.cfg, "acme/shop", "renovate/example-web-1.x")
        self.assertEqual(e.exception.status, 503)

    def test_github_skips_pull_requests(self):
        FakeForge.issues = [{"number": 9, "title": "Dependency Dashboard", "body": "", "pull_request": {}},
                            {"number": 1, "title": "Dependency Dashboard", "body": DASHBOARD}]
        cfg = {"forge": {"type": "github", "url": self.cfg["forge"]["url"], "token_env": "TEST_RENOVATE_TOKEN"}}
        self.assertEqual(renovate.approve(cfg, "acme/shop", "renovate/example-web-1.x")["status"], "approved")
        self.assertEqual(FakeForge.patched[0][0], "/api/v3/repos/acme/shop/issues/1")   # GitHub Enterprise path
        self.assertEqual(FakeForge.seen_auth[0], "Bearer t0ken")
        self.assertEqual(renovate.pr_url(cfg, "acme/shop", 4), f"{self.cfg['forge']['url']}/acme/shop/pull/4")


class Endpoints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (main.RENOVATE, dict(main.CONFIG), main.STATE.result)
        main.RENOVATE = renovate.Store(Path(self.tmp.name))
        main.CONFIG["renovate"] = CFG
        main.STATE.result = {"scanned_at": "2026-10-03T00:00:00+00:00", "duration_s": 1.0, "summary": {"total": 1},
                             "components": ROWS, "argocd": {"namespaces": ["argocd"], "applications": 2, "namespace_repos": NS_REPOS}}
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), main.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        main.RENOVATE, cfg, main.STATE.result = self.saved
        main.CONFIG.clear()
        main.CONFIG.update(cfg)
        self.tmp.cleanup()

    def post(self, path, obj):
        req = urllib.request.Request(self.base + path, data=json.dumps(obj).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_report_then_drift_rows(self):
        code, res = self.post("/api/renovate/report", report())
        self.assertEqual((code, res["awaiting_approval"]), (200, 4))
        with urllib.request.urlopen(self.base + "/api/drift") as r:
            d = json.loads(r.read())
        self.assertEqual(set(d) - props("VersionScan"), set())   # every field documented
        scan_props = next(p["properties"] for p in main.SPEC["components"]["schemas"]["VersionScan"]["allOf"] if "properties" in p)
        self.assertEqual(set(d["renovate"]) - set(scan_props["renovate"]["properties"]), set())
        self.assertEqual(d["renovate"]["repositories"], 2)
        self.assertFalse(d["renovate"]["can_approve"])   # no token in the environment
        states = {c["id"] + "@" + c["namespace"]: c.get("renovate", {}).get("state") for c in d["components"]}
        self.assertEqual(states["workload/shop/web/web@shop"], "approve")
        self.assertEqual({k for c in d["components"] for k in c.get("renovate", {})} - set(
            main.SPEC["components"]["schemas"]["RenovateState"]["properties"]), set())
        code, res = self.post("/api/renovate/approve", {"repo": "acme/shop", "branch": "renovate/example-web-1.x"})
        self.assertEqual(code, 503)   # no forge token

    def test_off_is_404(self):
        main.RENOVATE = None
        code, res = self.post("/api/renovate/report", report())
        self.assertEqual(code, 404)
        self.assertIn("renovate", res["error"])


if __name__ == "__main__":
    unittest.main()
