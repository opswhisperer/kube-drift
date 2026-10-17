"""Version parsing, tag templating and comparison helpers (stdlib only)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

# Tags that are moving pointers, not versions.
FLOATING = {
    "latest", "main", "master", "stable", "main-stable", "nightly", "dev", "develop",
    "edge", "beta", "alpine", "rolling", "lts", "current",
}
PRERELEASE_RE = re.compile(r"(rc|alpha|beta|dev|nightly|pre|preview|canary|snapshot|test|unstable)", re.I)
CORE_RE = re.compile(r"(\d+(?:\.\d+)*)")
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
# A build stamp: YYYYMMDD, optionally followed by a time (20261008-0811, 20261008T081130).
DATE_RE = re.compile(r"((?:19|20)\d\d(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]))(?:([-_.T])(\d{4,6}))?(?!\d)")


@dataclass(frozen=True)
class Template:
    """The non-numeric shape of a tag: `prefix` + <numbers> + `suffix`.

    A `date` template's numbers are a build stamp (date, then time) rather than a version.
    """

    prefix: str
    suffix: str
    parts: int
    date: bool = False

    def match(self, tag: str, loose_suffix: bool = False) -> Optional[tuple[int, ...]]:
        if not tag.startswith(self.prefix):
            return None
        rest = tag[len(self.prefix):]
        if self.date:
            m = DATE_RE.match(rest)
            if not m:
                return None
            nums = tuple(int(p) for p in (m.group(1), m.group(3)) if p)
        else:
            m = CORE_RE.match(rest)
            if not m:
                return None
            nums = tuple(int(p) for p in m.group(1).split("."))
        suffix = rest[m.end():]
        if not loose_suffix and suffix != self.suffix:
            return None
        if loose_suffix and PRERELEASE_RE.search(suffix):
            return None
        return nums


def parse_tag(tag: str) -> Optional[tuple[Template, tuple[int, ...]]]:
    """Split a tag into its template and numeric core. None for floating / sha tags."""
    if tag in FLOATING or SHA_RE.match(tag) and not DATE_RE.fullmatch(tag):
        return None
    m = CORE_RE.search(tag)
    if not m:
        return None
    d = DATE_RE.match(tag, m.start())
    if d:  # the version is a build stamp: compare it as date + time, not as one huge number
        nums = tuple(int(p) for p in (d.group(1), d.group(3)) if p)
        return Template(tag[: d.start()], tag[d.end():], len(nums), date=True), nums
    prefix, suffix = tag[: m.start()], tag[m.end():]
    nums = tuple(int(p) for p in m.group(1).split("."))
    return Template(prefix, suffix, len(nums)), nums


def is_floating(tag: str) -> bool:
    return parse_tag(tag) is None


def numeric(v: str) -> tuple[int, ...]:
    """Loose numeric core of a version string ("v1.2.3-stable" -> (1,2,3))."""
    m = CORE_RE.search(v or "")
    return tuple(int(p) for p in m.group(1).split(".")) if m else ()


def cmp_versions(a: str, b: str) -> int:
    """Compare two version strings by numeric core, padding shorter with zeros."""
    x, y = numeric(a), numeric(b)
    n = max(len(x), len(y))
    x += (0,) * (n - len(x))
    y += (0,) * (n - len(y))
    return (x > y) - (x < y)


def is_prerelease(v: str) -> bool:
    return bool(PRERELEASE_RE.search(v or ""))


def render(tpl: Template, nums: Iterable[int]) -> str:
    return f"{tpl.prefix}{'.'.join(str(n) for n in nums)}{tpl.suffix}"


def pick_latest(installed_tag: str, candidates: Iterable[str], track: str = "any",
                loose_suffix: bool = False) -> dict:
    """Pick the newest tag shaped like `installed_tag` from `candidates`.

    Returns {"latest": tag-at-installed-precision, "latest_any": newest regardless of
    track, "outdated": bool}. `track` = any | major | minor restricts the main answer
    to the installed major(.minor); the unrestricted answer is always in latest_any.
    """
    parsed = parse_tag(installed_tag)
    if not parsed:
        return {"latest": None, "latest_any": None, "outdated": None}
    tpl, cur = parsed
    best_any: Optional[tuple[int, ...]] = None
    best_track: Optional[tuple[int, ...]] = None
    tag_any = tag_track = None
    for tag in candidates:
        nums = tpl.match(tag, loose_suffix)
        if nums is None or PRERELEASE_RE.search(tag[len(tpl.prefix):]) and not loose_suffix:
            continue
        # A candidate must be at least as precise as the installed tag (so '12.4.2' never
        # matches a bare build number like '9799770991'), and no part may be absurdly large.
        if len(nums) < len(cur) or not tpl.date and any(n > 99999 for n in nums):
            continue
        key = nums[: len(cur)] + (0,) * max(0, len(cur) - len(nums))
        if best_any is None or key > best_any:
            best_any, tag_any = key, tag
        keep = (
            track == "any"
            or (track == "major" and key[:1] == cur[:1])
            or (track == "minor" and key[:2] == cur[:2])
        )
        if keep and (best_track is None or key > best_track):
            best_track, tag_track = key, tag
    if best_track is None:
        return {"latest": None, "latest_any": None, "outdated": None}
    if loose_suffix or tpl.date:  # the suffix varies per build, or the stamp is zero-padded: show the real tag
        latest, latest_any = tag_track, tag_any
    else:
        latest = render(tpl, best_track)
        latest_any = render(tpl, best_any) if best_any else None
    return {"latest": latest, "latest_any": latest_any, "outdated": best_track > cur}


def walk_up(installed_tag: str, exists: Callable[[str], bool], track: str = "any", limit: int = 20) -> dict:
    """Find newer tags by asking whether the next ones exist, for when a registry lists too
    many tags to read them all (Quay and Docker Hub list build tags newest-first, and a busy
    repo's release tags fall off the end).

    From the installed version, try the next major, then minor, then patch (v4.0.0, v3.33.0,
    v3.32.2), move to the first that exists and repeat. A release that skipped its .0 is
    missed. Asks `exists` at most `limit` times; same result shape as pick_latest.
    """
    none = {"latest": None, "latest_any": None, "outdated": None}
    parsed = parse_tag(installed_tag)
    if not parsed or parsed[0].date or parsed[0].parts > 3:
        return none
    tpl, cur = parsed
    seen: dict[str, bool] = {}

    def has(nums: tuple[int, ...]) -> bool:
        tag = render(tpl, nums)
        if tag not in seen:
            if len(seen) >= limit:
                return False
            seen[tag] = exists(tag)
        return seen[tag]

    def climb(level: int) -> tuple[int, ...]:
        best = cur
        while True:
            nxt = next((b for i in range(level, len(best))
                        if has(b := best[:i] + (best[i] + 1,) + (0,) * (len(best) - i - 1))), None)
            if nxt is None:
                return best
            best = nxt

    best_any = climb(0)
    level = {"major": 1, "minor": 2}.get(track, 0)
    best_track = best_any if level == 0 else climb(level) if level < len(cur) else cur
    return {"latest": render(tpl, best_track), "latest_any": render(tpl, best_any), "outdated": best_track > cur}


_TERM_RE = re.compile(r"^(>=|<=|=<|!=|~>|=|<|>|~|\^)?v?(\d+|[xX*])(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?(?:[-+]\S*)?$")


def _term(op: str, parts: tuple[int, ...], v: tuple[int, int, int]) -> bool:
    """One semver comparison; `parts` holds the numbers given before any wildcard."""
    n = len(parts)
    lo = parts + (0,) * (3 - n)
    nxt = (parts[:-1] + (parts[-1] + 1,) + (0,) * (3 - n)) if n else None  # first version past the prefix
    if op in ("", "="):
        return n == 0 or (v == lo if n == 3 else lo <= v < nxt)
    if op == "!=":
        return not _term("=", parts, v)
    if op == ">":
        return n == 0 or (v > lo if n == 3 else v >= nxt)
    if op == ">=":
        return v >= lo
    if op == "<":
        return n > 0 and v < lo
    if op in ("<=", "=<"):
        return n == 0 or (v <= lo if n == 3 else v < nxt)
    if op in ("~", "~>"):
        return n == 0 or lo <= v < ((lo[0] + 1, 0, 0) if n == 1 else (lo[0], lo[1] + 1, 0))
    # ^: the leftmost non-zero part given stays fixed
    if n == 0:
        return True
    if lo[0] or n == 1:
        return lo <= v < (lo[0] + 1, 0, 0)
    if lo[1] or n == 2:
        return lo <= v < (0, lo[1] + 1, 0)
    return lo <= v < (0, 0, lo[2] + 1)


def satisfies(version: str, constraint: str) -> Optional[bool]:
    """Whether `version` meets a Helm `kubeVersion` range such as '>=1.25.0-0', '>= 1.19, < 1.30',
    '~1.28' or '1.26 - 1.29 || ^2'. Pre-release and build suffixes are ignored on both sides
    (charts write '-0' so that 'v1.29.3-eks-1a2b' counts as 1.29.3). None if it can't be parsed."""
    m = CORE_RE.search(version or "")
    if not m or not (constraint or "").strip():
        return None
    v = tuple(int(p) for p in m.group(1).split(".")[:3])
    v += (0,) * (3 - len(v))
    result = False
    for alt in constraint.split("||"):
        alt = re.sub(r"(\S+)\s+-\s+(\S+)", r">=\1 <=\2", alt.strip())  # hyphen range
        alt = re.sub(r"(>=|<=|=<|!=|~>|[<>=~^])\s+", r"\1", alt)
        terms = [t for t in re.split(r"[\s,]+", alt) if t]
        ok = True
        for t in terms:
            tm = _TERM_RE.match(t)
            if not tm:
                return None
            parts: list[int] = []
            for p in tm.groups()[1:]:
                if p is None or not p.isdigit():
                    break
                parts.append(int(p))
            ok = ok and _term(tm.group(1) or "", tuple(parts), v)
        result = result or (ok and bool(terms))
    return result
