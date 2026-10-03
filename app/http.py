"""Tiny urllib wrapper with timeouts, JSON decoding and header access."""
from __future__ import annotations

import json
import logging
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("kube-drift.http")
UA = "kube-drift/1.0 (+https://github.com/opswhisperer/kube-drift)"


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
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return Resp(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
    except urllib.error.HTTPError as e:
        body = e.read() if hasattr(e, "read") else b""
        raise HttpError(e.code, url, {k.lower(): v for k, v in e.headers.items()}, body) from None


def get_json(url: str, headers: Optional[dict] = None, timeout: float = 20.0, insecure: bool = False):
    return request(url, headers=headers, timeout=timeout, insecure=insecure).json()
