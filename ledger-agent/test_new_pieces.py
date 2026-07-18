"""Tests for the new pieces: preflight split-mode detection and the
income rules engine (full INCOME-DESIGN lifecycle on a migrated DB)."""
import json
import os
import sqlite3
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "future"))

FAILED = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


def make_db(path, split_line):
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.executescript(f"""
    CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT,
        display_name TEXT, password_hash TEXT DEFAULT 'x');
    CREATE TABLE transactions (
        id INTEGER PRIMARY KEY,
        tx_date TEXT,
        description TEXT,
        amount_cents INTEGER,
        category TEXT DEFAULT 'Other',
        paid_by INTEGER,
        shared INTEGER DEFAULT 1,
        {split_line}
        simplefin_id TEXT UNIQUE,
        account_id TEXT
    );
    """)
    conn.execute("INSERT INTO users VALUES (1,'alta','Alta','x'),(2,'charlee','Charlee','x')")
    conn.execute("INSERT INTO transactions (tx_date, description, amount_cents,"
                 " paid_by, simplefin_id) VALUES ('2026-07-01','X',100,1,'s1')")
    conn.commit()
    conn.close()


def preflight_json(db):
    env = {**os.environ, "LEDGER_DB": db}
    out = subprocess.run([sys.executable, os.path.join(HERE, "preflight.py"),
                          "--json"], capture_output=True, text=True, env=env)
    return json.loads(out.stdout), out.returncode


# ── preflight: split detection across comment variants ──────────────────
make_db("/tmp/lt/p1.db",
        "split_pct INTEGER NOT NULL DEFAULT 50, -- % of the cost owed by user 1")
rep, rc = preflight_json("/tmp/lt/p1.db")
check("preflight exits 0", rc == 0)
sp = rep["split_mode"]
check("comment 'owed by user 1' -> user1_share high",
      sp["mode"] == "user1_share" and sp["confidence"] == "high", sp["why"])

make_db("/tmp/lt/p2.db",
        "payer_share_pct REAL DEFAULT 50, -- percent of cost that is the payer's share")
sp = preflight_json("/tmp/lt/p2.db")[0]["split_mode"]
check("comment 'payer's share' -> payer_share high",
      sp["mode"] == "payer_share" and sp["confidence"] == "high")

make_db("/tmp/lt/p3.db", "payer_share_pct REAL DEFAULT 50,")
sp = preflight_json("/tmp/lt/p3.db")[0]["split_mode"]
check("no comment, payer-named column -> medium",
      sp["mode"] == "payer_share" and sp["confidence"] == "medium")

make_db("/tmp/lt/p4.db", "split_pct INTEGER DEFAULT 50,")
sp = preflight_json("/tmp/lt/p4.db")[0]["split_mode"]
check("no comment, ambiguous column -> unknown, stays gated",
      sp["mode"] is None and sp["confidence"] == "none")

# ── deploy.sh at least parses ───────────────────────────────────────────
rc = subprocess.run(["bash", "-n", os.path.join(HERE, "deploy.sh")]).returncode
check("deploy.sh syntax", rc == 0)

# ── income migration + rules engine lifecycle ───────────────────────────
os.environ["LEDGER_DB"] = "/tmp/lt/p1.db"
import importlib
import ledger_core
importlib.reload(ledger_core)
r = subprocess.run([sys.executable,
                    os.path.join(HERE, "future", "migrate_income.py")],
                   capture_output=True, text=True, env=os.environ.copy())
check("income migration runs", r.returncode == 0, r.stderr.strip()[:100])
r2 = subprocess.run([sys.executable,
                     os.path.join(HERE, "future", "migrate_income.py")],
                    capture_output=True, text=True, env=os.environ.copy())
check("income migration idempotent",
      r2.returncode == 0 and "none (already migrated)" in r2.stdout)

import income_rules as ir
conn = sqlite3.connect("/tmp/lt/p1.db")
conn.row_factory = sqlite3.Row
check("existing rows backfilled 'out'", conn.execute(
    "SELECT COUNT(*) FROM transactions WHERE direction='out'").fetchone()[0] == 1)

now = "2026-07-16T12:00:00"
conn.execute("INSERT INTO income_rules (priority, match_desc, min_cents, "
             "set_type, set_paid_by, origin_text, created_at) VALUES "
             "(0, 'ADP PAYROLL', 100000, 'paycheck', 1, "
             "'ADP deposits over $1,000 are my paycheck', ?)", (now,))
conn.execute("INSERT INTO income_rules (priority, match_desc, set_type, "
             "created_at) VALUES (5, 'TRANSFER', 'transfer', ?)", (now,))
conn.commit()

# sync-path classification
t, owner = ir.classify_new_inflow(conn, "ADP PAYROLL 8842", "acct-1", 320000)
check("paycheck rule matches with owner", t == "paycheck" and owner == 1)
t, _ = ir.classify_new_inflow(conn, "ADP PAYROLL 8842", "acct-1", 4700)
check("min_cents bound blocks small ADP row", t == "unclassified")
t, _ = ir.classify_new_inflow(conn, "ONLINE TRANSFER FROM SAVINGS", None, 20000)
check("transfer rule matches", t == "transfer")
hc = conn.execute("SELECT hit_count FROM income_rules WHERE id=1").fetchone()[0]
check("hit_count bumped on sync path", hc == 1)

# queue + bulk apply
for i, (d, a) in enumerate([("ADP PAYROLL 8842", 320000),
                            ("VENMO CASHOUT", 50000),
                            ("TRANSFER TO CHECKING", 15000)]):
    conn.execute("INSERT INTO transactions (tx_date, description, amount_cents,"
                 " paid_by, shared, direction, income_type, simplefin_id) "
                 "VALUES ('2026-07-10',?,?,2,0,'in','unclassified',?)",
                 (d, a, f"q{i}"))
conn.commit()

cand = ir.Rule(id=0, priority=1, match_desc="VENMO", match_account=None,
               min_cents=None, max_cents=None, set_type="reimbursement",
               set_paid_by=None)
pv = ir.preview_rule(conn, cand, "description", "account_id")
check("preview counts venmo row only", pv["would_match_now"] == 1
      and "VENMO" in pv["sample_rows"][0]["description"])
check("preview flags no conflicts", pv["conflicting_rule_ids"] == [])

dry = ir.apply_rules(conn, "description", "account_id", "paid_by", dry_run=True)
check("dry run: 2 of 3 classifiable", dry["rows_affected"] == 2)
check("dry run wrote nothing", conn.execute(
    "SELECT COUNT(*) FROM transactions WHERE income_type='unclassified'"
    ).fetchone()[0] == 3)

real = ir.apply_rules(conn, "description", "account_id", "paid_by")
check("apply classifies 2", real["rows_affected"] == 2)
row = conn.execute("SELECT income_type, paid_by FROM transactions "
                   "WHERE description LIKE 'ADP%' AND direction='in'"
                   ).fetchone()
check("apply set type AND owner override",
      row["income_type"] == "paycheck" and row["paid_by"] == 1)
check("venmo stays unclassified (no rule)", conn.execute(
    "SELECT income_type FROM transactions WHERE description LIKE 'VENMO%'"
    ).fetchone()[0] == "unclassified")
check("hit_counts accumulated", conn.execute(
    "SELECT hit_count FROM income_rules WHERE id=1").fetchone()[0] == 2)
check("origin_text preserved", "over $1,000" in conn.execute(
    "SELECT origin_text FROM income_rules WHERE id=1").fetchone()[0])

# priority: a broad rule at lower priority must win over a later one
conn.execute("INSERT INTO income_rules (priority, match_desc, set_type, "
             "created_at) VALUES (-1, 'VENMO', 'gift', ?)", (now,))
conn.execute("INSERT INTO income_rules (priority, match_desc, set_type, "
             "created_at) VALUES (10, 'VENMO', 'other', ?)", (now,))
conn.commit()
t, _ = ir.classify_new_inflow(conn, "VENMO CASHOUT", None, 100, bump_hit=False)
check("priority order: lowest wins", t == "gift")
conn.close()

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {FAILED}")
    sys.exit(1)
print("All new-piece tests passed.")
