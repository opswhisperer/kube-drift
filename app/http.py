"""Tiny urllib wrapper with timeouts, JSON decoding and header access."""
from __future__ import annotations

import email.utils
import json
import logging
import random
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("kube-drift.http")
UA = "kube-drift/1.0 (+https://github.com/opswhisperer/kube-drift)"

# 429 Too Many Requests: wait and try again, a few times. Registries' anonymous limits are low
# (ECR Public allows about one request a second per IP) and a scan asks many things at once.
RETRIES = 3
MAX_WAIT = 30.0  # asked to wait longer than this, give up now rather than stall the scan
_sleep = time.sleep


def _retry_after(headers: dict, attempt: int) -> float:
    """Seconds to wait: the server's Retry-After (seconds or an HTTP date), else 1s, 2s, 4s with jitter."""
    val = (headers.get("retry-after") or "").strip()
    if val:
        try:
            return max(0.0, float(val))
        except ValueError:
            pass
        try:
            return max(0.0, email.utils.parsedate_to_datetime(val).timestamp() - time.time())
        except (TypeError, ValueError):
            pass  # unparseable: back off as if it weren't there
    return 2 ** attempt * random.uniform(0.75, 1.25)


@dataclass
class Resp:
    status: int
    headers: dict
    body: bytes

    def json(self):
        return json.loads(self.body.decode("utf-8") or "null")

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


class HttpError(Exception):
    def __init__(self, status: int, url: str, headers: Optional[dict] = None, body: bytes = b""):
        super().__init__(f"HTTP {status} for {url}")
        self.status, self.url, self.headers, self.body = status, url, headers or {}, body


def request(url: str, method: str = "GET", headers: Optional[dict] = None, data: Optional[bytes] = None,
            timeout: float = 20.0, insecure: bool = False) -> Resp:
    h = {"User-Agent": UA, "Accept": "application/json, */*"}
    h.update(headers or {})
    req = urllib.request.Request(url, method=method, headers=h, data=data)
    ctx = None
    if insecure and url.startswith("https://"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    for attempt in range(RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return Resp(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
        except urllib.error.HTTPError as e:
            body = e.read() if hasattr(e, "read") else b""
            err = HttpError(e.code, url, {k.lower(): v for k, v in e.headers.items()}, body)
        if err.status != 429 or attempt == RETRIES:
            raise err
        wait = _retry_after(err.headers, attempt)
        if wait > MAX_WAIT:
            raise err
        log.info("429 from %s, retrying in %.1fs", url.split("?")[0], wait)
        _sleep(wait)
    raise AssertionError("unreachable")


def get_json(url: str, headers: Optional[dict] = None, timeout: float = 20.0, insecure: bool = False):
    return request(url, headers=headers, timeout=timeout, insecure=insecure).json()
