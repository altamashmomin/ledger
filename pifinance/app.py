"""Pi Finance — a two-person household finance app.

Flask + SQLite, no build step. See README.md for setup.
"""
import functools
import os
import secrets
import sqlite3
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv
from flask import Flask, g, jsonify, request, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "finance.db"))

app = Flask(__name__, static_folder="static", static_url_path="")

_secret = os.environ.get("SECRET_KEY")
if not _secret:
    _secret = secrets.token_hex(32)
    app.logger.warning(
        "SECRET_KEY not set in .env — using a temporary key. "
        "Sessions will not survive a restart. Set SECRET_KEY in .env."
    )
app.secret_key = _secret
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 90,  # 90 days
    MAX_CONTENT_LENGTH=64 * 1024,
)

DEFAULT_CATEGORIES = [
    "Groceries", "Dining", "Rent", "Utilities", "Internet", "Phone",
    "Transport", "Gas", "Health", "Pets", "Household", "Entertainment",
    "Subscriptions", "Travel", "Gifts", "Other",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    display_name  TEXT NOT NULL,
    password_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id              INTEGER PRIMARY KEY,
    txn_date        TEXT NOT NULL,                 -- ISO date YYYY-MM-DD
    amount_cents    INTEGER NOT NULL CHECK (amount_cents > 0),
    description     TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT 'Other',
    paid_by         INTEGER NOT NULL REFERENCES users(id),
    is_shared       INTEGER NOT NULL DEFAULT 1,    -- 0/1
    payer_share_pct REAL NOT NULL DEFAULT 50
                    CHECK (payer_share_pct >= 0 AND payer_share_pct <= 100),
    source          TEXT NOT NULL DEFAULT 'manual', -- manual | bill | simplefin | settlement
    external_id     TEXT UNIQUE,                   -- dedupe key for automated sources
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_txn_date ON transactions(txn_date);

CREATE TABLE IF NOT EXISTS bills (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    due_day      INTEGER NOT NULL CHECK (due_day BETWEEN 1 AND 31),
    category     TEXT NOT NULL DEFAULT 'Bills',
    active       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS bill_payments (
    id      INTEGER PRIMARY KEY,
    bill_id INTEGER NOT NULL REFERENCES bills(id) ON DELETE CASCADE,
    period  TEXT NOT NULL,                          -- YYYY-MM
    paid_on TEXT NOT NULL,
    txn_id  INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
    UNIQUE (bill_id, period)
);

CREATE TABLE IF NOT EXISTS goals (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    target_cents INTEGER NOT NULL CHECK (target_cents > 0),
    target_date  TEXT,
    created_at   TEXT NOT NULL DEFAULT (date('now'))
);

CREATE TABLE IF NOT EXISTS goal_contributions (
    id           INTEGER PRIMARY KEY,
    goal_id      INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents != 0),
    c_date       TEXT NOT NULL,
    note         TEXT
);
"""


# ---------------------------------------------------------------- database

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA journal_mode = WAL")
        g.db.execute("PRAGMA busy_timeout = 5000")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    db.commit()
    db.close()


init_db()


# ---------------------------------------------------------------- helpers

def to_cents(value):
    """Parse a dollar amount (number or string) into integer cents, exactly."""
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("invalid amount")
    cents = int((d * 100).to_integral_value(rounding="ROUND_HALF_UP"))
    return cents


def dollars(cents):
    return round(cents / 100.0, 2)


def bad_request(msg):
    return jsonify({"error": msg}), 400


def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return jsonify({"error": "authentication required"}), 401
        return fn(*args, **kwargs)
    return wrapper


def get_users(db):
    return db.execute("SELECT id, username, display_name FROM users ORDER BY id").fetchall()


def parse_iso_date(s, field="date"):
    try:
        return date.fromisoformat(s).isoformat()
    except (TypeError, ValueError):
        raise ValueError(f"invalid {field} (expected YYYY-MM-DD)")


def txn_to_json(r):
    return {
        "id": r["id"],
        "date": r["txn_date"],
        "amount": dollars(r["amount_cents"]),
        "description": r["description"],
        "category": r["category"],
        "paid_by": r["paid_by"],
        "is_shared": bool(r["is_shared"]),
        "payer_share_pct": r["payer_share_pct"],
        "source": r["source"],
    }


def compute_balance(db):
    """Net balance across all shared transactions.

    For each shared transaction the payer covered 100% up front but is only
    responsible for payer_share_pct%, so the other person owes
    amount * (100 - payer_share_pct) / 100.
    Returns a dict describing who owes whom, in dollars.
    """
    users = get_users(db)
    if len(users) < 2:
        return {"settled": True, "amount": 0, "message": "Waiting for setup"}
    u1, u2 = users[0], users[1]
    net = 0  # positive => u2 owes u1 (in cents)
    rows = db.execute(
        "SELECT amount_cents, paid_by, payer_share_pct FROM transactions WHERE is_shared = 1"
    ).fetchall()
    for r in rows:
        other_owes = round(r["amount_cents"] * (100 - r["payer_share_pct"]) / 100)
        if r["paid_by"] == u1["id"]:
            net += other_owes
        elif r["paid_by"] == u2["id"]:
            net -= other_owes
    if net == 0:
        return {
            "settled": True, "amount": 0,
            "owes": None, "owed": None,
            "message": "All settled up",
            "users": [{"id": u["id"], "name": u["display_name"]} for u in (u1, u2)],
        }
    ower, owed = (u2, u1) if net > 0 else (u1, u2)
    return {
        "settled": False,
        "amount": dollars(abs(net)),
        "owes": {"id": ower["id"], "name": ower["display_name"]},
        "owed": {"id": owed["id"], "name": owed["display_name"]},
        "message": f"{ower['display_name']} owes {owed['display_name']} ${dollars(abs(net)):,.2f}",
        "users": [{"id": u["id"], "name": u["display_name"]} for u in (u1, u2)],
    }


# ---------------------------------------------------------------- auth & setup

@app.get("/api/status")
def status():
    db = get_db()
    count = db.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    out = {"setup_required": count == 0, "logged_in": "user_id" in session}
    if out["logged_in"]:
        out["user_id"] = session["user_id"]
    return jsonify(out)


@app.post("/api/setup")
def setup():
    """One-time creation of exactly two accounts. Disabled once users exist."""
    db = get_db()
    if db.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] > 0:
        return jsonify({"error": "setup already completed"}), 403
    data = request.get_json(silent=True) or {}
    users = data.get("users")
    if not isinstance(users, list) or len(users) != 2:
        return bad_request("provide exactly two users")
    seen = set()
    for u in users:
        username = (u.get("username") or "").strip().lower()
        display = (u.get("display_name") or "").strip()
        password = u.get("password") or ""
        if not username or not display:
            return bad_request("each user needs a username and display name")
        if len(password) < 8:
            return bad_request("passwords must be at least 8 characters")
        if username in seen:
            return bad_request("usernames must be different")
        seen.add(username)
    for u in users:
        db.execute(
            "INSERT INTO users (username, display_name, password_hash) VALUES (?, ?, ?)",
            (u["username"].strip().lower(), u["display_name"].strip(),
             generate_password_hash(u["password"])),
        )
    db.commit()
    return jsonify({"ok": True})


@app.post("/api/login")
def login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip().lower()
    password = data.get("password") or ""
    db = get_db()
    row = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if row is None or not check_password_hash(row["password_hash"], password):
        return jsonify({"error": "wrong username or password"}), 401
    session.permanent = True
    session["user_id"] = row["id"]
    return jsonify({"ok": True, "user": {"id": row["id"], "display_name": row["display_name"]}})


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/me")
@login_required
def me():
    db = get_db()
    users = [{"id": u["id"], "username": u["username"], "display_name": u["display_name"]}
             for u in get_users(db)]
    return jsonify({"user_id": session["user_id"], "users": users})


# ---------------------------------------------------------------- transactions

def validate_txn_payload(db, data, partial=False):
    """Returns dict of column->value for insert/update. Raises ValueError."""
    out = {}
    if "date" in data or not partial:
        out["txn_date"] = parse_iso_date(data.get("date"), "date")
    if "amount" in data or not partial:
        cents = to_cents(data.get("amount"))
        if cents <= 0:
            raise ValueError("amount must be positive")
        out["amount_cents"] = cents
    if "description" in data or not partial:
        desc = (data.get("description") or "").strip()
        if not desc:
            raise ValueError("description is required")
        out["description"] = desc[:200]
    if "category" in data or not partial:
        out["category"] = (data.get("category") or "Other").strip()[:60] or "Other"
    if "paid_by" in data or not partial:
        uid = data.get("paid_by")
        ids = {u["id"] for u in get_users(db)}
        if uid not in ids:
            raise ValueError("paid_by must be one of the two users")
        out["paid_by"] = uid
    if "is_shared" in data or not partial:
        out["is_shared"] = 1 if data.get("is_shared", True) else 0
    if "payer_share_pct" in data or not partial:
        try:
            pct = float(data.get("payer_share_pct", 50))
        except (TypeError, ValueError):
            raise ValueError("payer_share_pct must be a number")
        if not (0 <= pct <= 100):
            raise ValueError("payer_share_pct must be between 0 and 100")
        out["payer_share_pct"] = pct
    return out


@app.get("/api/transactions")
@login_required
def list_transactions():
    db = get_db()
    month = request.args.get("month")  # YYYY-MM
    q = "SELECT * FROM transactions"
    params = []
    if month:
        q += " WHERE substr(txn_date, 1, 7) = ?"
        params.append(month)
    q += " ORDER BY txn_date DESC, id DESC LIMIT 500"
    rows = db.execute(q, params).fetchall()
    return jsonify([txn_to_json(r) for r in rows])


@app.post("/api/transactions")
@login_required
def create_transaction():
    db = get_db()
    data = request.get_json(silent=True) or {}
    try:
        cols = validate_txn_payload(db, data)
    except ValueError as e:
        return bad_request(str(e))
    cols["source"] = "settlement" if data.get("source") == "settlement" else "manual"
    keys = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    cur = db.execute(f"INSERT INTO transactions ({keys}) VALUES ({marks})", list(cols.values()))
    db.commit()
    row = db.execute("SELECT * FROM transactions WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(txn_to_json(row)), 201


@app.put("/api/transactions/<int:txn_id>")
@login_required
def update_transaction(txn_id):
    db = get_db()
    if db.execute("SELECT id FROM transactions WHERE id = ?", (txn_id,)).fetchone() is None:
        return jsonify({"error": "not found"}), 404
    data = request.get_json(silent=True) or {}
    try:
        cols = validate_txn_payload(db, data, partial=True)
    except ValueError as e:
        return bad_request(str(e))
    if not cols:
        return bad_request("nothing to update")
    sets = ", ".join(f"{k} = ?" for k in cols)
    db.execute(f"UPDATE transactions SET {sets} WHERE id = ?", [*cols.values(), txn_id])
    db.commit()
    row = db.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    return jsonify(txn_to_json(row))


@app.delete("/api/transactions/<int:txn_id>")
@login_required
def delete_transaction(txn_id):
    db = get_db()
    cur = db.execute("DELETE FROM transactions WHERE id = ?", (txn_id,))
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


@app.get("/api/categories")
@login_required
def categories():
    db = get_db()
    used = [r["category"] for r in db.execute(
        "SELECT DISTINCT category FROM transactions ORDER BY category")]
    merged = list(dict.fromkeys(DEFAULT_CATEGORIES + used))
    return jsonify(merged)


@app.get("/api/balance")
@login_required
def balance():
    return jsonify(compute_balance(get_db()))


# ---------------------------------------------------------------- bills

def bill_to_json(db, r, period):
    payment = db.execute(
        "SELECT * FROM bill_payments WHERE bill_id = ? AND period = ?", (r["id"], period)
    ).fetchone()
    return {
        "id": r["id"],
        "name": r["name"],
        "amount": dollars(r["amount_cents"]),
        "due_day": r["due_day"],
        "category": r["category"],
        "paid_this_period": payment is not None,
        "paid_on": payment["paid_on"] if payment else None,
        "period": period,
    }


def current_period():
    return date.today().strftime("%Y-%m")


@app.get("/api/bills")
@login_required
def list_bills():
    db = get_db()
    period = request.args.get("period") or current_period()
    rows = db.execute("SELECT * FROM bills WHERE active = 1 ORDER BY due_day, name").fetchall()
    return jsonify([bill_to_json(db, r, period) for r in rows])


@app.post("/api/bills")
@login_required
def create_bill():
    db = get_db()
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return bad_request("name is required")
    try:
        cents = to_cents(data.get("amount"))
        due_day = int(data.get("due_day"))
    except (ValueError, TypeError):
        return bad_request("invalid amount or due day")
    if cents <= 0:
        return bad_request("amount must be positive")
    if not (1 <= due_day <= 31):
        return bad_request("due day must be between 1 and 31")
    category = (data.get("category") or "Bills").strip()[:60] or "Bills"
    cur = db.execute(
        "INSERT INTO bills (name, amount_cents, due_day, category) VALUES (?, ?, ?, ?)",
        (name[:100], cents, due_day, category),
    )
    db.commit()
    row = db.execute("SELECT * FROM bills WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(bill_to_json(db, row, current_period())), 201


@app.put("/api/bills/<int:bill_id>")
@login_required
def update_bill(bill_id):
    db = get_db()
    row = db.execute("SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()
    if row is None:
        return jsonify({"error": "not found"}), 404
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or row["name"]).strip()[:100]
    category = (data.get("category") or row["category"]).strip()[:60]
    try:
        cents = to_cents(data["amount"]) if "amount" in data else row["amount_cents"]
        due_day = int(data.get("due_day", row["due_day"]))
    except (ValueError, TypeError):
        return bad_request("invalid amount or due day")
    if cents <= 0 or not (1 <= due_day <= 31):
        return bad_request("invalid amount or due day")
    db.execute(
        "UPDATE bills SET name = ?, amount_cents = ?, due_day = ?, category = ? WHERE id = ?",
        (name, cents, due_day, category, bill_id),
    )
    db.commit()
    row = db.execute("SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()
    return jsonify(bill_to_json(db, row, current_period()))


@app.delete("/api/bills/<int:bill_id>")
@login_required
def delete_bill(bill_id):
    db = get_db()
    cur = db.execute("UPDATE bills SET active = 0 WHERE id = ? AND active = 1", (bill_id,))
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


@app.post("/api/bills/<int:bill_id>/pay")
@login_required
def pay_bill(bill_id):
    """Mark a bill paid for a period and log it as a transaction."""
    db = get_db()
    bill = db.execute("SELECT * FROM bills WHERE id = ? AND active = 1", (bill_id,)).fetchone()
    if bill is None:
        return jsonify({"error": "not found"}), 404
    data = request.get_json(silent=True) or {}
    period = data.get("period") or current_period()
    if len(period) != 7 or period[4] != "-":
        return bad_request("period must be YYYY-MM")
    if db.execute("SELECT id FROM bill_payments WHERE bill_id = ? AND period = ?",
                  (bill_id, period)).fetchone():
        return bad_request("already marked paid for this period")
    paid_by = data.get("paid_by", session["user_id"])
    if paid_by not in {u["id"] for u in get_users(db)}:
        return bad_request("paid_by must be one of the two users")
    is_shared = 1 if data.get("is_shared", True) else 0
    try:
        pct = float(data.get("payer_share_pct", 50))
    except (TypeError, ValueError):
        return bad_request("payer_share_pct must be a number")
    if not (0 <= pct <= 100):
        return bad_request("payer_share_pct must be between 0 and 100")
    today = date.today().isoformat()
    cur = db.execute(
        """INSERT INTO transactions
           (txn_date, amount_cents, description, category, paid_by,
            is_shared, payer_share_pct, source)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'bill')""",
        (today, bill["amount_cents"], f"{bill['name']} ({period})",
         bill["category"], paid_by, is_shared, pct),
    )
    db.execute(
        "INSERT INTO bill_payments (bill_id, period, paid_on, txn_id) VALUES (?, ?, ?, ?)",
        (bill_id, period, today, cur.lastrowid),
    )
    db.commit()
    return jsonify(bill_to_json(db, bill, period)), 201


@app.delete("/api/bills/<int:bill_id>/pay")
@login_required
def unpay_bill(bill_id):
    """Undo a payment for a period; removes the linked transaction too."""
    db = get_db()
    period = request.args.get("period") or current_period()
    payment = db.execute(
        "SELECT * FROM bill_payments WHERE bill_id = ? AND period = ?", (bill_id, period)
    ).fetchone()
    if payment is None:
        return jsonify({"error": "no payment for this period"}), 404
    if payment["txn_id"]:
        db.execute("DELETE FROM transactions WHERE id = ?", (payment["txn_id"],))
    db.execute("DELETE FROM bill_payments WHERE id = ?", (payment["id"],))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- goals

def goal_to_json(db, r):
    saved = db.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) AS s FROM goal_contributions WHERE goal_id = ?",
        (r["id"],),
    ).fetchone()["s"]
    return {
        "id": r["id"],
        "name": r["name"],
        "target": dollars(r["target_cents"]),
        "target_date": r["target_date"],
        "saved": dollars(saved),
        "progress": min(1.0, saved / r["target_cents"]) if r["target_cents"] else 0,
    }


@app.get("/api/goals")
@login_required
def list_goals():
    db = get_db()
    rows = db.execute("SELECT * FROM goals ORDER BY created_at, id").fetchall()
    return jsonify([goal_to_json(db, r) for r in rows])


@app.post("/api/goals")
@login_required
def create_goal():
    db = get_db()
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return bad_request("name is required")
    try:
        cents = to_cents(data.get("target"))
    except ValueError:
        return bad_request("invalid target amount")
    if cents <= 0:
        return bad_request("target must be positive")
    target_date = None
    if data.get("target_date"):
        try:
            target_date = parse_iso_date(data["target_date"], "target date")
        except ValueError as e:
            return bad_request(str(e))
    cur = db.execute(
        "INSERT INTO goals (name, target_cents, target_date) VALUES (?, ?, ?)",
        (name[:100], cents, target_date),
    )
    db.commit()
    row = db.execute("SELECT * FROM goals WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(goal_to_json(db, row)), 201


@app.delete("/api/goals/<int:goal_id>")
@login_required
def delete_goal(goal_id):
    db = get_db()
    cur = db.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


@app.post("/api/goals/<int:goal_id>/contribute")
@login_required
def contribute(goal_id):
    db = get_db()
    goal = db.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone()
    if goal is None:
        return jsonify({"error": "not found"}), 404
    data = request.get_json(silent=True) or {}
    try:
        cents = to_cents(data.get("amount"))
    except ValueError:
        return bad_request("invalid amount")
    if cents == 0:
        return bad_request("amount cannot be zero")
    note = (data.get("note") or "").strip()[:200] or None
    db.execute(
        """INSERT INTO goal_contributions (goal_id, user_id, amount_cents, c_date, note)
           VALUES (?, ?, ?, ?, ?)""",
        (goal_id, session["user_id"], cents, date.today().isoformat(), note),
    )
    db.commit()
    return jsonify(goal_to_json(db, goal)), 201


@app.get("/api/goals/<int:goal_id>/contributions")
@login_required
def contributions(goal_id):
    db = get_db()
    rows = db.execute(
        """SELECT gc.*, u.display_name FROM goal_contributions gc
           JOIN users u ON u.id = gc.user_id
           WHERE gc.goal_id = ? ORDER BY gc.c_date DESC, gc.id DESC""",
        (goal_id,),
    ).fetchall()
    return jsonify([
        {"id": r["id"], "amount": dollars(r["amount_cents"]), "date": r["c_date"],
         "by": r["display_name"], "note": r["note"]}
        for r in rows
    ])


# ---------------------------------------------------------------- dashboard

@app.get("/api/dashboard")
@login_required
def dashboard():
    db = get_db()
    month = request.args.get("month") or current_period()
    spend_rows = db.execute(
        """SELECT category, SUM(amount_cents) AS total FROM transactions
           WHERE substr(txn_date, 1, 7) = ? AND source != 'settlement'
           GROUP BY category ORDER BY total DESC""",
        (month,),
    ).fetchall()
    total = sum(r["total"] for r in spend_rows)
    bills = db.execute("SELECT * FROM bills WHERE active = 1 ORDER BY due_day").fetchall()
    upcoming = [bill_to_json(db, b, month) for b in bills]
    unpaid = [b for b in upcoming if not b["paid_this_period"]]
    goals = [goal_to_json(db, r) for r in
             db.execute("SELECT * FROM goals ORDER BY created_at, id").fetchall()]
    recent = [txn_to_json(r) for r in db.execute(
        "SELECT * FROM transactions ORDER BY txn_date DESC, id DESC LIMIT 6").fetchall()]
    return jsonify({
        "month": month,
        "month_total": dollars(total),
        "by_category": [
            {"category": r["category"], "amount": dollars(r["total"])} for r in spend_rows
        ],
        "balance": compute_balance(db),
        "unpaid_bills": unpaid,
        "goals": goals,
        "recent": recent,
    })


# ---------------------------------------------------------------- static

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=False)
