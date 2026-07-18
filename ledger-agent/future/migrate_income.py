"""Income feature migration — INCOME-DESIGN.md step 1's schema, pre-built.

DO NOT RUN until the income build starts (after the visibility decision).
Running it early is harmless to existing numbers — every current row
backfills to direction='out' and the sync still skips inflows until the
sync flip — but there's no reason to carry unused columns.

Adds, idempotently:
  transactions.direction    'out' | 'in'   (all existing rows -> 'out')
  transactions.income_type  NULL for out; for in: paycheck | reimbursement
                            | refund | transfer | gift | other | unclassified
  income_rules              per INCOME-DESIGN, plus origin_text — the
                            user's own words when the rule was created
                            (the one idea worth stealing from Era).

  LEDGER_DB=/path/to/finance.db python3 migrate_income.py
"""
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ledger_core import db_path

RULES_DDL = """
CREATE TABLE IF NOT EXISTS income_rules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    priority      INTEGER NOT NULL DEFAULT 0,   -- lower runs first
    match_desc    TEXT,          -- substring match on description, case-insensitive
    match_account TEXT,          -- SimpleFIN account id, or NULL = any
    min_cents     INTEGER,       -- inclusive bounds, either may be NULL
    max_cents     INTEGER,
    set_type      TEXT NOT NULL, -- income_type to assign
    set_paid_by   INTEGER REFERENCES users(id),  -- owner override, or NULL
    origin_text   TEXT,          -- the user's own words when this rule was made
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    hit_count     INTEGER NOT NULL DEFAULT 0
);
"""


def main() -> int:
    conn = sqlite3.connect(db_path(), timeout=10)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(transactions)")}
        added = []
        if "direction" not in cols:
            conn.execute("ALTER TABLE transactions ADD COLUMN direction "
                         "TEXT NOT NULL DEFAULT 'out'")
            added.append("direction")
        if "income_type" not in cols:
            conn.execute("ALTER TABLE transactions ADD COLUMN income_type TEXT")
            added.append("income_type")
        conn.executescript(RULES_DDL)
        conn.commit()
        print(f"db: {db_path()}")
        print(f"columns added: {', '.join(added) or 'none (already migrated)'}")
        print("income_rules table present (with origin_text)")
        n = conn.execute("SELECT COUNT(*) FROM transactions "
                        "WHERE direction='out'").fetchone()[0]
        print(f"{n} existing rows carry direction='out'")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
