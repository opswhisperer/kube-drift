"""Orphaned resources: kor finds them, we add context, the user ignores them or copies a command.

kor (github.com/yonahd/kor) does the detection — "ConfigMap is not used in any pod",
"RoleBinding references a non-existing ServiceAccount", ... We run it as a subprocess
with JSON output, then enrich each finding with what kor doesn't look at:

- references kor misses (Gateway API TLS certificateRefs, cert-manager issuer secrets) —
  those are kor false positives, flagged "in-use";
- who manages it (Helm release, cert-manager Certificate, Argo CD, managed-by label) —
  deleting those is usually undone by the manager, so they're flagged "managed";
- leftovers of Helm releases that are no longer installed (still "orphan", with a note);
- protect rules (system:/kubeadm: RBAC, kubeadm ConfigMaps, …) that can never be deleted.

kube-drift never changes the cluster. Export and delete are shell scripts the user copies
and runs with their own kubectl credentials: each object is saved (server-owned fields
stripped, so `kubectl apply` restores it) and only deleted if that backup succeeded.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from typing import Iterable, Optional

from .scan import _match, now_iso
from .store import key

log = logging.getLogger("kube-drift.orphans")

# canonical kind -> (api prefix, plural, namespaced)
KINDS: dict[str, tuple[str, str, bool]] = {
    "ConfigMap": ("/api/v1", "configmaps", True),
    "Secret": ("/api/v1", "secrets", True),
    "Service": ("/api/v1", "services", True),
    "ServiceAccount": ("/api/v1", "serviceaccounts", True),
    "Pod": ("/api/v1", "pods", True),
    "PersistentVolumeClaim": ("/api/v1", "persistentvolumeclaims", True),
    "PersistentVolume": ("/api/v1", "persistentvolumes", False),
    "Deployment": ("/apis/apps/v1", "deployments", True),
    "StatefulSet": ("/apis/apps/v1", "statefulsets", True),
    "DaemonSet": ("/apis/apps/v1", "daemonsets", True),
    "ReplicaSet": ("/apis/apps/v1", "replicasets", True),
    "Job": ("/apis/batch/v1", "jobs", True),
    "Ingress": ("/apis/networking.k8s.io/v1", "ingresses", True),
    "NetworkPolicy": ("/apis/networking.k8s.io/v1", "networkpolicies", True),
    "HorizontalPodAutoscaler": ("/apis/autoscaling/v2", "horizontalpodautoscalers", True),
    "PodDisruptionBudget": ("/apis/policy/v1", "poddisruptionbudgets", True),
    "Role": ("/apis/rbac.authorization.k8s.io/v1", "roles", True),
    "RoleBinding": ("/apis/rbac.authorization.k8s.io/v1", "rolebindings", True),
    "ClusterRole": ("/apis/rbac.authorization.k8s.io/v1", "clusterroles", False),
    "ClusterRoleBinding": ("/apis/rbac.authorization.k8s.io/v1", "clusterrolebindings", False),
    "StorageClass": ("/apis/storage.k8s.io/v1", "storageclasses", False),
    "VolumeAttachment": ("/apis/storage.k8s.io/v1", "volumeattachments", False),
    "PriorityClass": ("/apis/scheduling.k8s.io/v1", "priorityclasses", False),
    "CustomResourceDefinition": ("/apis/apiextensions.k8s.io/v1", "customresourcedefinitions", False),
}
_ALIASES = {k.lower(): k for k in KINDS} | {
    "pvc": "PersistentVolumeClaim", "pv": "PersistentVolume", "hpa": "HorizontalPodAutoscaler",
    "pdb": "PodDisruptionBudget", "crd": "CustomResourceDefinition", "cm": "ConfigMap",
}

# kor subcommands that `kor all` covers, minus crd (slow: lists every CR of every CRD, and
# mostly reports CRDs that operators install for optional features).
DEFAULT_RESOURCES = [
    "configmap", "secret", "service", "serviceaccount", "deployment", "statefulset", "daemonset",
    "replicaset", "job", "pod", "pvc", "pv", "ingress", "hpa", "pdb", "role", "rolebinding",
    "clusterrole", "clusterrolebinding", "networkpolicy", "storageclass", "priorityclass", "volumeattachment",
]
DEFAULT_KOR_ARGS = ["--ignore-owner-references", "--older-than=1h"]
# Bump when the shape of a scan result changes; a persisted scan of another format is
# discarded on startup (and rescanned) instead of being served to code that can't read it.
FORMAT = 2

# Never offered a delete command, whatever the config says. kube-drift's own resources are
# added per scan, since its namespace comes from POD_NAMESPACE.
BUILTIN_PROTECT = [
    {"name_regex": r"^(system:|kubeadm:)", "why": "Kubernetes system object"},
    {"kind": "ConfigMap", "name": "kube-root-ca.crt", "why": "managed by kube-controller-manager"},
    {"kind": "ConfigMap", "namespace": "kube-system", "name_regex": r"^(kubeadm-config|kubelet-config|kube-proxy|"
     r"extension-apiserver-authentication|coredns|kube-apiserver-legacy-service-account-token-tracking)$",
     "why": "cluster bootstrap config"},
    {"kind": "ConfigMap", "namespace": "kube-public", "name": "cluster-info", "why": "cluster bootstrap config"},
]

# jq filter for backups: drop what the API server owns so `kubectl apply -f` re-creates
# the object as-is (a resourceVersion or uid in the file makes the create fail).
_STRIP = ("del(.status, .metadata.uid, .metadata.resourceVersion, .metadata.creationTimestamp, "
          ".metadata.generation, .metadata.managedFields, .metadata.ownerReferences, .metadata.selfLink, "
          '.metadata.annotations["kubectl.kubernetes.io/last-applied-configuration"], '
          '.metadata.annotations["deployment.kubernetes.io/revision"], '
          '.metadata.annotations["pv.kubernetes.io/bind-completed"], '
          '.metadata.annotations["pv.kubernetes.io/bound-by-controller"])')
_STRIP_KIND = {
    "Service": 'if .spec.clusterIP != "None" then del(.spec.clusterIP, .spec.clusterIPs) else . end',
    "Job": ('del(.spec.selector, .metadata.labels["controller-uid"], .metadata.labels["batch.kubernetes.io/controller-uid"], '
            '.spec.template.metadata.labels["controller-uid"], .spec.template.metadata.labels["batch.kubernetes.io/controller-uid"])'),
    "PersistentVolume": "del(.spec.claimRef.uid, .spec.claimRef.resourceVersion)",
}


def canonical_kind(kind: str) -> Optional[str]:
    return _ALIASES.get((kind or "").lower())


def object_path(kind: str, namespace: str, name: str) -> str:
    prefix, plural, namespaced = KINDS[kind]
    q = lambda s: re.sub(r"[^A-Za-z0-9_.:@-]", lambda m: "%%%02X" % ord(m.group()), s)  # noqa: E731
    if namespaced:
        return f"{prefix}/namespaces/{q(namespace)}/{plural}/{q(name)}"
    return f"{prefix}/{plural}/{q(name)}"


def rule_match(rule: dict, kind: str, ns: str, name: str, sources: Iterable[str] = ()) -> bool:
    """Every field the rule sets must match. `source` matches the evidence kube-drift found
    (e.g. "cert-manager", "gateway", "helm", "managed-by:tigera-operator")."""
    if rule.get("kind") and (canonical_kind(rule["kind"]) or rule["kind"]) != kind:
        return False
    if rule.get("source") and rule["source"] not in set(sources):
        return False
    return _match(rule, ns, name)


RULE_KEYS = ("kind", "namespace", "name_regex", "source")


def validate_rule(raw: dict) -> dict:
    """A user-made ignore rule: only known keys, at least one, sane sizes, a valid regex."""
    rule = {k: str(raw[k]).strip() for k in RULE_KEYS if str(raw.get(k) or "").strip()}
    if not rule:
        raise ValueError(f"rule needs at least one of {', '.join(RULE_KEYS)}")
    if any(len(v) > 200 for v in rule.values()):
        raise ValueError("rule fields are limited to 200 characters")
    if "kind" in rule:
        rule["kind"] = canonical_kind(rule["kind"]) or rule["kind"]
    if "name_regex" in rule:
        try:
            re.compile(rule["name_regex"])
        except re.error as e:
            raise ValueError(f"name_regex: {e}") from None
    return rule


def apply_ignores(items: list[dict], per_item: dict, user_rules: list[dict], config_rules: list[dict]):
    """Mark ignored items (one resource, a dashboard rule, or a config rule — first wins)
    and count each rule's matches. Returns (items, rules)."""
    rules = ([{**r, "from": "dashboard"} for r in user_rules] +
             [{**r, "id": f"config-{n}", "from": "config", "note": r.get("why", "")} for n, r in enumerate(config_rules)])
    counts = {r["id"]: 0 for r in rules}
    out = []
    for it in items:
        it = dict(it)
        it.pop("config_ignored", None)
        srcs = [h.get("source", "") for h in it.get("hints", [])]
        hit = [r for r in rules if rule_match(r, it["kind"], it["namespace"], it["name"], srcs)]
        for r in hit:
            counts[r["id"]] += 1
        if it["id"] in per_item:
            it["ignored"] = {**per_item[it["id"]], "by": "dashboard"}
        elif hit:
            r = hit[0]
            it["ignored"] = {"by": "rule" if r["from"] == "dashboard" else "config", "rule": r["id"],
                             "note": r.get("note", ""), "at": r.get("at")}
        out.append(it)
    return out, [{**r, "matches": counts[r["id"]]} for r in rules]


# --------------------------------------------------------------------------- kor
class Kor:
    """Runs the kor binary. In a pod kor uses the service account; out of cluster it uses
    `kubeconfig` (env KOR_KUBECONFIG) or the current kubectl context."""

    def __init__(self, binary: str = "kor", kubeconfig: Optional[str] = None, timeout: float = 1200):
        self.binary, self.kubeconfig, self.timeout = binary, kubeconfig, timeout
        self.stderr_tail: list[str] = []

    def version(self) -> str:
        try:
            out = subprocess.run([self.binary, "version"], capture_output=True, text=True, timeout=20).stdout
            m = re.search(r"v?\d+\.\d+\.\d+\S*", out)
            return m.group(0) if m else out.strip()
        except Exception as e:  # noqa: BLE001
            return f"unavailable ({type(e).__name__})"

    def run(self, resources: list[str], args: list[str]) -> dict:
        cmd = [self.binary, ",".join(resources), "-o", "json", "--group-by", "resource", "--show-reason", *args]
        if self.kubeconfig:
            cmd += ["--kubeconfig", self.kubeconfig]
        log.info("running %s", " ".join(cmd))
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        self.stderr_tail = [ln for ln in p.stderr.splitlines() if ln.strip() and "Warning:" not in ln][-20:]
        if p.returncode != 0:
            raise RuntimeError(f"kor exited {p.returncode}: {' | '.join(self.stderr_tail[-3:])[:400]}")
        out = p.stdout.strip()
        if not out:
            return {}
        return json.loads(out[out.index("{"):])


def parse_kor(raw: dict) -> list[dict]:
    """kor `-o json --group-by resource --show-reason` → flat findings."""
    out = []
    for kor_kind, by_ns in (raw or {}).items():
        kind = canonical_kind(kor_kind) or kor_kind
        for ns, entries in (by_ns or {}).items():
            for e in entries or []:
                name, reason = (e.get("name"), e.get("reason", "")) if isinstance(e, dict) else (str(e), "")
                if name:
                    out.append({"kind": kind, "namespace": ns or "", "name": name, "reason": reason or ""})
    return out


# --------------------------------------------------------------------------- scan
def _secret_refs(obj, out: set[str]):
    """Collect Secret names from an issuer spec: *SecretRef{name}, secretName."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower().endswith("secretref") and isinstance(v, dict) and v.get("name"):
                out.add(v["name"])
            elif k == "secretName" and isinstance(v, str):
                out.add(v)
            else:
                _secret_refs(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _secret_refs(v, out)


class OrphanScanner:
    def __init__(self, k8s, cfg: dict, kor: Optional[Kor] = None, self_namespace: Optional[str] = None):
        self.k8s = k8s
        self.cfg = cfg.get("orphans", {}) or {}
        self.kor = kor or Kor()
        self.self_namespace = self_namespace or os.environ.get("POD_NAMESPACE") or "kube-drift"
        self.warnings: list[str] = []

    def _try_items(self, path: str, **kw) -> list[dict]:
        try:
            return list(self.k8s.items(path, **kw))
        except Exception as e:  # noqa: BLE001
            status = getattr(e, "status", None)
            if status not in (404,):  # CRD not installed is normal
                self.warnings.append(f"list {path}: {e}"[:300])
            return []

    def references(self) -> dict[tuple[str, str, str], list[tuple[str, str]]]:
        """References kor doesn't follow: (kind, ns, name) -> [(source, text)]."""
        refs: dict[tuple[str, str, str], list[tuple[str, str]]] = {}
        add = lambda ns, name, src, why: refs.setdefault(("Secret", ns, name), []).append((src, why))  # noqa: E731
        for gw in self._try_items("/apis/gateway.networking.k8s.io/v1/gateways"):
            ns, gname = gw["metadata"]["namespace"], gw["metadata"]["name"]
            for ln in (gw.get("spec") or {}).get("listeners") or []:
                for ref in ((ln.get("tls") or {}).get("certificateRefs") or []):
                    if ref.get("kind", "Secret") == "Secret" and ref.get("name"):
                        add(ref.get("namespace") or ns, ref["name"], "gateway",
                            f"TLS certificate for Gateway {ns}/{gname} (kor doesn't check Gateways)")
        cm_ns = self.cfg.get("cert_manager_namespace", "cert-manager")
        for kind, path in (("ClusterIssuer", "/apis/cert-manager.io/v1/clusterissuers"),
                           ("Issuer", "/apis/cert-manager.io/v1/issuers")):
            for iss in self._try_items(path):
                ns = iss["metadata"].get("namespace") or cm_ns
                names: set[str] = set()
                _secret_refs(iss.get("spec") or {}, names)
                for n in names:
                    add(ns, n, "cert-manager-issuer", f"secret of cert-manager {kind} {iss['metadata']['name']} (kor doesn't check issuers)")
        return refs

    def metadata(self, kinds: Iterable[str]) -> dict[tuple[str, str, str], dict]:
        meta = {}
        for kind in kinds:
            if kind not in KINDS:
                continue
            prefix, plural, _ = KINDS[kind]
            for it in self._try_items(f"{prefix}/{plural}", metadata_only=True):
                m = it.get("metadata", {})
                meta[(kind, m.get("namespace", "") or "", m.get("name", ""))] = m
        return meta

    def helm_releases(self) -> Optional[set[tuple[str, str]]]:
        """(namespace, release) of every installed Helm release, from release Secret labels."""
        try:
            return {(m["metadata"].get("namespace", ""), (m["metadata"].get("labels") or {}).get("name", ""))
                    for m in self.k8s.items("/api/v1/secrets", {"labelSelector": "owner=helm"}, metadata_only=True)}
        except Exception as e:  # noqa: BLE001
            self.warnings.append(f"list helm releases: {e}"[:300])
            return None

    @staticmethod
    def hints(m: dict, releases: Optional[set[tuple[str, str]]] = None) -> list[dict]:
        """Evidence from metadata, as {type, source, text}: `recreated` (something will put it back
        if deleted) or `leftover` (its owner is gone)."""
        ann, lab = m.get("annotations") or {}, m.get("labels") or {}
        out = []
        rel, rel_ns = ann.get("meta.helm.sh/release-name"), ann.get("meta.helm.sh/release-namespace", "")
        if rel and releases is not None and (rel_ns, rel) not in releases:
            out.append({"type": "leftover", "source": "helm-leftover", "text": f"from Helm release {rel_ns}/{rel}, which is no longer installed"})
        elif rel:
            out.append({"type": "recreated", "source": "helm", "text": f"Helm release {rel_ns}/{rel} puts it back on its next upgrade"})
        if ann.get("cert-manager.io/certificate-name"):
            out.append({"type": "recreated", "source": "cert-manager",
                        "text": f"cert-manager reissues it from Certificate {ann['cert-manager.io/certificate-name']}"})
        if ann.get("argocd.argoproj.io/tracking-id") or lab.get("argocd.argoproj.io/instance"):
            out.append({"type": "recreated", "source": "argocd", "text": "Argo CD puts it back on its next sync"})
        mb = lab.get("app.kubernetes.io/managed-by")
        if mb and mb.lower() != "helm":
            out.append({"type": "recreated", "source": f"managed-by:{mb}", "text": f"{mb} manages it (managed-by label) and may put it back"})
        return out

    def run(self) -> dict:
        t0 = time.time()
        self.warnings = []
        resources = self.cfg.get("resources") or DEFAULT_RESOURCES
        args = list(self.cfg.get("kor_args", DEFAULT_KOR_ARGS))
        if self.cfg.get("exclude_namespaces"):
            args.append("--exclude-namespaces=" + ",".join(self.cfg["exclude_namespaces"]))
        found = parse_kor(self.kor.run(resources, args))
        self.warnings += [f"kor: {ln}"[:300] for ln in self.kor.stderr_tail]
        meta = self.metadata({f["kind"] for f in found})
        refs = self.references()
        releases = self.helm_releases()
        protect_rules = BUILTIN_PROTECT + [
            {"namespace": self.self_namespace, "name_regex": r"^kube-drift", "why": "kube-drift itself"},
        ] + (self.cfg.get("protect") or [])
        items = []
        for f in found:
            kind, ns, name = f["kind"], f["namespace"], f["name"]
            m = meta.get((kind, ns, name))
            it = {**f, "id": key(kind, ns, name), "known_kind": kind in KINDS, "namespaced": KINDS.get(kind, ("", "", True))[2]}
            it["created"] = (m or {}).get("creationTimestamp")
            used = [{"type": "in-use", "source": src, "text": t} for src, t in refs.get((kind, ns, name), [])]
            it["hints"] = used + (self.hints(m, releases) if m else [])
            types = {h["type"] for h in it["hints"]}
            prot = next((r for r in protect_rules if rule_match(r, kind, ns, name)), None)
            if prot:
                it["status"], it["protected"] = "protected", prot.get("why") or "protect rule"
            elif "in-use" in types:
                it["status"] = "in-use"
            elif "recreated" in types:
                it["status"] = "managed"
            else:
                it["status"] = "orphan"
            if m is None and kind in KINDS:
                it["hints"].append({"type": "gone", "source": "gone", "text": "not found when looked up — may already be deleted"})
            items.append(it)
        items.sort(key=lambda i: (i["kind"], i["namespace"], i["name"]))
        return {
            "format": FORMAT, "scanned_at": now_iso(), "duration_s": round(time.time() - t0, 1),
            "kor_version": self.kor.version(), "kor_args": [",".join(resources), *args],
            "items": items, "warnings": self.warnings[-30:],
        }


# --------------------------------------------------------------------------- commands
_SAFE_ARG = re.compile(r"^[A-Za-z0-9_.:@/=+-]+$")


def _q(s: str) -> str:
    return s if _SAFE_ARG.match(s) else "'" + s.replace("'", "'\\''") + "'"


def jq_filter(kinds: Iterable[str]) -> str:
    parts = [_STRIP] + [f'(if .kind == "{k}" then {_STRIP_KIND[k]} else . end)'
                        for k in sorted(set(kinds)) if k in _STRIP_KIND]
    return " | ".join(parts)


def commands(items: list[dict], action: str, context: Optional[str] = None) -> str:
    """A paste-able script that backs up each object to a local file and, for `delete`,
    deletes it only if its backup succeeded. Runs in a subshell so `set -e` can't close
    the user's terminal; umask 077 because Secret backups hold secret data."""
    if action not in ("export", "delete"):
        raise ValueError("action must be export or delete")
    items = [i for i in items if i.get("known_kind")]
    if action == "delete":
        items = [i for i in items if i.get("status") != "protected"]
    if not items:
        return ""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    d = f"kube-drift-{'backup' if action == 'delete' else 'export'}-{ts}"
    ctx = f" --context {_q(context)}" if context else ""
    verb = "back up and delete" if action == "delete" else "export"
    n = len(items)
    lines = [
        f"# kube-drift: {verb} {n} orphaned resource{'s' if n != 1 else ''}"
        + (f" on {context}" if context else ""),
        f"# restore with: kubectl{ctx} apply -f {d}/",
        "(",
        "set -euo pipefail; umask 077",
        f"d={d}; mkdir -p \"$d\"",
        f"strip={_q(jq_filter(i['kind'] for i in items))}",
    ]
    for i in items:
        kind, ns, name = i["kind"], i["namespace"], i["name"]
        res, nsarg = kind.lower(), f" -n {_q(ns)}" if ns else ""
        fname = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{kind}_{ns or 'cluster'}_{name}") + ".json"
        lines.append(f"kubectl{ctx}{nsarg} get {res} {_q(name)} -o json | jq \"$strip\" > \"$d/{fname}\"")
        if action == "delete":
            lines.append(f"kubectl{ctx}{nsarg} delete {res} {_q(name)}")
    lines += [f'echo "{"backed up and deleted" if action == "delete" else "exported"} {n} → $d/"', ")"]
    return "\n".join(lines) + "\n"
