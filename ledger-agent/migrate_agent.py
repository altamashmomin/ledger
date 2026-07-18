"""Migration: agent-layer tables for Ledger. Idempotent — run any time.

Adds ONLY new tables owned by the agent layer; never alters app tables.
(The income feature's ALTERs live in INCOME-DESIGN.md and ship with that
build — including, per AGENT-DESIGN, an `origin_text` column on
income_rules recording the user's own words when a rule is created.)

Usage:  LEDGER_DB=/path/to/finance.db python3 migrate_agent.py
"""
import sqlite3
import sys

from ledger_core import db_path

DDL = """
CREATE TABLE IF NOT EXISTS api_tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash   TEXT NOT NULL UNIQUE,
    label        TEXT NOT NULL,
    user_id      INTEGER REFERENCES users(id),
    scopes       TEXT NOT NULL DEFAULT 'read',
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    revoked      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT NOT NULL,
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    target       TEXT,
    detail_json  TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS household_context (
    key          TEXT PRIMARY KEY,
    value        TEXT NOT NULL,
    updated_by   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token        TEXT NOT NULL UNIQUE,
    action_type  TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    preview_json TEXT NOT NULL,
    created_by   INTEGER REFERENCES api_tokens(id),
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
);

CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log(at);
"""


def main() -> int:
    path = db_path()
    conn = sqlite3.connect(path, timeout=10)
    try:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        conn.executescript(DDL)
        conn.commit()
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('api_tokens','audit_log','household_context','pending_actions') "
            "ORDER BY name")]
        print(f"db: {path}")
        print(f"journal_mode: {mode}")
        print(f"agent tables present: {', '.join(tables)}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
