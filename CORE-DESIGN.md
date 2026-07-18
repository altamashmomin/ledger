# Design: The Core Grammar

The constitution for Ledger's rework: the target architecture (nouns,
verbs, policies, derivations) and the ordered migration path that reaches
it without the app having a single broken day.
Status: **ratified — this document governs the rework branch.**

## What this document is

INCOME-DESIGN.md specs a feature. AGENT-DESIGN.md specs a surface. This
document is the parent both hang off: it defines what the data *means*,
how it is allowed to *change*, and the pipeline by which the running app
becomes the redesigned app. New ideas get checked against this doc.
Increments on the rework branch get reviewed against it. Where this
document and a feature doc disagree, this document wins and the feature
doc gets amended (amendments to both are listed at the end).

## The core claim

Two claims, actually.

**A rewrite is a migration sequence, not a greenfield.** Ledger is live,
holds real financial history, and has a second user who did not sign up
for downtime. So the rework is not "Ledger v2" — it is a target grammar
plus an ordered list of small steps, where the app is deployable after
every step, every step is rehearsed on a copy of the real database first,
and no step merges without proving the numbers didn't move. The most
dangerous event in a working app's life is its owner rewriting it to
honor a theory; the pipeline below exists to make that event boring.

**Versatility comes from grammar, not engine.** The lesson taken from
studying Palantir's Ontology, scaled to a Pi: a system stays cheap to
extend when it has typed nouns (every row's meaning is explicit), governed
verbs (every write is a named, validated, audited action), policy at one
chokepoint (not scattered through queries), and aggregates derived on
read. Adding a use case must never reopen the question of what the data
means. Everything in this document serves that property; everything that
serves only scale we don't have is refused at the bottom.

## The invariants (not up for debate)

1. **Every write is a named action.** One verb per kind of change,
   defined once in `actions.py`: validate → edit → side effects → audit.
   Flask routes, the sync script, and MCP tools are thin callers. No
   caller writes SQL against mutating tables directly.
2. **The household is the tenant.** One SQLite database is one household,
   forever. No query crosses households because no query ever could.
   Per-household data stays small permanently, which is why invariant 6
   is safe permanently.
3. **Member count is data, never schema.** A household has 1..N members
   stored as rows. No code and no column may assume N=2. Features that
   only make sense for some N gate themselves with submission criteria
   (a check inside the verb), not with forks in the codebase.
4. **Every aggregate declares which types it counts.** `true_income` is
   paychecks only; spending is outflows net of refunds; transfers count
   nowhere. An aggregate whose inputs can't be stated in one sentence
   doesn't ship. (This is INCOME-DESIGN's thesis, promoted to a system
   rule.)
5. **Links are additive and reversible.** Relationships between rows —
   refund to purchase, settlement to what it settles, bill to the payment
   that satisfied it — live in the `links` table. Creating a link mutates
   nothing; deleting one reverts everything.
6. **Nothing derived is stored.** Balance, totals, summaries, savings
   rate: computed on read by named functions, and every surface (SPA,
   MCP, future anything) calls the same function. At household scale this
   is correct forever, not a placeholder for a cache.
7. **Schema changes ship only as numbered, idempotent migrations** run by
   the migration runner, in order, recorded in `schema_version`. No
   ad-hoc ALTER statements, ever, including on dev copies.
8. **`main` is always deployable.** Nothing merges without passing the
   balance gate against a copy of the real database. The Pi only ever
   runs merged, gated code.

## Settled decisions

Formerly open questions, now closed. Recorded here so they stay closed.

**Income visibility: full transparency.** Both members see all rows,
income included — matching how the rest of the app works. One courtesy to
the future: consolidated reads pass through a single
`visible_to(row, member) -> bool` that returns `True`. It costs one
function and means any future household wanting a different answer
changes one place instead of excavating every query. (Closes
INCOME-DESIGN open question #1, which was blocking the income build.)

**One repo, one lineage.** The current deployed state gets tagged `v1.0`
before any rework commit lands. The rework happens on a branch and merges
back in small gated increments. There is no second project, no long-lived
mega-branch, no big-bang cutover. Products fork only when they are
genuinely different software (a hosted control plane, someday, maybe);
the core app never forks.

**Architect for N, ship for ≤2.** The schema stops encoding two-ness
(members and splits, below). The settlement *feature* keeps a N≤2
submission criterion until real households with more members ask for it —
N>2 settlement is a debt-graph product with its own UX questions and gets
its own design doc if that day comes. A household of one is fully
supported: the sharing layer simply lies dormant (no split rows, balance
hidden, settle-up unavailable), and the personal core — transactions,
income, rules, bills, goals, agent — is identical at every N.

**Split semantics normalized at migration time.** The deployed split
column and its interpretation ambiguity (payer's share vs. a fixed
person's share) are resolved once, during migration #002, by converting
every shared transaction into explicit per-member split rows. After #002
there is exactly one representation and the old column is dropped. The
ambiguity does not survive into the new grammar with a compatibility
flag.

## Nouns

Conventions unchanged: integer cents, ISO-8601 text timestamps. As in
AGENT-DESIGN.md, where deployed `schema.sql` spellings differ from names
used here, the deployed spelling wins; roles are what matter.

### New and changed tables

```sql
-- Migration bookkeeping. Created by migration #001, which also creates
-- the runner that reads it.
CREATE TABLE schema_version (
    version     INTEGER PRIMARY KEY,     -- migration number
    applied_at  TEXT NOT NULL,
    description TEXT NOT NULL
);

-- The household's people. Generalizes `users`: same auth role, no
-- hardcoded count. Members are never deleted — `active=0` handles
-- departures, so history stays attributable.
CREATE TABLE members (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    -- existing auth columns migrate over unchanged
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

-- How a shared transaction divides. Basis points, not floats; rows, not
-- a percentage column — a percentage can only split between exactly two
-- parties, which is precisely the assumption being retired.
CREATE TABLE splits (
    transaction_id INTEGER NOT NULL REFERENCES transactions(id),
    member_id      INTEGER NOT NULL REFERENCES members(id),
    share_bp       INTEGER NOT NULL CHECK (share_bp BETWEEN 0 AND 10000),
    PRIMARY KEY (transaction_id, member_id)
);
-- Invariant (enforced by the verbs, verified by the gate): for a shared
-- transaction, share_bp sums to 10000. Unshared transactions have no
-- split rows at all.

-- Typed relationships between transactions. Additive metadata: creating
-- one changes no row; deleting one reverts everything.
CREATE TABLE links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    link_type   TEXT NOT NULL,   -- 'refund_of' | 'transfer_pair'
                                 -- | 'reimburses' | 'settles'
                                 -- | 'bill_payment'
    from_id     INTEGER NOT NULL REFERENCES transactions(id),
    to_id       INTEGER NOT NULL REFERENCES transactions(id),
    created_by  TEXT NOT NULL,   -- actor string, same vocabulary as audit
    created_at  TEXT NOT NULL,
    UNIQUE(link_type, from_id, to_id)
);
```

`transactions` changes shape in two migrations, not one. #002:
`paid_by` becomes a member id (payer for outflows; for inflows it means
*owner*, per INCOME-DESIGN), the split column is exploded into `splits`
rows and dropped. Later, the income build adds `direction` and
`income_type` plus the `income_rules` table exactly as INCOME-DESIGN
specifies — that design is unamended except that its people-references
are now member ids.

`audit_log`, `api_tokens`, `pending_actions` are as specified in
AGENT-DESIGN.md, with one amendment: `api_tokens.user_id` references
`members`, and `audit_log` receives rows from *every* write path — UI
and sync included — not only the agent's. A log that covers one actor
can't answer "why does this row look like this," which is the only
question it exists to answer.

### Why members replaces users rather than sitting beside it

Same table, honest name, no count assumption. Keeping `users` as "the
two people who log in" next to `members` as "the people money is about"
would mean every person exists twice and the tables drift. One table,
one row per human, `active` for lifecycle. The N=1→2 transition — or
2→3, or a roommate leaving — becomes an INSERT or an UPDATE, never a
migration. Household composition is data.

## Verbs

The action registry. Each verb lives in `actions.py` with the same
four-part contract: **validate** (including submission criteria),
**edit** (one SQLite transaction), **side effects** (after commit:
notifications, rule runs), **audit** (one row, always). Actor vocabulary
is shared with AGENT-DESIGN: `ui:<member>` | `sync` | `mcp:<token-label>`.

| Verb | Callers | Submission criteria (beyond validation) | Notes |
|---|---|---|---|
| `record_transaction` | sync, UI | — | Sync's insert path becomes a call to this; dedupe stays inside it |
| `edit_transaction` / `delete_transaction` | UI only | — | Deliberately absent from MCP (AGENT-DESIGN invariant 3) |
| `set_splits` | UI | shared rows only; shares sum to 10000 | Replaces direct split edits |
| `settle_up` | UI only | **requires active members ≥ 2** | Writes the settlement and `settles` links to covered rows |
| `mark_bill_paid` | UI | — | Creates the transaction and a `bill_payment` link in one edit |
| `classify_inflow` | UI, MCP direct | row must be `direction='in'` | Lands with the income build |
| `create_income_rule` | UI, MCP two-phase | conflict check against existing rules | Two-phase via `pending_actions` when the caller is an agent |
| `set_rule_enabled` | UI, MCP direct | — | No delete; disabled rules keep history |
| `apply_rules` | UI, MCP two-phase | — | Dry-run preview first, per AGENT-DESIGN |
| `link_transactions` / `unlink_transactions` | UI, later MCP | typed; both rows must exist; type-specific checks (e.g. `transfer_pair` needs opposite directions) | The tag-a-link workflow that makes refund/transfer/ reimbursement matching non-destructive |
| `contribute_to_goal` | UI | — | |

The table grows as features land (scenario verbs arrive with the
scenarios build), but growth means *adding rows here first* — a write
path that isn't in this registry doesn't exist.

Submission criteria are how member count gates features without forking
code: `settle_up` in a household of one isn't an error state, it's a
verb whose criteria are never met, so the UI hides it and the agent's
docstring explains it. Same pattern Palantir uses to make an action
invalid in a context; same pattern the two-phase agent writes already
use. One mechanism, every gate.

## Policies

**Identity.** Every actor resolves to a member or to `sync`. Sessions
map to members; `api_tokens.user_id` maps agent tokens to the member
whose agent it is (AGENT-DESIGN decision #2, per-person tokens, stands).
An audit row's `actor` is therefore always a person or the feed — which
is what makes the log meaningful.

**Visibility.** `visible_to(row, member)` returns `True`, called from
the consolidated read path. It is the single place a future answer would
live; it is not an invitation to build one now.

**Scopes.** Token scopes (`read` | `read,write`) as designed. The read
tier of the agent can exist against any schema state; write tools appear
only as their verbs land.

## Derivations

All read-time, all named functions, all consumed by every surface:

- `compute_balance()` — who-owes-whom from split rows. For N=2 this is
  the current closed-form number. The function signature already returns
  pairwise nets so an eventual N>2 implementation changes the inside,
  not the callers. N=1 returns empty.
- `spending_summary(month, months_back)` — outflows only, net of
  refunds, per INCOME-DESIGN.
- `income_summary(month, months_back)` — `gross_inflows`, `true_income`,
  `net_cash_flow`, `savings_rate`, `unclassified_count`, per
  INCOME-DESIGN.

The MCP read tools and dashboard cards are presentations of these three
plus simple listings. If a number appears anywhere in the product, one
of these functions computed it.

## The pipeline

### Repo mechanics

```
main  ──o──────o────o────o────o──►   ← live on the Pi, always gated
        │tag  ▲    ▲    ▲    ▲
        │v1.0 │merge (small, gated)
        └──o──o─o──o─o──o─o──o──►    ← rework branch
```

Tag `v1.0` at the deployed state before the first rework commit. Branch.
Merge one increment at a time — one migration or one verb extraction —
never a batch.

### The per-increment loop

1. **Build** the increment on the rework branch.
2. **Stage**: `cp finance.db dev.db` (the entire staging environment is
   one file copy; real data is the only test fixture that matters).
3. **Gate** (below) against `dev.db`.
4. **Apply**: `cp finance.db finance.db.bak-<date>` on the Pi (the
   backup *is* the rollback), run the migration/deploy, re-verify the
   gate numbers on the live database.
5. **Merge** to `main`. Repeat.

### The balance gate

Before any merge, old code and new code run side by side against
`dev.db` and must agree on: the who-owes-whom balance **to the cent**,
monthly spend totals for every month in the data, and per-table row
counts. An increment that intends to change a number (e.g. a backfill)
must enumerate the expected diff in its notes, and only the enumerated
diff is accepted. The balance is the app's crown jewel — the number two
people trust each other with — and it is the one place a silent
regression costs more than correctness.

## Migration sequence

Ordered; the app works after every step; no step depends on a later one.

0. Deploy current code to the Pi; live sync; **tag `v1.0`**. Nothing
   below happens before this exists. (Every day the Pi runs before the
   rework starts, `finance.db` becomes a better test fixture.)
1. **#001 — the runner.** Numbered idempotent migration files, applied
   in order in a transaction, recorded in `schema_version`. Migration
   infrastructure is itself migration one because everything else rides
   on it — and because fleet-upgradability is the entire cost of the
   someday-distribution future, bought here for pennies.
2. **#002 — members + splits.** `users` → `members`; backfill both
   people; explode the split column into per-member rows (two rows
   summing to 10000 bp per shared transaction — isomorphic to today, so
   the gate must show zero balance change); drop the old column and its
   ambiguity permanently.
3. **#003 — links.** Create the table. Wire `settles` and
   `bill_payment` link creation into their verbs going forward;
   historic backfill is forward-only (see refusals).
4. **Audit everywhere.** Extend `audit_log` writes to UI and sync paths
   (mechanically: this lands as each verb is extracted, since the verb
   contract includes the audit row).
5. **Verb extraction**, one route at a time, strangler-style:
   `settle_up` first (it's what the gate protects), then bills, goals,
   transaction edits, and the sync insert path (`record_transaction`)
   last — positioned deliberately just before the income build touches
   sync anyway.
6. **The income build** — INCOME-DESIGN steps 1–3, unamended, now built
   *inside* the grammar: its classification verbs enter the registry,
   its aggregates are derivations, its rules run as side effects of
   `record_transaction`. Income is the first feature born in the new
   grammar, and deliberately so: it exercises every part of this
   document, and if the grammar fights it, better to learn on feature
   one.
7. **Agent write tier** (AGENT-DESIGN step 4) — the write tools wrap
   verbs that now exist.

Steps 1–3 are days, not weeks. Step 5 is background-pace work — one
verb per session — and the loop gets faster each time.

## The scaling horizon (context, not commitments)

Recorded so the rewrite remembers why certain choices were made; none of
this is being built now. If Ledger ever grows beyond this household, the
unit of scale is the household and the problem is a *fleet* problem, not
a data problem — per-household data stays tiny forever. Path A
(distribute the self-hosted app: Docker, versioned migrations,
Apollo-style delivery to machines we'll never touch) ships first and is
~5% of the work of hosting; Path C (hosted control plane orchestrating
one isolated per-household database) preserves every invariant here if
demand ever justifies a company. Path B (conventional multi-tenant SaaS)
is understood and declined. The hedges those futures require are already
in this document as invariants 1, 2, 6, and 7 — that's why they're
invariants.

## Deliberately refused

- **Event sourcing.** The verbs-and-audit design begs for it; no. Tables
  hold state, the audit log tells the story, and the bank feed is
  already the external source of truth for most rows. Replay machinery
  is enormous ceremony for N households of tiny data.
- **ORM, framework migration, plugin architecture.** Flask + sqlite3 +
  SQL strings survived contact with reality; the rework changes the
  grammar, not the stack.
- **Palantir's engine.** No microservices, indexing layers,
  materializations, CDC, or branching infrastructure. The grammar is the
  import; the engine solves scale this app is architected never to need.
- **N>2 settlement UX.** The schema is ready; the debt-graph feature
  waits for a real household to want it, and gets its own design doc.
- **Historic link backfill.** `settles` and `bill_payment` links are
  created going forward. Reconstructing which past transactions a past
  settlement covered is archaeology with little payoff; forward-only
  keeps #003 trivial.
- **A second repo, a mega-branch, a big-bang cutover.** Covered above;
  listed here so it stays refused.

## Amendments to the other design docs

- **INCOME-DESIGN.md**: open question #1 (visibility) closed — full
  transparency via the `visible_to` stub; the build is unblocked.
  People-references (`set_paid_by`, inflow ownership) are member ids.
  Otherwise stands as written; builds at sequence step 6.
- **AGENT-DESIGN.md**: `api_tokens.user_id` references `members`;
  `audit_log` scope widened to all writes; the action registry here is
  the single write path its invariant 2 requires. Otherwise stands;
  write tier builds at sequence step 7.
- **PLACES-DESIGN.md** (scenarios): untouched. Scenario verbs join the
  registry when that feature builds.

## Open questions

Only two, neither blocking anything in the sequence:

1. **Member auth mechanics beyond two.** Sessions for two people who
   share a home work fine; invites/passwords for a hypothetical third
   member get decided if a third member ever exists.
2. **When the income build lands relative to verb extraction.** The
   sequence says after; if the income feature gets urgent, steps 5 and 6
   can interleave — the only hard rule is that income's write paths are
   born as verbs, not extracted later.
