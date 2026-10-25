"""Argo CD Applications as Helm releases (opt-in `argocd:`), on invented Applications."""
import copy
import unittest
from unittest import mock

from app import argocd, upstream
from app.k8s import KubeError
from app.scan import Scanner, apply_ignores
from tests.fake_k8s import FakeK8s
from tests.test_openapi import SCHEMAS, props

ON = {"argocd": {"enabled": True, "namespaces": ["argocd", "team-a"]}}


def app(name, sources, sync, ns="argocd", dest_ns="apps", server="https://kubernetes.default.svc", history=None,
        resources=None, op=None):
    spec = {"destination": {"server": server, "namespace": dest_ns}, "project": "default"}
    spec.update({"sources": sources} if isinstance(sources, list) else {"source": sources})
    status = {"sync": sync, "history": history or [], "resources": resources or []}
    if op:
        status["operationState"] = op
    return {"metadata": {"name": name, "namespace": ns}, "spec": spec, "status": status}


VALUES = {"repoURL": "https://git.example.com/platform/values.git", "targetRevision": "main", "ref": "values"}
KSM = {"repoURL": "https://prometheus-community.github.io/helm-charts", "chart": "kube-state-metrics",
       "targetRevision": "5.25.*", "helm": {"releaseName": "ksm", "valueFiles": ["$values/ksm.yaml"]}}

APPS = {
    "argocd": [
        # single source, HTTP chart repository
        app("podinfo", {"repoURL": "https://stefanprodan.github.io/podinfo", "chart": "podinfo", "targetRevision": "6.*"},
            {"status": "Synced", "revision": "6.7.1"}, dest_ns="web",
            history=[{"id": 3, "revision": "6.7.1", "deployedAt": "2026-10-20T08:00:00Z",
                      "source": {"repoURL": "https://stefanprodan.github.io/podinfo", "chart": "podinfo"}}]),
        # several sources: a $values ref from git, then the chart
        app("monitoring-stack", [VALUES, KSM], {"status": "Synced", "revisions": ["0f3c9e1", "5.25.1"]}, dest_ns="monitoring"),
        # OCI chart, written without a scheme; its workload is only in the resource list (label tracking)
        app("widget", {"repoURL": "ghcr.io/example/charts", "chart": "widget", "targetRevision": "1.4.0"},
            {"status": "Synced", "revision": "1.4.0"}, dest_ns="tools",
            resources=[{"group": "apps", "kind": "Deployment", "namespace": "tools", "name": "widget", "version": "v1"}]),
        # never synced: only the target is known
        app("newapp", {"repoURL": "https://charts.example.com", "chart": "whoami", "targetRevision": "1.2.*"},
            {"status": "OutOfSync"}, dest_ns="apps"),
        # out of sync: the target moved on, the cluster still runs what history says
        app("cache", {"repoURL": "https://charts.example.com", "chart": "valkey", "targetRevision": "2.0.0"},
            {"status": "OutOfSync", "revision": "2.0.0"}, dest_ns="db",
            history=[{"id": 7, "revision": "1.9.0", "deployedAt": "2026-09-01T00:00:00Z",
                      "source": {"repoURL": "https://charts.example.com", "chart": "valkey"}}]),
        # moved from `helm install`: the gitea release Secret (12.1.0) is stale
        app("gitea", {"repoURL": "https://dl.gitea.com/charts/", "chart": "gitea", "targetRevision": "12.4.0"},
            {"status": "Synced", "revision": "12.4.0"}, dest_ns="gitea"),
        # plain git path: its workloads stay manifests
        app("homepage", {"repoURL": "https://git.example.com/platform/apps.git", "path": "homepage", "targetRevision": "main"},
            {"status": "Synced", "revision": "9a8b7c6"}),
        # another cluster: not this cluster's release
        app("remote", {"repoURL": "https://charts.example.com", "chart": "whoami", "targetRevision": "1.0.0"},
            {"status": "Synced", "revision": "1.0.0"}, server="https://spoke.example.com:6443"),
    ],
    # an Application outside Argo CD's namespace: tracking ids name it <namespace>_<name>
    "team-a": [app("tool", {"repoURL": "oci://registry.example.com/charts", "chart": "tool", "targetRevision": "0.3.0"},
                   {"status": "Synced", "revision": "0.3.0"}, ns="team-a", dest_ns="team-a")],
}

EXTRA = [  # (ns, name, tracking-id or None, labels, image)
    ("web", "podinfo", "podinfo:apps/Deployment:web/podinfo", {}, "ghcr.io/stefanprodan/podinfo:6.7.1"),
    ("monitoring", "ksm-kube-state-metrics", "monitoring-stack:apps/Deployment:monitoring/ksm-kube-state-metrics",
     {"helm.sh/chart": "kube-state-metrics-5.25.1"}, "registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.13.0"),
    ("tools", "widget", None, {"app.kubernetes.io/instance": "widget"}, "ghcr.io/example/widget:1.4.0"),
    ("db", "cache-valkey", "cache:apps/Deployment:db/cache-valkey", {}, "docker.io/valkey/valkey:8.0.1"),
    ("team-a", "tool", "team-a_tool:apps/Deployment:team-a/tool", {}, "registry.example.com/tool:0.3.0"),
    # a copied annotation that names another object: not Argo CD's
    ("db", "copy", "cache:apps/Deployment:db/cache-valkey", {}, "docker.io/valkey/valkey:8.0.1"),
]
TRACKED = {("apps", "homepage"): "homepage:apps/Deployment:apps/homepage",
           ("gitea", "gitea"): "gitea:apps/Deployment:gitea/gitea"}


class ArgoK8s(FakeK8s):
    """The fixture cluster plus Argo CD. Records which namespaces' Applications were listed."""

    def __init__(self, apps=APPS, fail=None):
        self.apps, self.fail, self.listed = apps, fail or {}, []

    def argo_applications(self, namespace):
        self.listed.append(namespace)
        if namespace in self.fail:
            raise KubeError(self.fail[namespace], "GET", f"/apis/argoproj.io/v1alpha1/namespaces/{namespace}/applications",
                            '{"message": "forbidden"}')
        return copy.deepcopy(self.apps.get(namespace, []))

    def workloads(self):
        out = super().workloads()
        for w in out:
            tid = TRACKED.get((w["metadata"]["namespace"], w["metadata"]["name"]))
            if tid:
                w["metadata"]["annotations"][argocd.TRACKING] = tid
        for ns, name, tid, labels, image in EXTRA:
            out.append({"kind": "Deployment", "metadata": {"namespace": ns, "name": name, "labels": labels,
                                                           "annotations": {argocd.TRACKING: tid} if tid else {}},
                        "spec": {"template": {"spec": {"containers": [{"name": "c", "image": image}]}}}})
        return out


class Offline(Scanner):
    """No upstream lookups: every row gets a made-up status, so `run` sorts and counts offline."""

    def probe(self, comps):
        pass

    def resolve(self, comp):
        return {**comp, "status": "outdated" if comp.get("release") else "current"}


def rows(cfg=ON, k8s=None):
    return {c["id"]: c for c in Scanner(k8s or ArgoK8s(), cfg).inventory()}


class FlagOff(unittest.TestCase):
    def test_off_or_absent_changes_nothing(self):
        def scan(k8s, cfg):
            res = Offline(k8s, cfg).run()
            return {k: v for k, v in res.items() if k not in ("scanned_at", "duration_s")}
        before = scan(FakeK8s(), {})
        for cfg in ({}, {"argocd": {"enabled": False, "namespaces": ["argocd"]}}, {"argocd": {"namespaces": ["argocd"]}},
                    {"argocd": {"enabled": "yes"}}):
            k8s = ArgoK8s()
            k8s.workloads = FakeK8s().workloads  # the same cluster, so only the flag differs
            self.assertEqual(scan(k8s, cfg), before, cfg)
            self.assertEqual(k8s.listed, [], cfg)
            self.assertNotIn("argocd", scan(k8s, cfg))

    def test_off_ignores_tracking_annotations(self):
        comps = rows({})
        self.assertEqual(comps["workload/web/podinfo/podinfo"]["category"], "manifest")
        self.assertEqual(comps["helm/gitea/gitea"]["install"], "helm")


class Releases(unittest.TestCase):
    def setUp(self):
        self.k8s = ArgoK8s()
        self.sc = Scanner(self.k8s, ON)
        self.c = {c["id"]: c for c in self.sc.inventory()}

    def test_single_source(self):
        r = self.c["helm/web/podinfo"]
        self.assertEqual((r["category"], r["install"], r["name"], r["namespace"], r["installed"], r["image"]),
                         ("helm", "argocd", "podinfo", "web", "6.7.1", "chart podinfo"))
        self.assertEqual(r["source"], {"type": "artifacthub-search", "chart": "podinfo",
                                       "repo_url": "https://stefanprodan.github.io/podinfo"})
        self.assertEqual(r["argocd"], {"application": "argocd/podinfo", "repo": "https://stefanprodan.github.io/podinfo",
                                       "target_revision": "6.*", "sync_status": "Synced", "deployed": True})
        self.assertEqual((r["note"], r["updated"]), ("Argo CD argocd/podinfo · 2026-10-20", "2026-10-20T08:00:00Z"))
        self.assertEqual(self.k8s.listed, ["argocd", "team-a"])
        self.assertEqual(self.sc.argo, {"namespaces": ["argocd", "team-a"], "applications": 8})

    def test_multi_source_with_values_ref(self):
        mine = [c for c in self.c.values() if (c.get("argocd") or {}).get("application") == "argocd/monitoring-stack"]
        self.assertEqual([c["id"] for c in mine], ["helm/monitoring/ksm"])  # the $values source is no release
        r = mine[0]
        self.assertEqual((r["installed"], r["argocd"]["source_index"], r["argocd"]["target_revision"]), ("5.25.1", 1, "5.25.*"))
        w = self.c["workload/monitoring/ksm-kube-state-metrics/kube-state-metrics"]
        self.assertEqual((w["category"], w["release"], w["install"]), ("helm-workload", "helm/monitoring/ksm", "argocd (ksm)"))

    def test_oci_chart(self):
        r = self.c["helm/tools/widget"]
        self.assertEqual(r["source"], {"type": "oci", "ref": "ghcr.io/example/charts/widget"})
        self.assertEqual(self.c["helm/team-a/tool"]["source"], {"type": "oci", "ref": "registry.example.com/charts/tool"})
        # found through the Application's resource list: no tracking-id annotation
        self.assertEqual(self.c["workload/tools/widget/widget"]["release"], "helm/tools/widget")
        self.sc.reg = mock.Mock()
        self.sc.reg.tags.return_value = ("1.3.0", "1.4.0", "1.5.0")
        self.sc.reg.digest.return_value = "sha256:m"
        self.sc.reg.chart_meta.return_value = {"kube_version": "", "app_version": "v3.1.0"}
        done = self.sc.resolve(dict(r))
        self.assertEqual((done["status"], done["latest"], done["installed_app"]), ("outdated", "1.5.0", "v3.1.0"))
        self.sc.reg.digest.assert_any_call("ghcr.io", "example/charts/widget", "1.4.0")

    def test_not_synced_yet(self):
        r = self.c["helm/apps/newapp"]
        self.assertEqual((r["installed"], r["argocd"]["deployed"]), ("1.2.*", False))
        self.assertEqual(r["note"], "Argo CD argocd/newapp · not synced yet, target 1.2.*")
        self.assertNotIn("updated", r)

    def test_out_of_sync_shows_what_runs(self):
        r = self.c["helm/db/cache"]
        self.assertEqual((r["installed"], r["argocd"]["deployed"], r["argocd"]["sync_status"]), ("1.9.0", True, "OutOfSync"))
        self.assertEqual(r["note"], "Argo CD argocd/cache · OutOfSync, target 2.0.0 · 2026-09-01")

    def test_replaces_stale_helm_secret(self):
        same = [c for c in self.c.values() if c["category"] == "helm" and (c["namespace"], c["name"]) == ("gitea", "gitea")]
        self.assertEqual(len(same), 1)
        r = same[0]
        self.assertEqual((r["id"], r["install"], r["installed"]), ("helm/gitea/gitea", "argocd", "12.4.0"))
        self.assertEqual(r["argocd"]["replaced_secret_version"], "12.1.0")
        self.assertIn("replaces Helm release Secret (12.1.0, rev 1 · 2026-10-03)", r["note"])
        # tracked by Argo CD (Helm's annotation left over from before the move) and only Helm's annotation
        self.assertEqual(self.c["workload/gitea/gitea/gitea"]["install"], "argocd (gitea)")
        self.assertEqual(self.c["workload/gitea/gitea-valkey/valkey"]["release"], "helm/gitea/gitea")
        # other Secret releases are untouched
        self.assertEqual(self.c["helm/cert-manager/cert-manager"]["install"], "helm")

    def test_git_source_and_remote_cluster_left_alone(self):
        self.assertEqual(self.c["workload/apps/homepage/homepage"]["category"], "manifest")
        self.assertFalse([c for c in self.c.values() if (c.get("argocd") or {}).get("application") == "argocd/remote"])
        self.assertNotIn("helm/apps/homepage", self.c)

    def test_any_namespace_and_copied_annotation(self):
        self.assertEqual(self.c["workload/team-a/tool/tool"]["release"], "helm/team-a/tool")
        self.assertEqual(self.c["workload/db/cache-valkey/valkey"]["release"], "helm/db/cache")
        self.assertEqual(self.c["workload/db/copy/valkey"]["category"], "manifest")

    def test_summary_counts_release_once(self):
        res = Offline(ArgoK8s(), ON).run()
        self.assertEqual(res["argocd"]["applications"], 8)
        by_id = {c["id"]: c for c in res["components"]}
        self.assertEqual((by_id["helm/web/podinfo"]["images"], by_id["helm/web/podinfo"]["images_outdated"]), (1, 1))
        self.assertEqual(res["summary"]["total"], len([c for c in res["components"] if not c.get("release")]))

    def test_documented(self):
        res = Offline(ArgoK8s(fail={"team-a": 403}), ON).run()
        self.assertEqual(set(res) - props("VersionScan"), set())
        self.assertEqual({k for c in res["components"] for k in c} - props("Component"), set())
        self.assertEqual({k for c in res["components"] for k in c.get("argocd", {})}
                         - set(SCHEMAS["Component"]["properties"]["argocd"]["properties"]), set())
        argo_schema = SCHEMAS["VersionScan"]["allOf"][1]["properties"]["argocd"]["properties"]
        self.assertEqual(set(res["argocd"]) - set(argo_schema), set())


def gen_app(name, charts, revisions, namespace=None, manifests=True):
    """An Application shaped like a generator's: one source per chart (pinned version, releaseName, a
    values file from git), then the git repo as the `$values` ref, then the repo again as manifests.
    The destination namespace is only set when the app has one namespace."""
    git = f"https://git.example.com/platform/{name}.git"
    sources = [{"repoURL": repo, "chart": chart, "targetRevision": ver,
                "helm": {"releaseName": rel, "valueFiles": [f"$values/{name}/{rel}-values.yaml"]}}
               for rel, repo, chart, ver in charts]
    sources.append({"repoURL": git, "targetRevision": "main", "ref": "values"})
    if manifests:
        sources.append({"repoURL": git, "targetRevision": "main", "path": "."})
    dest = {"server": "https://kubernetes.default.svc", **({"namespace": namespace} if namespace else {})}
    return {"metadata": {"name": name, "namespace": "argocd"},
            "spec": {"project": "platform", "sources": sources, "destination": dest},
            "status": {"sync": {"status": "Synced", "revisions": revisions}}}


GEN_APPS = {"argocd": [
    # two charts in one namespace, plus the repo's own manifests
    gen_app("agent", [("agent", "https://charts.example.com", "agent", "0.7.0"),
                      ("agent-crds", "https://charts.example.com", "agent-crds", "0.7.0")],
            ["0.7.0", "0.7.0", "1a2b3c4", "1a2b3c4"], namespace="agent"),
    # one OCI chart plus manifests
    gen_app("llm", [("llm", "ghcr.io/example/charts", "llm-helm", "1.80.0")], ["1.80.0", "1a2b3c4", "1a2b3c4"], namespace="llm"),
    # charts for two namespaces: the generator sets no destination namespace
    gen_app("agents", [("registry", "ghcr.io/example/charts", "registry", "0.4.1"),
                       ("gw", "cr.example.com/charts", "gateway", "1.2.0"),
                       ("gw-crds", "cr.example.com/charts", "gateway-crds", "1.2.0")],
            ["0.4.1", "1.2.0", "1.2.0", "1a2b3c4", "1a2b3c4"]),
]}
GEN_SECRETS = [  # (release, namespace, chart, version): what Helm installed before the move
    ("agent", "agent", "agent", "0.6.0"), ("agent-crds", "agent", "agent-crds", "0.6.0"), ("llm", "llm", "llm-helm", "1.70.0"),
    ("registry", "agents", "registry", "0.4.0"), ("gw", "gateway-system", "gateway", "1.1.0"),
    ("gw-crds", "gateway-system", "gateway-crds", "1.1.0")]
GEN_WORKLOADS = [  # (ns, name, app or None, labels, Helm's leftover release annotation)
    ("agent", "agent-controller", "agent", {"helm.sh/chart": "agent-0.7.0", "app.kubernetes.io/instance": "agent"}, None),
    ("agent", "agent-crd-webhook", "agent", {"helm.sh/chart": "agent-crds-0.7.0"}, None),
    ("agent", "agent-extra", "agent", {}, None),  # the repo's own manifest
    ("llm", "llm", "llm", {"app.kubernetes.io/managed-by": "Helm"}, None),
    ("llm", "llm-db", "llm", {}, "llm"),  # unlabelled, but Helm installed it before the move
    ("llm", "llm-proxy", "llm", {}, None),  # the repo's own manifest
    ("agents", "registry", "agents", {"helm.sh/chart": "registry-0.4.1"}, None),
    ("gateway-system", "gw", "agents", {"helm.sh/chart": "gateway-1.2.0"}, None),
]


class GeneratorK8s(ArgoK8s):
    def __init__(self):
        super().__init__(GEN_APPS)

    def helm_releases(self):
        return [{"name": n, "namespace": ns, "revision": 3, "status": "deployed", "updated": "2026-09-01T00:00:00Z",
                 "chart": c, "chart_version": v, "app_version": "", "home": "", "sources": [], "annotations": {}}
                for n, ns, c, v in GEN_SECRETS]

    def workloads(self):
        out = []
        for ns, name, app_name, labels, helm in GEN_WORKLOADS:
            ann = {argocd.TRACKING: f"{app_name}:apps/Deployment:{ns}/{name}"} if app_name else {}
            if helm:
                ann.update({"meta.helm.sh/release-name": helm, "meta.helm.sh/release-namespace": ns})
            out.append({"kind": "Deployment", "metadata": {"namespace": ns, "name": name, "labels": labels, "annotations": ann},
                        "spec": {"template": {"spec": {"containers": [{"name": "c", "image": f"ghcr.io/example/{name}:1.0.0"}]}}}})
        return out


class GeneratorShaped(unittest.TestCase):
    """Multi-source Applications as a generator writes them for charts moved off `helm install`."""

    def setUp(self):
        self.c = rows({"argocd": {"enabled": True}}, GeneratorK8s())

    def test_every_chart_replaces_its_secret(self):
        helm = {k: v for k, v in self.c.items() if v["category"] == "helm"}
        self.assertEqual(sorted(helm), ["helm/agent/agent", "helm/agent/agent-crds", "helm/agents/registry",
                                        "helm/gateway-system/gw", "helm/gateway-system/gw-crds", "helm/llm/llm"])
        self.assertTrue(all(v["install"] == "argocd" for v in helm.values()))
        r = helm["helm/agent/agent-crds"]
        self.assertEqual((r["installed"], r["argocd"]["source_index"], r["argocd"]["replaced_secret_version"]), ("0.7.0", 1, "0.6.0"))
        self.assertEqual(helm["helm/llm/llm"]["source"], {"type": "oci", "ref": "ghcr.io/example/charts/llm-helm"})

    def test_no_namespace_set_uses_the_secrets(self):
        gw = self.c["helm/gateway-system/gw"]
        self.assertTrue(gw["argocd"]["namespace_assumed"])
        self.assertIn("the Application sets no namespace, assumed gateway-system", gw["note"])
        self.assertEqual(self.c["helm/agents/registry"]["argocd"]["replaced_secret_version"], "0.4.0")
        self.assertNotIn("namespace_assumed", self.c["helm/agent/agent"]["argocd"])

    def test_helm_namespace_wins(self):
        apps = copy.deepcopy(GEN_APPS)
        apps["argocd"][2]["spec"]["sources"][1]["helm"]["namespace"] = "edge"
        self.assertEqual([s["namespace"] for s in argocd.helm_sources(apps["argocd"][2])], ["", "edge", ""])

    def test_workloads_go_to_the_right_chart(self):
        def rel(ns, name):
            return self.c[f"workload/{ns}/{name}/{name}"].get("release")
        self.assertEqual(rel("agent", "agent-controller"), "helm/agent/agent")
        self.assertEqual(rel("agent", "agent-crd-webhook"), "helm/agent/agent-crds")  # not agent's, despite the prefix
        self.assertEqual(rel("llm", "llm"), "helm/llm/llm")
        self.assertEqual(rel("llm", "llm-db"), "helm/llm/llm")
        self.assertEqual(self.c["workload/llm/llm-db/llm-db"]["install"], "argocd (llm)")
        self.assertEqual(rel("agents", "registry"), "helm/agents/registry")
        self.assertEqual(rel("gateway-system", "gw"), "helm/gateway-system/gw")
        for ns, name in (("agent", "agent-extra"), ("llm", "llm-proxy")):  # the repo's own manifests
            self.assertEqual(self.c[f"workload/{ns}/{name}/{name}"]["category"], "manifest", name)

    def test_documented(self):
        fields = SCHEMAS["Component"]["properties"]["argocd"]["properties"]
        self.assertEqual({k for c in self.c.values() for k in c.get("argocd", {})} - set(fields), set())

    def test_chart_label(self):
        self.assertTrue(argocd.chart_label_matches("agent-0.7.0", "agent"))
        self.assertTrue(argocd.chart_label_matches("gateway-v1.2.0", "gateway"))
        self.assertFalse(argocd.chart_label_matches("agent-crds-0.7.0", "agent"))
        self.assertFalse(argocd.chart_label_matches("agent", "agent"))
        self.assertEqual(argocd.oci_ref("ghcr.io/example/llm-helm", "llm-helm"), "ghcr.io/example/llm-helm")


class Rules(unittest.TestCase):
    def test_helm_rules_match_by_release_name(self):
        c = rows({**ON, "helm": {"podinfo": {"track": "minor"}, "ksm@monitoring": {"source": {"type": "github", "repo": "kubernetes/kube-state-metrics"}}}})
        self.assertEqual((c["helm/web/podinfo"]["track"], c["helm/web/podinfo"]["config_key"]), ("minor", "podinfo"))
        r = c["helm/monitoring/ksm"]
        self.assertEqual((r["source"]["type"], r["ref"]), ("github", "https://github.com/kubernetes/kube-state-metrics/releases"))

    def test_ignoring_the_release_ignores_its_images(self):
        res = Offline(ArgoK8s(), ON).run()
        ig = {"latest": None, "note": "n", "until": "2999-01-01T00:00:00+00:00", "kube_version": None,
              "until_newer": False, "latest_any": None, "digest": None}
        out, ended = apply_ignores(res, {"helm/web/podinfo": ig})
        by_id = {c["id"]: c for c in out["components"]}
        self.assertEqual(by_id["workload/web/podinfo/podinfo"]["ignored"]["via"], "helm/web/podinfo")
        self.assertEqual(out["summary"]["ignored"], 1)
        self.assertEqual(ended, set())

    def test_unreadable_applications_keep_secret_releases(self):
        k8s = ArgoK8s(fail={"argocd": 403, "team-a": 404})
        sc = Scanner(k8s, ON)
        c = {x["id"]: x for x in sc.inventory()}
        self.assertEqual(c["helm/gitea/gitea"]["install"], "helm")
        self.assertEqual(sc.argo["applications"], 0)
        self.assertEqual(len(sc.argo["errors"]), 2)
        self.assertIn("HTTP 403", sc.argo["errors"][0])


class Upstream(unittest.TestCase):
    def test_chart_search_prefers_the_applications_repository(self):
        sc = Scanner(ArgoK8s(), ON)
        r = {c["id"]: c for c in sc.inventory()}["helm/web/podinfo"]
        found = {"version": "6.8.0", "app_version": "6.8.0", "repo": "podinfo", "ref": "https://artifacthub.io/x",
                 "kube_version": ">=1.99.0-0"}
        with mock.patch.object(upstream, "artifacthub_search", return_value=found) as search, \
             mock.patch.object(upstream, "artifacthub_package", return_value={"app_version": "6.7.1"}) as pkg:
            done = sc.resolve(r)
        search.assert_called_once_with("podinfo", ("https://stefanprodan.github.io/podinfo",), "6.7.1",
                                       "https://stefanprodan.github.io/podinfo")
        pkg.assert_called_once_with("podinfo", "podinfo", "6.7.1")
        self.assertEqual((done["status"], done["latest"], done["installed_app"], done["latest_app"]),
                         ("outdated", "6.8.0", "6.7.1", "6.8.0"))
        self.assertIn("needs Kubernetes >=1.99.0-0", done["incompatible"])

    def test_current_release_takes_app_version_from_latest(self):
        sc = Scanner(ArgoK8s(), ON)
        r = {c["id"]: c for c in sc.inventory()}["helm/web/podinfo"]
        with mock.patch.object(upstream, "artifacthub_search", return_value={"version": "6.7.1", "app_version": "6.7.1", "repo": "podinfo"}), \
             mock.patch.object(upstream, "artifacthub_package") as pkg:
            done = sc.resolve(r)
        self.assertEqual((done["status"], done["installed_app"]), ("current", "6.7.1"))
        pkg.assert_not_called()

    def test_search_ranks_exact_repository_first(self):
        def pkg(repo, stars, url):
            return {"name": "widget", "version": "1.0.0", "stars": stars, "repository": {"name": repo, "url": url}}
        upstream.artifacthub_search.cache_clear()
        self.addCleanup(upstream.artifacthub_search.cache_clear)
        with mock.patch.object(upstream, "get_json", return_value={"packages": [
                pkg("popular", 500, "https://popular.example/charts"), pkg("ours", 2, "https://charts.example.com/")]}), \
             mock.patch.object(upstream, "artifacthub_package", return_value=None):
            r = upstream.artifacthub_search("widget", (), "", "HTTPS://charts.example.com")
        self.assertEqual((r["repo"], r["ambiguous"]), ("ours", False))

    def test_package_version_url(self):
        with mock.patch.object(upstream, "get_json", return_value={"version": "1.0.0", "app_version": "v2"}) as get:
            upstream.artifacthub_package.cache_clear()
            self.assertEqual(upstream.artifacthub_package("repo", "chart", "1.0.0")["app_version"], "v2")
        get.assert_called_once_with("https://artifacthub.io/api/v1/packages/helm/repo/chart/1.0.0")
        upstream.artifacthub_package.cache_clear()


class Parsing(unittest.TestCase):
    def test_repo_kinds(self):
        self.assertTrue(argocd.is_oci("ghcr.io/example/charts"))
        self.assertTrue(argocd.is_oci("oci://registry.example.com/charts"))
        self.assertFalse(argocd.is_oci("https://charts.example.com"))
        self.assertEqual(argocd.oci_ref("oci://registry.example.com/charts/", "x"), "registry.example.com/charts/x")

    def test_destination(self):
        self.assertTrue(argocd.local({"spec": {"destination": {"name": "in-cluster", "namespace": "x"}}}))
        self.assertFalse(argocd.local({"spec": {"destination": {"name": "spoke"}}}))

    def test_last_successful_sync_when_history_is_empty(self):
        a = app("x", {"repoURL": "https://charts.example.com", "chart": "x", "targetRevision": "2.0.0"},
                {"status": "OutOfSync", "revision": "2.0.0"},
                op={"phase": "Succeeded", "syncResult": {"revision": "1.5.0", "source": {"chart": "x"}}})
        self.assertEqual([(s["version"], s["deployed"]) for s in argocd.helm_sources(a)], [("1.5.0", True)])
        # a failed sync applied nothing worth reporting
        a["status"]["operationState"]["phase"] = "Failed"
        self.assertEqual([(s["version"], s["deployed"]) for s in argocd.helm_sources(a)], [("2.0.0", False)])

    def test_history_of_another_chart_does_not_count(self):
        a = app("x", [VALUES, KSM], {"status": "OutOfSync", "revisions": ["abc", "5.26.0"]},
                history=[{"revisions": ["abc", "3.0.0"], "sources": [VALUES, {"chart": "something-else"}]}])
        self.assertEqual([(s["version"], s["deployed"]) for s in argocd.helm_sources(a)], [("5.26.0", False)])

    def test_tracking_id(self):
        w = {"kind": "Deployment", "metadata": {"namespace": "web", "name": "podinfo",
                                                "annotations": {argocd.TRACKING: "podinfo:apps/Deployment:web/podinfo"}}}
        self.assertEqual(argocd.tracking_app(w), "podinfo")
        w["metadata"]["name"] = "other"
        self.assertIsNone(argocd.tracking_app(w))
        self.assertIsNone(argocd.tracking_app({"kind": "Deployment", "metadata": {"name": "x"}}))


if __name__ == "__main__":
    unittest.main()
