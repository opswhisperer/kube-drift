"""Helm charts deployed by Argo CD (opt-in: `argocd.enabled` in the config).

Argo CD renders a chart with `helm template` and applies the result, so it never writes the Helm
release Secrets kube-drift reads releases from. Its Application objects say instead which chart,
repository and version each source deployed, and which objects belong to the Application.
"""
from __future__ import annotations

from typing import Optional

TRACKING = "argocd.argoproj.io/tracking-id"
LOCAL_SERVER, LOCAL_NAME = "https://kubernetes.default.svc", "in-cluster"


def is_oci(repo_url: str) -> bool:
    """Argo CD writes an OCI chart repository without a scheme (ghcr.io/org/charts), or as oci://."""
    return repo_url.startswith("oci://") or "://" not in repo_url


def oci_ref(repo_url: str, chart: str) -> str:
    return f"{repo_url.removeprefix('oci://').rstrip('/')}/{chart}"


def local(app: dict) -> bool:
    """Whether an Application deploys to the cluster it lives in (the one kube-drift scans)."""
    dest = (app.get("spec") or {}).get("destination") or {}
    return (dest.get("server") or LOCAL_SERVER) == LOCAL_SERVER and (dest.get("name") or LOCAL_NAME) == LOCAL_NAME


def _sources(spec: dict) -> tuple[list[dict], bool]:
    """(sources, multi): `spec.sources` when it has any, else `spec.source` alone."""
    if spec.get("sources"):
        return [s or {} for s in spec["sources"]], True
    return [spec.get("source") or {}], False


def _at(items, i: int):
    return items[i] if isinstance(items, list) and i < len(items) else None


def helm_sources(app: dict) -> list[dict]:
    """One entry per Helm chart source (`chart:` set) of an Application. Sources that only provide
    values (`ref:`) or render a git path are left out.

    `version` is what Argo CD deployed: the synced revision while the Application is Synced; else
    the newest deployment in its history (or the last successful sync) of that same chart; else,
    when it has never been deployed, the revision it would sync to (`targetRevision` until Argo CD
    has resolved it), with `deployed` false.
    """
    meta, spec, status = app.get("metadata") or {}, app.get("spec") or {}, app.get("status") or {}
    sources, multi = _sources(spec)
    sync = status.get("sync") or {}
    sync_revs = sync.get("revisions") if multi else [sync.get("revision")]
    history = status.get("history") or []
    last = history[-1] if history else {}
    op = status.get("operationState") or {}
    result = (op.get("syncResult") or {}) if op.get("phase") == "Succeeded" else {}

    def from_record(rec: dict, i: int, chart: str) -> Optional[str]:
        """The revision a history entry or sync result applied for source i, if it was this chart."""
        src = _at(rec.get("sources"), i) if multi else rec.get("source")
        rev = _at(rec.get("revisions"), i) if multi else rec.get("revision")
        return rev if rev and (src or {}).get("chart") == chart else None

    dest = spec.get("destination") or {}
    out = []
    for i, src in enumerate(sources):
        chart = src.get("chart")
        if not chart:
            continue
        target = str(src.get("targetRevision") or "")
        synced_rev = _at(sync_revs, i)
        deployed_rev = from_record(last, i, chart) or from_record(result, i, chart)
        if sync.get("status") == "Synced" and synced_rev:
            version, deployed = synced_rev, True
        elif deployed_rev:
            version, deployed = deployed_rev, True
        else:
            version, deployed = synced_rev or target, False
        out.append({
            "app": f"{meta.get('namespace', '')}/{meta.get('name', '')}",
            "index": i if multi else None,
            "release": (src.get("helm") or {}).get("releaseName") or meta.get("name", ""),
            "namespace": dest.get("namespace") or "default",
            "chart": chart, "repo": src.get("repoURL") or "",
            "version": str(version or ""), "target": target, "deployed": deployed,
            "sync_status": sync.get("status") or "",
            "deployed_at": (last.get("deployedAt") or "") if deployed else "",
        })
    return out


def renderers(app: dict) -> int:
    """How many of an Application's sources produce objects: a chart or a git path. A `ref:`
    source only provides values files to the others."""
    return sum(1 for s in _sources(app.get("spec") or {})[0] if s.get("chart") or s.get("path"))


def tracking_app(obj: dict) -> Optional[str]:
    """The Application named by an object's tracking-id annotation
    (`<app>:<group>/<kind>:<namespace>/<name>`; `<app>` is `<namespace>_<name>` for an
    Application outside Argo CD's own namespace), when the annotation describes this very object.
    A copied annotation names another object, and Argo CD doesn't count that one as its own."""
    meta = obj.get("metadata") or {}
    tid = (meta.get("annotations") or {}).get(TRACKING) or ""
    app, _, rest = tid.partition(":")
    group_kind, _, ns_name = rest.partition(":")
    ns, _, name = ns_name.partition("/")
    if not app or group_kind.rsplit("/", 1)[-1] != obj.get("kind") or name != meta.get("name") \
            or (ns or "") != (meta.get("namespace") or ""):
        return None
    return app
