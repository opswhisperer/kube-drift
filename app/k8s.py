"""Minimal in-cluster Kubernetes API client (service-account token + CA bundle).

Outside the cluster it falls back to KUBE_API / KUBE_TOKEN env vars, which is handy
with `kubectl proxy` (KUBE_API=http://127.0.0.1:8001, no token needed).
"""
from __future__ import annotations

import base64
import gzip
import json
import logging
import os
import ssl
import urllib.error
import urllib.request
from typing import Iterator, Optional

log = logging.getLogger("kube-drift.k8s")

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


class KubeError(Exception):
    def __init__(self, status: int, method: str, path: str, body: str = ""):
        try:
            msg = json.loads(body).get("message") or body
        except Exception:  # noqa: BLE001
            msg = body
        super().__init__(f"{method} {path}: HTTP {status}: {msg[:300]}")
        self.status, self.message = status, msg


class K8s:
    def __init__(self):
        self.base = os.environ.get("KUBE_API")
        self.token = os.environ.get("KUBE_TOKEN")
        self.ctx: Optional[ssl.SSLContext] = None
        if not self.base:
            host, port = os.environ.get("KUBERNETES_SERVICE_HOST"), os.environ.get("KUBERNETES_SERVICE_PORT", "443")
            if not host:
                raise RuntimeError("not in-cluster and KUBE_API unset")
            self.base = f"https://{host}:{port}"
            with open(f"{SA_DIR}/token") as f:
                self.token = f.read().strip()
            self.ctx = ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt")
        self.base = self.base.rstrip("/")

    def request(self, method: str, path: str, params: Optional[dict] = None, body: Optional[dict] = None,
                accept: str = "application/json"):
        url = self.base + path
        if params:
            url += "?" + "&".join(f"{k}={urllib.request.quote(str(v), safe='=,')}" for k, v in params.items())
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, method=method, data=data, headers={"Accept": accept})
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=30, context=self.ctx) as r:
                return json.loads(r.read().decode() or "null")
        except urllib.error.HTTPError as e:
            raise KubeError(e.code, method, path, e.read().decode("utf-8", "replace")) from None

    def get(self, path: str, params: Optional[dict] = None, accept: str = "application/json"):
        return self.request("GET", path, params, accept=accept)

    def items(self, path: str, params: Optional[dict] = None, metadata_only: bool = False) -> Iterator[dict]:
        """Iterate a list endpoint, following `continue` tokens. `metadata_only` asks the API
        server for PartialObjectMetadata (no Secret data, no specs)."""
        params = dict(params or {})
        params.setdefault("limit", 500)
        accept = ("application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1,application/json"
                  if metadata_only else "application/json")
        while True:
            page = self.get(path, params, accept=accept)
            yield from page.get("items", [])
            cont = page.get("metadata", {}).get("continue")
            if not cont:
                return
            params["continue"] = cont

    # ----- convenience -------------------------------------------------------
    def version(self) -> dict:
        return self.get("/version")

    def nodes(self) -> list[dict]:
        return list(self.items("/api/v1/nodes"))

    def workloads(self) -> list[dict]:
        out = []
        for kind, path in (("Deployment", "deployments"), ("StatefulSet", "statefulsets"), ("DaemonSet", "daemonsets")):
            for it in self.items(f"/apis/apps/v1/{path}"):
                it["kind"] = kind
                out.append(it)
        return out

    def pods(self, namespace: Optional[str] = None) -> list[dict]:
        path = f"/api/v1/namespaces/{namespace}/pods" if namespace else "/api/v1/pods"
        return list(self.items(path))

    def helm_releases(self) -> list[dict]:
        """Decode Helm v3 release Secrets. Returns the newest revision per release."""
        best: dict[tuple[str, str], dict] = {}
        for s in self.items("/api/v1/secrets", {"labelSelector": "owner=helm"}):
            if s.get("type") != "helm.sh/release.v1":
                continue
            lab = s["metadata"].get("labels", {})
            key = (s["metadata"]["namespace"], lab.get("name", ""))
            rev = int(lab.get("version", "0"))
            if key in best and best[key]["_rev"] >= rev:
                continue
            try:
                raw = base64.b64decode(s["data"]["release"])
                raw = base64.b64decode(raw)
                if raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                rel = json.loads(raw)
            except Exception as e:  # noqa: BLE001
                log.warning("cannot decode helm release %s/%s: %s", *key, e)
                continue
            meta = (rel.get("chart") or {}).get("metadata") or {}
            best[key] = {
                "_rev": rev,
                "name": rel.get("name") or key[1],
                "namespace": rel.get("namespace") or key[0],
                "revision": rev,
                "status": (rel.get("info") or {}).get("status", lab.get("status", "")),
                "updated": (rel.get("info") or {}).get("last_deployed", ""),
                "chart": meta.get("name", ""),
                "chart_version": meta.get("version", ""),
                "app_version": meta.get("appVersion", ""),
                "home": meta.get("home", ""),
                "sources": meta.get("sources") or [],
                "annotations": meta.get("annotations") or {},
            }
        return sorted(best.values(), key=lambda r: (r["namespace"], r["name"]))
