"""notify — post-sync alert digest for Ledger, pushed via ntfy.

The proactive layer (Era's "Agency", scaled to a Pi): runs after each
SimpleFIN sync via ExecStartPost, checks four things, and pushes ONE
digest message only when something is worth saying. Silence is a feature.

Checks:
  1. bills due within LEDGER_ALERT_BILL_DAYS (default 3) without autopay
  2. transactions needing review (paid_by unset), if the schema allows it
  3. unclassified inflows (once the income feature exists)
  4. any category's month-to-date spend > 130% of its prior-3-month
     average (only when MTD also exceeds $50 — no noise about $6 blips)

Env:  NTFY_TOPIC (required), NTFY_SERVER (default https://ntfy.sh),
      LEDGER_DB, LEDGER_ALERT_BILL_DAYS
Test: python3 notify.py --dry-run
"""
from __future__ import annotations

import calendar
import os
import sys
import urllib.request
from datetime import date

import ledger_core as core


def bills_due_soon(conn, s, days: int) -> list[str]:
    if not s.has_bills:
        return []
    today = date.today()
    dim = calendar.monthrange(today.year, today.month)[1]
    out = []
    for b in conn.execute(
            "SELECT name, amount_cents, due_day, autopay FROM bills "
            "WHERE autopay = 0"):
        due_day = min(b["due_day"], dim)
        delta = due_day - today.day
        if delta < 0:  # wraps to next month
            nxt = calendar.monthrange(
                today.year + (today.month == 12),
                1 if today.month == 12 else today.month + 1)[1]
            delta = (dim - today.day) + min(b["due_day"], nxt)
        if 0 <= delta <= days:
            when = "today" if delta == 0 else (
                "tomorrow" if delta == 1 else f"in {delta} days")
            out.append(f"{b['name']} ({core.fmt_cents(b['amount_cents'])}) "
                       f"due {when}, no autopay")
    return out


def needs_review(conn, s) -> list[str]:
    pb = s.col.get("paid_by")
    if not pb:
        return []
    n = conn.execute(
        f"SELECT COUNT(*) FROM transactions WHERE {pb} IS NULL").fetchone()[0]
    return [f"{n} transaction(s) need review (no owner assigned)"] if n else []


def unclassified(conn, s) -> list[str]:
    if not s.has_income:
        return []
    q = core.unclassified_inflows(conn, s)
    n = q.get("count", 0)
    return [f"{n} inflow(s) waiting to be tagged"] if n else []


def category_surge(conn, s) -> list[str]:
    today = date.today()
    ym = today.strftime("%Y-%m")
    months = core.month_list(ym, 4)          # 3 prior full months + current
    prior, current = months[:-1], months[-1]
    cur = core.spending_summary(conn, s, current, 1)[0]["by_category"]
    hist: dict[str, list[int]] = {}
    for m in prior:
        for cat, v in core.spending_summary(conn, s, m, 1)[0][
                "by_category"].items():
            hist.setdefault(cat, []).append(v["cents"])
    out = []
    for cat, v in cur.items():
        mtd = v["cents"]
        past = hist.get(cat)
        if not past or mtd < 5000:
            continue
        avg = sum(past) / len(past)
        if avg > 0 and mtd > avg * 1.3:
            out.append(f"{cat} at {core.fmt_cents(mtd)} MTD — "
                       f"{round(mtd / avg * 100)}% of its 3-month average")
    return out


def build_digest() -> str | None:
    conn = core.connect_ro()
    try:
        s = core.LedgerSchema(conn)
        days = int(os.environ.get("LEDGER_ALERT_BILL_DAYS", "3"))
        lines = (bills_due_soon(conn, s, days) + unclassified(conn, s)
                 + needs_review(conn, s) + category_surge(conn, s))
        if not lines:
            return None
        return "\n".join("• " + ln for ln in lines)
    finally:
        conn.close()


def send(msg: str) -> None:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("NTFY_TOPIC not set — printing instead:\n" + msg,
              file=sys.stderr)
        return
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    req = urllib.request.Request(
        f"{server}/{topic}", data=msg.encode(),
        headers={"Title": "Ledger", "Tags": "ledger,moneybag"})
    urllib.request.urlopen(req, timeout=10)


def main() -> int:
    digest = build_digest()
    if digest is None:
        print("Nothing to report.")
        return 0
    if "--dry-run" in sys.argv:
        print(digest)
        return 0
    send(digest)
    print("Sent:\n" + digest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
