"""kube-drift: version-drift dashboard for a Kubernetes cluster.

Scans Helm releases, workloads, static pods and nodes; resolves the newest upstream
version for each; serves a dashboard and a JSON API. Rescans on a timer.

Second category: orphaned resources, found by kor and enriched (orphans.py). They can be
ignored (persisted); export and delete are kubectl scripts to copy — the app never writes
to the cluster.

Auth (auth.py) is optional: with `auth.oidc` in the config, people sign in through OIDC and
scripts use API keys; without it, kube-drift must sit behind a gateway that authenticates.

Env: PORT (8080), CONFIG (/config/config.yaml; YAML or JSON, optional), CLUSTER_NAME (overrides
config), POD_NAMESPACE (kube-drift's own namespace, protected), SCAN_INTERVAL_HOURS (overrides config),
STATE_FILE (/tmp/drift.json), DATA_DIR (/data: ignores, last orphan scan, image label cache), KOR_BIN (kor), KOR_KUBECONFIG,
GITHUB_TOKEN (optional), LOCAL_REGISTRY_AUTH (user:pass, optional),
KUBE_API / KUBE_TOKEN (out-of-cluster dev, e.g. `kubectl proxy`),
OIDC_CLIENT_SECRET (or the env var auth.oidc.client_secret_env names), SESSION_SECRET (optional).
"""
from __future__ import annotations

import html
import json
import logging
import os
import threading
import urllib.parse
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from . import auth, orphans, overrides
from .openapi import SPEC
from .k8s import K8s
from .scan import STATUS_ORDER, Scanner, apply_ignores, kube_version, now_iso
from .store import Store

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("kube-drift")

HERE = Path(__file__).parent
STATIC = HERE / "static"
STATE_FILE = Path(os.environ.get("STATE_FILE", "/tmp/drift.json"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CLUSTER = {"name": os.environ.get("CLUSTER_NAME", "")}  # set from config in main()
CONFIG: dict = {}  # the config file as loaded, set in main(); dashboard image/Helm rules go on top (overrides.py)
VERSION = os.environ.get("KUBE_DRIFT_VERSION") or "dev"  # baked into the image at build time (Dockerfile)
MAX_BODY = 1 << 20
# Swagger UI for /docs: bundled into the image (Dockerfile); from source, the same version on a CDN.
SWAGGER_UI_VERSION = "5.33.1"
SWAGGER_DIR = STATIC / "swagger-ui"
SWAGGER_ASSETS = {"swagger-ui-bundle.js": "application/javascript", "swagger-ui.css": "text/css"}
API_LINKS = '</openapi.json>; rel="service-desc", </docs>; rel="service-doc"'  # RFC 8631
AUTH: auth.Auth | None = None  # set from config in main(); None = no built-in auth (gateway does it)
OPEN = {"/healthz", "/readyz", "/openapi.json", "/docs", "/icon.svg"}  # reachable without signing in
PAGES = {"/", "/index.html"}  # signed-out people are sent to sign in; API callers get 401


def load_config() -> dict:
    """YAML (or JSON — YAML is a superset) from $CONFIG. Missing file = all defaults.
    Keys left empty (e.g. `helm:` with only commented examples) load as null; drop them so
    the code's defaults apply."""
    p = Path(os.environ.get("CONFIG", "/config/config.yaml"))
    if not p.exists():
        log.warning("no config at %s — using defaults (see config/example.yaml)", p)
        return {}
    with open(p) as f:
        cfg = yaml.safe_load(f) or {}
    if not isinstance(cfg, dict):
        raise ValueError(f"{p}: expected a mapping at the top level")
    cfg = {k: v for k, v in cfg.items() if v is not None}
    if isinstance(cfg.get("orphans"), dict):
        cfg["orphans"] = {k: v for k, v in cfg["orphans"].items() if v is not None}
    return cfg


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.result: dict | None = None
        self.scanning = False
        self.last_error: str | None = None
        self.next_scan: float = 0
        self.wake = threading.Event()
        if STATE_FILE.exists():
            try:
                self.result = json.loads(STATE_FILE.read_text())
                log.info("loaded previous scan from %s (%s)", STATE_FILE, self.result.get("scanned_at"))
            except Exception as e:  # noqa: BLE001
                log.warning("ignoring %s: %s", STATE_FILE, e)

    def snapshot(self) -> dict:
        """The last scan with ignored updates applied at read time, so they take effect at once."""
        store = ORPHANS.store
        with self.lock:
            meta = {
                "scanning": self.scanning, "last_error": self.last_error,
                "next_scan_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(self.next_scan)) if self.next_scan else None,
                "generated_at": now_iso(), "cluster": CLUSTER["name"], "version": VERSION,
                "ignore_enabled": store.writable,
            }
            result = self.result or {"scanned_at": None, "summary": {}, "components": []}
        result, _ = apply_ignores(result, store.update_ignores())
        return {**meta, **result}

    def find(self, ids: list[str]) -> dict[str, dict]:
        with self.lock:
            return {c["id"]: c for c in (self.result or {}).get("components", []) if c["id"] in set(ids)}


STATE = State()


class OrphanState:
    """Latest orphan scan + the ignore store. Ignores are merged in at read time so
    they apply immediately, not on the next scan."""

    def __init__(self, store: Store, context: str | None = None, config_rules: list | None = None):
        self.store = store
        self.context = context
        self.config_rules = config_rules or []
        self.file = store.root / "orphans.json"
        self.lock = threading.Lock()
        self.result: dict | None = None
        self.scanning = False
        self.last_error: str | None = None
        self.next_scan: float = 0
        self.wake = threading.Event()
        if self.file.exists():
            try:
                saved = json.loads(self.file.read_text())
                if saved.get("format") == orphans.FORMAT:
                    self.result = saved
                else:
                    log.info("discarding %s: format %s, want %s — rescanning", self.file, saved.get("format"), orphans.FORMAT)
            except Exception as e:  # noqa: BLE001
                log.warning("ignoring %s: %s", self.file, e)

    def persist(self):
        try:
            with self.lock:
                text = json.dumps(self.result)
            self.file.write_text(text)
        except Exception as e:  # noqa: BLE001
            log.warning("cannot persist orphan scan: %s", e)

    def find(self, ids: list[str]) -> dict[str, dict]:
        with self.lock:
            items = (self.result or {}).get("items", [])
            return {i["id"]: i for i in items if i["id"] in set(ids)}

    def snapshot(self) -> dict:
        ignored = self.store.ignored()
        with self.lock:
            res = dict(self.result or {"scanned_at": None, "items": []})
            items, rules = orphans.apply_ignores(res.get("items", []), ignored, self.store.rules(), self.config_rules)
            meta = {"scanning": self.scanning, "last_error": self.last_error,
                    "next_scan_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(self.next_scan)) if self.next_scan else None}
        summary = {"orphan": 0, "in-use": 0, "managed": 0, "protected": 0, "ignored": 0}
        for i in items:
            summary["ignored" if i.get("ignored") else i["status"]] += 1
        summary["total"] = len(items) - summary["ignored"]
        return {**res, **meta, "items": items, "rules": rules, "summary": summary, "kubectl_context": self.context,
                "cluster": CLUSTER["name"], "version": VERSION,
                "ignore_enabled": self.store.writable,
                "stale_ignores": sorted(set(ignored) - {i["id"] for i in items}),
                "generated_at": now_iso()}


ORPHANS = OrphanState(Store(DATA_DIR))  # context is set from config in main()


def scan_loop(cfg: dict):
    interval = float(os.environ.get("SCAN_INTERVAL_HOURS") or cfg.get("scan_interval_hours", 6)) * 3600
    while True:
        with STATE.lock:
            STATE.scanning = True
        try:
            scanner = Scanner(K8s(), overrides.effective(cfg, ORPHANS.store.config_rules()), DATA_DIR)
            result = scanner.run()
            with STATE.lock:
                STATE.result, STATE.last_error = result, None
            _, ended = apply_ignores(result, ORPHANS.store.update_ignores())
            if ended:  # a new scan can end an ignore (Kubernetes upgraded, awaited version out)
                ORPHANS.store.unignore_updates(ended)
                log.info("ignores ended: %s", ", ".join(sorted(ended)))
            try:
                STATE_FILE.write_text(json.dumps(result))
            except Exception as e:  # noqa: BLE001
                log.warning("cannot persist state: %s", e)
            log.info("scan done in %ss: %s", result["duration_s"], result["summary"])
        except Exception as e:  # noqa: BLE001
            log.exception("scan failed")
            with STATE.lock:
                STATE.last_error = f"{type(e).__name__}: {e}"
        finally:
            with STATE.lock:
                STATE.scanning = False
                STATE.next_scan = time.time() + interval
        STATE.wake.wait(timeout=interval)
        STATE.wake.clear()


def orphan_loop(cfg: dict):
    ocfg = cfg.get("orphans", {}) or {}
    hours = ocfg.get("interval_hours") or os.environ.get("SCAN_INTERVAL_HOURS") or cfg.get("scan_interval_hours", 6)
    interval = float(hours) * 3600
    kor = orphans.Kor(os.environ.get("KOR_BIN", "kor"), os.environ.get("KOR_KUBECONFIG") or None)
    first = True
    while True:
        # A persisted scan younger than the interval is good enough after a restart.
        age = time.time() - _parse_ts((ORPHANS.result or {}).get("scanned_at"))
        if not (first and age < interval):
            with ORPHANS.lock:
                ORPHANS.scanning = True
            try:
                result = orphans.OrphanScanner(K8s(), cfg, kor).run()
                with ORPHANS.lock:
                    ORPHANS.result, ORPHANS.last_error = result, None
                ORPHANS.persist()
                log.info("orphan scan done in %ss: %d findings", result["duration_s"], len(result["items"]))
            except Exception as e:  # noqa: BLE001
                log.exception("orphan scan failed")
                with ORPHANS.lock:
                    ORPHANS.last_error = f"{type(e).__name__}: {e}"[:400]
            finally:
                with ORPHANS.lock:
                    ORPHANS.scanning = False
            age = 0
        first = False
        wait = max(60.0, interval - age)
        with ORPHANS.lock:
            ORPHANS.next_scan = time.time() + wait
        ORPHANS.wake.wait(timeout=wait)
        ORPHANS.wake.clear()


def _parse_ts(iso: str | None) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp() if iso else 0.0
    except ValueError:
        return 0.0


class Handler(BaseHTTPRequestHandler):
    server_version = f"kube-drift/{VERSION}"

    def log_message(self, fmt, *args):  # quieter access log
        if self.path not in ("/healthz", "/readyz"):
            log.debug("%s %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8", extra: dict | None = None,
              cookies: list[str] | None = None):
        headers = {"Cache-Control": "no-store", **({"Link": API_LINKS} if self.path.startswith("/api/") else {}), **(extra or {})}
        if any("\r" in v or "\n" in v for v in [*headers.values(), *(cookies or [])]):
            raise ValueError("line break in a response header")  # it would start a header of its own
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers.items():
            self.send_header(k, v)
        for c in cookies or []:
            self.send_header("Set-Cookie", c)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # ----- authentication (only when auth.oidc is configured) -----
    def _gate(self, path: str) -> bool:
        """True if the request may go on. Otherwise answers it: signed-out people are sent to sign
        in, everything else gets 401. Sets self.who to the caller (None when auth is off)."""
        self.who = None
        if AUTH is None or path in OPEN or (path.startswith("/docs/") and path[6:] in SWAGGER_ASSETS):
            return True
        self.who = AUTH.identify(self.headers)
        if self.who:
            return True
        if path in PAGES and self.command in ("GET", "HEAD"):
            self._send(302, b"", "text/plain", {"Location": "/auth/login?" + urllib.parse.urlencode({"next": self.path})})
        else:
            self._send(401, b'{"error":"sign in, or send an API key as Authorization: Bearer <key>"}', extra={
                "WWW-Authenticate": 'Bearer realm="kube-drift"'})
        return False

    def _page(self, code: int, title: str, text: str, link: tuple[str, str] = ("/", "Back to kube-drift")):
        body = (f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                f'<title>{html.escape(title)}</title><body style="font:15px/1.5 system-ui,sans-serif;max-width:36em;'
                f'margin:15vh auto;padding:0 16px"><h1 style="font-size:1.3em">{html.escape(title)}</h1>'
                f'<p>{html.escape(text)}</p><p><a href="{html.escape(link[0])}">{html.escape(link[1])}</a></p>')
        self._send(code, body.encode(), "text/html; charset=utf-8")

    def _auth_get(self, path: str):
        query = dict(urllib.parse.parse_qsl(self.path.split("?", 1)[1] if "?" in self.path else ""))
        if path == "/auth/me":
            who = AUTH.identify(self.headers) if AUTH else None
            self._json(200, {"auth": "oidc" if AUTH else "none", "via": (who or {}).get("via"),
                             "user": (who or {}).get("user"), "key": (who or {}).get("key")})
        elif AUTH is None:
            self._send(404, b'{"error":"sign-in is off: set auth.oidc in the config"}')
        elif path == "/auth/login":
            try:
                url, cookie = AUTH.start_login(query.get("next") or "/")
            except Exception as e:  # noqa: BLE001
                log.warning("sign-in: cannot reach the identity provider: %s", e)
                self._page(502, "Can't reach the identity provider", f"{type(e).__name__}: {e}"[:300], ("/auth/login", "Try again"))
                return
            self._send(302, b"", "text/plain", {"Location": url}, [cookie])
        elif path == "/auth/callback":
            try:
                where, cookies = AUTH.finish_login(self.headers, query)
            except PermissionError as e:
                self._page(403, "Not allowed", str(e), ("/auth/login", "Sign in as someone else"))
                return
            except Exception as e:  # noqa: BLE001
                log.warning("sign-in failed: %s", e)
                self._page(400, "Sign-in failed", f"{e}"[:300], ("/auth/login", "Try again"))
                return
            self._send(302, b"", "text/plain", {"Location": where}, cookies)
        else:
            self._send(404, b'{"error":"not found"}')

    def _logout(self):
        if AUTH is None:
            self._send(404, b'{"error":"sign-in is off: set auth.oidc in the config"}')
            return
        self._send(200, json.dumps({"redirect": AUTH.oidc.end_session_url() or "/"}).encode(), cookies=[AUTH.logout_cookie()])

    # ----- API keys (kube-drift's own state; made and revoked by signed-in people only) -----
    def _keys_allowed(self) -> bool:
        if AUTH is None:
            self._send(404, b'{"error":"API keys need sign-in: set auth.oidc in the config"}')
        elif (self.who or {}).get("via") != "session":
            self._send(403, b'{"error":"API keys are managed by signed-in people, not with an API key"}')
        else:
            return True
        return False

    def _create_key(self, body: dict):
        name, days = str(body.get("name") or "").strip(), body.get("days")
        if not name:
            self._json(400, {"error": "name: what the key is for"})
            return
        if days is not None and (not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 3650):
            self._json(400, {"error": "days: a whole number from 1 to 3650, or leave it out for a key that doesn't expire"})
            return
        user = self.who["user"]
        token, key = AUTH.keys.create(name, user.get("email") or user.get("name") or user.get("sub") or "", days)
        log.info("api key %s (%s) created by %s", key["id"], key["name"], key["created_by"])
        self._json(201, {"token": token, "key": key})

    def _revoke_key(self, body: dict):
        kid = str(body.get("id") or "")
        ok = AUTH.keys.revoke(kid)
        if ok:
            log.info("api key %s revoked by %s", kid, self.who["user"].get("email") or self.who["user"].get("sub"))
        self._json(200 if ok else 404, {"revoked": ok})

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/auth/"):
            self._auth_get(path)
            return
        if not self._gate(path):
            return
        if path in ("/", "/index.html"):
            self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path in ("/api/drift", "/api/drift.csv"):
            try:
                keep = drift_filter(self.path.split("?", 1)[1] if "?" in self.path else "")
            except ValueError as e:
                self._json(400, {"error": str(e)})
                return
            snap = STATE.snapshot()
            if keep:
                snap["components"] = [c for c in snap["components"] if keep(c)]
            if path == "/api/drift":
                self._send(200, json.dumps(snap).encode())
            else:
                self._send(200, to_csv(snap).encode(), "text/csv; charset=utf-8",
                           {"Content-Disposition": "attachment; filename=kube-drift.csv"})
        elif path == "/api/orphans":
            self._send(200, json.dumps(ORPHANS.snapshot()).encode())
        elif path == "/api/config":
            with STATE.lock:
                comps = list((STATE.result or {}).get("components", []))
            env = {k: os.environ[k] for k in ("CLUSTER_NAME", "SCAN_INTERVAL_HOURS") if os.environ.get(k)}
            self._json(200, {**overrides.view(CONFIG, ORPHANS.store.config_rules(), comps),
                             "file": os.environ.get("CONFIG", "/config/config.yaml"), "env": env,
                             "editable": ORPHANS.store.writable, "cluster": CLUSTER["name"], "version": VERSION})
        elif path == "/api/keys":
            if self._keys_allowed():
                self._json(200, {"keys": AUTH.keys.list()})
        elif path == "/openapi.json":
            self._send(200, json.dumps(SPEC).encode())
        elif path == "/docs":
            self._send(200, (STATIC / "docs.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/icon.svg":
            self._send(200, (STATIC / "icon.svg").read_bytes(), "image/svg+xml", {"Cache-Control": "public, max-age=86400"})
        elif path.startswith("/docs/") and path[6:] in SWAGGER_ASSETS:
            name = path[6:]
            if (SWAGGER_DIR / name).is_file():
                self._send(200, (SWAGGER_DIR / name).read_bytes(), SWAGGER_ASSETS[name] + "; charset=utf-8",
                           {"Cache-Control": "public, max-age=86400"})
            else:
                self._send(302, b"", "text/plain", {"Location": f"https://cdn.jsdelivr.net/npm/swagger-ui-dist@{SWAGGER_UI_VERSION}/{name}"})
        elif path in ("/healthz", "/readyz"):
            ok = path == "/healthz" or STATE.result is not None or STATE.scanning
            self._send(200 if ok else 503, b'{"ok":true}' if ok else b'{"ok":false}')
        else:
            self._send(404, b'{"error":"not found"}')

    do_HEAD = do_GET

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        if path == "/auth/logout":
            self._logout()
            return
        if not self._gate(path):
            return
        if path == "/api/rescan":
            scope = "orphans" if "scope=orphans" in query else "versions" if "scope=versions" in query else "all"
            busy = []
            for name, st in (("versions", STATE), ("orphans", ORPHANS)):
                if scope not in ("all", name):
                    continue
                if st.scanning:
                    busy.append(name)
                else:
                    st.wake.set()
            status = f"already scanning: {', '.join(busy)}" if busy else "rescan requested"
            self._send(202, json.dumps({"status": status, "scope": scope}).encode())
            return
        actions = {"/api/orphans/ignore": self._ignore, "/api/orphans/unignore": self._unignore,
                   "/api/orphans/commands": self._commands,
                   "/api/orphans/rules": self._add_rule, "/api/orphans/rules/delete": self._remove_rule,
                   "/api/drift/ignore": self._ignore_updates, "/api/drift/unignore": self._unignore_updates,
                   "/api/config/rules": self._set_config_rule, "/api/config/rules/delete": self._remove_config_rule,
                   "/api/keys": self._create_key, "/api/keys/delete": self._revoke_key}
        if path not in actions:
            self._send(404, b'{"error":"not found"}')
            return
        if path.startswith("/api/keys") and not self._keys_allowed():
            return
        # JSON only: a cross-site form or text/plain POST can't reach the ignore endpoints
        # without a CORS preflight, which this server never answers.
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            self._send(415, b'{"error":"Content-Type must be application/json"}')
            return
        body = self._body()
        if body is None:
            return
        try:
            actions[path](body)
        except Exception as e:  # noqa: BLE001
            log.exception("%s failed", path)
            self._json(500, {"error": f"{type(e).__name__}: {e}"[:300]})

    # ----- orphan actions (dashboard state only; nothing here touches the cluster) -----
    def _body(self) -> dict | None:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                raise ValueError("body too large")
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
            if self.path.split("?", 1)[0] in ("/api/orphans/ignore", "/api/orphans/unignore", "/api/orphans/commands"):
                ids = [str(i) for i in body.get("ids", [])]
                if not ids or len(ids) > 1000:
                    raise ValueError("ids: 1..1000 resource ids (Kind/namespace/name)")
                body["ids"] = ids
            elif self.path.split("?", 1)[0] in ("/api/drift/ignore", "/api/drift/unignore"):
                ids = [str(i) for i in body.get("ids", [])]
                if not ids or len(ids) > 1000:
                    raise ValueError("ids: 1..1000 component ids")
                body["ids"] = ids
            return body
        except (ValueError, AttributeError) as e:
            self._send(400, json.dumps({"error": str(e)}).encode())
            return None

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj).encode())

    def _ignore(self, body: dict):
        if not ORPHANS.store.writable:
            self._json(503, {"error": f"{ORPHANS.store.root} is not writable"})
            return
        found = ORPHANS.find(body["ids"])
        n = ORPHANS.store.ignore(list(found.values()), str(body.get("note") or ""))
        self._json(200, {"ignored": n, "missing": [i for i in body["ids"] if i not in found]})

    def _unignore(self, body: dict):
        if not ORPHANS.store.writable:
            self._json(503, {"error": f"{ORPHANS.store.root} is not writable"})
            return
        items = []
        for i in body["ids"]:
            kind, _, rest = i.partition("/")
            ns, _, name = rest.partition("/")
            items.append({"kind": kind, "namespace": ns, "name": name})
        self._json(200, {"unignored": ORPHANS.store.unignore(items)})

    def _add_rule(self, body: dict):
        if not ORPHANS.store.writable:
            self._json(503, {"error": f"{ORPHANS.store.root} is not writable"})
            return
        try:
            rule = orphans.validate_rule(body.get("rule") or {})
        except ValueError as e:
            self._json(400, {"error": str(e)})
            return
        self._json(200, {"rule": ORPHANS.store.add_rule(rule, str(body.get("note") or ""))})

    def _remove_rule(self, body: dict):
        if not ORPHANS.store.writable:
            self._json(503, {"error": f"{ORPHANS.store.root} is not writable"})
            return
        ok = ORPHANS.store.remove_rule(str(body.get("id") or ""))
        self._json(200 if ok else 404, {"removed": ok})

    # ----- ignored version updates (kube-drift's own state) -----
    def _ignore_updates(self, body: dict):
        """Hide components' updates until the first of: `days` pass, the cluster's Kubernetes version
        changes (`until_kube_change`), or a version newer than the one ignored is offered (`until_newer`)."""
        if not ORPHANS.store.writable:
            self._json(503, {"error": f"{ORPHANS.store.root} is not writable"})
            return
        days, newer = body.get("days"), bool(body.get("until_newer"))
        if days is not None and (not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 3650):
            self._json(400, {"error": "days: a whole number from 1 to 3650"})
            return
        if days is None and not body.get("until_kube_change") and not newer:
            self._json(400, {"error": "say when the ignore ends: days, until_kube_change or until_newer"})
            return
        with STATE.lock:
            kube = kube_version((STATE.result or {}).get("components", []))
        if body.get("until_kube_change") and not kube:
            self._json(400, {"error": "the cluster's Kubernetes version isn't known yet; wait for a scan"})
            return
        found = STATE.find(body["ids"])
        entries, skipped = {}, []
        for cid, c in found.items():
            digest = None if c.get("latest") else c.get("remote_digest")
            if newer and not c.get("latest") and not digest:
                skipped.append({"id": cid, "why": "no version or image digest to wait past"})
                continue
            entries[cid] = {"latest": c.get("latest"), "latest_any": c.get("latest_any"), "digest": digest, "note": body.get("note"),
                            "until": (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds") if days else None,
                            "kube_version": kube if body.get("until_kube_change") else None,
                            "until_newer": newer}
        n = ORPHANS.store.ignore_updates(entries) if entries else 0
        self._json(200, {"ignored": n, "skipped": skipped, "missing": [i for i in body["ids"] if i not in found]})

    def _unignore_updates(self, body: dict):
        if not ORPHANS.store.writable:
            self._json(503, {"error": f"{ORPHANS.store.root} is not writable"})
            return
        self._json(200, {"unignored": ORPHANS.store.unignore_updates(body["ids"])})

    # ----- image and Helm rules (kube-drift's own state, laid over the config file) -----
    def _actor(self) -> str:
        who = self.who or {}
        if who.get("via") == "api-key":
            return f"API key {(who.get('key') or {}).get('name', '')}".strip()
        user = who.get("user") or {}
        return user.get("email") or user.get("name") or user.get("sub") or ""

    def _set_config_rule(self, body: dict):
        if not ORPHANS.store.writable:
            self._json(503, {"error": f"{ORPHANS.store.root} is not writable"})
            return
        try:
            section = body.get("section")
            key = overrides.validate_key(section, body.get("key"))
            rule = overrides.validate_rule(section, body.get("rule") or {})
        except ValueError as e:
            self._json(400, {"error": str(e)})
            return
        entry = ORPHANS.store.set_config_rule(section, key, rule, self._actor())
        log.info("%s rule %s set by %s: %s", section, key, entry["by"] or "dashboard", rule)
        STATE.wake.set()  # rescan versions with it; a scan running now is followed by another
        self._json(200, {"section": section, "key": key, **entry})

    def _remove_config_rule(self, body: dict):
        if not ORPHANS.store.writable:
            self._json(503, {"error": f"{ORPHANS.store.root} is not writable"})
            return
        section, key = str(body.get("section") or ""), str(body.get("key") or "")
        ok = ORPHANS.store.remove_config_rule(section, key)
        if ok:
            log.info("%s rule %s removed by %s", section, key, self._actor() or "dashboard")
            STATE.wake.set()
        self._json(200 if ok else 404, {"removed": ok})

    def _commands(self, body: dict):
        """kubectl script to export, or back up and delete, the given scanned items."""
        found = ORPHANS.find(body["ids"])
        items = [found[i] for i in body["ids"] if i in found]
        action = body.get("action")
        if action not in ("export", "delete"):
            self._json(400, {"error": "action must be export or delete"})
            return
        script = orphans.commands(items, action, ORPHANS.context)
        skipped = [{"id": i, "why": "not in the current orphan scan"} for i in body["ids"] if i not in found]
        for it in items:
            if not it.get("known_kind"):
                skipped.append({"id": it["id"], "why": "unsupported kind"})
            elif action == "delete" and it["status"] == "protected":
                skipped.append({"id": it["id"], "why": f"protected: {it.get('protected')}"})
        self._json(200, {"script": script, "skipped": skipped})


def drift_filter(query: str):
    """Which components GET /api/drift(.csv) keeps for its query string, as a test; None when the
    query asks for no filter. `status` (repeatable, or comma-separated), `ignored` and `top` combine
    with AND; `status=outdated&ignored=false&top=true` is the set `summary.outdated` counts.
    Raises ValueError for anything else, rather than quietly returning everything."""
    params = urllib.parse.parse_qs(query, keep_blank_values=True)
    if unknown := sorted(set(params) - {"status", "ignored", "top"}):
        raise ValueError(f"unknown parameter {unknown[0]!r}: use status, ignored or top")
    statuses = {s for v in params.get("status", []) for s in v.split(",")}
    if bad := sorted(statuses - set(STATUS_ORDER)):
        raise ValueError(f"status: {bad[0]!r} isn't one of outdated, current, unknown, error")
    flags = {}
    for k in ("ignored", "top"):
        if k in params:
            if params[k] not in (["true"], ["false"]):
                raise ValueError(f"{k}: true or false, given once")
            flags[k] = params[k] == ["true"]
    if not statuses and not flags:
        return None
    return lambda c: ((not statuses or c.get("status", "unknown") in statuses)
                      and flags.get("ignored", bool(c.get("ignored"))) == bool(c.get("ignored"))
                      and flags.get("top", not c.get("release")) == (not c.get("release")))


def to_csv(snap: dict) -> str:
    import csv
    import io

    cols = ["category", "namespace", "name", "release", "install", "installed", "running_version", "latest", "latest_any", "status", "incompatible", "image", "ref"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for c in snap.get("components", []):
        w.writerow([c.get(k, "") or "" for k in cols])
    return buf.getvalue()


def main():
    global AUTH
    cfg = load_config()
    CONFIG.update(cfg)
    AUTH = auth.from_config(cfg, DATA_DIR)  # a broken auth config stops startup rather than run open
    CLUSTER["name"] = os.environ.get("CLUSTER_NAME") or str(cfg.get("cluster_name") or "")
    threading.Thread(target=scan_loop, args=(cfg,), daemon=True, name="scan").start()
    ORPHANS.context = (cfg.get("orphans") or {}).get("kubectl_context")
    ORPHANS.config_rules = (cfg.get("orphans") or {}).get("ignore") or []
    if (cfg.get("orphans") or {}).get("enabled", True):
        threading.Thread(target=orphan_loop, args=(cfg,), daemon=True, name="orphans").start()
    port = int(os.environ.get("PORT", "8080"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    if AUTH:
        log.info("sign-in: OIDC via %s; API keys in %s", AUTH.oidc.cfg.issuer, AUTH.keys.file)
    else:
        log.warning("built-in sign-in is off (no auth.oidc in the config): expose kube-drift only through "
                    "a gateway that authenticates every request (README: 'Exposing it')")
    log.info("kube-drift %s listening on :%d", VERSION, port)
    srv.serve_forever()


if __name__ == "__main__":
    main()
