"""preflight — run this FIRST on the Pi. Read-only; changes nothing.

Checks the environment, introspects the deployed schema, and — the useful
part — reads the split column's original DDL comment out of sqlite_master
to recommend LEDGER_SPLIT_MODE, turning the manual gate into a
confirmation. Exit 0 = deployable.

  LEDGER_DB=/path/to/finance.db python3 preflight.py [--json]
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ledger_core as core


def detect_split_mode(conn, s) -> dict:
    """Parse the transactions DDL (preserved verbatim in sqlite_master)
    for the split column's comment."""
    col = s.col.get("split")
    if not col:
        return {"column": None, "mode": None, "confidence": "none",
                "why": "No split column found — is this the right DB?"}
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='transactions'").fetchone()[0] or ""
    line = next((ln for ln in ddl.splitlines() if col in ln), "").lower()
    comment = line.split("--", 1)[1].strip() if "--" in line else ""
    if comment:
        if "payer" in comment:
            return {"column": col, "mode": "payer_share",
                    "confidence": "high", "why": f'DDL comment: "{comment}"'}
        if re.search(r"user\s*1|owed by user", comment):
            return {"column": col, "mode": "user1_share",
                    "confidence": "high", "why": f'DDL comment: "{comment}"'}
    if col == "payer_share_pct":
        return {"column": col, "mode": "payer_share", "confidence": "medium",
                "why": "No usable comment; the column NAME says payer share. "
                       "Verify one balance against the dashboard."}
    return {"column": col, "mode": None, "confidence": "none",
            "why": f"No comment on `{col}` and the name is ambiguous. "
                   "Check app.py's balance computation, or verify both "
                   "modes against the dashboard."}


def sync_service_candidates() -> list[str]:
    if not shutil.which("systemctl"):
        return []
    try:
        out = subprocess.run(
            ["systemctl", "list-units", "--type=service,timer", "--all",
             "--no-legend", "--no-pager", "--plain"],
            capture_output=True, text=True, timeout=10).stdout
        return sorted({ln.split()[0] for ln in out.splitlines()
                       if re.search(r"sync|simplefin|finance", ln, re.I)})
    except Exception:
        return []


def port_free(port: int) -> bool:
    with socket.socket() as sk:
        return sk.connect_ex(("127.0.0.1", port)) != 0


def main() -> int:
    report: dict = {"ok": True, "checks": []}

    def check(name, ok, detail="", fatal=True):
        report["checks"].append({"name": name, "ok": bool(ok),
                                 "detail": detail})
        if not ok and fatal:
            report["ok"] = False

    check("python >= 3.9", sys.version_info >= (3, 9),
          sys.version.split()[0])
    path = core.db_path()
    exists = os.path.isfile(path)
    check("database exists", exists,
          f"{path} ({os.path.getsize(path):,} bytes)" if exists else
          f"{path} not found — set LEDGER_DB")
    if not exists:
        _emit(report); return 1

    conn = core.connect_ro(path)
    try:
        s = core.LedgerSchema(conn)
        check("transactions table", True,
              f"date={s.col['date']}, split={s.col['split']}, "
              f"external_id={s.col['external_id']}")
        check("exactly two users", len(s.users) == 2,
              ", ".join(map(str, s.users.values())) or "none found")
        n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        check("transactions present", n > 0, f"{n} rows", fatal=False)
        check("income feature", True,
              "built" if s.has_income else
              "not built yet (expected — income tools will self-gate)")
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        check("journal mode", True,
              f"{mode} (migration will switch to WAL)" if mode != "wal"
              else "wal")
        split = detect_split_mode(conn, s)
        report["split_mode"] = split
        check("split-mode detection", split["confidence"] != "none",
              f"{split['mode'] or 'UNKNOWN'} ({split['confidence']}) — "
              f"{split['why']}", fatal=False)
    finally:
        conn.close()

    try:
        import mcp, uvicorn  # noqa: F401
        check("mcp + uvicorn installed", True)
    except ImportError as e:
        check("mcp + uvicorn installed", False,
              f"{e.name} missing — deploy.sh will install", fatal=False)

    check("port 8091 free", port_free(8091),
          "" if port_free(8091) else "something already listening",
          fatal=False)
    cands = sync_service_candidates()
    report["sync_services"] = cands
    check("sync service found", bool(cands), ", ".join(cands) or
          "none matched sync|simplefin|finance — name it manually",
          fatal=False)

    _emit(report)
    return 0 if report["ok"] else 1


def _emit(report: dict) -> None:
    if "--json" in sys.argv:
        print(json.dumps(report))
        return
    for c in report["checks"]:
        mark = "PASS" if c["ok"] else "WARN"
        print(f"{mark}  {c['name']}" + (f" — {c['detail']}" if c["detail"] else ""))
    sp = report.get("split_mode") or {}
    print()
    if sp.get("confidence") == "high":
        print(f"Split mode: {sp['mode']} (auto-detected). deploy.sh will "
              "set it; still verify one balance against the dashboard.")
    elif sp.get("mode"):
        print(f"Split mode: probably {sp['mode']} — verify against the "
              "dashboard before trusting the balance tool.")
    else:
        print("Split mode: could not determine — the balance tool stays "
              "gated until you set LEDGER_SPLIT_MODE by hand.")
    print("READY TO DEPLOY" if report["ok"] else
          "FIX THE FAILED CHECKS ABOVE FIRST")


if __name__ == "__main__":
    sys.exit(main())
