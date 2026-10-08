"""kube-drift: version-drift dashboard for a Kubernetes cluster.

Scans Helm releases, workloads, static pods and nodes; resolves the newest upstream
version for each; serves a dashboard and a JSON API. Rescans on a timer.

Second category: orphaned resources, found by kor and enriched (orphans.py). They can be
ignored (persisted); export and delete are kubectl scripts to copy — the app never writes
to the cluster.

Env: PORT (8080), CONFIG (/config/config.yaml; YAML or JSON, optional), CLUSTER_NAME (overrides
config), POD_NAMESPACE (kube-drift's own namespace, protected), SCAN_INTERVAL_HOURS (overrides config),
STATE_FILE (/tmp/drift.json), DATA_DIR (/data: ignores, last orphan scan, image label cache), KOR_BIN (kor), KOR_KUBECONFIG,
GITHUB_TOKEN (optional), LOCAL_REGISTRY_AUTH (user:pass, optional),
KUBE_API / KUBE_TOKEN (out-of-cluster dev, e.g. `kubectl proxy`).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from . import orphans
from .openapi import SPEC
from .k8s import K8s
from .scan import Scanner, apply_ignores, kube_version, now_iso
from .store import Store

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("kube-drift")

HERE = Path(__file__).parent
STATIC = HERE / "static"
STATE_FILE = Path(os.environ.get("STATE_FILE", "/tmp/drift.json"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CLUSTER = {"name": os.environ.get("CLUSTER_NAME", "")}  # set from config in main()
VERSION = os.environ.get("KUBE_DRIFT_VERSION") or "dev"  # baked into the image at build time (Dockerfile)
MAX_BODY = 1 << 20
# Swagger UI for /docs: bundled into the image (Dockerfile); from source, the same version on a CDN.
SWAGGER_UI_VERSION = "5.33.1"
SWAGGER_DIR = STATIC / "swagger-ui"
SWAGGER_ASSETS = {"swagger-ui-bundle.js": "application/javascript", "swagger-ui.css": "text/css"}
API_LINKS = '</openapi.json>; rel="service-desc", </docs>; rel="service-doc"'  # RFC 8631


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
            scanner = Scanner(K8s(), cfg, DATA_DIR)
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

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8", extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        headers = {"Cache-Control": "no-store", **({"Link": API_LINKS} if self.path.startswith("/api/") else {}), **(extra or {})}
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/drift":
            self._send(200, json.dumps(STATE.snapshot()).encode())
        elif path == "/api/drift.csv":
            self._send(200, to_csv(STATE.snapshot()).encode(), "text/csv; charset=utf-8",
                       {"Content-Disposition": "attachment; filename=kube-drift.csv"})
        elif path == "/api/orphans":
            self._send(200, json.dumps(ORPHANS.snapshot()).encode())
        elif path == "/openapi.json":
            self._send(200, json.dumps(SPEC).encode())
        elif path == "/docs":
            self._send(200, (STATIC / "docs.html").read_bytes(), "text/html; charset=utf-8")
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
                   "/api/drift/ignore": self._ignore_updates, "/api/drift/unignore": self._unignore_updates}
        if path not in actions:
            self._send(404, b'{"error":"not found"}')
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
    cfg = load_config()
    CLUSTER["name"] = os.environ.get("CLUSTER_NAME") or str(cfg.get("cluster_name") or "")
    threading.Thread(target=scan_loop, args=(cfg,), daemon=True, name="scan").start()
    ORPHANS.context = (cfg.get("orphans") or {}).get("kubectl_context")
    ORPHANS.config_rules = (cfg.get("orphans") or {}).get("ignore") or []
    if (cfg.get("orphans") or {}).get("enabled", True):
        threading.Thread(target=orphan_loop, args=(cfg,), daemon=True, name="orphans").start()
    port = int(os.environ.get("PORT", "8080"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log.warning("kube-drift has no authentication: expose it only through a gateway that "
                "authenticates every request (README: 'Exposing it')")
    log.info("kube-drift %s listening on :%d", VERSION, port)
    srv.serve_forever()


if __name__ == "__main__":
    main()
