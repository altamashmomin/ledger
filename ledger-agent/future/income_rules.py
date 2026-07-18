"""income_rules — the classification engine from INCOME-DESIGN.md.

Pure logic, importable by BOTH consumers so rules behave identically
everywhere (the design's core requirement):

  - simplefin_sync.py, at the moment an inflow row is inserted
  - the Flask API, for POST /api/income/rules/apply and rule previews

Semantics, exactly as specified:
  rules run in priority order (lower first), ties by id; first ENABLED
  match wins; no match -> 'unclassified'. A rule matches when ALL of its
  non-NULL conditions hold: match_desc as case-insensitive substring,
  match_account by exact SimpleFIN account id, min/max_cents inclusive.
  A match sets income_type, bumps hit_count, and applies set_paid_by as
  the money's OWNER when present.

Nothing here writes share fields — income never touches the shared
balance (invariant 1).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

VALID_TYPES = ("paycheck", "reimbursement", "refund", "transfer",
               "gift", "other")


@dataclass(frozen=True)
class Rule:
    id: int
    priority: int
    match_desc: Optional[str]
    match_account: Optional[str]
    min_cents: Optional[int]
    max_cents: Optional[int]
    set_type: str
    set_paid_by: Optional[int]
    enabled: bool = True

    @classmethod
    def from_row(cls, r) -> "Rule":
        d = dict(r)
        return cls(id=d["id"], priority=d["priority"],
                   match_desc=d.get("match_desc"),
                   match_account=d.get("match_account"),
                   min_cents=d.get("min_cents"), max_cents=d.get("max_cents"),
                   set_type=d["set_type"], set_paid_by=d.get("set_paid_by"),
                   enabled=bool(d.get("enabled", 1)))


def rule_matches(rule: Rule, description: str | None,
                 account_id: str | None, amount_cents: int) -> bool:
    if not rule.enabled:
        return False
    if rule.match_desc is not None:
        if rule.match_desc.lower() not in (description or "").lower():
            return False
    if rule.match_account is not None:
        if rule.match_account != account_id:
            return False
    if rule.min_cents is not None and amount_cents < rule.min_cents:
        return False
    if rule.max_cents is not None and amount_cents > rule.max_cents:
        return False
    return True


def load_rules(conn: sqlite3.Connection) -> list[Rule]:
    conn.row_factory = sqlite3.Row
    return [Rule.from_row(r) for r in conn.execute(
        "SELECT * FROM income_rules ORDER BY priority, id")]


def classify(rules: list[Rule], description: str | None,
             account_id: str | None, amount_cents: int) -> Rule | None:
    """First enabled match in priority order, or None -> 'unclassified'."""
    for rule in rules:
        if rule_matches(rule, description, account_id, amount_cents):
            return rule
    return None


def preview_rule(conn, candidate: Rule, desc_col: str,
                 account_col: str | None) -> dict:
    """Dry-run a candidate against current UNCLASSIFIED inflows — the
    preview shown to the human in the two-phase confirm flow. Also lists
    existing rules that already match the same rows (conflicts)."""
    acct_sel = f", {account_col} AS acct" if account_col else ", NULL AS acct"
    rows = conn.execute(
        f"SELECT id, {desc_col} AS d, amount_cents AS a{acct_sel} "
        f"FROM transactions WHERE direction='in' "
        f"AND income_type='unclassified'").fetchall()
    existing = load_rules(conn)
    hits, conflicts = [], set()
    for r in rows:
        if rule_matches(candidate, r["d"], r["acct"], r["a"]):
            hits.append({"id": r["id"], "description": r["d"],
                         "amount_cents": r["a"]})
            prior = classify(existing, r["d"], r["acct"], r["a"])
            if prior is not None:
                conflicts.add(prior.id)
    return {"would_match_now": len(hits), "sample_rows": hits[:5],
            "conflicting_rule_ids": sorted(conflicts)}


def apply_rules(conn: sqlite3.Connection, desc_col: str,
                account_col: str | None, paid_by_col: str,
                dry_run: bool = False) -> dict:
    """Run all enabled rules over the unclassified queue. Returns
    {rows_affected, by_rule}. dry_run=True computes without writing —
    the preview for ledger_apply_rules."""
    rules = load_rules(conn)
    acct_sel = f", {account_col} AS acct" if account_col else ", NULL AS acct"
    rows = conn.execute(
        f"SELECT id, {desc_col} AS d, amount_cents AS a, "
        f"{paid_by_col} AS pb{acct_sel} FROM transactions "
        f"WHERE direction='in' AND income_type='unclassified'").fetchall()
    by_rule: dict[int, int] = {}
    affected = 0
    for r in rows:
        rule = classify(rules, r["d"], r["acct"], r["a"])
        if rule is None:
            continue
        affected += 1
        by_rule[rule.id] = by_rule.get(rule.id, 0) + 1
        if not dry_run:
            sets, args = ["income_type = ?"], [rule.set_type]
            if rule.set_paid_by is not None:
                sets.append(f"{paid_by_col} = ?")
                args.append(rule.set_paid_by)
            conn.execute(
                f"UPDATE transactions SET {', '.join(sets)} WHERE id = ?",
                (*args, r["id"]))
    if not dry_run:
        for rid, n in by_rule.items():
            conn.execute(
                "UPDATE income_rules SET hit_count = hit_count + ? "
                "WHERE id = ?", (n, rid))
        conn.commit()
    return {"rows_affected": affected, "by_rule": by_rule,
            "dry_run": dry_run}


def classify_new_inflow(conn: sqlite3.Connection, description: str | None,
                        account_id: str | None, amount_cents: int,
                        bump_hit: bool = True) -> tuple[str, Optional[int]]:
    """For the sync insert path: returns (income_type, owner_override).
    Call at insert time for each incoming inflow; the caller writes the
    row (with shared=0 and no split — invariant 1) using the returned
    type, falling back to the account's SYNC_PAID_BY when owner is None.
    """
    rule = classify(load_rules(conn), description, account_id, amount_cents)
    if rule is None:
        return "unclassified", None
    if bump_hit:
        conn.execute("UPDATE income_rules SET hit_count = hit_count + 1 "
                     "WHERE id = ?", (rule.id,))
    return rule.set_type, rule.set_paid_by
