import unittest
from unittest import mock

from app.scan import Scanner
from tests.fake_k8s import FakeK8s
from app import upstream
from app.upstream import split_image
from app.http import HttpError
from app.versions import cmp_versions, is_floating, pick_latest, satisfies, walk_up


class SplitImage(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(split_image("mongo:8"), ("docker.io", "library/mongo", "8", None))
        self.assertEqual(split_image("traefik/whoami"), ("docker.io", "traefik/whoami", "latest", None))
        self.assertEqual(split_image("10.0.0.5:5000/api:87540e3"), ("10.0.0.5:5000", "api", "87540e3", None))
        self.assertEqual(split_image("ghcr.io/a/b:v1@sha256:abc"), ("ghcr.io", "a/b", "v1", "sha256:abc"))
        self.assertEqual(split_image("lscr.io/linuxserver/syncthing:latest")[0], "lscr.io")


class Versions(unittest.TestCase):
    def test_floating(self):
        for t in ("latest", "main", "main-stable", "nightly", "87540e3", "dev"):
            self.assertTrue(is_floating(t), t)
        for t in ("v1.2.3", "1.31.1", "9.0-alpine", "distroless-v1.39.1", "3.6.6-0", "0.0.161-589c824437-r1", "8"):
            self.assertFalse(is_floating(t), t)

    def test_cmp(self):
        self.assertLess(cmp_versions("v1.20.2", "1.21.2"), 0)
        self.assertEqual(cmp_versions("1.2", "1.2.0"), 0)
        self.assertGreater(cmp_versions("2.41.6", "2.34.6"), 0)

    def test_pick_latest_shape(self):
        tags = ["v1.35.9", "v1.36.3", "v1.37.1", "v1.37.2-rc.0", "latest", "v1.35.10"]
        self.assertEqual(pick_latest("v1.35.9", tags, "minor"), {"latest": "v1.35.10", "latest_any": "v1.37.1", "outdated": True})
        self.assertEqual(pick_latest("v1.35.10", tags, "minor")["outdated"], False)

    def test_suffix_and_precision(self):
        tags = ["9.0-alpine", "9.2-alpine", "9.2.1-alpine", "9.2.1", "10.0-alpine"]
        r = pick_latest("9.0-alpine", tags)
        self.assertEqual(r["latest"], "10.0-alpine")
        r = pick_latest("9.0-alpine", tags, "major")
        self.assertEqual(r["latest"], "9.2-alpine")
        # a bare build number must never beat a 3-part version
        self.assertEqual(pick_latest("12.4.2", ["12.4.2", "9799770991", "13.2.3"])["latest"], "13.2.3")
        self.assertEqual(pick_latest("8", ["8", "8.0.14", "9", "9.0.1"], "major")["latest"], "8")

    def test_loose_suffix_first_party(self):
        tags = ["0.0.161-589c824437-r1", "0.0.162-aaaaaaaaaa-r1", "0.0.162-aaaaaaaaaa-rc1"]
        self.assertEqual(pick_latest("0.0.161-589c824437-r1", tags, loose_suffix=True)["latest"], "0.0.162-aaaaaaaaaa-r1")

    def test_date_stamps(self):
        # one huge number to the semver rules: compared as date + time instead
        self.assertFalse(is_floating("20261008"))
        tags = ["20261008-0811", "20261015-0930", "20261015-0930-arm64", "20261009", "20261014-2359", "latest"]
        self.assertEqual(pick_latest("20261008-0811", tags), {"latest": "20261015-0930", "latest_any": "20261015-0930", "outdated": True})
        self.assertEqual(pick_latest("20261008-0811", ["20261008-0811"])["outdated"], False)
        self.assertEqual(pick_latest("app-20261008", ["app-20261008", "app-20261102", "app-20261103-rc"])["latest"], "app-20261102")
        # a date after the version is just a suffix
        self.assertEqual(pick_latest("v1.2.3-20261008", ["v1.2.3-20261008", "v1.2.4-20261008"])["latest"], "v1.2.4-20261008")

    def test_walk_up(self):
        tags = {"v3.32.2", "v3.33.0", "v3.33.1", "v3.34.0"}
        self.assertEqual(walk_up("v3.32.1", tags.__contains__), {"latest": "v3.34.0", "latest_any": "v3.34.0", "outdated": True})
        self.assertEqual(walk_up("v3.32.1", tags.__contains__, "minor")["latest"], "v3.32.2")
        self.assertEqual(walk_up("v3.34.0", tags.__contains__)["outdated"], False)
        self.assertEqual(walk_up("8", {"9", "10"}.__contains__, "major"), {"latest": "8", "latest_any": "10", "outdated": False})
        asked = []
        walk_up("v1.0.0", lambda t: asked.append(t) or True, limit=5)
        self.assertEqual(len(asked), 5)
        self.assertIsNone(walk_up("20261008-0811", tags.__contains__)["outdated"])


class WalkUpFallback(unittest.TestCase):
    """A registry that lists only build tags newest-first still gets an answer for v3.32.1."""

    def resolve(self, existing, tags=("v3.32.2-156-g6c1d95ab4e11", "release-v3.32")):
        sc = Scanner(FakeK8s(), {})
        sc.reg = mock.Mock()
        sc.reg.tags.return_value = tags
        def digest(host, repo, tag):
            if tag in existing:
                return "sha256:" + tag
            raise HttpError(404, tag)
        sc.reg.digest.side_effect = digest
        comp = {"image": "quay.io/acme/widget:v3.32.1", "image_host": "quay.io", "image_repo": "acme/widget",
                "tag": "v3.32.1", "floating": False, "running_digests": ["sha256:v3.32.1"], "ref_overridden": True}
        sc._resolve_registry(comp)
        return comp

    def test_finds_newer(self):
        comp = self.resolve({"v3.32.1", "v3.32.2", "v3.33.0"})
        self.assertEqual((comp["status"], comp["latest"]), ("outdated", "v3.33.0"))

    def test_nothing_newer(self):
        self.assertEqual(self.resolve({"v3.32.1"})["status"], "current")

    def test_registry_trouble_leaves_it_unknown(self):
        with mock.patch.object(Scanner, "_tag_exists", side_effect=HttpError(429, "x")):
            self.assertEqual(self.resolve({"v3.32.1"})["status"], "unknown")


class OwnRegistry(unittest.TestCase):
    def test_credentials_for_a_public_registry_keep_exact_suffixes(self):
        cfg = {"registries": {"docker.io": {"auth_env": "HUB_AUTH"}, "reg.example:5000": {"insecure": True}}}
        k8s = FakeK8s()
        ws = k8s.workloads()
        ws.append({"kind": "Deployment", "metadata": {"namespace": "apps", "name": "built-here"},
                   "spec": {"template": {"spec": {"containers": [{"name": "a", "image": "reg.example:5000/app:1.0.0-abc"}]}}}})
        k8s.workloads = lambda: ws
        comps = {c["image"]: c for c in Scanner(k8s, cfg).inventory() if c.get("image_host")}
        self.assertFalse(comps["nginx:1.27-alpine"]["loose_suffix"])
        self.assertTrue(comps["reg.example:5000/app:1.0.0-abc"]["loose_suffix"])


    def test_a_registry_in_the_pull_secret_is_your_own(self):
        # No `registries` entry: credentials in the mounted pull secret are enough.
        k8s = FakeK8s()
        ws = k8s.workloads()
        ws.append({"kind": "Deployment", "metadata": {"namespace": "apps", "name": "built-here"},
                   "spec": {"template": {"spec": {"containers": [{"name": "a", "image": "reg.example/team/app:1.0.0-abc"}]}}}})
        k8s.workloads = lambda: ws
        creds = {"reg.example": {"team": "eDp5"}, "docker.io": {"": "eDp5"}}
        with mock.patch.object(upstream, "docker_config", return_value=creds):
            comps = {c["image"]: c for c in Scanner(k8s, {}).inventory() if c.get("image_host")}
        self.assertTrue(comps["reg.example/team/app:1.0.0-abc"]["loose_suffix"])
        self.assertFalse(comps["nginx:1.27-alpine"]["loose_suffix"])  # docker.io stays public

class KubeVersionRange(unittest.TestCase):
    def test_satisfies(self):
        cases = [
            ("v1.24.3", ">=1.25.0-0", False), ("v1.29.3-eks-1a2b", ">=1.25.0-0", True),
            ("v1.31.0+k3s1", ">= 1.19, < 1.30", False), ("v1.29.9", ">= 1.19 < 1.30", True),
            ("1.28.4", "~1.28", True), ("1.29.0", "~1.28", False), ("1.29.5", "1.26 - 1.29", True),
            ("1.30.1", "1.26 - 1.29", False), ("1.30.1", "1.26 - 1.29 || >=1.30", True),
            ("1.28.0", "1.28.x", True), ("1.27.9", "^1.28", False), ("1.27.0", "!=1.27.0", False),
        ]
        for v, c, want in cases:
            self.assertIs(satisfies(v, c), want, f"{v} {c}")
        self.assertIsNone(satisfies("1.28.0", "not a range"))
        self.assertIsNone(satisfies("1.28.0", ""))
        self.assertIsNone(satisfies("", ">=1.20"))


class Incompatible(unittest.TestCase):
    """A chart update whose kubeVersion excludes the cluster's version says why."""

    def chart(self, kube_version):
        sc = Scanner(FakeK8s(), {})
        sc.kube_version = "v1.24.3"
        comp = {"installed": "1.0.0"}
        sc._apply_chart(comp, {"version": "2.0.0", "kube_version": kube_version})
        return comp

    def test_flagged_with_reason(self):
        comp = self.chart(">=1.25.0-0")
        self.assertEqual(comp["status"], "outdated")
        self.assertEqual(comp["incompatible"], "chart 2.0.0 needs Kubernetes >=1.25.0-0; the cluster runs v1.24.3")

    def test_compatible_or_unknown_range(self):
        for kv in (">=1.20.0-0", None, "garbage!"):
            self.assertNotIn("incompatible", self.chart(kv), kv)

    def test_oci_chart(self):
        sc = Scanner(FakeK8s(), {})
        sc.kube_version = "v1.24.3"
        sc.reg = mock.Mock()
        sc.reg.tags.return_value = ("1.0.0", "1.1.0", "2.0.0")
        sc.reg.digest.return_value = "sha256:m"
        sc.reg.chart_meta.return_value = {"kube_version": ">=1.25.0-0"}
        comp = sc.resolve({"installed": "1.0.0", "image": "chart widget", "source": {"type": "oci", "ref": "reg.example/charts/widget"}})
        self.assertEqual((comp["status"], comp["latest"]), ("outdated", "2.0.0"))
        self.assertEqual(comp["incompatible"], "chart 2.0.0 needs Kubernetes >=1.25.0-0; the cluster runs v1.24.3")
        sc.reg.digest.assert_called_with("reg.example", "charts/widget", "2.0.0")
        self.assertIn("sha256:m", sc.label_digests)


class HelmNesting(unittest.TestCase):
    """Containers a Helm release installs are listed under that release, not on their own."""

    def scan(self, k8s=None):
        sc = Scanner(k8s or FakeK8s(), {})
        outdated = {"gitea-valkey", "prometheus-stack-grafana", "gitea"}
        def resolve(c):
            c["status"] = "outdated" if c["name"] in outdated and c["category"] != "helm" else "current"
            return c
        sc.resolve = resolve
        return sc.run()

    def test_linked_to_release(self):
        comps = {c["id"]: c for c in self.scan()["components"]}
        self.assertEqual(comps["workload/gitea/gitea-valkey/valkey"]["release"], "helm/gitea/gitea")
        self.assertNotIn("release", comps["workload/apps/whoami/whoami"])
        gitea = comps["helm/gitea/gitea"]
        self.assertEqual((gitea["status"], gitea["images"], gitea["images_outdated"]), ("current", 2, 2))
        self.assertEqual(comps["helm/monitoring/prometheus-stack"]["images"], 3)
        self.assertNotIn("images", comps["helm/kube-system/descheduler"])

    def test_summary_counts_releases_not_containers(self):
        res = self.scan()
        top = [c for c in res["components"] if not c.get("release")]
        self.assertLess(len(top), len(res["components"]))
        self.assertEqual(res["summary"]["total"], len(top))
        self.assertNotIn("outdated", res["summary"])  # outdated images only show on their (current) releases

    def test_workloads_an_operator_creates_follow_its_release(self):
        class K(FakeK8s):
            def workloads(self):
                ws = super().workloads()
                ws.append({"kind": "DaemonSet", "metadata": {"namespace": "longhorn-system", "name": "longhorn-manager",
                           "annotations": {"meta.helm.sh/release-name": "longhorn"}},
                           "spec": {"template": {"spec": {"containers": [{"name": "m", "image": "longhornio/longhorn-manager:v1.12.1"}]}}}})
                for w in ws:
                    if w["metadata"]["name"] == "csi-attacher":  # created by longhorn-manager at runtime
                        w["metadata"]["labels"] = {"longhorn.io/managed-by": "longhorn-manager"}
                    if w["metadata"]["name"] == "whoami":  # names no workload: stays standalone
                        w["metadata"]["labels"] = {"app.kubernetes.io/managed-by": "kustomize"}
                return ws
        comps = {c["id"]: c for c in self.scan(K())["components"]}
        csi = comps["workload/longhorn-system/csi-attacher/csi-attacher"]
        self.assertEqual((csi["release"], csi["category"], csi["install"]),
                         ("helm/longhorn-system/longhorn", "helm-workload", "longhorn-manager (helm longhorn)"))
        self.assertEqual(comps["helm/longhorn-system/longhorn"]["images"], 2)
        self.assertNotIn("release", comps["workload/apps/whoami/whoami"])

    def test_release_namespace_annotation_and_missing_release(self):
        class K(FakeK8s):
            def workloads(self):
                ws = super().workloads()
                for w in ws:
                    if w["metadata"]["name"] == "operator":  # installed by a release in another namespace
                        w["metadata"]["annotations"]["meta.helm.sh/release-namespace"] = "kube-system"
                        w["metadata"]["annotations"]["meta.helm.sh/release-name"] = "descheduler"
                    if w["metadata"]["name"] == "litellm":  # its release isn't deployed
                        w["metadata"]["annotations"]["meta.helm.sh/release-name"] = "gone"
                return ws
        comps = {c["id"]: c for c in Scanner(K(), {}).inventory()}
        self.assertEqual(comps["workload/tailscale/operator/k8s-operator"]["release"], "helm/kube-system/descheduler")
        self.assertNotIn("release", comps["workload/ai/litellm/litellm"])


class ReleaseNotesLinks(unittest.TestCase):
    """Without a configured `ref`, rows link to where the project writes its release notes."""

    def helm_k8s(self, **meta):
        class K(FakeK8s):
            def helm_releases(self):
                rels = super().helm_releases()
                for r in rels:
                    if r["name"] == "longhorn":
                        r.update(meta)
                return rels
        return K()

    def helm_row(self, cfg=None, **meta):
        return {c["id"]: c for c in Scanner(self.helm_k8s(**meta), cfg or {}).inventory()}["helm/longhorn-system/longhorn"]

    def test_helm_rows_prefer_a_source_repos_releases(self):
        row = self.helm_row(home="https://widget.example/", sources=["https://github.com/acme/widget"])
        self.assertEqual(row["ref"], "https://github.com/acme/widget/releases")
        self.assertEqual(self.helm_row(home="https://widget.example/", sources=[])["ref"], "https://widget.example/")
        row = self.helm_row({"helm": {"longhorn": {"source": {"type": "github", "repo": "acme/gizmo"}}}},
                            home="https://widget.example/")
        self.assertEqual(row["ref"], "https://github.com/acme/gizmo/releases")
        row = self.helm_row({"helm": {"longhorn": {"ref": "https://notes.example/"}}}, sources=["https://github.com/acme/widget"])
        self.assertEqual(row["ref"], "https://notes.example/")

    def test_image_with_github_source_links_its_releases(self):
        cfg = {"images": {"ghcr.io/gethomepage/homepage": {"source": {"type": "github", "repo": "acme/homepage"}}}}
        comps = {c["id"]: c for c in Scanner(FakeK8s(), cfg).inventory()}
        self.assertEqual(comps["workload/apps/homepage/homepage"]["ref"], "https://github.com/acme/homepage/releases")

    def test_chart_search_gets_the_charts_links_and_version(self):
        sc = Scanner(self.helm_k8s(home="https://widget.example/", sources=["https://github.com/acme/widget"]), {})
        row = {c["id"]: c for c in sc.inventory()}["helm/longhorn-system/longhorn"]
        with mock.patch.object(upstream, "artifacthub_search", return_value=None) as search:
            sc.resolve(row)
        search.assert_called_once_with("longhorn", ("https://github.com/acme/widget", "https://widget.example/"), row["installed"])

    def image_ref(self, labels=None, chart_links=(), guess=None, release="helm/apps/widget", repo="acme/widget"):
        sc = Scanner(FakeK8s(), {})
        sc.reg = mock.Mock()
        sc.reg.digest.return_value = "sha256:r"
        sc.reg.tags.return_value = ("1.0.0",)
        sc.reg.labels.return_value = labels or {}
        sc.chart_links = {"helm/apps/widget": chart_links}
        comp = {"image": f"{repo}:1.0.0", "image_host": "docker.io", "image_repo": repo, "tag": "1.0.0",
                "installed": "1.0.0", "floating": False, "running_digests": ["sha256:r"],
                "ref": "https://hub.docker.com/r/acme/widget/tags", "source": {"type": "registry"}}
        if release:
            comp["release"] = release
        with mock.patch.object(upstream, "github_guess", return_value=guess) as g:
            sc.resolve(comp)
        return comp["ref"], g

    def test_image_link_order(self):
        ref, g = self.image_ref(labels={"source": "https://github.com/acme/widget"}, guess="https://github.com/x/y")
        self.assertEqual(ref, "https://github.com/acme/widget/releases")
        g.assert_not_called()
        ref, g = self.image_ref(chart_links=("https://github.com/acme/helm-charts", "https://github.com/acme-labs/widget"),
                                guess="https://github.com/acme/widget")
        self.assertEqual(ref, "https://github.com/acme-labs/widget/releases")
        g.assert_not_called()
        ref, g = self.image_ref(chart_links=("https://github.com/acme/helm-charts",), guess="https://github.com/acme/widget")
        self.assertEqual(ref, "https://github.com/acme/widget")
        g.assert_called_once_with("docker.io", "acme/widget")
        self.assertEqual(self.image_ref(release=None)[0], "https://hub.docker.com/r/acme/widget/tags")

    def test_github_trouble_keeps_the_registry_page(self):
        sc_ref = None
        with mock.patch.object(upstream, "github_guess", side_effect=HttpError(403, "x", {})):
            sc = Scanner(FakeK8s(), {})
            sc.reg = mock.Mock()
            sc.reg.digest.return_value = None
            sc.reg.tags.return_value = ("1.0.0",)
            comp = {"image": "acme/widget:1.0.0", "image_host": "docker.io", "image_repo": "acme/widget", "tag": "1.0.0",
                    "installed": "1.0.0", "floating": False, "running_digests": [], "ref": "https://hub.docker.com/r/acme/widget/tags"}
            sc_ref = sc.resolve(comp)
        self.assertEqual(sc_ref["ref"], "https://hub.docker.com/r/acme/widget/tags")
        self.assertNotEqual(sc_ref["status"], "error")


class RunningDigests(unittest.TestCase):
    """The runtime reports Docker Hub images fully qualified; the spec may use the short form."""

    def test_short_docker_hub_ref_finds_its_running_digest(self):
        class K(FakeK8s):
            def workloads(self):
                ws = super().workloads()
                for name, image in (("hubapp", "example/hubapp"), ("hubtool", "example/hubtool:latest")):
                    ws.append({"kind": "Deployment", "metadata": {"namespace": "apps", "name": name, "annotations": {}},
                               "spec": {"template": {"spec": {"containers": [{"name": "c", "image": image}]}}}})
                return ws

            def pods(self, namespace=None):
                pods = super().pods(namespace)
                for name, dig in (("hubapp", "sha256:aaa"), ("hubtool", "sha256:bbb")):
                    full = f"docker.io/example/{name}"
                    pods.append({"metadata": {"name": name, "namespace": "apps", "ownerReferences": [{"kind": "ReplicaSet"}]},
                                 "spec": {"containers": [{"name": "c", "image": full + ":latest"}]},
                                 "status": {"containerStatuses": [{"image": full + ":latest", "imageID": f"{full}@{dig}"}]}})
                return pods

        sc = Scanner(K(), {})
        sc.resolve = lambda c: c
        comps = {c["id"]: c for c in sc.run()["components"]}
        self.assertEqual(comps["workload/apps/hubapp/hubapp"]["running_digests"], ["sha256:aaa"])
        self.assertEqual(comps["workload/apps/hubtool/hubtool"]["running_digests"], ["sha256:bbb"])

    def test_running_digest_found_when_runtime_reports_another_name(self):
        # The node already held the image under another name, so the runtime reports that name
        # (a version tag, or an old registry) instead of the floating tag the pod asked for.
        cases = (("shelf", "ghcr.io/example/shelf:latest", "ghcr.io/example/shelf:2.4.1", "sha256:ccc"),
                 ("mcptool", "registry.example/team/mcptool:latest", "10.0.0.5:5002/mcptool:v0.2.6", "sha256:ddd"))

        class K(FakeK8s):
            def workloads(self):
                ws = super().workloads()
                for name, asked, _, _ in cases:
                    ws.append({"kind": "Deployment", "metadata": {"namespace": "apps", "name": name, "annotations": {}},
                               "spec": {"template": {"spec": {"containers": [{"name": name, "image": asked}]}}}})
                return ws

            def pods(self, namespace=None):
                pods = super().pods(namespace)
                for name, asked, reported, dig in cases:
                    pods.append({"metadata": {"name": name, "namespace": "apps", "ownerReferences": [{"kind": "ReplicaSet"}]},
                                 "spec": {"containers": [{"name": name, "image": asked}]},
                                 "status": {"containerStatuses": [{"name": name, "image": reported,
                                                                   "imageID": f"{reported.rsplit(':', 1)[0]}@{dig}"}]}})
                return pods

        sc = Scanner(K(), {})
        sc.resolve = lambda c: c
        comps = {c["id"]: c for c in sc.run()["components"]}
        self.assertEqual(comps["workload/apps/shelf/shelf"]["running_digests"], ["sha256:ccc"])
        self.assertEqual(comps["workload/apps/mcptool/mcptool"]["running_digests"], ["sha256:ddd"])


if __name__ == "__main__":
    unittest.main()
