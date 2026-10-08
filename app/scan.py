"""Build the component inventory from the cluster and resolve upstream versions."""
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import upstream
from .http import HttpError, get_json
from .versions import cmp_versions, is_floating, pick_latest

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


def _lookup(table: dict, *keys: str) -> dict:
    """Exact key match first; then any `re:<pattern>` key whose regex matches a key."""
    for k in keys:
        if k in table:
            return table[k]
    for pat, val in table.items():
        if pat.startswith("re:") and any(re.search(pat[3:], k) for k in keys):
            return val
    return {}


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


class Scanner:
    def __init__(self, k8s, config: dict, data_dir: Optional[Path] = None):
        self.k8s = k8s
        self.cfg = config
        self.reg = upstream.Registry(config.get("registries"))
        self.labels_file = Path(data_dir) / "image-labels.json" if data_dir else None
        self.label_digests: set[str] = set()

    # ------------------------------------------------------------------ inventory
    def inventory(self) -> list[dict]:
        comps: list[dict] = []
        comps += self._cluster()
        comps += self._nodes()
        comps += self._helm()
        comps += self._workloads()
        # A helm-managed container is upgraded with its release, so it hangs off the release's row.
        releases = {c["id"] for c in comps if c["category"] == "helm"}
        for c in comps:
            if c.get("release") not in (None, *releases):
                del c["release"]  # release not deployed (failed, uninstalling): keep the row standalone
        return comps

    def _cluster(self) -> list[dict]:
        v = self.k8s.version()
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
            ov = _lookup(overrides, f"{r['name']}@{r['namespace']}", r["name"], r["chart"])
            src = ov.get("source") or {"type": "artifacthub-search", "chart": r["chart"]}
            out.append({
                "id": f"helm/{r['namespace']}/{r['name']}", "category": "helm", "install": "helm",
                "name": r["name"], "namespace": r["namespace"],
                "installed": r["chart_version"], "installed_app": r["app_version"],
                "image": f"chart {r['chart']}", "track": ov.get("track", "any"),
                "source": src, "ref": ov.get("ref") or r["home"] or (r["sources"][0] if r["sources"] else ""),
                "note": f"rev {r['revision']} · {r['updated'][:10]}", "updated": r["updated"],
            })
        return out

    def _workloads(self) -> list[dict]:
        out: list[dict] = []
        ignore = self.cfg.get("ignore", [])
        groups = self.cfg.get("groups", [])
        images_cfg = self.cfg.get("images", {})
        grouped: dict[str, dict] = {}

        # Running image digests from pods, keyed by image ref -> set(digest)
        digests: dict[str, set[str]] = {}
        for p in self.k8s.pods():
            for cs in p.get("status", {}).get("containerStatuses", []) or []:
                iid = cs.get("imageID", "")
                if "@" in iid:
                    digests.setdefault(cs.get("image", ""), set()).add(iid.split("@", 1)[1])

        def add_image(ns: str, name: str, kind: str, image: str, helm_release: Optional[str], note: str = "",
                      release_ns: Optional[str] = None):
            host, repo, tag, pinned_digest = upstream.split_image(image)
            key = f"{host}/{repo}" if host != "docker.io" else repo.removeprefix("library/")
            icfg = _lookup(images_cfg, f"{host}/{repo}", key, repo)
            comp = {
                "id": f"workload/{ns}/{name}/{repo.split('/')[-1]}",
                "category": "helm-workload" if helm_release else "manifest",
                "install": f"helm ({helm_release})" if helm_release else "manifest",
                "name": icfg.get("name") or name, "namespace": ns, "kind": kind,
                "image": image, "image_host": host, "image_repo": repo, "tag": tag,
                "installed": tag, "pinned_digest": pinned_digest,
                "running_digests": sorted(digests.get(image, [])),
                "floating": is_floating(tag), "track": icfg.get("track", "any"),
                "loose_suffix": icfg.get("loose_suffix", host in self.cfg.get("registries", {})),
                "source": icfg.get("source") or {"type": "registry"},
                "ref": icfg.get("ref") or upstream.reference_url(host, repo), "ref_overridden": bool(icfg.get("ref")),
                "note": icfg.get("note") or note, "first_party": host in self.cfg.get("registries", {}),
            }
            if helm_release:
                comp["release"] = f"helm/{release_ns or ns}/{helm_release}"
            return comp

        for w in self.k8s.workloads():
            ns, name, kind = w["metadata"]["namespace"], w["metadata"]["name"], w["kind"]
            if any(_match(r, ns, name) for r in ignore):
                continue
            ann = w["metadata"].get("annotations", {}) or {}
            helm_release = ann.get("meta.helm.sh/release-name")
            release_ns = ann.get("meta.helm.sh/release-namespace")
            containers = w["spec"]["template"]["spec"].get("containers", [])
            grp = next((g for g in groups if _match(g, ns, name)), None)
            if grp:
                g = grouped.setdefault(grp["label"], {"members": set(), "images": {}})
                g["members"].add(f"{ns}/{name}")
                for c in containers:
                    g["images"].setdefault(c["image"], (ns, name, kind, c["name"]))
                continue
            for c in containers:
                if any(re.search(p, c["image"]) for p in self.cfg.get("ignore_images", [])):
                    continue
                note = c["name"] if len(containers) > 1 else ""
                out.append(add_image(ns, name, kind, c["image"], helm_release, note, release_ns))

        for label, g in grouped.items():
            for image, (ns, name, kind, cname) in g["images"].items():
                comp = add_image(ns, label, kind, image, None, f"{len(g['members'])} workloads · e.g. {name}")
                comp["id"] = f"group/{re.sub(r'[^a-z0-9]+', '-', label.lower())}/{comp['image_repo'].split('/')[-1]}"
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
                r = upstream.artifacthub_search(src.get("chart") or comp["image"].removeprefix("chart "))
                if not r:
                    comp.update(status="unknown", latest=None, note=(comp.get("note", "") + " · chart not on ArtifactHub").strip(" ·"))
                else:
                    self._apply_chart(comp, r)
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

    def _resolve_artifacthub(self, comp: dict, repo: str, chart: str):
        r = upstream.artifacthub_package(repo, chart)
        if not r:
            comp.update(status="unknown", latest=None, error=f"ArtifactHub: {repo}/{chart} not found")
        else:
            self._apply_chart(comp, r)

    def _label_ref(self, comp: dict, digest: Optional[str]):
        """Prefer the link the image's own labels give over the registry's page (see label_link)."""
        if not digest:
            return
        self.label_digests.add(digest)
        try:
            link = upstream.label_link(self.reg.labels(comp["image_host"], comp["image_repo"], digest), comp["image_repo"])
        except Exception as e:  # noqa: BLE001 — a missing link must never fail the row
            log.info("labels for %s: %s", comp["image"], e)
            return
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
        running = comp.get("running_digests") or ([comp["pinned_digest"]] if comp.get("pinned_digest") else [])
        if not comp.get("ref_overridden"):
            self._label_ref(comp, (running or [remote])[0])
        if remote and running:
            comp["digest_current"] = remote in running
        # 2) newest tag of the same shape (pinned tags only)
        if not comp["floating"]:
            tags = self.reg.tags(host, repo)
            res = pick_latest(tag, tags, comp.get("track", "any"), comp.get("loose_suffix", False))
            comp["latest"], comp["latest_any"] = res["latest"], res["latest_any"]
            if res["outdated"] is None:
                comp["status"] = "unknown"
            else:
                comp["status"] = "outdated" if res["outdated"] else "current"
                if comp["status"] == "current" and comp.get("digest_current") is False:
                    comp["status"] = "outdated"
                    comp["note"] = (comp.get("note", "") + " · tag re-pushed, running image is stale").strip(" ·")
        else:
            comp["latest"] = None
            if comp.get("digest_current") is True:
                comp["status"] = "current"
            elif comp.get("digest_current") is False:
                comp["status"] = "outdated"
                comp["note"] = (comp.get("note", "") + f" · newer image behind '{tag}'").strip(" ·")
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
                rel = by_id[c["release"]]
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
        }


def upstream_clear_caches(reg: upstream.Registry):
    """Caches live for one scan so "latest" never goes stale across scans."""
    for fn in (upstream.artifacthub_package, upstream.artifacthub_search, upstream.github_latest,
               upstream.k8s_stable, upstream.Registry.tags, upstream.Registry.digest):
        fn.cache_clear()
