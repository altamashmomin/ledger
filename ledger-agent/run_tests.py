"""Smoke tests for the agent layer against BOTH deployed schema variants.

Variant A = the STEP-1 build (tx_date, split_pct, simplefin_id, contributions)
Variant B = the FABLE-5 build (txn_date, payer_share_pct, is_shared)
             + simulated future income columns (direction, income_type)
"""
import json
import os
import sqlite3
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

FAILED = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


def build_variant_a(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE,
        display_name TEXT, password_hash TEXT DEFAULT 'x');
    CREATE TABLE transactions (id INTEGER PRIMARY KEY, tx_date TEXT,
        description TEXT, amount_cents INTEGER, category TEXT DEFAULT 'Other',
        paid_by INTEGER, shared INTEGER DEFAULT 1, split_pct INTEGER DEFAULT 50,
        simplefin_id TEXT UNIQUE, created_at TEXT DEFAULT '2026-01-01');
    CREATE TABLE goals (id INTEGER PRIMARY KEY, name TEXT, target_cents INTEGER,
        created_at TEXT DEFAULT '2026-01-01');
    CREATE TABLE contributions (id INTEGER PRIMARY KEY, goal_id INTEGER,
        user_id INTEGER, amount_cents INTEGER, c_date TEXT DEFAULT '2026-06-01');
    CREATE TABLE bills (id INTEGER PRIMARY KEY, name TEXT, amount_cents INTEGER,
        due_day INTEGER, autopay INTEGER DEFAULT 0, notes TEXT DEFAULT '');
    """)
    conn.execute("INSERT INTO users VALUES (1,'alta','Alta','x'),(2,'charlee','Charlee','x')")
    txns = [
        ("2026-07-02", "WHOLEFDS 1234", 8250, "Groceries", 1, 1, 50),
        ("2026-07-05", "PSEG UTILITY", 14300, "Utilities", 2, 1, 50),
        ("2026-07-08", "AMAZON MKTP", 4700, "Household", 1, 1, 50),
        ("2026-06-15", "WHOLEFDS 1234", 7900, "Groceries", 2, 1, 50),
        ("2026-06-20", "CHEWY.COM", 6200, "Pets", 1, 1, 50),
        ("2026-05-12", "WHOLEFDS 1234", 8100, "Groceries", 1, 1, 50),
        ("2026-04-10", "WHOLEFDS 1234", 7800, "Groceries", 2, 1, 50),
        ("2026-07-10", "SOLO COFFEE", 600, "Dining", 1, 0, 100),
    ]
    for i, t in enumerate(txns):
        conn.execute(
            "INSERT INTO transactions (tx_date, description, amount_cents, "
            "category, paid_by, shared, split_pct, simplefin_id) "
            "VALUES (?,?,?,?,?,?,?,?)", (*t, f"sfa-{i}"))
    conn.execute("INSERT INTO goals (name, target_cents) VALUES ('Move-in cash', 600000)")
    conn.execute("INSERT INTO contributions (goal_id, user_id, amount_cents) VALUES (1,1,150000)")
    conn.execute("INSERT INTO bills (name, amount_cents, due_day, autopay) "
                 "VALUES ('Rent', 165000, 1, 0), ('Internet', 7999, 18, 1)")
    conn.commit(); conn.close()


def build_variant_b(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE,
        display_name TEXT, password_hash TEXT DEFAULT 'x');
    CREATE TABLE transactions (id INTEGER PRIMARY KEY, txn_date TEXT,
        amount_cents INTEGER, description TEXT, category TEXT DEFAULT 'Other',
        paid_by INTEGER, is_shared INTEGER DEFAULT 1,
        payer_share_pct REAL DEFAULT 50, external_id TEXT UNIQUE,
        direction TEXT NOT NULL DEFAULT 'out', income_type TEXT);
    CREATE TABLE income_rules (id INTEGER PRIMARY KEY, priority INTEGER DEFAULT 0,
        match_desc TEXT, match_account TEXT, min_cents INTEGER, max_cents INTEGER,
        set_type TEXT, set_paid_by INTEGER, enabled INTEGER DEFAULT 1,
        created_at TEXT DEFAULT '2026-07-01', hit_count INTEGER DEFAULT 0);
    CREATE TABLE goals (id INTEGER PRIMARY KEY, name TEXT, target_cents INTEGER,
        current_cents INTEGER DEFAULT 0);
    CREATE TABLE bills (id INTEGER PRIMARY KEY, name TEXT, amount_cents INTEGER,
        due_day INTEGER, autopay INTEGER DEFAULT 0);
    """)
    conn.execute("INSERT INTO users VALUES (1,'alta','Alta','x'),(2,'charlee','Charlee','x')")
    rows = [
        ("2026-07-01", 320000, "ADP PAYROLL 8842", "Other", 1, 0, 100, "in", "paycheck"),
        ("2026-07-03", 8250, "WHOLEFDS 1234", "Groceries", 1, 1, 50, "out", None),
        ("2026-07-06", 4700, "AMAZON MKTP RETURN", "Household", 1, 0, 100, "in", "refund"),
        ("2026-07-07", 20000, "TRANSFER FROM SAVINGS", "Other", 1, 0, 100, "in", "transfer"),
        ("2026-07-09", 50000, "VENMO CASHOUT", "Other", 2, 0, 100, "in", "unclassified"),
        ("2026-07-11", 9900, "AMAZON MKTP", "Household", 2, 1, 50, "out", None),
        ("2026-06-01", 320000, "ADP PAYROLL 8842", "Other", 1, 0, 100, "in", "paycheck"),
        ("2026-06-12", 14300, "PSEG UTILITY", "Utilities", 2, 1, 50, "out", None),
    ]
    for i, r in enumerate(rows):
        conn.execute(
            "INSERT INTO transactions (txn_date, amount_cents, description, "
            "category, paid_by, is_shared, payer_share_pct, direction, "
            "income_type, external_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (*r, f"sfb-{i}"))
    conn.execute("INSERT INTO income_rules (priority, match_desc, set_type, "
                 "set_paid_by, hit_count) VALUES (0,'ADP PAYROLL','paycheck',1,2)")
    conn.execute("INSERT INTO goals (name, target_cents, current_cents) "
                 "VALUES ('Move-in cash', 600000, 210000)")
    conn.execute("INSERT INTO bills (name, amount_cents, due_day, autopay) "
                 "VALUES ('Rent', 165000, 1, 0)")
    conn.commit(); conn.close()


def run(db, tests):
    os.environ["LEDGER_DB"] = db
    os.environ.pop("LEDGER_SPLIT_MODE", None)
    import importlib
    import ledger_core as core
    importlib.reload(core)

    r = subprocess.run([sys.executable, os.path.join(HERE, "migrate_agent.py")],
                       capture_output=True, text=True, env=os.environ.copy())
    check("migration runs", r.returncode == 0, r.stderr.strip()[:120])
    r2 = subprocess.run([sys.executable, os.path.join(HERE, "migrate_agent.py")],
                        capture_output=True, text=True, env=os.environ.copy())
    check("migration idempotent", r2.returncode == 0)

    conn = core.connect_ro(db)
    s = core.LedgerSchema(conn)
    tests(core, conn, s)
    conn.close()

    tok = subprocess.run(
        [sys.executable, os.path.join(HERE, "tokens_cli.py"), "create",
         "--label", "test", "--user", "alta"],
        capture_output=True, text=True, env=os.environ.copy())
    check("token create", tok.returncode == 0)
    raw = next((ln.strip() for ln in tok.stdout.splitlines()
                if ln.strip().startswith("lgr_")), "")
    check("token verify ok", core.verify_token(raw) is not None)
    check("bad token rejected", core.verify_token("lgr_nope") is None)

    ctx = core.context_set("move_target", "March 2027, $6,000", "mcp:test")
    check("context set", ctx["ok"])
    got = core.context_get(key="move_target")
    check("context get", got["found"] and "6,000" in got["entry"]["value"])

    dry = subprocess.run([sys.executable, os.path.join(HERE, "notify.py"),
                          "--dry-run"], capture_output=True, text=True,
                         env=os.environ.copy())
    check("notify dry-run", dry.returncode == 0, dry.stdout.strip().replace("\n", " | ")[:150])


def tests_a(core, conn, s):
    check("A: schema detects tx_date", s.col["date"] == "tx_date")
    check("A: no income feature", not s.has_income)
    ss = core.spending_summary(conn, s, "2026-07", 3)
    jul = ss[-1]
    check("A: july total", jul["total"]["cents"] == 8250 + 14300 + 4700 + 600,
          jul["total"]["display"])
    check("A: display format", jul["total"]["display"] == "$278.50")
    inc = core.income_summary(conn, s)
    check("A: income gated with note", inc.get("available") is False)
    sr = core.search_transactions(conn, s, query="wholefds")
    check("A: search finds 4 grocery runs", sr["total_matches"] == 4)
    gb = core.goals_and_bills(conn, s)
    check("A: goal current from contributions",
          gb["goals"][0]["current"]["cents"] == 150000)
    try:
        core.balance(conn, s)
        check("A: balance gated without config", False)
    except core.NeedsConfig:
        check("A: balance gated without config", True)
    b = core.balance(conn, s, split_mode="user1_share")
    # shared txns: 8250(p1)+14300(p2)+4700(p1)+7900(p2)+6200(p1)+8100(p1)+7800(p2) all 50%
    # u2 owes u1 half of what u1 paid shared (27250/2=13625); u1 owes half of u2's (30000/2=15000)
    check("A: balance math (user1_share)",
          b["owes"] == "Alta" and b["amount"]["cents"] == 1375,
          f"{b['owes']} owes {b['amount']['display']}")
    b2 = core.balance(conn, s, split_mode="payer_share")
    check("A: balance math (payer_share) same at 50/50",
          b2["amount"]["cents"] == 1375)


def tests_b(core, conn, s):
    check("B: schema detects txn_date", s.col["date"] == "txn_date")
    check("B: income feature detected", s.has_income and s.has_rules)
    ss = core.spending_summary(conn, s, "2026-07", 1)[0]
    check("B: outflows only + refund netted",
          ss["total"]["cents"] == (8250 + 9900) - 4700, ss["total"]["display"])
    check("B: household dipped by refund",
          ss["by_category"]["Household"]["cents"] == 9900 - 4700)
    inc = core.income_summary(conn, s, "2026-07", 1)[0]
    check("B: true_income = paycheck only",
          inc["true_income"]["cents"] == 320000, inc["true_income"]["display"])
    check("B: gross includes transfer+refund+venmo",
          inc["gross_inflows"]["cents"] == 320000 + 4700 + 20000 + 50000)
    check("B: savings rate", inc["savings_rate"] == round(
        (320000 - 13450) / 320000, 3))
    check("B: unclassified counted", inc["unclassified_count"] == 1)
    q = core.unclassified_inflows(conn, s)
    check("B: queue has the venmo row", q["count"] == 1 and
          "VENMO" in q["inflows"][0]["description"])
    rules = core.list_income_rules(conn, s)
    check("B: rules listed with hit_count",
          rules["rules"][0]["hit_count"] == 2)
    b = core.balance(conn, s, split_mode="payer_share")
    # shared out: 8250 p1, 9900 p2, 14300 p2 @50 → u2 owes 4125; u1 owes 12100
    check("B: income excluded from balance",
          b["owes"] == "Alta" and b["amount"]["cents"] == 7975,
          f"{b['owes']} owes {b['amount']['display']}")
    snap_inc = core.income_summary(conn, s)
    check("B: snapshot-safe income call", isinstance(snap_inc, list))


if __name__ == "__main__":
    os.makedirs("/tmp/lt", exist_ok=True)
    for f in ("/tmp/lt/a.db", "/tmp/lt/b.db"):
        if os.path.exists(f):
            os.remove(f)
    build_variant_a("/tmp/lt/a.db")
    build_variant_b("/tmp/lt/b.db")
    print("── variant A (deployed today) ──")
    run("/tmp/lt/a.db", tests_a)
    print("── variant B (post-income-feature) ──")
    run("/tmp/lt/b.db", tests_b)
    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {FAILED}")
        sys.exit(1)
    print("All tests passed.")
