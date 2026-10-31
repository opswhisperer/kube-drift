"""Renovate (opt-in: `renovate:` in the config): which updates Renovate already handles, and approving them.

Renovate's own report (run Renovate with `reportType: file`) is POSTed to /api/renovate/report after each run.
kube-drift keeps a condensed copy per repository (renovate.json under DATA_DIR): the dependencies Renovate
tracks and the branches it wants to open. A run for one repository replaces just that repository.

Each version-drift row an image or a chart is matched to a Renovate dependency, and gets `renovate`:
    approve    an update waits in the repository's Dependency Dashboard: the Upgrade button ticks its box
    pr         Renovate opened the PR (pr, pr_url)
    pending    Renovate has the branch planned (rate limits, schedule…); `result` says which
    tracked    Renovate watches it and has nothing to do
    untracked  Renovate doesn't track it (only once a report exists)
Every row Renovate knows carries `checked_at`: when the run that said so read that repository. A report for one repository
replaces only its own entry, so the repositories in a report differ in age; the dashboard compares `checked_at`
with the row's own check before calling an update "nothing pending".
An image is matched by name (with or without its registry) and tag, a chart by name and installed version. When
the same image and tag are in several repositories, the Argo CD Applications of the row's namespace (argocd:
enabled) say which repository is its own.

Approving needs the forge (renovate.forge: forgejo, gitea or github) and a token that may edit issues: kube-drift
ticks `<!-- approve-branch=… -->` in the repository's open dashboard issue, exactly as a person would. Renovate
opens the PR on its next run (or at once, when a workflow in the repository starts Renovate on that edit).

renovate.trigger (optional) names the workflow that runs Renovate, so Check now can ask it to look at a repository
again instead of waiting for its schedule: a workflow dispatch, the same call a person makes from the forge's UI.
Nothing here touches the cluster.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import http

log = logging.getLogger("kube-drift.renovate")

DEFAULT_TITLE = "Dependency Dashboard"   # Renovate's default dependencyDashboardTitle
DEFAULT_TOKEN_ENV = "RENOVATE_FORGE_TOKEN"
IMAGE_DATASOURCES = {"docker"}
CHART_DATASOURCES = {"helm", "docker"}   # an OCI chart is looked up as a docker image


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ----- the report ------------------------------------------------------------------------------------------------
def condense(report: dict) -> dict:
    """{repo: {deps: [...], branches: [...]}} from Renovate's report (reportType: file)."""
    repos = report.get("repositories")
    if not isinstance(repos, dict):
        raise ValueError("not a Renovate report: no `repositories` object (run Renovate with reportType: file)")
    out = {}
    for repo, r in repos.items():
        deps = []
        for mgr, files in ((r or {}).get("packageFiles") or {}).items():
            for pf in files or []:
                for d in pf.get("deps") or []:
                    if d.get("currentValue") is None:
                        continue
                    deps.append({"manager": mgr, "file": pf.get("packageFile"), "datasource": d.get("datasource"),
                                 "dep": d.get("depName"), "package": d.get("packageName") or d.get("depName"),
                                 "current": str(d["currentValue"]), "skip": d.get("skipReason")})
        branches = []
        for b in (r or {}).get("branches") or []:
            ups = [{"dep": u.get("depName"), "package": u.get("packageName") or u.get("depName"),
                    "file": u.get("packageFile"), "datasource": u.get("datasource"),
                    "current": str(u.get("currentValue")), "new": str(u.get("newValue") or u.get("newVersion") or ""),
                    "update_type": u.get("updateType")} for u in b.get("upgrades") or []]
            branches.append({"branch": b.get("branchName"), "title": b.get("prTitle"), "result": b.get("result"),
                             "pr": b.get("prNo"), "upgrades": ups})
        out[repo] = {"deps": deps, "branches": branches}
    return out


class Store:
    """renovate.json: {received_at, repos: {repo: {at, base_branch, deps, branches}}}."""

    def __init__(self, root: Path):
        self.file = root / "renovate.json"
        self.lock = threading.Lock()
        self.doc = {"received_at": None, "repos": {}}
        if self.file.exists():
            try:
                self.doc = json.loads(self.file.read_text())
            except Exception as e:  # noqa: BLE001
                log.warning("ignoring %s: %s", self.file, e)

    def ingest(self, report: dict, cfg: Optional[dict] = None) -> dict:
        repos = condense(report)
        at = _now()
        # the default branch, for the file links: asked once per run, and kept when the forge won't say
        branches = {repo: (default_branch(cfg, repo) if cfg else "") for repo in repos}
        with self.lock:
            for repo, r in repos.items():
                keep = (self.doc["repos"].get(repo) or {}).get("base_branch") or ""
                self.doc["repos"][repo] = {"at": at, "base_branch": branches[repo] or keep, **r}
            self.doc["received_at"] = at
            text = json.dumps(self.doc)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(text)
        os.replace(tmp, self.file)
        pending = sum(1 for r in repos.values() for b in r["branches"] if b["result"] == "needs-approval")
        return {"repositories": len(repos), "dependencies": sum(len(r["deps"]) for r in repos.values()),
                "awaiting_approval": pending, "received_at": at}

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.doc))


# ----- matching rows ---------------------------------------------------------------------------------------------
def _image_name(name: str) -> str:
    name = re.sub(r"^(docker\.io|index\.docker\.io|registry-1\.docker\.io)/", "", name or "")
    return re.sub(r"^library/", "", name)


def _same_image(row_name: str, dep: str) -> bool:
    a, b = _image_name(row_name), _image_name(dep)
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def _repo_slug(url: str) -> str:
    """'https://git.example.com/org/repo.git' or 'git@host:org/repo.git' -> 'org/repo'."""
    u = re.sub(r"\.git$", "", (url or "").strip().rstrip("/"))
    u = re.sub(r"^[a-z]+://[^/]+/", "", u)
    u = re.sub(r"^[^@]+@[^:]+:", "", u)
    return u.lower()


def _candidates(comp: dict, doc: dict) -> list[tuple[str, dict]]:
    """(repo, dep) pairs that are this row's image or chart."""
    out = []
    chart = comp["image"][6:] if comp.get("category") == "helm" and (comp.get("image") or "").startswith("chart ") else None
    for repo, r in doc.get("repos", {}).items():
        for d in r.get("deps", []):
            if chart:
                if d["datasource"] in CHART_DATASOURCES and chart in (d["dep"], (d["package"] or "").rsplit("/", 1)[-1]) \
                        and d["current"].lstrip("v") == str(comp.get("installed") or "").lstrip("v"):
                    out.append((repo, d))
            elif comp.get("image_repo") and d["datasource"] in IMAGE_DATASOURCES and d["current"] == comp.get("tag"):
                host = comp.get("image_host") or "docker.io"
                if _same_image(f"{host}/{comp['image_repo']}", d["package"] or d["dep"]):
                    out.append((repo, d))
    return out


def _branch_for(r: dict, dep: dict) -> Optional[tuple[dict, dict]]:
    for b in r.get("branches", []):
        for u in b.get("upgrades", []):
            if u["dep"] == dep["dep"] and u["current"] == dep["current"] and (u["file"] or "") == (dep["file"] or ""):
                return b, u
    return None


def annotate(components: list[dict], doc: dict, cfg: dict, namespace_repos: Optional[dict] = None) -> list[dict]:
    """Copies of the components, each image or chart row with a `renovate` field."""
    if not doc.get("received_at"):
        return components
    ns_repos = {ns: {_repo_slug(u) for u in urls} for ns, urls in (namespace_repos or {}).items()}
    out = []
    for c in components:
        if c.get("category") not in ("manifest", "helm-workload", "helm") or not (c.get("image_repo") or c.get("category") == "helm"):
            out.append(c)
            continue
        cands = _candidates(c, doc)
        if len(cands) > 1 and ns_repos.get(c.get("namespace")):
            own = [(repo, d) for repo, d in cands if repo.lower() in ns_repos[c["namespace"]]]
            cands = own or cands
        info = {"state": "untracked"}
        best = None
        for repo, d in cands:
            hit = _branch_for(doc["repos"][repo], d)
            rank = 0 if hit and hit[0]["result"] == "needs-approval" else 1 if hit and hit[0].get("pr") else 2 if hit else 3
            if best is None or rank < best[0]:
                best = (rank, repo, d, hit)
        if best:
            _, repo, d, hit = best
            info = {"state": "tracked", "repo": repo, "dependency": d["dep"], "file": d["file"],
                    **({"checked_at": at} if (at := doc["repos"][repo].get("at")) else {}),
                    **({"repo_url": u} if (u := repo_url(cfg, repo)) else {}),
                    **({"file_url": u} if (u := file_url(cfg, repo, d["file"], (doc["repos"][repo].get("base_branch") or ""))) else {})}
            if d.get("skip"):
                info["skipped"] = d["skip"]
            if hit:
                b, u = hit
                info.update({"branch": b["branch"], "title": b["title"], "new": u["new"], "update_type": u["update_type"],
                             "result": b["result"]})
                if b["result"] == "needs-approval":
                    info["state"] = "approve"
                elif b.get("pr"):
                    info.update({"state": "pr", "pr": b["pr"], "pr_url": pr_url(cfg, repo, b["pr"])})
                else:
                    info["state"] = "pending"
            if len({r for r, _ in cands}) > 1:
                info["also_in"] = sorted({r for r, _ in cands} - {repo})
        out.append({**c, "renovate": info})
    return out


# ----- the forge -------------------------------------------------------------------------------------------------
def forge_cfg(cfg: dict) -> dict:
    f = dict((cfg or {}).get("forge") or {})
    f.setdefault("type", "github")
    f.setdefault("url", "https://github.com" if f["type"] == "github" else "")
    f["url"] = f["url"].rstrip("/")
    f.setdefault("token_env", DEFAULT_TOKEN_ENV)
    f.setdefault("dashboard_title", (cfg or {}).get("dashboard_title") or DEFAULT_TITLE)
    return f


def pr_url(cfg: dict, repo: str, n: int) -> str:
    f = forge_cfg(cfg)
    return f"{f['url']}/{repo}/pull/{n}" if f["type"] == "github" else f"{f['url']}/{repo}/pulls/{n}"


def repo_url(cfg: dict, repo: str) -> str:
    f = forge_cfg(cfg)
    return f"{f['url']}/{repo}" if f["url"] and repo else ""


def file_url(cfg: dict, repo: str, path: str, branch: str) -> str:
    """The forge's page for a package file. Needs the branch: the repository's default, stored by ingest."""
    f = forge_cfg(cfg)
    if not (f["url"] and repo and path and branch):
        return ""
    at = "blob" if f["type"] == "github" else "src/branch"
    return f"{f['url']}/{repo}/{at}/{branch}/{path.lstrip('/')}"


def _api(f: dict) -> tuple[str, dict]:
    """(API base, auth headers) for the configured forge, or ("", {}) when it has no token."""
    tok = os.environ.get(f["token_env"], "")
    if not f["url"] or not tok:
        return "", {}
    if f["type"] == "github":
        api = "https://api.github.com" if f["url"] == "https://github.com" else f"{f['url']}/api/v3"
        return api, {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}
    if f["type"] in ("forgejo", "gitea"):
        return f"{f['url']}/api/v1", {"Authorization": f"token {tok}"}
    return "", {}


def default_branch(cfg: dict, repo: str) -> str:
    """The repository's default branch, for file links. Best effort: "" when the forge won't say."""
    api, auth = _api(forge_cfg(cfg))
    if not api or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo or ""):
        return ""
    try:
        return str(http.request(f"{api}/repos/{repo}", headers=auth).json().get("default_branch") or "")
    except Exception as e:  # noqa: BLE001  — a link is a nicety, never a reason to fail a report
        log.debug("default branch of %s: %s", repo, e)
        return ""


def can_approve(cfg: dict) -> bool:
    f = forge_cfg(cfg)
    return bool(f["url"] and os.environ.get(f["token_env"]))


class ForgeError(Exception):
    """A call to the forge that the caller should see: `status` is the HTTP status to answer with."""

    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


class ApproveError(ForgeError):
    pass


def approve(cfg: dict, repo: str, branch: str) -> dict:
    """Tick the branch's approval box in the repository's open Dependency Dashboard."""
    f = forge_cfg(cfg)
    if f["type"] not in ("github", "forgejo", "gitea"):
        raise ApproveError(503, f"renovate.forge.type {f['type']!r}: forgejo, gitea or github")
    api, auth = _api(f)
    if not api:
        raise ApproveError(503, f"approving needs renovate.forge.url and a token in ${f['token_env']}")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo or "") or not re.fullmatch(r"[\w./@+-]+", branch or ""):
        raise ApproveError(400, "repo must be owner/name and branch a branch name")
    if f["type"] == "github":
        listing, size = f"{api}/repos/{repo}/issues?state=open&per_page=100", 100
    else:
        # no q= search: Forgejo answers it from its issue indexer, which can miss an open issue (seen on 15.0)
        listing, size = f"{api}/repos/{repo}/issues?state=open&type=issues&limit=50", 50
    # GitHub lists pull requests as issues too, marked by a `pull_request` object; Forgejo/Gitea send
    # `"pull_request": null` on every plain issue, so test the value, not the key
    dash = []
    for page in range(1, 21):   # 20 pages: 1,000 (GitHub 2,000) open issues
        try:
            issues = http.request(f"{listing}&page={page}", headers=auth).json()
        except http.HttpError as e:
            raise ApproveError(502, f"listing issues of {repo}: HTTP {e.status}") from e
        dash = [i for i in issues if i.get("title") == f["dashboard_title"] and i.get("pull_request") is None]
        if dash or len(issues) < size:
            break
    if not dash:
        raise ApproveError(404, f"{repo} has no open issue titled {f['dashboard_title']!r}")
    issue = dash[0]
    body = issue.get("body") or ""
    box = re.compile(r"- \[([ xX])\] (<!-- approve-branch=" + re.escape(branch) + r" -->)")
    m = box.search(body)
    if not m:
        raise ApproveError(409, f"{branch} isn't waiting for approval in {repo}'s dashboard (already approved, or Renovate changed it)")
    url = issue.get("html_url") or ""
    if m.group(1).lower() == "x":
        return {"status": "already approved", "issue": url}
    new = body[:m.start()] + "- [x] " + m.group(2) + body[m.end():]
    try:
        http.request(f"{api}/repos/{repo}/issues/{issue['number']}", method="PATCH",
                     headers={**auth, "Content-Type": "application/json"}, data=json.dumps({"body": new}).encode())
    except http.HttpError as e:
        raise ApproveError(502, f"updating {repo}#{issue['number']}: HTTP {e.status}") from e
    log.info("approved %s in %s#%s", branch, repo, issue["number"])
    return {"status": "approved", "issue": url}


# ----- asking Renovate to look again -----------------------------------------------------------------------------
def trigger_cfg(cfg: dict) -> dict:
    """renovate.trigger, filled in: the workflow that runs Renovate, and how to name the repositories to it."""
    t = dict((cfg or {}).get("trigger") or {})
    t.setdefault("workflow", "renovate.yml")
    t.setdefault("ref", "main")
    t.setdefault("repos_input", "repos")
    t.setdefault("inputs", {})
    t.setdefault("token_env", forge_cfg(cfg)["token_env"])
    return t


def can_trigger(cfg: dict) -> bool:
    t = trigger_cfg(cfg)
    return bool(forge_cfg(cfg)["url"] and t.get("repo") and os.environ.get(t["token_env"]))


def trigger(cfg: dict, repos: list[str]) -> dict:
    """Start the Renovate workflow for these repositories (a workflow dispatch on the forge).

    The repositories go in as one input value, comma-separated, because one run can cover several."""
    f, t = forge_cfg(cfg), trigger_cfg(cfg)
    if not t.get("repo"):
        raise ForgeError(503, "asking Renovate to look needs renovate.trigger.repo (the repository holding its workflow)")
    if f["type"] not in ("github", "forgejo", "gitea"):
        raise ForgeError(503, f"renovate.forge.type {f['type']!r}: forgejo, gitea or github")
    api, auth = _api({**f, "token_env": t["token_env"]})
    if not api:
        raise ForgeError(503, f"asking Renovate to look needs renovate.forge.url and a token in ${t['token_env']}")
    bad = [r for r in repos if not re.fullmatch(r"[\w.-]+/[\w.-]+", r or "")]
    if not repos or bad:
        raise ForgeError(400, f"repositories must be owner/name: {', '.join(bad) or 'none given'}")
    repos = sorted(set(repos))
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", t["repo"]) or not re.fullmatch(r"[\w.-]+\.ya?ml", str(t["workflow"])):
        raise ForgeError(503, "renovate.trigger.repo must be owner/name and workflow a .yml file name")
    body = {"ref": t["ref"], "inputs": {**{k: str(v) for k, v in (t["inputs"] or {}).items()},
                                        t["repos_input"]: ",".join(repos)}}
    url = f"{api}/repos/{t['repo']}/actions/workflows/{t['workflow']}/dispatches"
    try:
        http.request(url, method="POST", headers={**auth, "Content-Type": "application/json"},
                     data=json.dumps(body).encode())
    except http.HttpError as e:
        raise ForgeError(502, f"starting {t['workflow']} in {t['repo']}: HTTP {e.status}") from e
    log.info("asked Renovate to look at %s (%s in %s)", ", ".join(repos), t["workflow"], t["repo"])
    return {"status": "started", "repositories": repos, "workflow": f"{t['repo']} {t['workflow']}"}


def behind(comp: dict) -> bool:
    """True when Renovate tracks this row, wants nothing, and last read that repository before the check that
    found the update. Then its silence says nothing yet, and a run is worth asking for."""
    r = comp.get("renovate") or {}
    if r.get("state") != "tracked" or not (comp.get("status") == "outdated" or comp.get("images_outdated")):
        return False
    return bool(r.get("checked_at") and comp.get("checked_at") and r["checked_at"] < comp["checked_at"])
