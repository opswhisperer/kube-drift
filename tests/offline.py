"""Run a scan against the fixture cluster and print a drift table.

    python -m tests.offline [--no-net] [--json out.json] [--orphans]
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CONFIG", str(ROOT / "config" / "example.yaml"))

from app.main import load_config  # noqa: E402
from app.scan import Scanner  # noqa: E402
from tests.fake_k8s import FakeK8s  # noqa: E402

logging.basicConfig(level=logging.WARNING)


def orphans(cfg):
    from app.orphans import OrphanScanner, apply_ignores
    from tests.fake_orphans import FakeKor, FakeOrphanK8s
    res = OrphanScanner(FakeOrphanK8s(), cfg, FakeKor()).run()
    res["items"], _ = apply_ignores(res["items"], {}, [], (cfg.get("orphans") or {}).get("ignore") or [])
    print(f"{'STATUS':10} {'KIND':20} {'NAMESPACE':14} {'NAME':40} WHY")
    for i in res["items"]:
        st = "ignored" if i.get("ignored") else i["status"]
        why = "; ".join([i["reason"]] + [f"{h['type']}: {h['text']}" for h in i["hints"]] + ([f"protected: {i['protected']}"] if i.get("protected") else []))
        print(f"{st:10} {i['kind']:20} {i['namespace'] or '-':14} {i['name'][:40]:40} {why[:90]}")


def main(argv):
    cfg = load_config()
    if "--orphans" in argv:
        return orphans(cfg)
    s = Scanner(FakeK8s(), cfg)
    if "--no-net" in argv:
        comps = s.inventory()
        for c in comps:
            print(f"{c['category']:14} {c['namespace']:22} {c['name']:45} {c['installed']}")
        return
    result = s.run()
    out = argv[argv.index("--json") + 1] if "--json" in argv else None
    if out:
        Path(out).write_text(json.dumps(result, indent=1))
    print(f"scan {result['duration_s']}s  {result['summary']}\n")
    print(f"{'STATUS':9} {'CAT':14} {'NAMESPACE':22} {'NAME':40} {'INSTALLED':24} {'LATEST':22} NOTE")
    for c in result["components"]:
        inst = c.get("running_version") or c.get("installed", "")
        if c.get("running_version"):
            inst += f" ({c['installed']})"
        note = c.get("error") or c.get("probe_error") or c.get("note", "")
        la = f" [any {c['latest_any']}]" if c.get("latest_any") and c.get("latest_any") != c.get("latest") else ""
        print(f"{c.get('status',''):9} {c['category']:14} {c['namespace']:22} {c['name'][:40]:40} {inst[:24]:24} {str(c.get('latest') or '-')[:22]:22} {note[:50]}{la}")


if __name__ == "__main__":
    main(sys.argv[1:])
