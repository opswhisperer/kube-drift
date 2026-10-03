"""Upstream "latest version" resolvers: OCI registries, ArtifactHub, GitHub, k8s."""
from __future__ import annotations

import base64
import functools
import logging
import os
import re
import threading
from typing import Optional

from .http import HttpError, get_json, request

log = logging.getLogger("kube-drift.upstream")

MANIFEST_ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])

# Registry hosts that are really somewhere else, or need special handling.
HOST_ALIASES = {
    "docker.io": "registry-1.docker.io",
    "index.docker.io": "registry-1.docker.io",
    "lscr.io": "ghcr.io",          # linuxserver's lscr.io fronts ghcr.io/linuxserver/*
    "docker.n8n.io": "docker.n8n.io",
}

_token_lock = threading.Lock()
_tokens: dict[str, str] = {}


# --------------------------------------------------------------------------- images
def split_image(ref: str) -> tuple[str, str, str, Optional[str]]:
    """'ghcr.io/a/b:tag@sha256:x' -> (host, repo, tag, digest). Docker Hub defaults applied."""
    digest = None
    if "@" in ref:
        ref, digest = ref.split("@", 1)
    first = ref.split("/", 1)[0]
    if "/" in ref and ("." in first or ":" in first or first == "localhost"):
        host, rest = first, ref[len(first) + 1:]
    else:
        host, rest = "docker.io", ref
    if ":" in rest.rsplit("/", 1)[-1]:
        repo, tag = rest.rsplit(":", 1)
    else:
        repo, tag = rest, "latest"
    if host == "docker.io" and "/" not in repo:
        repo = "library/" + repo
    return host, repo, tag, digest


def reference_url(host: str, repo: str) -> str:
    if host == "docker.io":
        return f"https://hub.docker.com/r/{repo}/tags" if not repo.startswith("library/") \
            else f"https://hub.docker.com/_/{repo[8:]}/tags"
    if host == "ghcr.io":
        owner, _, name = repo.partition("/")
        return f"https://github.com/{owner}/{name.split('/')[0]}/releases"
    if host == "lscr.io":
        return f"https://github.com/linuxserver/docker-{repo.split('/')[-1]}/releases"
    if host == "quay.io":
        return f"https://quay.io/repository/{repo}?tab=tags"
    if host == "registry.k8s.io":
        return f"https://explore.ggcr.dev/?repo=registry.k8s.io/{repo}"
    if host == "public.ecr.aws":
        return f"https://gallery.ecr.aws/{repo}"
    return f"https://{host}/v2/{repo}/tags/list"


class Registry:
    """Anonymous (or basic-auth) Docker Registry v2 client with bearer-token dance."""

    def __init__(self, registries_cfg: Optional[dict] = None):
        self.cfg = registries_cfg or {}

    def _scheme(self, host: str) -> str:
        return "http" if self.cfg.get(host, {}).get("insecure") else "https"

    def _basic(self, host: str) -> Optional[str]:
        env = self.cfg.get(host, {}).get("auth_env")
        val = os.environ.get(env, "") if env else ""
        return base64.b64encode(val.encode()).decode() if val else None

    def _call(self, host: str, repo: str, path: str, method: str = "GET", accept: Optional[str] = None):
        real = HOST_ALIASES.get(host, host)
        url = f"{self._scheme(host)}://{real}/v2/{repo}/{path}"
        headers = {"Accept": accept or "application/json"}
        key = f"{real}/{repo}"
        basic = self._basic(host)
        if basic:
            headers["Authorization"] = f"Basic {basic}"
        elif key in _tokens:
            headers["Authorization"] = f"Bearer {_tokens[key]}"
        try:
            return request(url, method=method, headers=headers, insecure=self.cfg.get(host, {}).get("insecure", False))
        except HttpError as e:
            if e.status != 401 or basic:
                raise
            challenge = e.headers.get("www-authenticate", "")
            token = self._token(challenge, repo)
            if not token:
                raise
            with _token_lock:
                _tokens[key] = token
            headers["Authorization"] = f"Bearer {token}"
            return request(url, method=method, headers=headers)

    @staticmethod
    def _token(challenge: str, repo: str) -> Optional[str]:
        m = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        realm = m.get("realm")
        if not realm or not challenge.lower().startswith("bearer"):
            return None
        q = {"service": m.get("service", ""), "scope": m.get("scope") or f"repository:{repo}:pull"}
        url = realm + ("&" if "?" in realm else "?") + "&".join(f"{k}={v}" for k, v in q.items() if v)
        data = get_json(url)
        return data.get("token") or data.get("access_token")

    @functools.lru_cache(maxsize=512)
    def tags(self, host: str, repo: str, max_pages: int = 40) -> tuple[str, ...]:
        """All tags of a repo (newest-first where the registry can sort; else creation order).

        Docker Hub's distribution API lists tags lexically and some repos carry tens of
        thousands of sha tags, so Hub uses its own API sorted by last_updated instead.
        """
        if host == "docker.io":
            return self._hub_tags(repo)
        if host == "quay.io":
            try:
                return self._quay_tags(repo)
            except HttpError:
                pass  # private repo or API hiccup: fall through to the distribution API
        out: list[str] = []
        path = "tags/list?n=1000"
        for _ in range(max_pages):
            r = self._call(host, repo, path)
            out.extend(r.json().get("tags") or [])
            link = r.headers.get("link", "")
            m = re.search(r"<([^>]+)>;\s*rel=\"next\"", link)
            if not m:
                break
            nxt = m.group(1)
            path = nxt.split(f"/v2/{repo}/", 1)[-1] if f"/v2/{repo}/" in nxt else nxt.lstrip("/")
        return tuple(out)

    @staticmethod
    def _hub_tags(repo: str, max_pages: int = 10) -> tuple[str, ...]:
        out: list[str] = []
        url: Optional[str] = f"https://hub.docker.com/v2/repositories/{repo}/tags?page_size=100&ordering=last_updated"
        for _ in range(max_pages):
            if not url:
                break
            data = get_json(url)
            out.extend(t["name"] for t in data.get("results", []))
            url = data.get("next")
        return tuple(out)

    @staticmethod
    def _quay_tags(repo: str, max_pages: int = 10) -> tuple[str, ...]:
        """Quay's own API lists active tags newest-first."""
        out: list[str] = []
        for page in range(1, max_pages + 1):
            data = get_json(f"https://quay.io/api/v1/repository/{repo}/tag/?limit=100&onlyActives=true&page={page}")
            out.extend(t["name"] for t in data.get("tags", []))
            if not data.get("has_additional"):
                break
        return tuple(out)

    @functools.lru_cache(maxsize=512)
    def digest(self, host: str, repo: str, tag: str) -> Optional[str]:
        r = self._call(host, repo, f"manifests/{tag}", method="HEAD", accept=MANIFEST_ACCEPT)
        return r.headers.get("docker-content-digest")


# ----------------------------------------------------------------------- artifacthub
AH = "https://artifacthub.io"


@functools.lru_cache(maxsize=256)
def artifacthub_package(repo: str, chart: str) -> Optional[dict]:
    try:
        p = get_json(f"{AH}/api/v1/packages/helm/{repo}/{chart}")
    except HttpError as e:
        if e.status == 404:
            return None
        raise
    return {
        "version": p.get("version"),
        "app_version": p.get("app_version"),
        "ref": f"{AH}/packages/helm/{repo}/{chart}",
        "home": p.get("home_url"),
    }


@functools.lru_cache(maxsize=256)
def artifacthub_search(chart: str) -> Optional[dict]:
    """Best-effort: find a chart by exact name, preferring official/verified repos."""
    data = get_json(f"{AH}/api/v1/packages/search?ts_query_web={chart}&kind=0&limit=25&facets=false")
    cands = [p for p in data.get("packages", []) if p.get("name") == chart]
    if not cands:
        return None
    cands.sort(key=lambda p: (
        -int(bool(p.get("official"))), -int(bool((p.get("repository") or {}).get("verified_publisher"))),
        -int(p.get("stars") or 0)))
    best = cands[0]
    repo = (best.get("repository") or {}).get("name", "")
    res = artifacthub_package(repo, chart) or {}
    res.setdefault("version", best.get("version"))
    res.setdefault("app_version", best.get("app_version"))
    res.setdefault("ref", f"{AH}/packages/helm/{repo}/{chart}")
    res["repo"] = repo
    res["ambiguous"] = len(cands) > 1
    return res


# ---------------------------------------------------------------------------- github
GH = "https://api.github.com"


def _gh_headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


@functools.lru_cache(maxsize=256)
def github_latest(owner_repo: str, version_regex: Optional[str] = None, prefer_tags: bool = False) -> Optional[dict]:
    """Latest release tag (falling back to newest non-prerelease tag). Optional regex extracts the version."""
    from .versions import cmp_versions, is_prerelease

    tag = None
    if not prefer_tags:
        try:
            rel = get_json(f"{GH}/repos/{owner_repo}/releases/latest", headers=_gh_headers())
            tag = rel.get("tag_name")
        except HttpError as e:
            if e.status not in (404,):
                raise
    if not tag:
        tags = get_json(f"{GH}/repos/{owner_repo}/tags?per_page=100", headers=_gh_headers())
        names = [t["name"] for t in tags if not is_prerelease(t["name"])]
        if version_regex:
            names = [n for n in names if re.search(version_regex, n)]
        names.sort(key=functools.cmp_to_key(cmp_versions), reverse=True)
        tag = names[0] if names else None
    if not tag:
        return None
    version = tag
    if version_regex:
        m = re.search(version_regex, tag)
        version = m.group(1) if m and m.groups() else (m.group(0) if m else tag)
    return {"version": version, "tag": tag, "ref": f"https://github.com/{owner_repo}/releases"}


# ------------------------------------------------------------------------- kubernetes
@functools.lru_cache(maxsize=16)
def k8s_stable(minor: Optional[str] = None) -> Optional[str]:
    """dl.k8s.io stable pointer: 'v1.35.9' for minor='1.35', or the newest overall."""
    name = f"stable-{minor}.txt" if minor else "stable.txt"
    try:
        return request(f"https://dl.k8s.io/release/{name}").text().strip()
    except HttpError:
        return None
