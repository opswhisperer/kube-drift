"""Version parsing, tag templating and comparison helpers (stdlib only)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

# Tags that are moving pointers, not versions.
FLOATING = {
    "latest", "main", "master", "stable", "main-stable", "nightly", "dev", "develop",
    "edge", "beta", "alpine", "rolling", "lts", "current",
}
PRERELEASE_RE = re.compile(r"(rc|alpha|beta|dev|nightly|pre|preview|canary|snapshot|test|unstable)", re.I)
CORE_RE = re.compile(r"(\d+(?:\.\d+)*)")
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


@dataclass(frozen=True)
class Template:
    """The non-numeric shape of a tag: `prefix` + <numbers> + `suffix`."""

    prefix: str
    suffix: str
    parts: int

    def match(self, tag: str, loose_suffix: bool = False) -> Optional[tuple[int, ...]]:
        if not tag.startswith(self.prefix):
            return None
        rest = tag[len(self.prefix):]
        m = CORE_RE.match(rest)
        if not m:
            return None
        core = m.group(1)
        suffix = rest[m.end():]
        if not loose_suffix and suffix != self.suffix:
            return None
        if loose_suffix and PRERELEASE_RE.search(suffix):
            return None
        return tuple(int(p) for p in core.split("."))


def parse_tag(tag: str) -> Optional[tuple[Template, tuple[int, ...]]]:
    """Split a tag into its template and numeric core. None for floating / sha tags."""
    if tag in FLOATING or SHA_RE.match(tag):
        return None
    m = CORE_RE.search(tag)
    if not m:
        return None
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
        if len(nums) < len(cur) or any(n > 99999 for n in nums):
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
    if loose_suffix:  # the suffix varies per build, so show the real tag
        latest, latest_any = tag_track, tag_any
    else:
        latest = render(tpl, best_track)
        latest_any = render(tpl, best_any) if best_any else None
    return {"latest": latest, "latest_any": latest_any, "outdated": best_track > cur}
