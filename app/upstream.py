"""Upstream "latest version" resolvers: OCI registries, ArtifactHub, GitHub, k8s."""
from __future__ import annotations

import base64
import functools
import json
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

# What was read from a digest: an image's OCI labels, or a Helm chart's metadata (chart_meta).
# A digest's content never changes, so this outlives scans (and is saved under DATA_DIR): a
# manifest GET counts against Docker Hub's anonymous pull limit.
_labels_lock = threading.Lock()
_labels: dict[str, dict] = {}
INDEX_TYPES = ("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json")
LINK_LABELS = {  # the ones that say where an image comes from; anything else is dropped
    "source": ("org.opencontainers.image.source", "org.label-schema.vcs-url"),
    "documentation": ("org.opencontainers.image.documentation",),
    "url": ("org.opencontainers.image.url", "org.label-schema.url"),
}


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
        # GitHub redirects this to the package page, which links the source repo even when the
        # image isn't named after it (ghcr.io/immich-app/immich-server lives in immich-app/immich).
        return f"https://ghcr.io/{repo}"
    if host == "lscr.io":
        return f"https://github.com/linuxserver/docker-{repo.split('/')[-1]}/releases"
    if host == "quay.io":
        return f"https://quay.io/repository/{repo}?tab=tags"
    if host == "registry.k8s.io":
        return f"https://explore.ggcr.dev/?repo=registry.k8s.io/{repo}"
    if host == "public.ecr.aws":
        return f"https://gallery.ecr.aws/{repo}"
    return ""  # no browsable page; the registry API wants a token, so a /v2/ link only shows 401


# Words that say nothing about which project an image is.
_GENERIC = {"docker", "image", "images", "container", "containers", "library", "latest", "base", "app",
            "server", "www", "com", "org", "io", "dev", "net", "github", "gitlab", "http", "https"}


def _words(text: str) -> set[str]:
    return {w for w in re.split(r"[^a-z0-9]+", text.lower()) if len(w) >= 3 and w not in _GENERIC}


def _relates(link: str, repo: str) -> bool:
    """Labels are inherited from the base image unless the build overrides them, so an app built
    FROM nginx or a distroless image can carry nginx's or Chainguard's source. Trust a label only
    when it names the image's owner or a word of its name."""
    owner = re.sub(r"[^a-z0-9]", "", repo.split("/")[0].lower()) if "/" in repo else ""
    if owner and owner != "library" and owner in re.sub(r"[^a-z0-9/]", "", link.lower()).split("/"):
        return True
    return bool(_words(repo.removeprefix("library/")) & _words(link))


def _release_page(source: str) -> str:
    """A source repo's releases page on GitHub or GitLab; any other source link as it is."""
    m = re.match(r"(?:git\+)?(?:https?://|git@)(github\.com|gitlab\.com)[/:]([^/\s]+)/([^/\s#?]+)", source)
    if not m:
        return source
    host, owner, name = m.group(1), m.group(2), m.group(3).removesuffix(".git")
    return f"https://{host}/{owner}/{name}/" + ("releases" if host == "github.com" else "-/releases")


def label_link(labels: dict, repo: str) -> Optional[str]:
    """Where to read release notes, from an image's OCI labels: the source repo's releases page,
    else its documentation, else its home page. None when no label clearly belongs to the image."""
    for key in ("source", "documentation", "url"):
        link = (labels.get(key) or "").strip()
        if link.startswith(("http://", "https://", "git@", "git+")) and _relates(link, repo):
            return _release_page(link) if key == "source" else link
    return None


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

    def _call(self, host: str, repo: str, path: str, method: str = "GET", accept: Optional[str] = None,
              _retry: bool = True):
        real = HOST_ALIASES.get(host, host)
        url = f"{self._scheme(host)}://{real}/v2/{repo}/{path}"
        headers = {"Accept": accept or "application/json"}
        key = f"{real}/{repo}"
        insecure = self.cfg.get(host, {}).get("insecure", False)
        basic = self._basic(host)
        cached = None if basic else _tokens.get(key)
        if basic:
            headers["Authorization"] = f"Basic {basic}"
        elif cached:
            headers["Authorization"] = f"Bearer {cached}"
        try:
            return request(url, method=method, headers=headers, insecure=insecure)
        except HttpError as e:
            # The cached token expired. Most registries answer 401; ECR Public answers 400 DENIED,
            # with no challenge, so drop the token and start the dance again from an anonymous call.
            if cached and _retry and e.status in (400, 401, 403):
                with _token_lock:
                    if _tokens.get(key) == cached:
                        del _tokens[key]
                return self._call(host, repo, path, method, accept, _retry=False)
            if e.status != 401 or basic:
                raise
            challenge = e.headers.get("www-authenticate", "")
            token = self._token(challenge, repo)
            if not token:
                raise
            with _token_lock:
                _tokens[key] = token
            headers["Authorization"] = f"Bearer {token}"
            return request(url, method=method, headers=headers, insecure=insecure)

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

    def labels(self, host: str, repo: str, digest: str) -> dict:
        """The source/documentation/url labels of an image, by digest; {} if it has none or they
        can't be read (private image, rate limit). Only definite answers are cached."""
        with _labels_lock:
            if digest in _labels:
                return _labels[digest]
        try:
            m = self._call(host, repo, f"manifests/{digest}", accept=MANIFEST_ACCEPT).json()
            if m.get("mediaType") in INDEX_TYPES or "manifests" in m:
                # Every platform is built from the same source, so any real one will do.
                ms = [x for x in m.get("manifests") or [] if (x.get("platform") or {}).get("os") not in (None, "unknown")]
                if not ms:
                    return {}
                pick = next((x for x in ms if x["platform"].get("architecture") == "amd64"), ms[0])
                m = self._call(host, repo, f"manifests/{pick['digest']}", accept=MANIFEST_ACCEPT).json()
            cfg_digest = (m.get("config") or {}).get("digest")
            raw = {}
            if cfg_digest:
                raw = (self._call(host, repo, f"blobs/{cfg_digest}").json().get("config") or {}).get("Labels") or {}
        except HttpError as e:
            if e.status not in (401, 403, 404):
                log.info("labels %s/%s@%s: HTTP %s", host, repo, digest[:19], e.status)
                return {}
            raw = {}
        except (ValueError, KeyError, AttributeError) as e:  # not JSON, or not an image
            log.info("labels %s/%s@%s: %s", host, repo, digest[:19], e)
            raw = {}
        found = {}
        for k, names in LINK_LABELS.items():
            v = next((raw[n].strip() for n in names if isinstance(raw.get(n), str) and raw[n].strip()), None)
            if v:
                found[k] = v[:300]
        with _labels_lock:
            _labels[digest] = found
        return found

    def chart_meta(self, host: str, repo: str, digest: str) -> dict:
        """{"kube_version": <range>} of a Helm chart pushed to an OCI registry, by manifest digest.
        Helm stores the chart's Chart.yaml as JSON in the config blob, so the chart itself is never
        pulled. kube_version is "" when the chart sets none; {} if it can't be read (not cached)."""
        with _labels_lock:
            if digest in _labels:
                return _labels[digest]
        try:
            m = self._call(host, repo, f"manifests/{digest}", accept=MANIFEST_ACCEPT).json()
            cfg = m.get("config") or {}
            if cfg.get("mediaType") != "application/vnd.cncf.helm.config.v1+json":
                return {}
            kv = self._call(host, repo, f"blobs/{cfg['digest']}").json().get("kubeVersion")
        except (HttpError, ValueError, KeyError, AttributeError) as e:
            log.info("chart metadata %s/%s@%s: %s", host, repo, digest[:19], e)
            return {}
        found = {"kube_version": kv.strip()[:200] if isinstance(kv, str) else ""}
        with _labels_lock:
            _labels[digest] = found
        return found

    @functools.lru_cache(maxsize=512)
    def digest(self, host: str, repo: str, tag: str) -> Optional[str]:
        r = self._call(host, repo, f"manifests/{tag}", method="HEAD", accept=MANIFEST_ACCEPT)
        return r.headers.get("docker-content-digest")


def load_labels(path) -> None:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return
    except (OSError, ValueError) as e:
        log.warning("ignoring unreadable %s: %s", path, e)
        return
    with _labels_lock:
        _labels.update({k: v for k, v in data.items() if isinstance(v, dict)})


def save_labels(path, keep: set[str]) -> None:
    """Write the label cache, pruned to the digests the last scan saw so it can't grow forever."""
    with _labels_lock:
        for d in set(_labels) - keep:
            del _labels[d]
        text = json.dumps(_labels, sort_keys=True)
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text)
        os.replace(tmp, path)
    except OSError as e:
        log.warning("can't save %s: %s", path, e)


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
        "kube_version": (p.get("data") or {}).get("kubeVersion") or None,  # the chart's Kubernetes range
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
