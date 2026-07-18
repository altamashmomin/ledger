"""ledger_core — read-tier logic for the Ledger agent layer.

Pure functions over SQLite. Opens the database READ-ONLY for all financial
queries (invariant: the agent layer never writes app-owned tables). The only
writable tables are the ones this layer owns: household_context, audit_log,
api_tokens (see migrate_agent.py).

Schema-compat: the deployed transactions table exists in two historical
variants. Everything here introspects column names at startup instead of
assuming them. The who-owes-whom balance is GATED: the two split columns
(`payer_share_pct` vs `split_pct`) carry different math semantics, so the
balance is only computed once LEDGER_SPLIT_MODE is explicitly configured.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import date, datetime, timedelta

# ── connection helpers ──────────────────────────────────────────────────

def db_path() -> str:
    return os.environ.get(
        "LEDGER_DB",
        os.path.expanduser("~/financeapp/finance.db"),
    )


def connect_ro(path: str | None = None) -> sqlite3.Connection:
    p = path or db_path()
    if not os.path.exists(p):
        raise FileNotFoundError(
            f"Ledger database not found at {p}. Set LEDGER_DB to the "
            "path of finance.db."
        )
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def connect_rw(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


# ── schema introspection ────────────────────────────────────────────────

_ALIASES = {
    "date": ("tx_date", "txn_date", "date"),
    "amount": ("amount_cents",),
    "description": ("description",),
    "category": ("category",),
    "paid_by": ("paid_by",),
    "shared": ("is_shared", "shared"),
    "split": ("payer_share_pct", "split_pct"),
    "external_id": ("simplefin_id", "external_id"),
    "direction": ("direction",),
    "income_type": ("income_type",),
}


class NeedsConfig(Exception):
    """Raised when a computation requires explicit human configuration."""


class LedgerSchema:
    def __init__(self, conn: sqlite3.Connection):
        self.tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "transactions" not in self.tables:
            raise RuntimeError(
                "No `transactions` table found — is LEDGER_DB pointing at "
                "the right file?"
            )
        cols = {r[1] for r in conn.execute("PRAGMA table_info(transactions)")}
        self.col: dict[str, str | None] = {}
        for logical, candidates in _ALIASES.items():
            self.col[logical] = next((c for c in candidates if c in cols), None)
        self.has_income = bool(self.col["direction"] and self.col["income_type"])
        self.has_rules = "income_rules" in self.tables
        self.has_goals = "goals" in self.tables
        self.has_bills = "bills" in self.tables
        self.has_contributions = "contributions" in self.tables
        self.users = {}
        if "users" in self.tables:
            ucols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
            name_col = "display_name" if "display_name" in ucols else "username"
            for r in conn.execute(f"SELECT id, {name_col} AS name FROM users"):
                self.users[r["id"]] = r["name"]

    def c(self, logical: str) -> str:
        name = self.col.get(logical)
        if not name:
            raise KeyError(f"transactions has no column for '{logical}'")
        return name


# ── formatting ──────────────────────────────────────────────────────────

def fmt_cents(cents: int | None) -> str:
    if cents is None:
        return "—"
    sign = "-" if cents < 0 else ""
    c = abs(int(cents))
    return f"{sign}${c // 100:,}.{c % 100:02d}"


def money(cents: int | None) -> dict:
    return {"cents": cents, "display": fmt_cents(cents)}


# ── month math ──────────────────────────────────────────────────────────

def month_bounds(ym: str) -> tuple[str, str]:
    y, m = int(ym[:4]), int(ym[5:7])
    start = date(y, m, 1)
    nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return start.isoformat(), nxt.isoformat()


def month_list(end_ym: str | None, months_back: int) -> list[str]:
    if end_ym is None:
        end_ym = date.today().strftime("%Y-%m")
    y, m = int(end_ym[:4]), int(end_ym[5:7])
    out = []
    for _ in range(months_back):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


# ── spend / income filters ──────────────────────────────────────────────

def _outflow_where(s: LedgerSchema) -> str:
    if s.has_income:
        return f"{s.c('direction')} = 'out'"
    return "1=1"


# ── aggregates ──────────────────────────────────────────────────────────

def spending_summary(conn, s: LedgerSchema, month: str | None = None,
                     months_back: int = 1) -> list[dict]:
    """Per-month outflow totals and category breakdown, net of refunds
    when the income feature exists."""
    out = []
    prev_total = None
    for ym in month_list(month, months_back):
        lo, hi = month_bounds(ym)
        dc, ac, cc = s.c("date"), s.c("amount"), s.c("category")
        by_cat: dict[str, int] = {}
        q = (f"SELECT {cc} AS cat, SUM({ac}) AS t FROM transactions "
             f"WHERE {dc} >= ? AND {dc} < ? AND {_outflow_where(s)} "
             f"GROUP BY {cc}")
        for r in conn.execute(q, (lo, hi)):
            by_cat[r["cat"] or "Uncategorized"] = r["t"] or 0
        if s.has_income:
            rq = (f"SELECT {cc} AS cat, SUM({ac}) AS t FROM transactions "
                  f"WHERE {dc} >= ? AND {dc} < ? AND {s.c('direction')}='in' "
                  f"AND {s.c('income_type')}='refund' GROUP BY {cc}")
            for r in conn.execute(rq, (lo, hi)):
                cat = r["cat"] or "Uncategorized"
                by_cat[cat] = by_cat.get(cat, 0) - (r["t"] or 0)
        total = sum(by_cat.values())
        vs_prior = None
        if prev_total not in (None, 0):
            vs_prior = round((total - prev_total) / prev_total * 100, 1)
        out.append({
            "month": ym,
            "total": money(total),
            "by_category": {k: money(v) for k, v in
                            sorted(by_cat.items(), key=lambda kv: -kv[1])},
            "vs_prior_month_pct": vs_prior,
            "refunds_netted": s.has_income,
        })
        prev_total = total
    return out


def income_summary(conn, s: LedgerSchema, month: str | None = None,
                   months_back: int = 1) -> dict | list:
    if not s.has_income:
        return {
            "available": False,
            "note": ("The income feature (INCOME-DESIGN.md) is not built "
                     "yet — the sync currently imports outflows only. "
                     "Income aggregates will appear once direction/"
                     "income_type land in the schema."),
        }
    dc, ac = s.c("date"), s.c("amount")
    dirc, itc = s.c("direction"), s.c("income_type")
    out = []
    for ym in month_list(month, months_back):
        lo, hi = month_bounds(ym)
        def one(extra, args=()):
            r = conn.execute(
                f"SELECT COALESCE(SUM({ac}),0) FROM transactions "
                f"WHERE {dc} >= ? AND {dc} < ? AND {extra}",
                (lo, hi, *args)).fetchone()
            return r[0]
        gross = one(f"{dirc}='in'")
        true_inc = one(f"{dirc}='in' AND {itc}='paycheck'")
        spend = spending_summary(conn, s, ym, 1)[0]["total"]["cents"]
        uncls = conn.execute(
            f"SELECT COUNT(*) FROM transactions WHERE {dc} >= ? AND {dc} < ? "
            f"AND {dirc}='in' AND {itc}='unclassified'", (lo, hi)).fetchone()[0]
        net = true_inc - spend
        out.append({
            "month": ym,
            "gross_inflows": money(gross),
            "true_income": money(true_inc),
            "spend_total": money(spend),
            "net_cash_flow": money(net),
            "savings_rate": (round(net / true_inc, 3) if true_inc else None),
            "unclassified_count": uncls,
        })
    return out


def search_transactions(conn, s: LedgerSchema, query=None, date_from=None,
                        date_to=None, direction=None, income_type=None,
                        category=None, paid_by=None, limit=20, offset=0):
    dc, ac = s.c("date"), s.c("amount")
    where, args = ["1=1"], []
    if query:
        where.append(f"LOWER({s.c('description')}) LIKE ?")
        args.append(f"%{query.lower()}%")
    if date_from:
        where.append(f"{dc} >= ?"); args.append(date_from)
    if date_to:
        where.append(f"{dc} <= ?"); args.append(date_to)
    if direction and s.has_income:
        where.append(f"{s.c('direction')} = ?"); args.append(direction)
    if income_type and s.has_income:
        where.append(f"{s.c('income_type')} = ?"); args.append(income_type)
    if category:
        where.append(f"{s.c('category')} = ?"); args.append(category)
    if paid_by is not None:
        uid = next((i for i, n in s.users.items()
                    if n.lower() == str(paid_by).lower()), paid_by)
        where.append(f"{s.c('paid_by')} = ?"); args.append(uid)
    w = " AND ".join(where)
    total = conn.execute(
        f"SELECT COUNT(*) FROM transactions WHERE {w}", args).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM transactions WHERE {w} "
        f"ORDER BY {dc} DESC, id DESC LIMIT ? OFFSET ?",
        (*args, limit, offset)).fetchall()
    txns = []
    for r in rows:
        d = dict(r)
        item = {
            "id": d.get("id"),
            "date": d.get(dc),
            "description": d.get(s.c("description")),
            "amount": money(d.get(ac)),
            "category": d.get(s.c("category")),
            "paid_by": s.users.get(d.get(s.c("paid_by")),
                                   d.get(s.c("paid_by"))),
        }
        if s.has_income:
            item["direction"] = d.get(s.c("direction"))
            item["income_type"] = d.get(s.c("income_type"))
        txns.append(item)
    return {"total_matches": total, "transactions": txns,
            "has_more": offset + len(txns) < total}


def unclassified_inflows(conn, s: LedgerSchema) -> dict:
    if not s.has_income:
        return {"available": False,
                "note": "Income feature not built yet — no inflow queue."}
    dc, itc, dirc = s.c("date"), s.c("income_type"), s.c("direction")
    desc, ac = s.c("description"), s.c("amount")
    rows = conn.execute(
        f"SELECT * FROM transactions WHERE {dirc}='in' AND "
        f"{itc}='unclassified' ORDER BY {dc} DESC LIMIT 50").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        prefix = (d[desc] or "")[:12]
        hint = None
        if prefix:
            prior = conn.execute(
                f"SELECT {itc} AS t, COUNT(*) AS n FROM transactions "
                f"WHERE {dirc}='in' AND {itc} NOT IN ('unclassified') "
                f"AND {desc} LIKE ? GROUP BY {itc} ORDER BY n DESC LIMIT 1",
                (prefix + "%",)).fetchone()
            if prior:
                hint = {"suggested_type": prior["t"],
                        "based_on": f"{prior['n']} prior rows with a "
                                    f"similar description"}
        out.append({"id": d.get("id"), "date": d.get(dc),
                    "description": d.get(desc), "amount": money(d.get(ac)),
                    "hint": hint})
    return {"available": True, "count": len(out), "inflows": out}


def list_income_rules(conn, s: LedgerSchema) -> dict:
    if not s.has_rules:
        return {"available": False,
                "note": "income_rules table not created yet."}
    rows = conn.execute(
        "SELECT * FROM income_rules ORDER BY priority, id").fetchall()
    return {"available": True, "rules": [dict(r) for r in rows]}


def goals_and_bills(conn, s: LedgerSchema) -> dict:
    out: dict = {"goals": [], "bills": []}
    if s.has_goals:
        gcols = {r[1] for r in conn.execute("PRAGMA table_info(goals)")}
        for g in conn.execute("SELECT * FROM goals"):
            d = dict(g)
            current = None
            for cand in ("current_cents", "saved_cents", "balance_cents"):
                if cand in gcols:
                    current = d.get(cand)
                    break
            if current is None and s.has_contributions:
                current = conn.execute(
                    "SELECT COALESCE(SUM(amount_cents),0) FROM contributions "
                    "WHERE goal_id = ?", (d["id"],)).fetchone()[0]
            tgt = d.get("target_cents")
            out["goals"].append({
                "name": d.get("name"), "target": money(tgt),
                "current": money(current),
                "pct": (round(current / tgt * 100, 1)
                        if current is not None and tgt else None),
            })
    if s.has_bills:
        for b in conn.execute("SELECT * FROM bills ORDER BY due_day"):
            d = dict(b)
            out["bills"].append({
                "name": d.get("name"),
                "amount": money(d.get("amount_cents")),
                "due_day": d.get("due_day"),
                "autopay": bool(d.get("autopay")),
            })
    return out


# ── the gated balance ───────────────────────────────────────────────────

SPLIT_MODES = ("payer_share", "user1_share")

_SPLIT_HELP = (
    "Who-owes-whom is gated until the split semantics are confirmed, "
    "because the two schema variants disagree: set LEDGER_SPLIT_MODE to "
    "'payer_share' (the split column is the PAYER's own share %% of each "
    "shared cost) or 'user1_share' (the split column is the %% owed by "
    "user id 1 regardless of who paid). Check the column comment in your "
    "deployed schema.sql, or ask Claude to verify against the dashboard's "
    "number before trusting this."
)


def balance(conn, s: LedgerSchema, split_mode: str | None = None) -> dict:
    split_mode = split_mode or os.environ.get("LEDGER_SPLIT_MODE")
    if split_mode not in SPLIT_MODES:
        raise NeedsConfig(_SPLIT_HELP)
    if len(s.users) != 2:
        raise NeedsConfig("Balance math assumes exactly two users; found "
                          f"{len(s.users)}.")
    (u1, n1), (u2, n2) = sorted(s.users.items())
    ac, pb = s.c("amount"), s.c("paid_by")
    sh, sp = s.col["shared"], s.col["split"]
    where = f"{sh} = 1" if sh else "1=1"
    if s.has_income:
        where += f" AND {s.c('direction')} = 'out'"
    net_u2_owes_u1 = 0.0
    q = f"SELECT {ac} AS a, {pb} AS p, {sp} AS s FROM transactions WHERE {where}"
    for r in conn.execute(q):
        amt, payer, pct = r["a"] or 0, r["p"], (r["s"] if r["s"] is not None else 50)
        if payer not in (u1, u2):
            continue
        if split_mode == "payer_share":
            other_share = amt * (100 - pct) / 100.0
            net_u2_owes_u1 += other_share if payer == u1 else -other_share
        else:  # user1_share: pct = % owed by user 1
            u1_share = amt * pct / 100.0
            u2_share = amt - u1_share
            net_u2_owes_u1 += u2_share if payer == u1 else -u1_share
    cents = round(net_u2_owes_u1)
    if cents == 0:
        return {"settled": True, "note": "All square.",
                "split_mode": split_mode}
    ower, owee = (n2, n1) if cents > 0 else (n1, n2)
    return {"settled": False, "owes": ower, "owed_to": owee,
            "amount": money(abs(cents)), "split_mode": split_mode}


# ── household context + audit (agent-owned, writable) ───────────────────

def context_get(path: str | None = None, key: str | None = None) -> dict:
    conn = connect_rw(path)
    try:
        if key:
            r = conn.execute(
                "SELECT * FROM household_context WHERE key = ?",
                (key,)).fetchone()
            return {"found": bool(r), "entry": dict(r) if r else None}
        rows = conn.execute(
            "SELECT * FROM household_context ORDER BY key").fetchall()
        return {"entries": [dict(r) for r in rows]}
    finally:
        conn.close()


def context_set(key: str, value: str, actor: str,
                path: str | None = None) -> dict:
    conn = connect_rw(path)
    try:
        now = datetime.now().isoformat(timespec="seconds")
        old = conn.execute(
            "SELECT value FROM household_context WHERE key = ?",
            (key,)).fetchone()
        conn.execute(
            "INSERT INTO household_context (key, value, updated_by, updated_at) "
            "VALUES (?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
            "value=excluded.value, updated_by=excluded.updated_by, "
            "updated_at=excluded.updated_at",
            (key, value, actor, now))
        audit(conn, actor, "context_set", f"context:{key}",
              {"old": old["value"] if old else None, "new": value})
        conn.commit()
        return {"ok": True, "key": key, "value": value,
                "previous": old["value"] if old else None}
    finally:
        conn.close()


def audit(conn, actor: str, action: str, target: str, detail: dict):
    conn.execute(
        "INSERT INTO audit_log (at, actor, action, target, detail_json) "
        "VALUES (?,?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), actor, action,
         target, json.dumps(detail)))


# ── api token verification ──────────────────────────────────────────────

def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def verify_token(token: str, path: str | None = None,
                 required_scope: str = "read") -> dict | None:
    if not token:
        return None
    conn = connect_rw(path)
    try:
        r = conn.execute(
            "SELECT * FROM api_tokens WHERE token_hash = ? AND revoked = 0",
            (hash_token(token),)).fetchone()
        if not r:
            return None
        scopes = {sc.strip() for sc in (r["scopes"] or "").split(",")}
        if required_scope not in scopes:
            return None
        conn.execute(
            "UPDATE api_tokens SET last_used_at = ? WHERE id = ?",
            (datetime.now().isoformat(timespec="seconds"), r["id"]))
        conn.commit()
        return dict(r)
    finally:
        conn.close()


# ── the snapshot ────────────────────────────────────────────────────────

def snapshot(conn, s: LedgerSchema) -> dict:
    ym = date.today().strftime("%Y-%m")
    snap: dict = {"month": ym}
    snap["spending"] = spending_summary(conn, s, ym, 1)[0]
    snap["income"] = income_summary(conn, s, ym, 1)
    if isinstance(snap["income"], list):
        snap["income"] = snap["income"][0]
    try:
        snap["balance"] = balance(conn, s)
    except NeedsConfig as e:
        snap["balance"] = {"available": False, "note": str(e)}
    snap.update(goals_and_bills(conn, s))
    snap["household_context"] = context_get()["entries"]
    return snap
