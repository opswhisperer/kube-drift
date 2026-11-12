"""kor output and API objects for offline orphan scans, for the invented cluster in fake_k8s.py."""
from __future__ import annotations

import copy
import json

from app.k8s import KubeError

# `kor <resources> -o json --group-by resource --show-reason --ignore-owner-references`
KOR_JSON = {
    "ConfigMap": {
        "apps": [{"name": "istio-ca-crl", "reason": "ConfigMap is not used in any pod or container"}],
        "web": [{"name": "web-assets-25thdt57cm", "reason": "ConfigMap is not used in any pod or container"}],
        "monitoring": [{"name": "dashboard-legacy", "reason": "ConfigMap is not used in any pod or container"},
                       {"name": "dashboard-nodes", "reason": "ConfigMap is not used in any pod or container"}],
    },
    "Secret": {
        "apps": [{"name": "kube-drift-tls", "reason": "Secret is not used in any pod, container, or ingress"},
                 {"name": "web-tls", "reason": "Secret is not used in any pod, container, or ingress"}],
        "cert-manager": [{"name": "letsencrypt", "reason": "Secret is not used in any pod, container, or ingress"}],
        "sandbox": [{"name": "demo-db-credentials", "reason": "Secret is not used in any pod, container, or ingress"}],
    },
    "Pvc": {"media": [{"name": "old-uploads", "reason": "PVC is not in use"}]},
    "Job": {"db": [{"name": "backup-verify", "reason": "Job has completed"}],
            "backup": [{"name": "repo-maintain-1775037600", "reason": "Job has completed"}]},
    "ClusterRoleBinding": {"": [
        {"name": "kubeadm:apiserver-kubelet-client", "reason": "ClusterRoleBinding references a non-existing ClusterRole"},
        {"name": "legacy-dashboard", "reason": "ClusterRoleBinding references a non-existing ClusterRole"}]},
    "RoleBinding": {"velero": [{"name": "velero-ui-admin", "reason": "RoleBinding references a non-existing ServiceAccount"}]},
    "StorageClass": {"": [{"name": "longhorn-static", "reason": "Not in Use"}]},
    "Pdb": None,
    "Service": {"apps": None},
}

TS = "2026-04-01T10:00:00Z"


def _obj(api, kind, ns, name, **extra):
    md = {"name": name, "uid": f"uid-{name}", "resourceVersion": "100", "creationTimestamp": TS,
          "managedFields": [{"manager": "kubectl"}]}
    if ns:
        md["namespace"] = ns
    md.update(extra.pop("metadata", {}))
    return {"apiVersion": api, "kind": kind, "metadata": md, **extra}


OBJECTS = {
    ("ConfigMap", "apps", "istio-ca-crl"): _obj("v1", "ConfigMap", "apps", "istio-ca-crl", data={"ca-crl.pem": ""}),
    # an older copy of a generated ConfigMap: Argo CD still tracks it, but web-assets-8k9f6b5cmd is the one mounted
    ("ConfigMap", "web", "web-assets-25thdt57cm"): _obj("v1", "ConfigMap", "web", "web-assets-25thdt57cm",
                                                               data={"index.html": "<html>\n  <body>hi</body>\n</html>\n"},
                                                               metadata={"annotations": {
                                                                   "argocd.argoproj.io/tracking-id": "web:/ConfigMap:web/web-assets-25thdt57cm"}}),
    ("ConfigMap", "web", "web-assets-8k9f6b5cmd"): _obj("v1", "ConfigMap", "web", "web-assets-8k9f6b5cmd", data={"index.html": "<html/>\n"},
                                                               metadata={"creationTimestamp": "2026-05-01T10:00:00Z"}),
    ("ConfigMap", "monitoring", "dashboard-legacy"): _obj(
        "v1", "ConfigMap", "monitoring", "dashboard-legacy", data={"nut.json": "{}"},
        metadata={"annotations": {"kubectl.kubernetes.io/last-applied-configuration": "{...}"}}),
    # Grafana's sidecar loads it by label; nothing mounts it. dashboard-legacy above lost its label
    ("ConfigMap", "monitoring", "dashboard-nodes"): _obj("v1", "ConfigMap", "monitoring", "dashboard-nodes", data={"nodes.json": "{}"},
                                                         metadata={"labels": {"grafana_dashboard": "1", "team": "platform"}}),
    ("Secret", "apps", "kube-drift-tls"): _obj("v1", "Secret", "apps", "kube-drift-tls", type="kubernetes.io/tls",
                                               data={"tls.crt": "Zm9v", "tls.key": "YmFy"},
                                               metadata={"annotations": {"cert-manager.io/certificate-name": "kube-drift-tls"}}),
    ("Secret", "apps", "web-tls"): _obj("v1", "Secret", "apps", "web-tls", type="kubernetes.io/tls", data={"tls.crt": "Zm9v"}),
    ("Secret", "cert-manager", "letsencrypt"): _obj("v1", "Secret", "cert-manager", "letsencrypt", data={"tls.key": "a2V5"}),
    ("Secret", "sandbox", "demo-db-credentials"): _obj("v1", "Secret", "sandbox", "demo-db-credentials", data={"password": "cHc="},
                                                       metadata={"labels": {"app.kubernetes.io/managed-by": "Helm"},
                                                                 "annotations": {"meta.helm.sh/release-name": "demo",
                                                                                 "meta.helm.sh/release-namespace": "sandbox"}}),
    ("PersistentVolumeClaim", "media", "old-uploads"): _obj("v1", "PersistentVolumeClaim", "media", "old-uploads",
                                                                spec={"volumeName": "pvc-1", "accessModes": ["ReadWriteOnce"]},
                                                                status={"phase": "Bound"}),
    ("Job", "db", "backup-verify"): _obj(
        "batch/v1", "Job", "db", "backup-verify",
        metadata={"labels": {"controller-uid": "abc", "job-name": "backup-verify"}},
        spec={"selector": {"matchLabels": {"batch.kubernetes.io/controller-uid": "abc"}},
              "template": {"metadata": {"labels": {"batch.kubernetes.io/controller-uid": "abc", "controller-uid": "abc"}},
                           "spec": {"containers": [{"name": "v", "image": "busybox"}], "restartPolicy": "Never"}}},
        status={"succeeded": 1}),
    # made by a backup operator every hour and pruned by it: no owner reference, no labels that say so
    ("Job", "backup", "repo-maintain-1775037600"): _obj(
        "batch/v1", "Job", "backup", "repo-maintain-1775037600",
        metadata={"managedFields": [{"manager": "backup-operator", "operation": "Update", "time": "2026-04-01T10:00:00Z"},
                                    {"manager": "kube-controller-manager", "operation": "Update", "subresource": "status",
                                     "time": "2026-04-01T10:05:00Z"}]},
        spec={"template": {"spec": {"containers": [{"name": "m", "image": "busybox"}], "restartPolicy": "Never"}}},
        status={"succeeded": 1}),
    ("ClusterRoleBinding", "", "kubeadm:apiserver-kubelet-client"): _obj("rbac.authorization.k8s.io/v1", "ClusterRoleBinding", "",
                                                                         "kubeadm:apiserver-kubelet-client"),
    ("ClusterRoleBinding", "", "legacy-dashboard"): _obj("rbac.authorization.k8s.io/v1", "ClusterRoleBinding", "", "legacy-dashboard",
                                                roleRef={"kind": "ClusterRole", "name": "legacy-dashboard"},
                                                metadata={"annotations": {"meta.helm.sh/release-name": "legacy-dashboard",
                                                                          "meta.helm.sh/release-namespace": "old-dashboard"}}),
    ("RoleBinding", "velero", "velero-ui-admin"): _obj("rbac.authorization.k8s.io/v1", "RoleBinding", "velero",
                                                                "velero-ui-admin"),
    ("StorageClass", "", "longhorn-static"): _obj("storage.k8s.io/v1", "StorageClass", "", "longhorn-static",
                                                  provisioner="driver.longhorn.io"),
}

GATEWAYS = [{"metadata": {"namespace": "apps", "name": "kube-drift"},
             "spec": {"listeners": [{"name": "https", "tls": {"certificateRefs": [{"kind": "Secret", "name": "kube-drift-tls"}]}}]}},
            {"metadata": {"namespace": "apps", "name": "web"},
             "spec": {"listeners": [{"name": "https", "tls": {"certificateRefs": [{"name": "web-tls"}]}}]}}]
CLUSTER_ISSUERS = [{"metadata": {"name": "letsencrypt"},
                    "spec": {"acme": {"privateKeySecretRef": {"name": "letsencrypt"},
                                      "solvers": [{"dns01": {"cloudflare": {"apiTokenSecretRef": {"name": "cloudflare-api-token-secret"}}}}]}}}]

PODS = [{"metadata": {"namespace": "monitoring", "name": "grafana-7d9f8b6c54-x2x4q",
                      "ownerReferences": [{"kind": "ReplicaSet", "name": "grafana-7d9f8b6c54"}]},
         "spec": {"containers": [
             {"name": "grafana", "image": "docker.io/grafana/grafana:12.0.0"},
             {"name": "grafana-sc-dashboard", "image": "quay.io/kiwigrid/k8s-sidecar:1.30.0",
              "env": [{"name": "LABEL", "value": "grafana_dashboard"}, {"name": "LABEL_VALUE", "value": "1"},
                      {"name": "NAMESPACE", "value": "ALL"}, {"name": "RESOURCE", "value": "both"}]}]}}]

HELM_RELEASE_SECRETS = [{"metadata": {"namespace": "sandbox", "name": "sh.helm.release.v1.demo.v3",
                                      "labels": {"owner": "helm", "name": "demo", "status": "deployed", "version": "3"}}}]

PLURAL = {"configmaps": "ConfigMap", "secrets": "Secret", "persistentvolumeclaims": "PersistentVolumeClaim", "jobs": "Job",
          "clusterrolebindings": "ClusterRoleBinding", "rolebindings": "RoleBinding", "storageclasses": "StorageClass"}


class FakeKor:
    def __init__(self, raw=None):
        self.raw = KOR_JSON if raw is None else raw
        self.stderr_tail: list[str] = []
        self.calls: list[tuple] = []

    def version(self):
        return "v0.6.9"

    def run(self, resources, args):
        self.calls.append((resources, args))
        return json.loads(json.dumps(self.raw))


class FakeOrphanK8s:
    """Just enough of app.k8s.K8s for the orphan scanner."""

    def __init__(self):
        self.objects = copy.deepcopy(OBJECTS)

    def _parse(self, path):
        parts = path.strip("/").split("/")
        if "namespaces" in parts:
            i = parts.index("namespaces")
            return PLURAL.get(parts[i + 2]), parts[i + 1], parts[i + 3] if len(parts) > i + 3 else None
        plural = parts[-1] if parts[-1] in PLURAL else parts[-2]
        name = None if parts[-1] in PLURAL else parts[-1]
        return PLURAL.get(plural), "", name

    def items(self, path, params=None, metadata_only=False):
        if path.endswith("/gateways"):
            return iter(GATEWAYS)
        if path.endswith("/clusterissuers"):
            return iter(CLUSTER_ISSUERS)
        if path == "/api/v1/pods":
            return iter(copy.deepcopy(PODS))
        if path.endswith("/issuers"):
            raise KubeError(404, "GET", path, '{"message":"not found"}')
        if (params or {}).get("labelSelector") == "owner=helm":
            return iter(copy.deepcopy(HELM_RELEASE_SECRETS))
        kind, _, _ = self._parse(path)
        out = [{"metadata": o["metadata"]} if metadata_only else o for (k, _, _), o in self.objects.items() if k == kind]
        return iter(copy.deepcopy(out))

    def get(self, path, params=None, accept="application/json"):
        kind, ns, name = self._parse(path)
        if (kind, ns, name) not in self.objects:
            raise KubeError(404, "GET", path, '{"message":"not found"}')
        return copy.deepcopy(self.objects[(kind, ns, name)])
