# Design: Income & Cash Flow

Turning Ledger from a spending tracker into a light accounting system.
Status: **design only, not built.**

## The core claim

Importing deposits is one deleted line in the sync script. The feature is
everything that has to exist so that deleted line doesn't poison the data.

The danger is specific: a bank feed reports your paycheck, a $200 transfer
from your own savings, a $47 Amazon refund, and a friend's $500 Venmo
repayment identically — "money in, name, amount." Import them naively and
your income reads $4,000/mo when your paycheck is $3,200, and every
downstream number (surplus, savings rate, months-to-move) is silently
optimistic. **An accounting system that does income wrong is worse than a
spending tracker that doesn't try.**

So the design is really a *classification system* with an import attached:
every inflow must end up with a type, the recurring ones must classify
themselves after being taught once, and every aggregate must be explicit
about which types it counts.

## The invariants (not up for debate)

1. **Income never touches the shared balance.** A paycheck belongs to the
   person who earned it. Inflow rows carry no share math; the balance
   computation ignores them entirely. Nothing about who-owes-whom changes.
2. **Spending totals never include inflows.** The dashboard month total,
   category bars, and analytics spend views filter to outflows exactly as
   they do today. Existing numbers don't move when this ships.
3. **"True income" means paychecks only.** Aggregates expose both
   `gross_inflows` (everything) and `true_income` (paycheck rows), and
   downstream features consume `true_income`. Refunds, transfers, and
   repayments are never income.

---

## Schema

One altered table, one new table. Same conventions: integer cents,
timestamps as ISO text.

```sql
-- transactions: two new columns
ALTER TABLE transactions ADD COLUMN direction TEXT NOT NULL DEFAULT 'out';
       -- 'out' (spend) | 'in' (inflow). Every existing row is 'out'.
ALTER TABLE transactions ADD COLUMN income_type TEXT;
       -- NULL for direction='out'. For 'in':
       -- 'paycheck' | 'reimbursement' | 'refund' | 'transfer'
       -- | 'gift' | 'other' | 'unclassified'

CREATE TABLE income_rules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    priority      INTEGER NOT NULL DEFAULT 0,   -- lower runs first
    match_desc    TEXT,          -- substring match on description, case-insensitive
    match_account TEXT,          -- SimpleFIN account id, or NULL = any
    min_cents     INTEGER,       -- inclusive bounds, either may be NULL
    max_cents     INTEGER,
    set_type      TEXT NOT NULL, -- income_type to assign
    set_paid_by   INTEGER REFERENCES users(id),  -- owner override, or NULL
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    hit_count     INTEGER NOT NULL DEFAULT 0    -- observability: is this rule alive?
);
```

### Why extend `transactions` instead of a new `income` table

An inflow is a transaction: date, amount, description, account, source,
external_id. A separate table would duplicate the dedupe machinery, the
Activity feed query, the edit/delete endpoints, and the sync insert path —
all to avoid two nullable columns. The `direction` flag costs one WHERE
clause in each aggregate; a second table costs a UNION in every query that
wants a unified feed. Same call as scenarios-vs-properties: one table,
discriminator column.

### Why rules live in the database, not the sync script

Rules are user data — they encode *your* banks' naming quirks ("ADP
PAYROLL 8842" means Alta's paycheck). Hardcoding them in `simplefin_sync.py`
means editing Python on the Pi to teach the system. A table means the app
can grow a small rules UI, rules apply identically whether a transaction
arrives via sync or manual entry, and `hit_count` shows which rules are
actually earning their keep.

### The classification lifecycle

```
inflow arrives → rules run in priority order → first match wins
                → no match: income_type = 'unclassified'
user tags an unclassified row → app offers: "make this a rule?"
                → future matches classify themselves
```

Teaching moment over rule-authoring: nobody writes regexes up front. You
tag the first paycheck by hand, accept the one-tap "always do this," and
the system converges within a month of real data.

---

## Sync changes

- Delete the `amount >= 0: skip` branch. Inflows insert with
  `direction='in'`, run through the rules, land classified or
  `'unclassified'`.
- Inflows get **no share fields** (shared=0, no split) regardless of the
  SYNC defaults that apply to spending.
- `paid_by` for an inflow means *owner* (whose money this is): from the
  matching rule's `set_paid_by`, else the account's SYNC_PAID_BY.
- Dedupe is unchanged — external_id already covers it.

## Surfaces

**Activity feed**: inflows render distinctly (green amount, income-type
chip). Unclassified rows carry a "tag this" affordance. A filter cycles
all / spending / income.

**Dashboard** gains one card:

```
gross_inflows  = Σ amount where direction='in', this month
true_income    = Σ amount where direction='in' and income_type='paycheck'
net_cash_flow  = true_income − month_spend_total
savings_rate   = net_cash_flow / true_income        -- guard: income 0 → null
unclassified_n = count of income_type='unclassified'  -- shown as a nudge
```

All derived on read, like `/api/balance`. Nothing stored.

**Analytics tab** (already planned) gains the chart this whole feature
exists for: true income vs. spending by month, the gap shaded — your
savings rate over time, from real data.

**Scenarios integration**: `monthly_income_cents` stops being purely
manual. The scenario editor offers *"use measured: $X,XXX/mo"* — the
trailing 3-month average of paycheck rows — with manual override retained
(model a raise, a job change). Same measured-vs-estimate pattern the rest
of the scenario table already uses, now applied to the income line.

---

## The hard cases, decided

**Refunds are cost reversals, not income.** A $47 Amazon refund is not
$47 you earned — it un-spends $47. Classified `refund` rows are *excluded
from income* and *subtracted from the spending totals of their category*
(net spend). This keeps category analytics honest: return the air fryer
and your Household number goes back down. Consequence to accept: a refund
landing in a later month than its purchase makes that category dip — true,
and occasionally surprising.

**Transfers are noise.** Money moving between your own accounts is neither
income nor spending. Classified `transfer` rows are excluded from every
aggregate; they exist in the feed for completeness only. Auto-pairing the
outflow leg with the inflow leg (same amount, opposite direction, few days
apart) is deliberately **v2** — the heuristic misfires on rent-sized
coincidences, and a wrong auto-pair is worse than two rows you tag by hand.

**Reimbursements stay simple.** A `reimbursement` inflow is excluded from
income (like refunds) but does **not** net against a spend category —
figuring out *which* expense it repays is a matching problem this design
refuses. If the reimbursement is your partner paying you back, that's what
Settle up is for, and it already works.

## Open problem: two people can see each other's paychecks

Today the app shows both people everything — fine for spending you've
agreed to pool visibility on. Income is more intimate; some couples share
numbers freely, some don't. There's no per-row privacy in the app at all,
and adding it (row-level ACLs, filtered feeds, aggregates that respect
visibility) is a genuinely large change hiding inside a checkbox.

Options: (1) full transparency — both see all income, matching how the
rest of the app works; (2) income rows visible only to their owner, with
shared aggregates (net household cash flow) still computed over both;
(3) per-person toggle. This is a relationship decision disguised as a
schema decision — it's yours and Charlee's to make, not mine.

---

## API sketch

```
GET    /api/income/summary            month aggregates (the dashboard card)
GET    /api/income/rules              list rules with hit counts
POST   /api/income/rules              create (also the "make this a rule?" path)
PUT    /api/income/rules/<id>         edit / enable / disable
DELETE /api/income/rules/<id>
POST   /api/income/rules/apply        re-run rules over unclassified rows
PUT    /api/transactions/<id>/classify   set income_type on one row
GET    /api/transactions?direction=in&type=unclassified   the tagging queue
```

Everything else reuses existing transaction endpoints — an inflow is a
transaction.

## Build order

1. Schema migration + classification endpoints + rules engine (foundation)
2. Sync flip: import inflows, auto-classify, land the rest unclassified
3. Dashboard card + Activity treatment + tagging flow
4. Analytics income-vs-spend chart (with the analytics tab build)
5. Scenarios "use measured income" wiring

Ship 1–3 together; 4–5 ride their own features.

## Deliberately out of scope

- Transfer auto-pairing (v2, stated above)
- Reimbursement-to-expense matching (refused, stated above)
- Budgets/envelopes — a different feature with its own design
- Multi-currency, invoicing, tax categories — this is a household, not a
  business

## Decisions needed from you (and Charlee)

1. **Income visibility**: full transparency, owner-only rows, or a toggle?
   (The one decision that blocks the build — everything else has a chosen
   default you can override later.)
2. **Refund netting**: comfortable with categories dipping when a refund
   lands in a later month, or should refunds be income-excluded but *not*
   netted (simpler, slightly less honest categories)?
3. **Auto-rule aggressiveness**: should the "make this a rule?" prompt
   appear after tagging one row (fast convergence, more misfires) or only
   after the same description shows up twice (slower, safer)?
