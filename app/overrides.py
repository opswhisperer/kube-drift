"""Image and Helm rules made in the dashboard, laid over the config file's `images:` and `helm:`.

They live on kube-drift's volume (store.py, overrides.json), like dashboard ignore rules. A
dashboard rule for a key the config file also has replaces that entry whole; remove it and the
config file's entry applies again. Everything else in the config is shown, not edited: sign-in,
probes (they make HTTP requests with headers) and registries stay in the file, in git.
"""
from __future__ import annotations

import re
import urllib.parse

import yaml

SECTIONS = ("images", "helm")
TRACKS = ("any", "major", "minor")
# Source types each section can set, and their fields. The default (registry tags for images, an
# ArtifactHub search for charts) is what you get with no `source`, so it isn't stored.
SOURCES = {
    "images": {"none": (), "github": ("repo", "version_regex", "prefer_tags")},
    "helm": {"none": (), "artifacthub": ("repo", "chart"), "oci": ("ref",), "github": ("repo", "version_regex", "prefer_tags")},
}
TEXT = {"images": ("name", "note"), "helm": ()}
GITHUB_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")
OCI_REF = re.compile(r"^[a-z0-9.-]+(:\d+)?(/[a-z0-9._-]+)+$")
NAME = re.compile(r"^[\w.-]+$")
SECRET_KEY = re.compile(r"secret|password|passwd|token|credential", re.I)
HIDDEN = "(hidden)"


def validate_key(section: str, key) -> str:
    if section not in SECTIONS:
        raise ValueError("section: images or helm")
    key = str(key or "").strip()
    if not key or len(key) > 300 or any(c.isspace() for c in key):
        raise ValueError("key: 1 to 300 characters, no spaces")
    if key.startswith("re:"):
        try:
            re.compile(key[3:])
        except re.error as e:
            raise ValueError(f"key: bad regex: {e}") from None
    return key


def validate_rule(section: str, rule) -> dict:
    """A clean copy of an images or helm entry; raises ValueError naming the bad field."""
    if not isinstance(rule, dict):
        raise ValueError("rule: an object")
    allowed = {"track", "source", "ref", *TEXT[section], *(("loose_suffix",) if section == "images" else ())}
    extra = set(rule) - allowed
    if extra:
        raise ValueError(f"rule: unknown field {', '.join(sorted(extra))} (allowed: {', '.join(sorted(allowed))})")
    out: dict = {}
    track = rule.get("track") or "any"
    if track not in TRACKS:
        raise ValueError("track: any, major or minor")
    if track != "any":
        out["track"] = track
    src = rule.get("source")
    if src:
        out["source"] = _source(section, src)
    if rule.get("ref"):
        ref = str(rule["ref"]).strip()
        u = urllib.parse.urlparse(ref)
        if u.scheme not in ("http", "https") or not u.netloc or len(ref) > 500:
            raise ValueError("ref: an http(s) URL")
        out["ref"] = ref
    for k in TEXT[section]:
        if rule.get(k):
            out[k] = str(rule[k]).strip()[:200]
    if rule.get("loose_suffix") is not None:
        if not isinstance(rule["loose_suffix"], bool):
            raise ValueError("loose_suffix: true or false; leave it out for the default")
        out["loose_suffix"] = rule["loose_suffix"]
    return out


def _source(section: str, src) -> dict:
    if not isinstance(src, dict) or src.get("type") not in SOURCES[section]:
        raise ValueError(f"source.type: one of {', '.join(SOURCES[section])}, or leave source out for the default")
    t = src["type"]
    extra = set(src) - {"type", *SOURCES[section][t]}
    if extra:
        raise ValueError(f"source: unknown field {', '.join(sorted(extra))} for type {t}")
    out = {"type": t}
    if t == "github":
        if not GITHUB_REPO.match(str(src.get("repo") or "")):
            raise ValueError("source.repo: owner/name of a GitHub repository")
        out["repo"] = src["repo"]
        if src.get("version_regex"):
            try:
                if re.compile(src["version_regex"]).groups < 1:
                    raise ValueError("source.version_regex: needs a group that captures the version")
            except re.error as e:
                raise ValueError(f"source.version_regex: bad regex: {e}") from None
            out["version_regex"] = src["version_regex"]
        if src.get("prefer_tags"):
            out["prefer_tags"] = True
    elif t == "artifacthub":
        for k in ("repo", "chart"):
            if src.get(k) and not NAME.match(str(src[k])):
                raise ValueError(f"source.{k}: letters, digits, . _ -")
        if not src.get("repo"):
            raise ValueError("source.repo: the ArtifactHub repository name")
        out.update({k: src[k] for k in ("repo", "chart") if src.get(k)})
    elif t == "oci":
        ref = str(src.get("ref") or "").strip().removeprefix("oci://")
        if not OCI_REF.match(ref):
            raise ValueError("source.ref: registry/path/to/chart, e.g. docker.io/envoyproxy/gateway-helm")
        out["ref"] = ref
    return out


def effective(cfg: dict, rules: dict) -> dict:
    """The config the scanner uses: dashboard rules replace the file's entry for the same key (in
    place, so `re:` keys keep their order), and new keys come after the file's."""
    out = dict(cfg)
    for sec in SECTIONS:
        if rules.get(sec):
            out[sec] = {**(cfg.get(sec) or {}), **{k: v["rule"] for k, v in rules[sec].items()}}
    return out


def redact(obj):
    """The config as the dashboard may show it: secret-looking values and probe headers hidden,
    credentials stripped from URLs. Env var *names* (`client_secret_env`, `auth_env`) stay."""
    if isinstance(obj, dict):
        return {k: (HIDDEN if SECRET_KEY.search(str(k)) and not str(k).endswith("_env") and not isinstance(v, (dict, list))
                    else {h: HIDDEN for h in v} if k == "headers" and isinstance(v, dict)
                    else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return re.sub(r"(://)[^/@\s]+@", r"\1" + HIDDEN + "@", obj)
    return obj


def view(cfg: dict, rules: dict, components: list[dict]) -> dict:
    """GET /api/config: the image and Helm rules in effect (with where each came from and the rows
    that used it in the last scan), and the rest of the config as it was read, redacted."""
    used: dict[tuple[str, str], list[str]] = {}
    for c in components:
        if c.get("config_key"):
            used.setdefault(("helm" if c["category"] == "helm" else "images", c["config_key"]), []).append(c["id"])
    out = {}
    for sec in SECTIONS:
        file, dash = cfg.get(sec) or {}, rules.get(sec) or {}
        entries = []
        for k in [*file, *(k for k in dash if k not in file)]:
            e = {"key": k, "from": "dashboard" if k in dash else "config",
                 "rule": redact(dash[k]["rule"] if k in dash else file[k]), "components": used.get((sec, k), [])}
            if k in dash:
                e.update(at=dash[k].get("at"), by=dash[k].get("by"))
                if k in file:
                    e["replaces"] = redact(file[k])
            entries.append(e)
        out[sec] = entries
    settings = {k: redact(v) for k, v in cfg.items() if k not in SECTIONS}
    dash_only = {sec: {k: v["rule"] for k, v in (rules.get(sec) or {}).items()} for sec in SECTIONS}
    return {**out, "settings": settings,
            "settings_yaml": {k: _yaml({k: v}) for k, v in settings.items()},
            "dashboard_yaml": _yaml({k: v for k, v in dash_only.items() if v}) if any(dash_only.values()) else ""}


def _yaml(obj: dict) -> str:
    """YAML the way config/example.yaml writes it: top-level keys and their entries on their own
    lines, small entries inline (`mongo: {track: major}`)."""
    out = []
    for k, v in obj.items():
        if isinstance(v, dict) and v:
            out.append(f"{_dump(k)}:\n" + "".join(f"  {_dump(sk)}:{_value(sv, 4)}" for sk, sv in v.items()))
        elif isinstance(v, list) and any(isinstance(i, (dict, list)) for i in v):
            out.append(f"{_dump(k)}:\n" + "".join(_item(i) for i in v))
        else:
            out.append(f"{_dump(k)}:{_value(v, 2)}")
    return "".join(out)


def _dump(v) -> str:
    return yaml.safe_dump(v, sort_keys=False, default_flow_style=None, allow_unicode=True, width=100).removesuffix("...\n").strip()


def _item(v) -> str:
    """`  - value`, a block's first line on the dash's line."""
    first, *rest = _value(v, 4).lstrip("\n").strip(" ").splitlines()
    return f"  - {first}\n" + "".join(line + "\n" for line in rest)


def _value(v, indent: int) -> str:
    """` value` on the same line when it fits on one, else the block below it, indented."""
    text = _dump(v)
    inline = not isinstance(v, (dict, list)) or text[:1] in "{["  # a block (`- a`, `k: v`) can't follow a key
    return f" {text}\n" if inline else "\n" + "".join(" " * indent + line + "\n" for line in text.splitlines())
