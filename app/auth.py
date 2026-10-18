"""Optional built-in authentication: OIDC sign-in for people, API keys for scripts and agents.

Off unless the config has `auth.oidc` — then kube-drift behaves as before and must sit behind an
authenticating gateway. When on:

- the dashboard needs a session: a signed cookie set after an OIDC authorization-code login
  (with PKCE) against the configured issuer;
- /api/* takes that session or an API key (`Authorization: Bearer kd_…`); API keys are made and
  revoked by signed-in people, and only their SHA-256 is stored (DATA_DIR/api-keys.json);
- /healthz, /readyz, /openapi.json, /docs and /auth/* stay open.

Standard library only, so there's no JWT signature check: the ID token comes straight from the
issuer's token endpoint over TLS, which OIDC Core (3.1.3.7, step 6) accepts in place of one. Its
issuer, audience, expiry and nonce are still checked.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from . import http

log = logging.getLogger("kube-drift.auth")

SESSION_COOKIE = "kd_session"
LOGIN_COOKIE = "kd_login"  # state, nonce and PKCE verifier between /auth/login and /auth/callback
LOGIN_TTL = 600
KEY_PREFIX = "kd_"
LAST_USED_EVERY = 300  # seconds between saving an API key's last_used_at


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _now_iso(t: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() if t is None else t))


def parse_cookies(header: str | None) -> dict[str, str]:
    out = {}
    for part in (header or "").split(";"):
        k, sep, v = part.strip().partition("=")
        if sep and k:
            out[k] = v
    return out


def safe_next(path: str | None) -> str:
    """A local path to return to after sign-in; anything else (another site, //host) becomes /."""
    p = path or "/"
    return p if p.startswith("/") and not p.startswith("//") and "\\" not in p else "/"


class Signer:
    """HMAC-signed, expiring JSON values for cookies."""

    def __init__(self, key: bytes):
        self.key = key

    def dumps(self, obj: dict, ttl: int) -> str:
        body = _b64(json.dumps({**obj, "exp": int(time.time()) + ttl}, separators=(",", ":")).encode())
        return body + "." + _b64(hmac.new(self.key, body.encode(), hashlib.sha256).digest())

    def loads(self, token: str | None) -> dict | None:
        body, _, sig = (token or "").partition(".")
        if not body or not sig:
            return None
        if not hmac.compare_digest(_b64(hmac.new(self.key, body.encode(), hashlib.sha256).digest()), sig):
            return None
        try:
            obj = json.loads(_unb64(body))
        except ValueError:
            return None
        return obj if isinstance(obj, dict) and obj.get("exp", 0) > time.time() else None


def session_key(data_dir: Path) -> bytes:
    """SESSION_SECRET if set (several replicas must share it), else a key kept on the data volume
    so sessions survive restarts. Without either, sessions last until the pod restarts."""
    env = os.environ.get("SESSION_SECRET")
    if env:
        return env.encode()
    p = Path(data_dir) / "session.key"
    try:
        if p.exists():
            return bytes.fromhex(p.read_text().strip())
        key = secrets.token_bytes(32)
        p.write_text(key.hex())
        p.chmod(0o600)
        return key
    except (OSError, ValueError) as e:
        log.warning("cannot keep a session key in %s (%s): sign-ins end when the pod restarts", p, e)
        return secrets.token_bytes(32)


# ---------------------------------------------------------------------------------- API keys
class KeyStore:
    """API keys, by id: {name, prefix, hash, created_by, created_at, expires_at, last_used_at}."""

    def __init__(self, root: Path):
        self.file = Path(root) / "api-keys.json"
        self.lock = threading.Lock()
        self._saved_at = 0.0
        try:
            self._keys: dict[str, dict] = json.loads(self.file.read_text()) if self.file.exists() else {}
        except Exception as e:  # noqa: BLE001
            log.warning("ignoring unreadable %s: %s", self.file, e)
            self._keys = {}

    def _save(self):
        tmp = self.file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._keys, indent=1, sort_keys=True))
        tmp.chmod(0o600)
        os.replace(tmp, self.file)
        self._saved_at = time.time()

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def list(self) -> list[dict]:
        with self.lock:
            keys = [{"id": kid, **{k: v for k, v in e.items() if k != "hash"}} for kid, e in self._keys.items()]
        return sorted(keys, key=lambda k: k["created_at"], reverse=True)

    def create(self, name: str, created_by: str, days: int | None) -> tuple[str, dict]:
        """Returns the token (shown once, never stored) and the key's public record."""
        token = KEY_PREFIX + secrets.token_urlsafe(32)
        kid = secrets.token_hex(4)
        entry = {"name": name[:100], "prefix": token[:len(KEY_PREFIX) + 6], "hash": self._hash(token),
                 "created_by": created_by, "created_at": _now_iso(),
                 "expires_at": _now_iso(time.time() + days * 86400) if days else None, "last_used_at": None}
        with self.lock:
            self._keys[kid] = entry
            self._save()
        return token, {"id": kid, **{k: v for k, v in entry.items() if k != "hash"}}

    def revoke(self, kid: str) -> bool:
        with self.lock:
            if self._keys.pop(kid, None) is None:
                return False
            self._save()
            return True

    def check(self, token: str) -> dict | None:
        """The key's record if `token` is a live key; notes when it was used."""
        if not token.startswith(KEY_PREFIX):
            return None
        h = self._hash(token)
        with self.lock:
            for kid, e in self._keys.items():
                if hmac.compare_digest(e["hash"], h):
                    if e.get("expires_at") and e["expires_at"] < _now_iso():
                        return None
                    e["last_used_at"] = _now_iso()
                    if time.time() - self._saved_at > LAST_USED_EVERY:
                        try:
                            self._save()
                        except OSError as err:
                            log.warning("cannot save api key use: %s", err)
                    return {"id": kid, "name": e["name"]}
        return None


# -------------------------------------------------------------------------------------- OIDC
@dataclass
class OIDCConfig:
    issuer: str
    client_id: str
    client_secret: str
    redirect_url: str
    scopes: list[str] = field(default_factory=lambda: ["openid", "profile", "email"])
    allowed_groups: list[str] = field(default_factory=list)
    allowed_emails: list[str] = field(default_factory=list)
    groups_claim: str = "groups"
    session_hours: float = 12

    @classmethod
    def from_config(cls, cfg: dict) -> "OIDCConfig":
        """`auth.oidc` from the config file; the client secret comes from the env var it names."""
        missing = [k for k in ("issuer", "client_id", "redirect_url") if not cfg.get(k)]
        if missing:
            raise ValueError(f"auth.oidc: missing {', '.join(missing)}")
        env = cfg.get("client_secret_env") or "OIDC_CLIENT_SECRET"
        secret = os.environ.get(env, "")
        if not secret:
            raise ValueError(f"auth.oidc: the client secret env var {env} is empty")
        for k in ("issuer", "redirect_url"):
            u = urllib.parse.urlparse(cfg[k])
            if u.scheme != "https" and u.hostname not in ("localhost", "127.0.0.1"):
                raise ValueError(f"auth.oidc.{k} must be https")
        scopes = list(cfg.get("scopes") or ["openid", "profile", "email"])
        if "openid" not in scopes:
            scopes.insert(0, "openid")
        return cls(issuer=str(cfg["issuer"]), client_id=str(cfg["client_id"]), client_secret=secret,
                   redirect_url=str(cfg["redirect_url"]), scopes=scopes,
                   allowed_groups=[str(g) for g in cfg.get("allowed_groups") or []],
                   allowed_emails=[str(e).lower() for e in cfg.get("allowed_emails") or []],
                   groups_claim=str(cfg.get("groups_claim") or "groups"),
                   session_hours=float(cfg.get("session_hours") or 12))


class OIDC:
    def __init__(self, cfg: OIDCConfig):
        self.cfg = cfg
        self._meta: dict | None = None
        self._lock = threading.Lock()

    def metadata(self) -> dict:
        """The issuer's discovery document, fetched once (and again after a failure)."""
        with self._lock:
            if self._meta is None:
                url = self.cfg.issuer.rstrip("/") + "/.well-known/openid-configuration"
                meta = http.get_json(url, timeout=10)
                if meta.get("issuer", "").rstrip("/") != self.cfg.issuer.rstrip("/"):
                    raise ValueError(f"{url} names issuer {meta.get('issuer')!r}, not {self.cfg.issuer!r}")
                self._meta = meta
            return self._meta

    def login_url(self, state: str, nonce: str, verifier: str) -> str:
        challenge = _b64(hashlib.sha256(verifier.encode()).digest())
        q = urllib.parse.urlencode({
            "response_type": "code", "client_id": self.cfg.client_id, "redirect_uri": self.cfg.redirect_url,
            "scope": " ".join(self.cfg.scopes), "state": state, "nonce": nonce,
            "code_challenge": challenge, "code_challenge_method": "S256"})
        ep = self.metadata()["authorization_endpoint"]
        return ep + ("&" if "?" in ep else "?") + q

    def exchange(self, code: str, verifier: str, nonce: str) -> dict:
        """Trade the code for tokens; returns the checked ID token claims."""
        basic = base64.b64encode(f"{urllib.parse.quote(self.cfg.client_id, safe='')}:"
                                 f"{urllib.parse.quote(self.cfg.client_secret, safe='')}".encode()).decode()
        data = urllib.parse.urlencode({"grant_type": "authorization_code", "code": code,
                                       "redirect_uri": self.cfg.redirect_url, "code_verifier": verifier}).encode()
        tok = http.request(self.metadata()["token_endpoint"], method="POST", data=data, timeout=15, headers={
            "Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"}).json()
        id_token = tok.get("id_token") or ""
        parts = id_token.split(".")
        if len(parts) != 3:
            raise ValueError("token response has no ID token")
        claims = json.loads(_unb64(parts[1]))
        aud = claims.get("aud")
        auds = aud if isinstance(aud, list) else [aud]
        now = time.time()
        if claims.get("iss", "").rstrip("/") != self.metadata()["issuer"].rstrip("/"):
            raise ValueError("ID token from another issuer")
        if self.cfg.client_id not in auds or (len(auds) > 1 and claims.get("azp") != self.cfg.client_id):
            raise ValueError("ID token for another client")
        if claims.get("exp", 0) < now - 60:
            raise ValueError("ID token expired")
        if not hmac.compare_digest(str(claims.get("nonce") or ""), nonce):
            raise ValueError("ID token nonce mismatch")
        return claims

    def allowed(self, claims: dict) -> str | None:
        """None if this person may sign in, else why not."""
        groups = claims.get(self.cfg.groups_claim) or []
        groups = [groups] if isinstance(groups, str) else list(groups)
        email = str(claims.get("email") or "").lower()
        if not self.cfg.allowed_groups and not self.cfg.allowed_emails:
            return None
        if set(groups) & set(self.cfg.allowed_groups):
            return None
        if email and email in self.cfg.allowed_emails and claims.get("email_verified", True) is not False:
            return None
        return "your account isn't in a group or email list allowed to use kube-drift"

    def end_session_url(self) -> str | None:
        try:
            return self.metadata().get("end_session_endpoint")
        except Exception:  # noqa: BLE001
            return None


@dataclass
class Auth:
    """What main.py asks: who is calling, and how to sign people in and out."""
    oidc: OIDC
    keys: KeyStore
    signer: Signer
    secure_cookies: bool = True

    def cookie(self, name: str, value: str, max_age: int, path: str = "/") -> str:
        return (f"{name}={value}; Path={path}; Max-Age={max_age}; HttpOnly; SameSite=Lax"
                + ("; Secure" if self.secure_cookies else ""))

    def identify(self, headers) -> dict | None:
        """{"via": "session"|"api-key", ...} for an authenticated request, else None."""
        authz = headers.get("Authorization") or ""
        if authz[:7].lower() == "bearer ":
            key = self.keys.check(authz[7:].strip())
            return {"via": "api-key", "key": key} if key else None
        s = self.signer.loads(parse_cookies(headers.get("Cookie")).get(SESSION_COOKIE))
        if s and s.get("t") == "s":
            return {"via": "session", "user": {k: s.get(k) for k in ("sub", "name", "email")}}
        return None

    def start_login(self, next_path: str) -> tuple[str, str]:
        """(redirect URL, Set-Cookie) for /auth/login."""
        state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        token = self.signer.dumps({"t": "l", "state": state, "nonce": nonce, "v": verifier,
                                   "next": safe_next(next_path)}, LOGIN_TTL)
        return self.oidc.login_url(state, nonce, verifier), self.cookie(LOGIN_COOKIE, token, LOGIN_TTL, "/auth")

    def finish_login(self, headers, query: dict) -> tuple[str, list[str]]:
        """(where to go, Set-Cookies) for /auth/callback. Raises ValueError with a reason to show."""
        login = self.signer.loads(parse_cookies(headers.get("Cookie")).get(LOGIN_COOKIE))
        if query.get("error"):
            raise ValueError(f"the identity provider said: {query.get('error_description') or query['error']}")
        if not login or login.get("t") != "l":
            raise ValueError("the sign-in took too long or was started in another browser; try again")
        if not hmac.compare_digest(login["state"], query.get("state") or ""):
            raise ValueError("state mismatch; try again")
        if not query.get("code"):
            raise ValueError("no authorization code")
        claims = self.oidc.exchange(query["code"], login["v"], login["nonce"])
        why = self.oidc.allowed(claims)
        if why:
            raise PermissionError(why)
        ttl = int(self.oidc.cfg.session_hours * 3600)
        name = claims.get("name") or claims.get("preferred_username") or claims.get("email") or claims.get("sub")
        session = self.signer.dumps({"t": "s", "sub": claims.get("sub"), "name": name, "email": claims.get("email")}, ttl)
        log.info("signed in: %s", claims.get("email") or claims.get("sub"))
        return login.get("next") or "/", [self.cookie(SESSION_COOKIE, session, ttl),
                                          self.cookie(LOGIN_COOKIE, "", 0, "/auth")]

    def logout_cookie(self) -> str:
        return self.cookie(SESSION_COOKIE, "", 0)


def from_config(cfg: dict, data_dir: Path) -> Auth | None:
    """Auth from the config's `auth:` section, or None when it's off."""
    oc = (cfg.get("auth") or {}).get("oidc")
    if not oc:
        return None
    oidc_cfg = OIDCConfig.from_config(oc)
    return Auth(OIDC(oidc_cfg), KeyStore(data_dir), Signer(session_key(data_dir)),
                secure_cookies=oidc_cfg.redirect_url.startswith("https://"))
