"""Build the component inventory from the cluster and resolve upstream versions."""
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import argocd, upstream
from .http import HttpError, get_json
from .k8s import KubeError
from .versions import cmp_versions, is_floating, pick_latest, satisfies, walk_up

log = logging.getLogger("kube-drift.scan")

STATUS_ORDER = {"outdated": 0, "unknown": 1, "error": 1, "current": 2}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _match(rule: dict, ns: str, name: str) -> bool:
    if rule.get("namespace") and rule["namespace"] != ns:
        return False
    if rule.get("name") and rule["name"] != name:
        return False
    if rule.get("name_regex") and not re.search(rule["name_regex"], name):
        return False
    return True


def _find(table: dict, *keys: str) -> tuple[Optional[str], dict]:
    """(matching key, entry): exact key match first; then any `re:<pattern>` key whose regex
    matches a key. (None, {}) when nothing matches."""
    for k in keys:
        if k in table:
            return k, table[k]
    for pat, val in table.items():
        if pat.startswith("re:") and any(re.search(pat[3:], k) for k in keys):
            return pat, val
    return None, {}


def _lookup(table: dict, *keys: str) -> dict:
    return _find(table, *keys)[1]


def _image_key(ref: str) -> str:
    """'x/y' and 'docker.io/x/y:latest@sha256:…' -> 'docker.io/x/y:latest' (digest dropped)."""
    host, repo, tag, _ = upstream.split_image(ref)
    return f"{host}/{repo}:{tag}"


def _jsonpath(obj, path: str):
    for part in path.split("."):
        if isinstance(obj, list):
            obj = obj[int(part)]
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
        if obj is None:
            return None
    return obj


def fold_chart_images(result: dict) -> dict:
    """An image a chart ships moves when the chart moves, so the chart is its track.

    Measuring it against the newest tag in its registry asks a question nobody can answer: while the
    chart is current there is no supported way to take that tag. The row sat outdated with no upgrade
    to offer, and its release read "current" and "1 outdated" at once. Such a row is current here, and
    the newer tag is kept as latest_any, which the dashboard shows the way it shows a `track` rule's.

    Two images are left alone. One Renovate found in a file (`renovate.file`): the tag is pinned in a
    repository you control, so it moves on its own and a PR can take it. And one whose running digest
    is stale, which is about the pod, not about the chart.

    Read time, not scan time: it needs Renovate's view of each row.
    """
    comps = result.get("components") or []
    rel_status = {c["id"]: c.get("status") for c in comps}
    out = []
    for c in comps:
        if (rel_status.get(c.get("release") or "") == "current" and c.get("status") == "outdated"
                and c.get("digest_current") is not False and not (c.get("renovate") or {}).get("file")):
            c = {**c, "latest_any": c.get("latest_any") or c.get("latest"),
                 "latest": c.get("installed"), "status": "current"}
        out.append(c)
    behind: dict[str, int] = {}
    for c in out:
        if c.get("release") and c.get("status") == "outdated":
            behind[c["release"]] = behind.get(c["release"], 0) + 1
    out = [{**c, "images_outdated": behind.get(c["id"], 0)} if c.get("images") else c for c in out]
    return {**result, "components": out}


class Scanner:
    def __init__(self, k8s, config: dict, data_dir: Optional[Path] = None):
        self.k8s = k8s
        self.cfg = config
        self.reg = upstream.Registry(config.get("registries"))
        self.labels_file = Path(data_dir) / "image-labels.json" if data_dir else None
        self.label_digests: set[str] = set()
        self.kube_version = ""  # the control plane's, set by inventory()
        self.chart_links: dict[str, tuple] = {}  # Helm row id -> its chart's source and home links
        self.argo: Optional[dict] = None  # what the Argo CD lookup found, for the result; None when off
        # Argo CD Application (as its tracking id names it) -> {"rows": [(row id, release, chart)],
        # "renderers": n}, and the same entries by the (kind, namespace, name) of each object it lists.
        self.argo_apps: dict[str, dict] = {}
        self.argo_resources: dict[tuple[str, str, str], dict] = {}
        self.argo_ids: set[str] = set()  # ids of the Argo CD release rows

    # ------------------------------------------------------------------ inventory
    def inventory(self) -> list[dict]:
        comps: list[dict] = []
        comps += self._cluster()
        comps += self._nodes()
        comps += self._releases()
        comps += self._workloads()
        # A helm-managed container is upgraded with its release, so it hangs off the release's row.
        releases = {c["id"] for c in comps if c["category"] == "helm"}
        for c in comps:
            if c.get("release") not in (None, *releases):
                del c["release"]  # release not deployed (failed, uninstalling): keep the row standalone
        return comps

    def _cluster(self) -> list[dict]:
        v = self.k8s.version()
        self.kube_version = v.get("gitVersion", "")
        return [{
            "id": "cluster/kubernetes", "category": "cluster", "install": "kubeadm",
            "name": "Kubernetes control plane", "namespace": "-",
            "installed": v.get("gitVersion", ""), "image": "", "track": "minor",
            "source": {"type": "k8s"}, "ref": "https://kubernetes.io/releases/",
            "note": f"platform {v.get('platform', '')}",
        }]

    def _nodes(self) -> list[dict]:
        out = []
        for n in self.k8s.nodes():
            info = n["status"]["nodeInfo"]
            name = n["metadata"]["name"]
            roles = [k.split("/", 1)[1] for k in n["metadata"].get("labels", {}) if k.startswith("node-role.kubernetes.io/")]
            cri = info.get("containerRuntimeVersion", "")
            cri_name, _, cri_ver = cri.partition("://")
            out.append({
                "id": f"node/{name}/kubelet", "category": "node", "install": "kubeadm",
                "name": f"{name} · kubelet", "namespace": "-", "installed": info.get("kubeletVersion", ""),
                "image": "", "track": "minor", "source": {"type": "k8s"},
                "ref": "https://kubernetes.io/releases/",
                "note": ", ".join(roles) or "worker",
            })
            cri_src = _lookup(self.cfg.get("runtimes", {}), cri_name) or {"type": "github", "repo": "containerd/containerd"}
            out.append({
                "id": f"node/{name}/{cri_name}", "category": "node", "install": "host package",
                "name": f"{name} · {cri_name}", "namespace": "-", "installed": cri_ver, "image": "",
                "track": cri_src.get("track", "any"), "source": cri_src,
                "ref": cri_src.get("ref") or f"https://github.com/{cri_src.get('repo', '')}/releases",
                "note": f"{info.get('osImage', '')} · kernel {info.get('kernelVersion', '')}",
            })
        return out

    def _helm(self) -> list[dict]:
        out = []
        overrides = self.cfg.get("helm", {})
        for r in self.k8s.helm_releases():
            if r["status"] not in ("deployed", "superseded"):
                continue
            ov_key, ov = _find(overrides, f"{r['name']}@{r['namespace']}", r["name"], r["chart"])
            src = ov.get("source") or {"type": "artifacthub-search", "chart": r["chart"]}
            rid = f"helm/{r['namespace']}/{r['name']}"
            links = (*r["sources"], r["home"])
            self.chart_links[rid] = links
            # Release notes: the configured link, else the GitHub repo versions come from, else the
            # chart's own source repo, else its home page.
            ref = (ov.get("ref") or (src.get("type") == "github" and src.get("repo") and f"https://github.com/{src['repo']}/releases")
                   or upstream.chart_release_page(links) or r["home"] or (r["sources"][0] if r["sources"] else ""))
            out.append({**({"config_key": ov_key} if ov_key else {}),
                "id": rid, "category": "helm", "install": "helm",
                "name": r["name"], "namespace": r["namespace"],
                "installed": r["chart_version"], "installed_app": r["app_version"],
                "image": f"chart {r['chart']}", "track": ov.get("track", "any"),
                "source": src, "ref": ref,
                "note": f"rev {r['revision']} · {r['updated'][:10]}", "updated": r["updated"],
            })
        return out

    def _releases(self) -> list[dict]:
        """Helm releases from release Secrets, plus the Helm sources of Argo CD Applications when
        `argocd.enabled`. An Argo CD release replaces a Secret one of the same name and namespace:
        after a move from `helm install` to Argo CD the Secret stays, stuck at the version Helm
        last installed."""
        helm = self._helm()
        argo = self._argocd(helm)
        if not argo:
            return helm
        stale = {(c["namespace"], c["name"]): c for c in helm}
        for c in argo:
            old = stale.pop((c["namespace"], c["name"]), None)
            if old:
                c["note"] += f" · replaces Helm release Secret ({old['installed']}, {old['note']})"
                c["argocd"]["replaced_secret_version"] = old["installed"]
                if old["image"] == c["image"]:  # same chart: its home and source links still apply
                    self.chart_links[c["id"]] += self.chart_links.get(old["id"], ())
        return list(stale.values()) + argo

    def _argocd(self, helm: list[dict]) -> list[dict]:
        """A Helm release row per Helm chart source of the Argo CD Applications in
        `argocd.namespaces` that deploy to this cluster. Nothing is listed unless `argocd.enabled`.
        `helm` (the release Secret rows) places a source whose Application sets no namespace."""
        acfg = self.cfg.get("argocd") or {}
        if acfg.get("enabled") is not True:
            return []
        namespaces = acfg.get("namespaces") or ["argocd"]
        ui = str(acfg.get("url") or "").rstrip("/")   # argocd.url: link each row to its Application
        apps, errors = [], []
        for ns in namespaces:
            try:
                apps += self.k8s.argo_applications(ns)
            except KubeError as e:  # no CRD, or no RBAC: the Secret-based releases still count
                log.warning("Argo CD Applications in %s: %s", ns, e)
                errors.append(f"{ns}: {e}")
        apps = [a for a in apps if argocd.local(a)]
        # the git repositories each destination namespace is synced from (Renovate: whose update is a row's)
        ns_repos: dict[str, set] = {}
        for a in apps:
            dest = ((a.get("spec") or {}).get("destination") or {}).get("namespace") or ""
            spec = a.get("spec") or {}
            for s in (spec.get("sources") or [spec.get("source") or {}]):
                if s and s.get("repoURL") and not s.get("chart") and dest:
                    ns_repos.setdefault(dest, set()).add(s["repoURL"])
        self.argo = {"namespaces": namespaces, "applications": len(apps), **({"errors": errors} if errors else {}),
                     **({"namespace_repos": {k: sorted(v) for k, v in sorted(ns_repos.items())}} if ns_repos else {})}
        overrides = self.cfg.get("helm", {})
        secret_ns: dict[str, set[str]] = {}
        for c in helm:
            secret_ns.setdefault(c["name"], set()).add(c["namespace"])
        out, ids = [], set()
        for app in apps:
            meta = app["metadata"]
            entry = {"rows": [], "renderers": argocd.renderers(app)}
            # Argo CD names an Application by its bare name in its own namespace (the first listed
            # that has one), and as <namespace>_<name> in any other.
            self.argo_apps.setdefault(meta["name"], entry)
            self.argo_apps[f"{meta['namespace']}_{meta['name']}"] = entry
            for r in (app.get("status") or {}).get("resources") or []:
                self.argo_resources.setdefault((r.get("kind", ""), r.get("namespace") or "", r.get("name", "")), entry)
            for s in argocd.helm_sources(app):
                ns, name, chart = s["namespace"], s["release"], s["chart"]
                assumed = not ns
                if assumed:  # neither helm.namespace nor a destination namespace: where Helm had it, if unique
                    found = secret_ns.get(name, set())
                    ns = next(iter(found)) if len(found) == 1 else "default"
                rid = f"helm/{ns}/{name}"
                if rid in ids:  # two charts rendered under one release name
                    rid = f"{rid}/{chart}"
                    if rid in ids:
                        log.warning("Argo CD %s: chart %s is deployed twice as %s/%s; listing it once", s["app"], chart, ns, name)
                        continue
                ids.add(rid)
                ov_key, ov = _find(overrides, f"{name}@{ns}", name, chart)
                if argocd.is_oci(s["repo"]):
                    default = {"type": "oci", "ref": argocd.oci_ref(s["repo"], chart)}
                    host, repo, _, _ = upstream.split_image(default["ref"] + ":x")
                    page = upstream.reference_url(host, repo)
                else:
                    default = {"type": "artifacthub-search", "chart": chart, "repo_url": s["repo"]}
                    page = ""
                src = ov.get("source") or default
                self.chart_links[rid] = (s["repo"],)
                ref = (ov.get("ref") or (src.get("type") == "github" and src.get("repo") and f"https://github.com/{src['repo']}/releases")
                       or page)
                if not s["deployed"]:
                    state = f" · not synced yet, target {s['target'] or '?'}"
                elif s["sync_status"] != "Synced":
                    state = f" · {s['sync_status'] or 'sync status unknown'}, target {s['target'] or '?'}"
                else:
                    state = ""
                out.append({**({"config_key": ov_key} if ov_key else {}),
                    "id": rid, "category": "helm", "install": "argocd",
                    "name": name, "namespace": ns,
                    "installed": s["version"], "installed_app": "",
                    "image": f"chart {chart}", "track": ov.get("track", "any"),
                    "source": src, "ref": ref,
                    "note": f"Argo CD {s['app']}{state}" + (f" · {s['deployed_at'][:10]}" if s["deployed_at"] else "")
                            + (f" · the Application sets no namespace, assumed {ns}" if assumed else ""),
                    **({"updated": s["deployed_at"]} if s["deployed_at"] else {}),
                    "argocd": {"application": s["app"], **({"source_index": s["index"]} if s["index"] is not None else {}),
                               **({"url": f"{ui}/applications/{s['app']}"} if ui else {}),
                               "repo": s["repo"], "target_revision": s["target"], "sync_status": s["sync_status"],
                               "deployed": s["deployed"], **({"namespace_assumed": True} if assumed else {})},
                })
                entry["rows"].append((rid, name, chart))
                self.argo_ids.add(rid)
        return out

    def _argo_release(self, w: dict) -> Optional[tuple[str, str, str]]:
        """(release, release row id, "argocd") for a workload an Argo CD Helm source rendered: its
        tracking-id annotation, else the Application's resource list, names the Application. When
        the Application renders more than that one chart (more charts, or git manifests too), the
        labels Helm charts set must say which: helm.sh/chart, else app.kubernetes.io/instance; with
        a single chart, app.kubernetes.io/managed-by: Helm is enough."""
        md = w["metadata"]
        key = argocd.tracking_app(w)
        entry = (key and self.argo_apps.get(key)) or self.argo_resources.get((w["kind"], md["namespace"], md["name"]))
        if not entry or not entry["rows"]:
            return None  # not Argo CD's, or a git source's: stays a plain manifest
        rows = entry["rows"]
        labels = md.get("labels") or {}
        by_chart = [r for r in rows if argocd.chart_label_matches(labels.get("helm.sh/chart", ""), r[2])]
        by_instance = [r for r in (by_chart or rows) if labels.get("app.kubernetes.io/instance") == r[1]]
        if len(rows) == 1 and (entry["renderers"] == 1 or labels.get("app.kubernetes.io/managed-by") == "Helm"):
            pick = rows[0]
        elif len(by_chart) == 1:
            pick = by_chart[0]
        elif len(by_instance) == 1:
            pick = by_instance[0]
        else:
            return None
        return pick[1], pick[0], "argocd"

    def _workloads(self) -> list[dict]:
        out: list[dict] = []
        ignore = self.cfg.get("ignore", [])
        groups = self.cfg.get("groups", [])
        images_cfg = self.cfg.get("images", {})
        grouped: dict[str, dict] = {}

        # Running image digests from pods, keyed by normalized image ref -> set(digest). The
        # runtime reports Docker Hub images fully qualified ("docker.io/x/y:latest") while the
        # workload spec may say "x/y" or "x/y:latest", so both sides go through _image_key.
        # The runtime may also report another name it holds for the same image ("x:2.37.1" for a
        # pod that asked for "x:latest", or an old registry's name), so each digest is keyed by the
        # image the pod spec asked for as well as the one the runtime reported.
        digests: dict[str, set[str]] = {}
        for p in self.k8s.pods():
            spec = p.get("spec", {})
            asked = {c.get("name"): c.get("image", "")
                     for c in (spec.get("containers") or []) + (spec.get("initContainers") or [])}
            for cs in p.get("status", {}).get("containerStatuses", []) or []:
                iid = cs.get("imageID", "")
                if "@" not in iid:
                    continue
                for ref in {asked.get(cs.get("name")), cs.get("image")}:
                    if ref:
                        digests.setdefault(_image_key(ref), set()).add(iid.split("@", 1)[1])

        def add_image(ns: str, name: str, kind: str, image: str, owner: Optional[tuple[str, str, str]] = None,
                      note: str = ""):
            host, repo, tag, pinned_digest = upstream.split_image(image)
            key = f"{host}/{repo}" if host != "docker.io" else repo.removeprefix("library/")
            icfg_key, icfg = _find(images_cfg, f"{host}/{repo}", key, repo)
            # your own registry: one the config names, or one kube-drift has credentials for
            own_registry = host not in upstream.PUBLIC_REGISTRIES and (
                host in self.cfg.get("registries", {}) or self.reg.has_credentials(host))
            source = icfg.get("source") or {"type": "registry"}
            ref = icfg.get("ref") or (source.get("type") == "github" and source.get("repo") and f"https://github.com/{source['repo']}/releases") \
                or upstream.reference_url(host, repo)
            comp = {
                "id": f"workload/{ns}/{name}/{repo.split('/')[-1]}",
                "category": "helm-workload" if owner else "manifest",
                "install": f"{owner[2]} ({owner[0]})" if owner else "manifest",
                "name": icfg.get("name") or name, "namespace": ns, "kind": kind,
                "image": image, "image_host": host, "image_repo": repo, "tag": tag,
                "installed": tag, "pinned_digest": pinned_digest,
                "running_digests": sorted(digests.get(_image_key(image), [])),
                "floating": is_floating(tag), "track": icfg.get("track", "any"),
                "loose_suffix": icfg.get("loose_suffix", own_registry),
                "source": source, "ref": ref, "ref_overridden": bool(icfg.get("ref")),
                "note": icfg.get("note") or note, "first_party": own_registry,
            }
            if icfg_key:
                comp["config_key"] = icfg_key
            if owner:
                comp["release"] = owner[1]
            return comp

        def owner_of(w: dict) -> Optional[tuple[str, str, str]]:
            """(release, release row id, installer) of a workload a Helm chart put there. Argo CD's
            word comes first: a workload Helm installed before the move keeps Helm's annotations."""
            if self.argo_apps and (argo := self._argo_release(w)):
                return argo
            md = w["metadata"]
            ann = md.get("annotations", {}) or {}
            rel = ann.get("meta.helm.sh/release-name")
            if not rel:
                return None
            rid = f"helm/{ann.get('meta.helm.sh/release-namespace') or md['namespace']}/{rel}"
            # Helm's annotations outlive a move to Argo CD; the release kept its name, so its id too
            return rel, rid, "argocd" if rid in self.argo_ids else "helm"

        workloads = list(self.k8s.workloads())
        # An operator a chart installs (longhorn-manager) creates more workloads at runtime and labels
        # them `<prefix>/managed-by: <operator>`; they are upgraded with that chart's release too.
        helm_by_name = {}
        for w in workloads:
            if o := owner_of(w):
                helm_by_name[(w["metadata"]["namespace"], w["metadata"]["name"])] = o

        for w in workloads:
            ns, name, kind = w["metadata"]["namespace"], w["metadata"]["name"], w["kind"]
            if any(_match(r, ns, name) for r in ignore):
                continue
            owner = helm_by_name.get((ns, name))
            operator = None
            if not owner:
                labels = w["metadata"].get("labels", {}) or {}
                operator = next((v for k, v in labels.items() if (k == "managed-by" or k.endswith("/managed-by"))
                                 and (ns, v) in helm_by_name and v != name), None)
                if operator:
                    owner = helm_by_name[(ns, operator)]
            containers = w["spec"]["template"]["spec"].get("containers", [])
            grp = next((g for g in groups if _match(g, ns, name)), None)
            if grp:
                g = grouped.setdefault(grp["label"], {"members": set(), "images": {}, "owners": set()})
                g["members"].add(f"{ns}/{name}")
                g["owners"].add((owner, operator))
                for c in containers:
                    g["images"].setdefault(c["image"], (ns, name, kind, c["name"]))
                continue
            for c in containers:
                if any(re.search(p, c["image"]) for p in self.cfg.get("ignore_images", [])):
                    continue
                note = c["name"] if len(containers) > 1 else ""
                comp = add_image(ns, name, kind, c["image"], owner, note)
                if operator:
                    comp["install"] = f"{operator} ({owner[2]} {owner[0]})"
                out.append(comp)

        for label, g in grouped.items():
            # The group keeps its members' owner when they agree: Envoy Gateway's data plane is the `eg`
            # chart's, and listing it apart made one chart bump look like two unrelated updates. A group
            # that spans releases (gateways of every namespace) has no single owner and stays on its own.
            owner, operator = next(iter(g["owners"])) if len(g["owners"]) == 1 else (None, None)
            for image, (ns, name, kind, cname) in g["images"].items():
                comp = add_image(ns, label, kind, image, owner, f"{len(g['members'])} workloads · e.g. {name}")
                comp["id"] = f"group/{re.sub(r'[^a-z0-9]+', '-', label.lower())}/{comp['image_repo'].split('/')[-1]}"
                if operator:
                    comp["install"] = f"{operator} ({owner[2]} {owner[0]})"
                elif not owner:
                    comp["install"] = "managed"
                out.append(comp)

        # Static pods (kubeadm control plane, kube-vip) — owned by a Node, not a controller.
        seen: set[str] = set()
        for ns in self.cfg.get("static_pod_namespaces", ["kube-system"]):
            for p in self.k8s.pods(ns):
                owners = p["metadata"].get("ownerReferences") or []
                if not owners or owners[0].get("kind") != "Node":
                    continue
                base = re.sub(r"-[^-]+$", "", p["metadata"]["name"])  # strip node suffix
                if base in seen:
                    continue
                seen.add(base)
                for c in p["spec"]["containers"]:
                    comp = add_image(ns, base, "StaticPod", c["image"], None)
                    comp["install"] = "static pod"
                    comp["category"] = "cluster"
                    if "registry.k8s.io/kube-" in c["image"] or "/etcd" in c["image"]:
                        comp["track"] = "minor"
                    out.append(comp)
        return out

    # ------------------------------------------------------------------ resolve
    def resolve(self, comp: dict) -> dict:
        src = comp.get("source") or {}
        t = src.get("type", "registry")
        try:
            if t == "k8s":
                self._resolve_k8s(comp)
            elif t == "artifacthub":
                self._resolve_artifacthub(comp, src["repo"], src.get("chart") or comp["image"].removeprefix("chart "))
            elif t == "artifacthub-search":
                chart = src.get("chart") or comp["image"].removeprefix("chart ")
                r = upstream.artifacthub_search(chart, self.chart_links.get(comp["id"], ()), comp.get("installed") or "",
                                                *([src["repo_url"]] if src.get("repo_url") else []))
                if not r:
                    comp.update(status="unknown", latest=None, note=(comp.get("note", "") + " · chart not on ArtifactHub").strip(" ·"))
                else:
                    self._apply_chart(comp, r)
                    self._installed_app(comp, chart, ah_repo=r.get("repo"))
                    if r.get("ambiguous"):
                        comp["note"] = (comp.get("note", "") + f" · AH guess: {r.get('repo')}").strip(" ·")
            elif t == "github":
                r = upstream.github_latest(src["repo"], src.get("version_regex"), src.get("prefer_tags", False))
                comp.setdefault("ref", r["ref"] if r else comp.get("ref"))
                self._apply_version(comp, r["version"] if r else None, r["ref"] if r else None)
            elif t == "oci":  # Helm chart stored in an OCI registry: compare chart tags
                host, repo, _, _ = upstream.split_image(src["ref"] + ":x")
                tags = self.reg.tags(host, repo)
                res = pick_latest(comp["installed"], tags, comp.get("track", "any"))
                comp.update(latest=res["latest"], latest_any=res["latest_any"],
                            status="outdated" if res["outdated"] else ("current" if res["outdated"] is False else "unknown"))
                if comp["status"] == "outdated":
                    self._oci_chart_range(comp, host, repo, res["latest"])
                self._installed_app(comp, repo, oci=(host, repo))
            elif t == "registry":
                self._resolve_registry(comp)
            elif t == "none":
                comp.update(status="unknown", latest=None)
            else:
                comp.update(status="unknown", latest=None, note=f"unknown source type {t}")
        except HttpError as e:
            comp.update(status="error", latest=None, error=f"HTTP {e.status} {e.url.split('?')[0]}")
        except Exception as e:  # noqa: BLE001
            comp.update(status="error", latest=None, error=f"{type(e).__name__}: {e}"[:200])
        comp["checked_at"] = now_iso()
        return comp

    def _apply_version(self, comp: dict, latest: Optional[str], ref: Optional[str] = None):
        if ref and not comp.get("ref"):
            comp["ref"] = ref
        comp["latest"] = latest
        installed = comp.get("running_version") or comp["installed"]
        if not latest or not installed or is_floating(installed):
            comp["status"] = "unknown"
        else:
            comp["status"] = "outdated" if cmp_versions(installed, latest) < 0 else "current"

    def _apply_chart(self, comp: dict, r: dict):
        comp["latest"] = r.get("version")
        comp["latest_app"] = r.get("app_version")
        if r.get("ref") and (not comp.get("ref") or "artifacthub" not in comp["ref"]):
            comp["ref_ah"] = r["ref"]
            comp.setdefault("ref", r["ref"])
        comp["status"] = ("outdated" if cmp_versions(comp["installed"], r["version"]) < 0 else "current") if r.get("version") else "unknown"
        if comp["status"] == "outdated":
            self._kube_range(comp, r["version"], r.get("kube_version"))

    def _kube_range(self, comp: dict, version: str, need: Optional[str]):
        """Mark a chart update whose kubeVersion range excludes the cluster as incompatible."""
        if need and satisfies(self.kube_version, need) is False:
            comp["incompatible"] = f"chart {version} needs Kubernetes {need}; the cluster runs {self.kube_version}"

    def _oci_chart_range(self, comp: dict, host: str, repo: str, tag: str):
        try:
            digest = self.reg.digest(host, repo, tag)  # a HEAD: free against Docker Hub's pull limit
        except HttpError as e:
            log.info("chart %s/%s:%s: HTTP %s", host, repo, tag, e.status)
            return
        if digest:
            self.label_digests.add(digest)  # keeps its metadata in the saved digest cache
            self._kube_range(comp, tag, self.reg.chart_meta(host, repo, digest).get("kube_version"))

    def _resolve_artifacthub(self, comp: dict, repo: str, chart: str):
        r = upstream.artifacthub_package(repo, chart)
        if not r:
            comp.update(status="unknown", latest=None, error=f"ArtifactHub: {repo}/{chart} not found")
        else:
            self._apply_chart(comp, r)
            self._installed_app(comp, chart, ah_repo=repo)

    def _installed_app(self, comp: dict, chart: str, ah_repo: Optional[str] = None, oci: Optional[tuple[str, str]] = None):
        """An Argo CD release's appVersion. No release Secret records it, so it comes from where the
        chart's versions do: ArtifactHub's page for the installed version, or the installed OCI
        chart's own metadata. Left empty when neither says."""
        if comp.get("install") != "argocd" or comp.get("installed_app") or not comp.get("installed"):
            return
        if comp.get("latest") == comp["installed"] and comp.get("latest_app"):
            comp["installed_app"] = comp["latest_app"]
            return
        app = None
        try:
            if ah_repo:
                app = (upstream.artifacthub_package(ah_repo, chart, comp["installed"]) or {}).get("app_version")
            elif oci:
                digest = self.reg.digest(*oci, comp["installed"])
                if digest:
                    self.label_digests.add(digest)
                    app = self.reg.chart_meta(*oci, digest).get("app_version")
        except Exception as e:  # noqa: BLE001 — a missing appVersion must never fail the row
            log.info("appVersion of %s %s: %s", comp["image"], comp["installed"], e)
        if app:
            comp["installed_app"] = app

    def _project_ref(self, comp: dict, digest: Optional[str]):
        """Where to read an image's release notes when the config doesn't say: the link its own
        labels give (see label_link), else a repo its Helm chart lists and that is named like it,
        else a GitHub repo named like it (see github_guess), else the registry's page, as set."""
        host, repo = comp["image_host"], comp["image_repo"]
        link = None
        if digest:
            self.label_digests.add(digest)
            try:
                link = upstream.label_link(self.reg.labels(host, repo, digest), repo)
            except Exception as e:  # noqa: BLE001 — a missing link must never fail the row
                log.info("labels for %s: %s", comp["image"], e)
        if not link and comp.get("release"):
            link = upstream.chart_source_link(self.chart_links.get(comp["release"], ()), repo)
        if not link:
            try:
                link = upstream.github_guess(host, repo)
            except HttpError as e:
                log.info("GitHub repo for %s: HTTP %s", comp["image"], e.status)
        if link:
            comp["ref"] = link

    def _resolve_k8s(self, comp: dict):
        installed = comp["installed"]
        m = re.match(r"v?(\d+\.\d+)", installed)
        latest_minor = upstream.k8s_stable(m.group(1)) if m else None
        latest_any = upstream.k8s_stable()
        comp["latest"] = latest_minor
        comp["latest_any"] = latest_any
        if latest_minor:
            comp["status"] = "outdated" if cmp_versions(installed, latest_minor) < 0 else "current"
        else:
            comp["status"] = "unknown"

    def _tag_exists(self, host: str, repo: str, tag: str) -> bool:
        try:
            return self.reg.digest(host, repo, tag) is not None
        except HttpError as e:
            if e.status == 404:
                return False
            raise

    def _resolve_registry(self, comp: dict):
        host, repo = comp["image_host"], comp["image_repo"]
        tag = comp["tag"]
        # 1) running digest vs the tag's current digest in the registry (works for floating tags too)
        try:
            remote = self.reg.digest(host, repo, tag)
        except HttpError as e:
            remote = None
            comp["digest_error"] = f"HTTP {e.status}"
        comp["remote_digest"] = remote
        pinned = comp.get("pinned_digest")
        running = comp.get("running_digests") or ([pinned] if pinned else [])
        if not comp.get("ref_overridden"):
            self._project_ref(comp, pinned or (running[0] if running else remote))
        # A manifest that pins a digest is judged on the pin, never on what the kubelet reports.
        # The two are often different digests for the same image: containerd keeps more than one
        # index digest per image and reports whichever its store resolves to, so a pinned row read
        # as "running image is stale" for ever. Whether a pod has caught up with the pin is a
        # rollout question, and the GitOps tool already answers that one.
        if remote and pinned:
            comp["digest_current"] = pinned == remote
        elif remote and running:
            comp["digest_current"] = remote in running
        # 2) newest tag of the same shape (pinned tags only)
        if not comp["floating"]:
            tags = self.reg.tags(host, repo)
            res = pick_latest(tag, tags, comp.get("track", "any"), comp.get("loose_suffix", False))
            if res["outdated"] is None and remote and not comp.get("loose_suffix"):
                # The listing may have been cut short before any tag of this shape: ask for the next ones.
                try:
                    res = walk_up(tag, lambda t: self._tag_exists(host, repo, t), comp.get("track", "any"))
                except HttpError as e:
                    log.info("walking up from %s: %s", comp["image"], e)
            comp["latest"], comp["latest_any"] = res["latest"], res["latest_any"]
            if res["outdated"] is None:
                comp["status"] = "unknown"
            else:
                comp["status"] = "outdated" if res["outdated"] else "current"
                if comp["status"] == "current" and comp.get("digest_current") is False:
                    comp["status"] = "outdated"
                    why = "pinned digest is behind this tag" if pinned else "tag re-pushed, running image is stale"
                    comp["note"] = (comp.get("note", "") + " · " + why).strip(" ·")
        else:
            comp["latest"] = None
            if comp.get("digest_current") is True:
                comp["status"] = "current"
            elif comp.get("digest_current") is False:
                comp["status"] = "outdated"
                why = f"pinned digest is behind '{tag}'" if pinned else f"newer image behind '{tag}'"
                comp["note"] = (comp.get("note", "") + " · " + why).strip(" ·")
            else:
                comp["status"] = "unknown"
            # running version from a probe + newest semver tag gives a real number to show
            if comp.get("running_version"):
                probe_src = comp.get("probe_source")
                if probe_src and probe_src.get("type") == "github":
                    r = upstream.github_latest(probe_src["repo"], probe_src.get("version_regex"))
                    latest = r["version"] if r else None
                    if r and not comp.get("ref_overridden"):
                        comp["ref"] = r["ref"]
                else:
                    tags = self.reg.tags(host, repo)
                    res = pick_latest(comp["running_version"], tags, "any", True)
                    latest = res["latest"]
                comp["latest"] = latest
                if latest:
                    comp["status"] = "outdated" if cmp_versions(comp["running_version"], latest) < 0 else "current"

    # ------------------------------------------------------------------- probes
    def probe(self, comps: list[dict]):
        """Ask floating-tag apps for their real running version over HTTP."""
        by_key = {(c["namespace"], c["name"]): c for c in comps if c["category"] in ("manifest", "helm-workload")}
        for p in self.cfg.get("probes", []):
            comp = by_key.get((p["namespace"], p["name"]))
            if not comp:
                continue
            try:
                hdrs = p.get("headers") or {}
                data = get_json(p["url"], headers=hdrs, timeout=10, insecure=True)
                val = _jsonpath(data, p["json"]) if p.get("json") else None
                if p.get("regex") and val:
                    m = re.search(p["regex"], str(val))
                    val = m.group(1) if m and m.groups() else (m.group(0) if m else val)
                if val:
                    comp["running_version"] = str(val).lstrip("v")
                    comp["probe_source"] = p.get("source")
                    if p.get("ref"):
                        comp["ref"], comp["ref_overridden"] = p["ref"], True
            except Exception as e:  # noqa: BLE001
                comp["probe_error"] = f"{type(e).__name__}: {e}"[:160]

    # --------------------------------------------------------------------- run
    def run(self, workers: int = 8) -> dict:
        t0 = time.time()
        upstream_clear_caches(self.reg)
        if self.labels_file:
            upstream.load_labels(self.labels_file)
        comps = self.inventory()
        self.probe(comps)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            comps = list(ex.map(self.resolve, comps))
        if self.labels_file:
            upstream.save_labels(self.labels_file, self.label_digests)
        comps.sort(key=lambda c: (STATUS_ORDER.get(c.get("status"), 1), c["category"], c["namespace"], c["name"]))
        by_id = {c["id"]: c for c in comps}
        for c in comps:
            if c.get("release"):
                rel = by_id.get(c["release"])
                if rel is None:          # a group whose owning release is not itself a row
                    continue
                rel["images"] = rel.get("images", 0) + 1
                rel["images_outdated"] = rel.get("images_outdated", 0) + (c.get("status") == "outdated")
        # Count what you'd act on: a release once, not once per container it ships.
        top = [c for c in comps if not c.get("release")]
        summary = {"total": len(top)}
        for c in top:
            summary[c.get("status", "unknown")] = summary.get(c.get("status", "unknown"), 0) + 1
        return {
            "scanned_at": now_iso(), "duration_s": round(time.time() - t0, 1),
            "summary": summary, "components": comps,
            **({"argocd": self.argo} if self.argo is not None else {}),
        }


def kube_version(components: list[dict]) -> str:
    return next((c.get("installed") or "" for c in components if c["id"] == "cluster/kubernetes"), "")


def ignore_ended(ig: dict, comp: dict, kube: str, now: datetime) -> Optional[str]:
    """Why an ignored update shows again, or None while it stays ignored. The first condition met
    ends it: the date passed, the cluster's Kubernetes version changed, or something newer than the
    ignored update is offered (for a floating tag: its image changed again)."""
    if ig.get("until") and now >= datetime.fromisoformat(ig["until"]):
        return "expired"
    if ig.get("kube_version") and kube and kube != ig["kube_version"]:
        return f"Kubernetes changed from {ig['kube_version']} to {kube}"
    if ig.get("until_newer"):
        for k in ("latest", "latest_any"):  # each against what it was, so an update outside the track doesn't end it at once
            if ig.get(k) and comp.get(k) and cmp_versions(comp[k], ig[k]) > 0:
                return f"{comp[k]} is available"
        if ig.get("digest") and comp.get("remote_digest") and comp["remote_digest"] != ig["digest"]:
            return "a newer image is available"
    return None


def apply_ignores(result: dict, ignores: dict, now: Optional[datetime] = None) -> tuple[dict, set[str]]:
    """The scan with ignored updates marked (`ignored` on the component) and moved out of the
    summary's status counts into `ignored`. Ignoring a Helm release ignores the images it ships
    too (`ignored.via` names the release). Also returns the ids whose ignore has ended."""
    now = now or datetime.now(timezone.utc)
    comps = result.get("components") or []
    kube = kube_version(comps)
    ended: set[str] = set()
    active = {}
    for c in comps:
        ig = ignores.get(c["id"])
        if ig and ignore_ended(ig, c, kube, now):
            ended.add(c["id"])
        elif ig:
            active[c["id"]] = ig
    out, summary = [], dict(result.get("summary") or {})
    for c in comps:
        ig = active.get(c["id"])
        if not ig and c.get("release") in active:
            ig = {**active[c["release"]], "via": c["release"]}
        if ig:
            c = {**c, "ignored": ig}
            if not c.get("release"):
                st = c.get("status", "unknown")
                summary[st] = summary.get(st, 0) - 1
                if not summary[st]:
                    del summary[st]
                summary["ignored"] = summary.get("ignored", 0) + 1
                summary["total"] = summary.get("total", 0) - 1
        out.append(c)
    return {**result, "components": out, "summary": summary}, ended


def upstream_clear_caches(reg: upstream.Registry):
    """Caches live for one scan so "latest" never goes stale across scans."""
    for fn in (upstream.artifacthub_package, upstream.artifacthub_search, upstream.github_latest,
               upstream.k8s_stable, upstream.Registry.tags, upstream.Registry.digest):
        fn.cache_clear()
