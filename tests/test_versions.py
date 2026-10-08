import unittest
from unittest import mock

from app.scan import Scanner
from tests.fake_k8s import FakeK8s
from app.upstream import split_image
from app.versions import cmp_versions, is_floating, pick_latest, satisfies


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


if __name__ == "__main__":
    unittest.main()
