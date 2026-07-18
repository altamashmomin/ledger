# Design: The Agent Layer (Ledger MCP)

Giving Ledger the Era architecture: a deterministic financial core with a
curated MCP tool surface on top, so Claude (or any MCP client) becomes the
conversational interface — no chatbot embedded in the app.
Status: **design only, not built.**

## The core claim

Ledger already made the hard architectural decisions without knowing it.
The math is computed server-side on read. The rules live in the database,
not the sync script. Every surface consumes the same API. That *is* the
Era pattern — what's missing is ~500 lines of FastMCP wrapper and two
small tables.

The danger is also specific: an agent with write access to a finance
database can quietly poison it — a hallucinated rule that tags transfers
as paychecks corrupts `true_income` and every scenario built on it. So the
design is really a **permission and confirmation system** with tools
attached: reads are free, writes are tiered by blast radius, and the
dangerous operations simply don't exist as tools.

## The invariants (not up for debate)

1. **The agent never does math.** Every aggregate — balance, spend totals,
   `true_income`, savings rate — comes from a tool that runs the same code
   the dashboard runs. Tools return computed numbers; docstrings forbid
   the agent from summing rows itself. One source of truth, no
   hallucinated arithmetic.
2. **One write path.** The MCP server is an HTTP client of the Flask API,
   not a second process opening the SQLite file. Every write funnels
   through the exact endpoints the UI uses — same validation, same
   invariants, and INCOME-DESIGN's rules (income never touches the shared
   balance, refund netting, etc.) can't be bypassed from the side door.
3. **Dangerous tools don't exist.** No tool edits amounts, deletes
   transactions, touches share math, or records settle-ups. Era's "your
   agent cannot exceed the access you define" — enforced by omission, the
   cheapest possible ACL.
4. **Every write is previewed, approved, and logged.** High-blast-radius
   writes are two-phase (propose → human approval → confirm). Every
   executed write lands in an audit table with what, when, and via which
   token.

---

## Schema: where we are

### Shipped (running on the Pi)

Column names below follow the conventions in INCOME-DESIGN.md; where the
deployed `schema.sql` spells things differently (`tx_date` vs `txn_date`,
`simplefin_id` vs `external_id`, `split_pct` vs `payer_share_pct`), the
deployed spelling wins. Roles are what matter here.

```
users            two people, session auth
transactions     spend rows only (sync skips deposits today):
                 date, description, amount_cents (positive = spent),
                 category, paid_by → users, shared flag, split %,
                 SimpleFIN external id (dedupe), source, created_at
goals            savings targets; sync writes live account balances
contributions    manual/derived progress toward goals
bills            recurring bills, detected from transaction history
housing          early apartment-list table — superseded by the
                 scenarios design, slated for migration/removal
```

Why it's shaped this way: SimpleFIN is the sole data source (zero manual
entry), amounts are integer cents everywhere, and everything derived —
the who-owes-whom balance, category totals, dashboard numbers — is
computed on read from `transactions`. Nothing aggregate is stored.

### Designed, not built

**PLACES-DESIGN.md** → `scenarios` table. One table for scenarios and
properties (a property is a scenario with an address), line items carrying
measured-vs-estimate pairs seeded from real spending, `monthly_income_cents`
as a manual field awaiting a measured source.

**INCOME-DESIGN.md** → two columns on `transactions`
(`direction: 'out'|'in'`, `income_type`) plus the `income_rules` table
(priority-ordered matchers with `hit_count`). Classification lifecycle:
inflow arrives → rules run → first match wins → no match lands
`'unclassified'` → user tags it → app offers "make this a rule?"

### New for this feature

Two tables. Same conventions: integer cents, ISO text timestamps.

```sql
-- Bearer tokens for the MCP server (and any future API client).
-- Session cookies are for browsers; agents need revocable tokens.
CREATE TABLE api_tokens (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash   TEXT NOT NULL UNIQUE,   -- store the hash, never the token
    label        TEXT NOT NULL,          -- "claude-code on laptop"
    user_id      INTEGER REFERENCES users(id),  -- whose agent this is
    scopes       TEXT NOT NULL DEFAULT 'read',  -- 'read' | 'read,write'
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    revoked      INTEGER NOT NULL DEFAULT 0
);

-- Two-phase writes: a propose call parks the action here with a preview;
-- a confirm call (after human approval) executes it.
CREATE TABLE pending_actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    token        TEXT NOT NULL UNIQUE,   -- short random string, single-use
    action_type  TEXT NOT NULL,          -- 'create_rule' | 'apply_rules' | ...
    payload_json TEXT NOT NULL,          -- exact args to execute with
    preview_json TEXT NOT NULL,          -- what was shown to the human
    created_by   INTEGER REFERENCES api_tokens(id),
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,          -- ~10 minutes; stale approvals die
    status       TEXT NOT NULL DEFAULT 'pending'
                 -- pending | confirmed | expired | cancelled
);

-- Audit log. Every executed write through the API, agent or UI.
CREATE TABLE audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT NOT NULL,
    actor        TEXT NOT NULL,          -- 'ui:alta' | 'mcp:claude-code' | 'sync'
    action       TEXT NOT NULL,          -- 'classify' | 'create_rule' | ...
    target       TEXT,                   -- 'transaction:412' | 'rule:7'
    detail_json  TEXT NOT NULL           -- before/after or payload
);
```

### Why these three and not more

**Why bearer tokens instead of reusing sessions**: cookies assume a
browser and expire on their own schedule. A hashed, labeled, revocable
token per agent gives you Era's "revocable from one place" property with
one table — and `scopes` means a read-only token exists on day one, so
the agent layer can ship before any write tool does.

**Why confirmations live in the database, not the conversation**: the
naive pattern — a `confirm: bool` parameter on write tools — fails in
exactly the way that matters: an eager agent sets `confirm=true` on the
first call. Parking the action server-side forces two distinct tool calls
with a human turn between them, the preview is *frozen* (what you approve
is byte-identical to what executes), the token is single-use, and expiry
kills stale approvals. Same reason income rules went in the database:
state the app must enforce can't live in a prompt.

**Why one audit table for everything**: the UI, the agent, and the sync
script all mutate the same data; a log that only covers one actor can't
answer "why does this row look like this?" — the only question an audit
log exists to answer. `income_rules.hit_count` stays; it's a cheap
observability counter, not an audit trail.

---

## Architecture placement

```
SimpleFIN ──► sync (systemd timer) ──► SQLite ◄── Flask API ◄── SPA (browser)
                                                     ▲
                                          bearer token│HTTP, localhost
                                                     │
                                              ledger_mcp (FastMCP)
                                                     ▲
                                        streamable HTTP over Tailscale
                                                     │
                              Claude Desktop / Claude Code / any MCP client
```

The MCP server is a *sibling process* on the Pi (its own systemd unit,
same recipe as everything else) that talks to Flask over localhost. It
holds no state and does no math — it reshapes API responses for agent
consumption and enforces the confirmation choreography.

Reachability: over Tailscale, Claude Desktop and Claude Code connect
directly (streamable HTTP). The claude.ai website and mobile app can only
reach *public* MCP servers — exposing the Pi via Tailscale Funnel is
possible but is a real security decision (see Decisions). Ship
Tailnet-only first.

---

## The tool surface

Twelve tools, three tiers. Blast radius determines ceremony.

| Tier | Tools | Ceremony |
|---|---|---|
| Read | `get_household_snapshot`, `get_balance`, `get_spending_summary`, `get_income_summary`, `search_transactions`, `get_unclassified_inflows`, `list_income_rules`, `list_goals_and_bills` | none |
| Direct write | `classify_inflow`, `set_rule_enabled` | logged; reversible, single-row/flag |
| Two-phase write | `propose_income_rule`, `apply_rules` → both executed via `confirm_action` | preview → human approval → confirm token |

Why `classify_inflow` is direct: it's the routine teaching action — the
whole lifecycle depends on tagging being one tap, and a two-call dance for
every row would make the agent *worse* than the UI at the one job it's
best at. It touches one row, it's trivially reversible, and it's logged.
Rule creation is two-phase because a rule touches *all future data* and
can bulk-apply backwards; that's the blast radius line.

Deliberately absent (invariant 3): transaction edit/delete, settle-up
recording, share-math changes, goal/bill mutation, scenario writes
(scenarios get their own tools when that feature builds — likely
`get_scenario_affordability` read-only first).

---

## FastMCP sketch

Docstrings are the product here — they're the only instructions the agent
reliably reads. Each one states units, when to use the tool, and what not
to do. Responses return both `*_cents` and a formatted string so the agent
never converts units itself.

```python
"""ledger_mcp — MCP server for Ledger, the household finance app.

Sibling process to the Flask app; talks to it over localhost HTTP with a
bearer token. Holds no state, does no math.
"""
from typing import Literal, Optional
from pydantic import BaseModel, Field, ConfigDict
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("ledger_mcp")

# ── shared plumbing ─────────────────────────────────────────────────────
# api_get / api_post: httpx against http://127.0.0.1:5000 with the bearer
# token from env. Error handler maps Flask errors to actionable messages:
#   401 → "Token revoked or expired. Ask the user to issue a new one in
#          Ledger's settings."
#   409 (rule conflict) → includes the conflicting rule so the agent can
#          propose a fix instead of retrying blindly.


# ═════════════════════════════ READ TIER ═══════════════════════════════

@mcp.tool(
    name="ledger_get_household_snapshot",
    annotations={"title": "Household snapshot", "readOnlyHint": True,
                 "idempotentHint": True, "openWorldHint": False},
)
async def ledger_get_household_snapshot() -> str:
    """One-call overview of the household's current month. START HERE for
    any open-ended question ("how are we doing?", "can we afford X?").

    Returns JSON:
      month, spend_total, spend_by_category (net of refunds),
      balance (who owes whom and how much),
      income {gross_inflows, true_income, net_cash_flow, savings_rate,
              unclassified_count},
      goals [{name, target, current, pct}], upcoming_bills.

    All money fields appear twice: `*_cents` (integer) and `*_display`
    ("$1,234.56"). Use the display strings verbatim in conversation.
    NEVER compute your own totals from transaction lists — every number
    you need exists in a summary tool, computed by the same code as the
    app's dashboard. `savings_rate` is null when true income is 0.

    If unclassified_count > 0, mention it: offer to run the tagging
    workflow (ledger_get_unclassified_inflows).
    """


class SpendingSummaryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    month: Optional[str] = Field(
        None, description="ISO month 'YYYY-MM'. Omit for current month.",
        pattern=r"^\d{4}-\d{2}$")
    months_back: int = Field(
        1, ge=1, le=24,
        description="Number of months to return, ending at `month`. Use "
                    "3–12 for trend questions ('are we spending more?').")


@mcp.tool(
    name="ledger_get_spending_summary",
    annotations={"title": "Spending by month/category", "readOnlyHint": True,
                 "idempotentHint": True, "openWorldHint": False},
)
async def ledger_get_spending_summary(params: SpendingSummaryInput) -> str:
    """Monthly spending totals and per-category breakdown, OUTFLOWS ONLY,
    net of refunds (a returned purchase reduces its category, matching the
    app's own analytics — a category can dip if the refund lands in a
    later month than the purchase; that's correct, not a bug).

    Returns JSON: [{month, total, by_category:{...}, vs_prior_month_pct}].
    Income never appears here — use ledger_get_income_summary for money in.
    """


@mcp.tool(
    name="ledger_get_income_summary",
    annotations={"title": "Income summary", "readOnlyHint": True,
                 "idempotentHint": True, "openWorldHint": False},
)
async def ledger_get_income_summary(params: SpendingSummaryInput) -> str:
    """Income aggregates per month. The vocabulary matters — use it:
      gross_inflows  = every deposit (paychecks + refunds + transfers + …)
      true_income    = paycheck rows ONLY. When the user says "income",
                       they mean this. Never present gross_inflows as
                       income — refunds and transfers are not earnings.
      net_cash_flow  = true_income − spending
      savings_rate   = net_cash_flow / true_income (null if income is 0)
      unclassified_count = inflows awaiting a type; if > 0, the numbers
                       above are provisional — say so, and offer to tag.

    Returns JSON list, one object per month, all fields in cents +
    display. Visibility note: income rows are filtered per the household's
    visibility setting; totals may be scoped to the token's owner.
    """


class SearchTransactionsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: Optional[str] = Field(None, max_length=80,
        description="Case-insensitive substring on description, e.g. 'amazon'.")
    date_from: Optional[str] = Field(None, description="ISO date, inclusive.")
    date_to: Optional[str] = Field(None, description="ISO date, inclusive.")
    direction: Optional[Literal["in", "out"]] = None
    income_type: Optional[Literal["paycheck", "reimbursement", "refund",
        "transfer", "gift", "other", "unclassified"]] = None
    category: Optional[str] = None
    paid_by: Optional[str] = Field(None,
        description="Username. For inflows this means the money's OWNER.")
    limit: int = Field(20, ge=1, le=100)
    offset: int = Field(0, ge=0)


@mcp.tool(
    name="ledger_search_transactions",
    annotations={"title": "Search transactions", "readOnlyHint": True,
                 "idempotentHint": True, "openWorldHint": False},
)
async def ledger_search_transactions(params: SearchTransactionsInput) -> str:
    """Find specific transactions. For EVIDENCE ("show me the three
    biggest grocery runs", "did the deposit land?"), not for totals —
    totals come from the summary tools, always.

    Returns JSON {total_matches, transactions:[{id, date, description,
    amount_cents, amount_display, direction, category, income_type,
    paid_by}], has_more}. Results are paginated; if has_more is true and
    the question needs the full set, page — don't extrapolate.
    """


@mcp.tool(
    name="ledger_get_unclassified_inflows",
    annotations={"title": "Inflow tagging queue", "readOnlyHint": True,
                 "idempotentHint": True, "openWorldHint": False},
)
async def ledger_get_unclassified_inflows() -> str:
    """The tagging queue: inflows with income_type='unclassified', plus
    hints the server computed for each — similar past classifications,
    recurrence ("seen 3× at ~30-day intervals, similar amount"), and the
    account it landed in.

    Workflow: present each row with your suggested type AND your reason;
    let the user confirm or correct; then ledger_classify_inflow. If the
    same description will clearly recur (payroll, rent from a roommate),
    offer to make it a rule — ledger_propose_income_rule. Suggest types;
    never classify without the user's answer.
    """


@mcp.tool(
    name="ledger_list_income_rules",
    annotations={"title": "List income rules", "readOnlyHint": True,
                 "idempotentHint": True, "openWorldHint": False},
)
async def ledger_list_income_rules() -> str:
    """All classification rules with hit_count and enabled flag, in
    priority order (lower runs first; first match wins).

    Use before proposing a new rule — overlap with an existing rule is a
    conflict to surface, not to silently create. A rule with hit_count 0
    after a month is probably dead; offer to disable it.
    """


@mcp.tool(
    name="ledger_get_balance",
    annotations={"title": "Who owes whom", "readOnlyHint": True,
                 "idempotentHint": True, "openWorldHint": False},
)
async def ledger_get_balance() -> str:
    """The settle-up number: who owes whom, computed from shared spending
    splits. Income NEVER affects this — a paycheck belongs to its earner
    and carries no share math. If asked to 'settle up', explain that
    recording a settlement happens in the app, not through me."""


@mcp.tool(
    name="ledger_list_goals_and_bills",
    annotations={"title": "Goals and bills", "readOnlyHint": True,
                 "idempotentHint": True, "openWorldHint": False},
)
async def ledger_list_goals_and_bills() -> str:
    """Savings goals (target, current from synced balances, %) and
    recurring bills (name, amount, due day, autopay). Read-only — goal and
    bill management lives in the app."""


# ══════════════════════════ DIRECT WRITE TIER ═══════════════════════════

class ClassifyInflowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transaction_id: int
    income_type: Literal["paycheck", "reimbursement", "refund",
                         "transfer", "gift", "other"]


@mcp.tool(
    name="ledger_classify_inflow",
    annotations={"title": "Classify an inflow", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
async def ledger_classify_inflow(params: ClassifyInflowInput) -> str:
    """Set the income_type on ONE inflow, after the user has confirmed the
    type in conversation. Only valid on direction='in' rows (the API
    rejects outflows). Reversible: call again with a different type.

    Semantics you must respect when discussing the effect:
      paycheck      → counts toward true_income
      refund        → nets against its category's spending
      transfer      → excluded from every aggregate (own-account shuffle)
      reimbursement → excluded from income, NOT netted against spending
    Returns the updated row + which aggregates changed. Logged to the
    audit trail. If this description will recur, offer a rule next.
    """


class SetRuleEnabledInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_id: int
    enabled: bool


@mcp.tool(
    name="ledger_set_rule_enabled",
    annotations={"title": "Enable/disable a rule", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True,
                 "openWorldHint": False},
)
async def ledger_set_rule_enabled(params: SetRuleEnabledInput) -> str:
    """Toggle a rule on/off after the user asks. Disabling never
    reclassifies existing rows — it only stops future matches. There is
    deliberately no delete; a disabled rule keeps its history."""


# ═══════════════════════ TWO-PHASE WRITE TIER ═══════════════════════════

class ProposeIncomeRuleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    match_desc: Optional[str] = Field(None, max_length=100,
        description="Case-insensitive substring, e.g. 'ADP PAYROLL'. "
                    "Prefer the stable prefix of the bank's description; "
                    "trailing digits often vary per deposit.")
    match_account: Optional[str] = Field(None,
        description="SimpleFIN account id, or null for any account.")
    min_cents: Optional[int] = Field(None, ge=0)
    max_cents: Optional[int] = Field(None, ge=0,
        description="Amount bounds catch collisions — e.g. only "
                    ">$100,000 cents is a paycheck from this payer.")
    set_type: Literal["paycheck", "reimbursement", "refund",
                      "transfer", "gift", "other"]
    set_owner: Optional[str] = Field(None,
        description="Username to assign as the money's owner, or null "
                    "to use the account's default.")
    also_apply_to_existing: bool = Field(True,
        description="Whether confirming should also reclassify current "
                    "unclassified rows that match.")


@mcp.tool(
    name="ledger_propose_income_rule",
    annotations={"title": "Propose income rule (preview)",
                 "readOnlyHint": False, "destructiveHint": False,
                 "idempotentHint": False, "openWorldHint": False},
)
async def ledger_propose_income_rule(params: ProposeIncomeRuleInput) -> str:
    """PHASE 1 of 2. Does NOT create the rule. The server dry-runs the
    matcher and parks a pending action. Returns JSON:
      {confirmation_token, expires_in_seconds,
       preview: {would_match_now: N, sample_rows: [...up to 5],
                 conflicts: [rules that also match these rows],
                 future_effect: "every future inflow matching X → type Y"}}

    REQUIRED next step: show the user the preview — the count, a sample,
    any conflicts — and ask explicitly. Only after the user approves IN
    THEIR OWN REPLY may you call ledger_confirm_action with the token.
    Never propose and confirm in the same turn. If would_match_now
    includes rows that look wrong (a transfer caught by a paycheck rule),
    tighten the matcher and propose again instead of confirming.
    """


@mcp.tool(
    name="ledger_apply_rules",
    annotations={"title": "Re-run rules over unclassified (preview)",
                 "readOnlyHint": False, "destructiveHint": False,
                 "idempotentHint": False, "openWorldHint": False},
)
async def ledger_apply_rules() -> str:
    """PHASE 1 of 2. Dry-runs all enabled rules against the current
    unclassified queue. Returns {confirmation_token, preview:
    {rows_affected, by_rule: [{rule_id, desc, count}]}}.
    Same contract as propose: show the preview, get the user's yes,
    then ledger_confirm_action."""


class ConfirmActionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation_token: str = Field(..., min_length=8, max_length=64)


@mcp.tool(
    name="ledger_confirm_action",
    annotations={"title": "Execute an approved action",
                 "readOnlyHint": False, "destructiveHint": False,
                 "idempotentHint": True, "openWorldHint": False},
)
async def ledger_confirm_action(params: ConfirmActionInput) -> str:
    """PHASE 2 of 2. Executes exactly the pending action the token points
    to — the frozen payload, not your current arguments. Single-use;
    expires ~10 minutes after propose.

    ONLY call this after the user has seen the preview and said yes in
    their own message. If the token expired, re-propose — never guess a
    token, never retry a consumed one. Returns what was executed + the
    audit_log id.
    """
```

### Why the confirmation pattern is shaped like this

The obvious alternatives fail in instructive ways. A `confirm=true`
parameter gets set to true on the first call by an eager model. Relying on
the MCP client's built-in tool-approval prompt shows the human raw
arguments (`match_desc='ADP'`) but not *consequences* ("would reclassify
14 rows, including one that's actually a transfer") — and consequence is
what the human is qualified to judge. Server-side pending actions give
you: a frozen payload (what you approve is what runs), a computed preview
(the dry-run is the safety feature), single-use + expiry (no stale or
replayed approvals), one generic confirm tool for N proposal tools, and a
pattern that works identically in every MCP client — which is the whole
Era thesis. The client is fungible; the guarantees live in the server.

### Docstring conventions (applied above, worth naming)

- **Units, twice**: every money field ships as `*_cents` and `*_display`;
  docstrings tell the agent to quote display strings and never convert.
- **Negative space**: each read tool says what it's *not* for
  (search ≠ totals; spending ≠ income) — misuse is the failure mode.
- **Vocabulary enforcement**: `true_income` vs `gross_inflows` is defined
  where the agent will read it, so "your income this month" can't quietly
  mean the wrong number.
- **Workflow hints**: the queue tool teaches the tagging conversation;
  classify teaches the rule offer. The lifecycle from INCOME-DESIGN lives
  in docstrings, not hope.

---

## What the agent layer does to the income visibility question

INCOME-DESIGN's open problem #1 (can each partner see the other's
paychecks?) gets one new constraint: **whatever you choose must be
enforced in the Flask API, keyed to the authenticated identity** — session
for the browser, `api_tokens.user_id` for agents. If the filter lives in
the frontend, the MCP path leaks income rows to whichever partner's agent
asks. Enforced at the API, every client — SPA, Claude, anything future —
inherits the policy for free. The decision itself is still yours and
Charlee's; this just fixes *where* it must be implemented.

## Build order

1. `api_tokens` + bearer auth on Flask + `audit_log` writes on existing
   mutating endpoints (useful even with zero agent code)
2. `ledger_mcp` read tier — 8 tools, `scopes='read'` token, systemd unit,
   connect from Claude Code over Tailscale and live with it for a week
3. Income feature steps 1–3 from INCOME-DESIGN (the write tools are
   wrappers over endpoints that don't exist yet)
4. `pending_actions` + the two-phase endpoints + all four write tools
5. Later, as their features land: scenario affordability read tool,
   "use measured income" wiring

Step 2 before step 3 is deliberate: the read-only agent is immediately
useful against spending data you already have, and a week of real
questions will reshape the docstrings before any write path exists.

## Deliberately out of scope

- Money movement of any kind (Era moves money; Ledger reports it — the
  Pi holds no bank credentials beyond SimpleFIN's read-only feed, keep it
  that way)
- Agent-side memory (Claude's own memory + the DB is the memory; a
  preferences table is a later nicety)
- Multi-client OAuth (two people, one household — bearer tokens suffice
  until claude.ai web access forces the question)
- Proactive monitoring (Era's "Agency") — a cron + notification problem
  for another design doc

## Decisions needed from you (and Charlee)

1. **Exposure**: Tailnet-only (Claude Desktop/Code work; claude.ai web
   and mobile don't), or public via Tailscale Funnel with real auth
   hardening? Recommendation: Tailnet-only until the write tier has
   soaked.
2. **One agent identity or two?** A single household token, or one token
   per person (`api_tokens.user_id`) so classify/rule actions are
   attributed and income visibility can differ per agent? Per-person
   costs nothing extra and keeps the visibility decision open —
   recommended.
3. **Ratify the write tiering**: comfortable with `classify_inflow` being
   direct (logged, reversible, no preview), or should every write be
   two-phase at the cost of making the routine tagging flow twice as
   chatty?
