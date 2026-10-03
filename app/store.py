"""Small file-backed store under DATA_DIR for dashboard state.

    ignored.json        {"Kind/ns/name": {kind, namespace, name, note, at}}
    rules.json          [{id, kind?, namespace?, name_regex?, source?, note, at}]
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("kube-drift.store")


def key(kind: str, namespace: str, name: str) -> str:
    return f"{kind}/{namespace or ''}/{name}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.lock = threading.Lock()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.writable = os.access(self.root, os.W_OK)
        except OSError as e:
            log.warning("data dir %s not writable: %s", self.root, e)
            self.writable = False
        self._ignored = self._read_json(self.root / "ignored.json", {})
        self._rules = self._read_json(self.root / "rules.json", [])

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _read_json(p: Path, default):
        try:
            return json.loads(p.read_text()) if p.exists() else default
        except Exception as e:  # noqa: BLE001
            log.warning("ignoring unreadable %s: %s", p, e)
            return default

    def _write_atomic(self, p: Path, text: str):
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(text)
        os.replace(tmp, p)

    # ------------------------------------------------------------------ ignores
    def ignored(self) -> dict:
        with self.lock:
            return dict(self._ignored)

    def ignore(self, items: list[dict], note: str = "") -> int:
        with self.lock:
            for it in items:
                self._ignored[key(it["kind"], it.get("namespace", ""), it["name"])] = {
                    "kind": it["kind"], "namespace": it.get("namespace", ""), "name": it["name"],
                    "note": note[:500], "at": _now(),
                }
            self._write_atomic(self.root / "ignored.json", json.dumps(self._ignored, indent=1, sort_keys=True))
        return len(items)

    def unignore(self, items: list[dict]) -> int:
        n = 0
        with self.lock:
            for it in items:
                n += self._ignored.pop(key(it["kind"], it.get("namespace", ""), it["name"]), None) is not None
            self._write_atomic(self.root / "ignored.json", json.dumps(self._ignored, indent=1, sort_keys=True))
        return n

    # ------------------------------------------------------------------ rules
    def rules(self) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self._rules]

    def add_rule(self, rule: dict, note: str = "") -> dict:
        with self.lock:
            r = {"id": secrets.token_hex(4), **rule, "note": note[:500], "at": _now()}
            self._rules.append(r)
            self._write_atomic(self.root / "rules.json", json.dumps(self._rules, indent=1))
        return r

    def remove_rule(self, rid: str) -> bool:
        with self.lock:
            before = len(self._rules)
            self._rules = [r for r in self._rules if r["id"] != rid]
            self._write_atomic(self.root / "rules.json", json.dumps(self._rules, indent=1))
            return len(self._rules) < before
