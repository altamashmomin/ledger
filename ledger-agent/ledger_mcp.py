"""ledger_mcp — read-tier MCP server for Ledger (AGENT-DESIGN build step 2).

Reads the SQLite file directly, READ-ONLY, with schema introspection. The
only writes it performs are to agent-owned tables (household_context +
audit_log). App-owned data has exactly one write path: the Flask API. The
write tier (classify, rules, two-phase confirm) ships with the income
feature and will wrap Flask endpoints, per AGENT-DESIGN.md.

Run:
  HTTP (Tailscale):  LEDGER_DB=... LEDGER_MCP_PORT=8091 python3 ledger_mcp.py
                     Requires Authorization: Bearer <token from tokens_cli.py>
  stdio (local dev): python3 ledger_mcp.py --stdio   (no auth — local trust)
"""
from __future__ import annotations

import json
import os
import sys
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

import ledger_core as core

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print("Missing dependency: pip install 'mcp[cli]' uvicorn",
          file=sys.stderr)
    raise

mcp = FastMCP("ledger_mcp")

ACTOR = "mcp:" + os.environ.get("LEDGER_MCP_LABEL", "agent")


def _dump(obj) -> str:
    return json.dumps(obj, indent=1, default=str)


def _with_schema(fn):
    conn = core.connect_ro()
    try:
        return fn(conn, core.LedgerSchema(conn))
    finally:
        conn.close()


# ═════════════════════════════ READ TIER ═══════════════════════════════

@mcp.tool(
    name="ledger_get_household_snapshot",
    annotations={"title": "Household snapshot", "readOnlyHint": True,
                 "idempotentHint": True, "openWorldHint": False},
)
def ledger_get_household_snapshot() -> str:
    """One-call overview of the household's current month. START HERE for
    any open-ended question ("how are we doing?", "can we afford X?").

    Returns JSON with this month's spending (total + by category, net of
    refunds once the income feature exists), income aggregates (or a note
    that the income feature isn't built yet), the who-owes-whom balance
    (or the config it still needs), goals, upcoming bills, and the
    household_context notes both partners' agents share.

    Every money field appears as {"cents": int, "display": "$1,234.56"}.
    Quote the display strings verbatim. NEVER compute your own totals from
    transaction lists — every number you need exists in a summary tool,
    computed by the same logic each time. If income shows
    unclassified_count > 0, mention it and offer the tagging workflow
    (ledger_get_unclassified_inflows).
    """
    return _dump(_with_schema(lambda c, s: core.snapshot(c, s)))


class MonthsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    month: Optional[str] = Field(
        None, description="ISO month 'YYYY-MM'. Omit for the current month.",
        pattern=r"^\d{4}-\d{2}$")
    months_back: int = Field(
        1, ge=1, le=24,
        description="How many months to return, ending at `month`. "
                    "Use 3-12 for trend questions.")


@mcp.tool(
    name="ledger_get_spending_summary",
    annotations={"title": "Spending by month and category",
                 "readOnlyHint": True, "idempotentHint": True,
                 "openWorldHint": False},
)
def ledger_get_spending_summary(params: MonthsInput) -> str:
    """Monthly spending totals and per-category breakdown — OUTFLOWS ONLY.
    Once the income feature exists, refunds net against their category
    (a category can dip when a return lands in a later month than its
    purchase; that is correct behavior, not a bug — say so if asked).

    Returns JSON [{month, total, by_category, vs_prior_month_pct}].
    Income never appears here; for money in, use ledger_get_income_summary.
    """
    return _dump(_with_schema(
        lambda c, s: core.spending_summary(c, s, params.month,
                                           params.months_back)))


@mcp.tool(
    name="ledger_get_income_summary",
    annotations={"title": "Income summary", "readOnlyHint": True,
                 "idempotentHint": True, "openWorldHint": False},
)
def ledger_get_income_summary(params: MonthsInput) -> str:
    """Income aggregates per month. The vocabulary matters — use it:
      gross_inflows = every deposit (paychecks + refunds + transfers + ...)
      true_income   = paycheck rows ONLY. When the user says "income",
                      they mean this. Never present gross_inflows as
                      income — refunds and transfers are not earnings.
      net_cash_flow = true_income - spending
      savings_rate  = net_cash_flow / true_income (null when income is 0)
    If unclassified_count > 0, the numbers are provisional — say so and
    offer to tag. If the response says the income feature isn't built,
    relay that honestly instead of estimating income from other data.
    """
    return _dump(_with_schema(
        lambda c, s: core.income_summary(c, s, params.month,
                                         params.months_back)))


class SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: Optional[str] = Field(None, max_length=80,
        description="Case-insensitive substring on description, "
                    "e.g. 'amazon'.")
    date_from: Optional[str] = Field(None, description="ISO date, inclusive.")
    date_to: Optional[str] = Field(None, description="ISO date, inclusive.")
    direction: Optional[Literal["in", "out"]] = None
    income_type: Optional[Literal["paycheck", "reimbursement", "refund",
        "transfer", "gift", "other", "unclassified"]] = None
    category: Optional[str] = None
    paid_by: Optional[str] = Field(None,
        description="Username or display name. For inflows this means "
                    "the money's OWNER.")
    limit: int = Field(20, ge=1, le=100)
    offset: int = Field(0, ge=0)


@mcp.tool(
    name="ledger_search_transactions",
    annotations={"title": "Search transactions", "readOnlyHint": True,
                 "idempotentHint": True, "openWorldHint": False},
)
def ledger_search_transactions(params: SearchInput) -> str:
    """Find specific transactions — for EVIDENCE ("show me the three
    biggest grocery runs", "did the deposit land?"), never for totals.
    Totals come from the summary tools, always.

    Returns {total_matches, transactions, has_more}. Paginated: if
    has_more is true and the question needs the full set, page with
    offset — don't extrapolate from a partial page.
    """
    return _dump(_with_schema(
        lambda c, s: core.search_transactions(c, s, **params.model_dump())))


@mcp.tool(
    name="ledger_get_unclassified_inflows",
    annotations={"title": "Inflow tagging queue", "readOnlyHint": True,
                 "idempotentHint": True, "openWorldHint": False},
)
def ledger_get_unclassified_inflows() -> str:
    """The tagging queue: inflows awaiting an income_type, each with a
    hint when prior classifications of similar descriptions exist.

    Workflow: present each row with a suggested type AND the reason; let
    the user confirm or correct. Classification itself is a write — until
    the write tier ships, direct the user to tag in the app, and offer to
    remember recurring patterns in household_context so the future rule
    is one step away. Never state a type as fact without the user's
    confirmation.
    """
    return _dump(_with_schema(lambda c, s: core.unclassified_inflows(c, s)))


@mcp.tool(
    name="ledger_list_income_rules",
    annotations={"title": "List income rules", "readOnlyHint": True,
                 "idempotentHint": True, "openWorldHint": False},
)
def ledger_list_income_rules() -> str:
    """Classification rules in priority order (lower runs first, first
    match wins), with hit_count. A rule with hit_count 0 after a month is
    probably dead — worth mentioning. Check this before discussing any
    new rule so overlaps are surfaced, not silently created."""
    return _dump(_with_schema(lambda c, s: core.list_income_rules(c, s)))


@mcp.tool(
    name="ledger_get_balance",
    annotations={"title": "Who owes whom", "readOnlyHint": True,
                 "idempotentHint": True, "openWorldHint": False},
)
def ledger_get_balance() -> str:
    """The settle-up number, computed from shared spending splits. Income
    NEVER affects this — a paycheck belongs to its earner and carries no
    share math.

    GATED: if the response says LEDGER_SPLIT_MODE is unset, relay the
    explanation to the user verbatim and do not guess a number. After
    it's first configured, ask the user to verify one balance against
    the app's dashboard before treating it as truth. Recording a
    settlement happens in the app, not through me.
    """
    def run(c, s):
        try:
            return core.balance(c, s)
        except core.NeedsConfig as e:
            return {"available": False, "needs_config": str(e)}
    return _dump(_with_schema(run))


@mcp.tool(
    name="ledger_list_goals_and_bills",
    annotations={"title": "Goals and bills", "readOnlyHint": True,
                 "idempotentHint": True, "openWorldHint": False},
)
def ledger_list_goals_and_bills() -> str:
    """Savings goals (target, current, percent) and recurring bills
    (amount, due day of month, autopay flag). Read-only — goal and bill
    management lives in the app."""
    return _dump(_with_schema(lambda c, s: core.goals_and_bills(c, s)))


# ═══════════════ AGENT-OWNED WRITES (household context) ═════════════════

class ContextGetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: Optional[str] = Field(None,
        description="Specific key, or omit to list everything.")


@mcp.tool(
    name="ledger_get_household_context",
    annotations={"title": "Household context notes", "readOnlyHint": True,
                 "idempotentHint": True, "openWorldHint": False},
)
def ledger_get_household_context(params: ContextGetInput) -> str:
    """Shared household memory: durable facts and agreements both
    partners' agents can see ('move_target: March 2027, $6,000 move-in
    cash', 'grocery_budget: $650/mo', 'vet bills count as Household').
    Check this before answering planning questions — an agreement noted
    here outranks your assumptions."""
    return _dump(core.context_get(key=params.key))


class ContextSetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(..., min_length=1, max_length=60,
        description="snake_case key, e.g. 'move_target'.")
    value: str = Field(..., min_length=1, max_length=500)


@mcp.tool(
    name="ledger_set_household_context",
    annotations={"title": "Save a household note", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
def ledger_set_household_context(params: ContextSetInput) -> str:
    """Save or update one shared household note, ONLY after the user has
    stated the fact or agreed it should be remembered. This is shared
    memory — the other partner's agent will see it, so never store
    anything one partner asked to keep private. Overwrites are logged
    with the previous value in the audit trail; report the previous
    value back when one existed."""
    return _dump(core.context_set(params.key, params.value, ACTOR))


# ═════════════════════════════ transport ═══════════════════════════════

class BearerAuthMiddleware:
    """ASGI wrapper: every HTTP request needs a valid, unrevoked token
    from api_tokens (see tokens_cli.py)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode()
                   for k, v in scope.get("headers", [])}
        token = headers.get("authorization", "").removeprefix("Bearer ").strip()
        tok = core.verify_token(token)
        if not tok:
            body = json.dumps({"error": "unauthorized",
                               "hint": "Create a token with tokens_cli.py "
                                       "and send it as Authorization: "
                                       "Bearer <token>."}).encode()
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        return await self.app(scope, receive, send)


def main() -> None:
    if "--stdio" in sys.argv:
        mcp.run(transport="stdio")
        return
    import uvicorn
    host = os.environ.get("LEDGER_MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("LEDGER_MCP_PORT", "8091"))
    app = BearerAuthMiddleware(mcp.streamable_http_app())
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
