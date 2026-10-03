"""A stand-in for app.k8s.K8s: an invented but realistic homelab-style cluster, for offline
runs and tests. Images and charts are real public ones, so a full run resolves real versions.

    python -m tests.offline            # full resolve against real upstreams (network)
    python -m tests.offline --no-net   # inventory only
"""
from __future__ import annotations

# (release, namespace, chart, chart version, app version)
HELM = [
    ("cert-manager", "cert-manager", "cert-manager", "v1.20.2", "v1.20.2"),
    ("cnpg", "cnpg-system", "cloudnative-pg", "0.28.0", "1.29.0"),
    ("descheduler", "kube-system", "descheduler", "0.35.1", "0.35.1"),
    ("eg", "envoy-gateway-system", "gateway-helm", "v1.9.2", "v1.9.2"),
    ("external-secrets", "external-secrets", "external-secrets", "2.3.0", "v2.3.0"),
    ("fluent-bit", "logging", "fluent-bit", "0.57.2", "5.0.2"),
    ("gitea", "gitea", "gitea", "12.1.0", "1.24.0"),
    ("kubelet-csr-approver", "kube-system", "kubelet-csr-approver", "1.2.15", "v1.2.15"),
    ("litellm", "ai", "litellm-helm", "1.97.0", "1.97.0"),
    ("longhorn", "longhorn-system", "longhorn", "1.12.1", "v1.12.1"),
    ("metrics-server", "kube-system", "metrics-server", "3.13.0", "0.8.0"),
    ("nfs-subdir-external-provisioner", "kube-system", "nfs-subdir-external-provisioner", "4.0.18", "4.0.2"),
    ("openbao", "vault", "openbao", "0.28.2", "v2.5.3"),
    ("prometheus-stack", "monitoring", "kube-prometheus-stack", "83.4.1", "v0.90.1"),
    ("tailscale-operator", "tailscale", "tailscale-operator", "1.88.3", "v1.88.3"),
]

# (kind, ns, name, helm-release-or-None, [images])
WORKLOADS = [
    ("Deployment", "apps", "whoami", None, ["traefik/whoami:latest"]),
    ("Deployment", "apps", "homepage", None, ["ghcr.io/gethomepage/homepage:latest"]),
    ("Deployment", "apps", "uptime-kuma", None, ["louislam/uptime-kuma:1"]),
    ("Deployment", "apps", "registry-ui", None, ["joxit/docker-registry-ui:main"]),
    ("Deployment", "apps", "debug-shell", None, ["ubuntu:24.04"]),
    ("Deployment", "apps", "web", None, ["nginx:1.27-alpine", "quay.io/oauth2-proxy/oauth2-proxy:v7.6.0"]),
    ("Deployment", "apps", "web-gateway-istio", None, ["docker.io/istio/proxyv2:1.31.1"]),
    # first-party images from a private registry: floating, commit-sha and build-suffixed tags
    ("Deployment", "apps", "notes", None, ["10.0.0.5:5000/notes:latest"]),
    ("Deployment", "apps", "api", None, ["10.0.0.5:5000/api:87540e3"]),
    ("Deployment", "apps", "worker", None, ["10.0.0.5:5000/worker:0.0.161-589c824437-r1"]),
    ("Deployment", "ai", "litellm", "litellm", ["ghcr.io/berriai/litellm:main-stable"]),
    ("Deployment", "ai", "openwebui", None, ["ghcr.io/open-webui/open-webui:main"]),
    ("Deployment", "ai", "qdrant", None, ["qdrant/qdrant:latest"]),
    ("Deployment", "calico-system", "calico-typha", None, ["quay.io/tigera/typha:v3.23.1"]),
    ("Deployment", "cert-manager", "cert-manager", "cert-manager", ["quay.io/jetstack/cert-manager-controller:v1.20.2"]),
    ("Deployment", "cloudflare", "cloudflared", None, ["cloudflare/cloudflared:latest"]),
    ("Deployment", "envoy-gateway-system", "envoy-apps-web-b9da20a5", None,
     ["docker.io/envoyproxy/envoy:distroless-v1.39.1@sha256:eb2c01c13125d1629637cb4e4cce7207009fb7cc2c8027f9742758549d15b6f4", "docker.io/envoyproxy/gateway:v1.9.2"]),
    ("Deployment", "envoy-gateway-system", "envoy-apps-api-8ec3dec0", None,
     ["docker.io/envoyproxy/envoy:distroless-v1.39.1@sha256:eb2c01c13125d1629637cb4e4cce7207009fb7cc2c8027f9742758549d15b6f4", "docker.io/envoyproxy/gateway:v1.9.2"]),
    ("Deployment", "gitea", "gitea", "gitea", ["docker.gitea.com/gitea:1.24.0-rootless"]),
    ("Deployment", "gitea", "gitea-valkey", "gitea", ["docker.io/valkey/valkey:9.0-alpine@sha256:b4ee67d73e00393e712accc72cfd7003b87d0fcd63f0eba798b23251bfc9c394"]),
    ("Deployment", "istio-system", "istiod", None, ["docker.io/istio/pilot:1.31.1"]),
    ("Deployment", "kube-system", "coredns", None, ["registry.k8s.io/coredns/coredns:v1.13.1"]),
    ("Deployment", "kube-system", "kube-vip-cloud-provider", None, ["ghcr.io/kube-vip/kube-vip-cloud-provider:v0.0.11"]),
    ("Deployment", "kube-system", "snapshot-controller", None, ["registry.k8s.io/sig-storage/snapshot-controller:v8.2.1"]),
    ("Deployment", "longhorn-system", "csi-attacher", None, ["docker.io/longhornio/csi-attacher:v4.12.0"]),
    ("Deployment", "media", "jellyfin", None, ["jellyfin/jellyfin:latest"]),
    ("Deployment", "monitoring", "prometheus-stack-grafana", "prometheus-stack", ["quay.io/kiwigrid/k8s-sidecar:1.30.0", "docker.io/grafana/grafana:12.4.2"]),
    ("Deployment", "n8n", "n8n", None, ["docker.n8n.io/n8nio/n8n:latest"]),
    ("Deployment", "tailscale", "operator", "tailscale-operator", ["tailscale/k8s-operator:v1.88.3"]),
    ("Deployment", "tigera-operator", "tigera-operator", None, ["quay.io/tigera/operator:v1.42.4"]),
    ("Deployment", "velero", "velero", None, ["velero/velero:v1.17.0"]),
    ("StatefulSet", "db", "mongo", None, ["mongo:8"]),
    ("StatefulSet", "vault", "openbao", "openbao", ["quay.io/openbao/openbao:2.2.0"]),
    ("DaemonSet", "calico-system", "calico-node", None, ["quay.io/tigera/node:v3.23.1"]),
    ("DaemonSet", "kube-system", "kube-proxy", None, ["registry.k8s.io/kube-proxy:v1.35.9"]),
    ("DaemonSet", "logging", "fluent-bit", "fluent-bit", ["cr.fluentbit.io/fluent/fluent-bit:5.0.2"]),
    ("DaemonSet", "monitoring", "prometheus-stack-prometheus-node-exporter", "prometheus-stack", ["quay.io/prometheus/node-exporter:v1.11.1"]),
]

STATIC_PODS = [  # (name, image)
    ("etcd-cp-1", "registry.k8s.io/etcd:3.6.6-0"),
    ("kube-apiserver-cp-1", "registry.k8s.io/kube-apiserver:v1.35.9"),
    ("kube-controller-manager-cp-1", "registry.k8s.io/kube-controller-manager:v1.35.9"),
    ("kube-scheduler-cp-1", "registry.k8s.io/kube-scheduler:v1.35.9"),
    ("kube-vip-cp-1", "ghcr.io/kube-vip/kube-vip:v1.2.4"),
]

RUNNING_DIGESTS = {
    "registry.k8s.io/coredns/coredns:v1.13.1": "sha256:9b9128672209474da07c91439bf15ed704ae05ad918dd6454e5b6ae14e35fee6",
    "registry.k8s.io/kube-proxy:v1.35.9": "sha256:d08adb0c8c2c68075f1e6fa6c4c82880c5af54c1686ac20d22af612082c32a31",
    "ghcr.io/postfinance/kubelet-csr-approver:v1.2.15": "sha256:6469ef517d2b6d3cb108fce8a913483533a48b1cd78b5d4af307fa09e04a7818",
}


class FakeK8s:
    def version(self):
        return {"gitVersion": "v1.35.9", "platform": "linux/amd64"}

    def nodes(self):
        mk = lambda n, cp: {"metadata": {"name": n, "labels": {"node-role.kubernetes.io/control-plane": ""} if cp else {}},
                            "status": {"nodeInfo": {"kubeletVersion": "v1.35.9", "containerRuntimeVersion": "containerd://2.3.6",
                                                    "osImage": "Ubuntu 24.04.4 LTS", "kernelVersion": "6.8.0-138-generic"}}}
        return [mk("cp-1", True), mk("cp-2", True), mk("cp-3", True), mk("worker-1", False)]

    def helm_releases(self):
        return [{"name": n, "namespace": ns, "revision": 1, "status": "deployed", "updated": "2026-10-03T00:00:00Z",
                 "chart": c, "chart_version": cv, "app_version": av, "home": "", "sources": [], "annotations": {}}
                for n, ns, c, cv, av in HELM]

    def workloads(self):
        out = []
        for kind, ns, name, rel, images in WORKLOADS:
            ann = {"meta.helm.sh/release-name": rel} if rel else {}
            out.append({"kind": kind, "metadata": {"namespace": ns, "name": name, "annotations": ann},
                        "spec": {"template": {"spec": {"containers": [{"name": f"c{i}", "image": im} for i, im in enumerate(images)]}}}})
        return out

    def pods(self, namespace=None):
        pods = []
        if namespace in (None, "kube-system"):
            for name, image in STATIC_PODS:
                pods.append({"metadata": {"name": name, "namespace": "kube-system", "ownerReferences": [{"kind": "Node"}]},
                             "spec": {"containers": [{"name": "c", "image": image}]},
                             "status": {"containerStatuses": []}})
        if namespace is None:
            for image, dig in RUNNING_DIGESTS.items():
                pods.append({"metadata": {"name": "x", "namespace": "x", "ownerReferences": [{"kind": "ReplicaSet"}]},
                             "spec": {"containers": [{"name": "c", "image": image}]},
                             "status": {"containerStatuses": [{"image": image, "imageID": f"{image.split(':')[0]}@{dig}"}]}})
        return pods
