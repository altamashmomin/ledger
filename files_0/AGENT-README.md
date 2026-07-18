# Ledger agent layer — phase 0

The Era pattern applied to Ledger: your deterministic financial core stays
exactly as it is; this package adds a read-only MCP tool surface, shared
household memory, bearer-token auth, an audit trail, and a proactive
alert digest. **No app tables are altered and no app code is modified** —
everything here is additive and sits beside the Flask app.

Tested against both historical schema variants (`tx_date`/`split_pct` and
`txn_date`/`payer_share_pct`); column names are introspected at runtime.
40/40 smoke checks pass, including refund netting, true-income semantics,
and income exclusion from the balance.

## Contents

    ledger_core.py     schema-introspecting read logic (shared)
    ledger_mcp.py      the MCP server — 10 tools, bearer auth over HTTP
    migrate_agent.py   creates api_tokens, audit_log, household_context,
                       pending_actions (idempotent; app tables untouched)
    tokens_cli.py      create / list / revoke bearer tokens
    notify.py          post-sync ntfy digest (bills due, tagging queue,
                       review queue, category surges)
    run_tests.py       the smoke suite (runs anywhere, uses fixture DBs)
    deploy/            systemd unit + sync-service drop-in

## Deploy (on the Pi) — the short way

    scp ledger-agent.zip pi@<tailscale-name>:/home/pi/
    ssh pi@<tailscale-name>
    unzip ledger-agent.zip && cd ledger-agent
    LEDGER_DB=/home/pi/financeapp/finance.db ./deploy.sh

`deploy.sh` runs preflight (environment + schema checks, and it reads the
split column's DDL comment to auto-set LEDGER_SPLIT_MODE when the comment
is unambiguous), installs dependencies, migrates, creates one token per
user, installs and verifies the systemd service, and walks the ntfy setup.
It prints the Claude connect command at the end. Re-runnable. To inspect
before committing to anything: `python3 preflight.py` alone is read-only.

`future/` holds the pre-built income-feature foundation (schema migration
with origin_text, and the rules engine both sync and Flask will import).
Nothing in it runs until the income build starts — see its docstrings.

## Deploy — the manual way

    pip install "mcp[cli]" uvicorn --break-system-packages

    # 1. migrate (safe to re-run; also enables WAL for concurrent readers)
    LEDGER_DB=/home/pi/financeapp/finance.db python3 migrate_agent.py

    # 2. one token per person — per-agent attribution costs nothing
    LEDGER_DB=... python3 tokens_cli.py create --label "alta claude-code" --user alta
    LEDGER_DB=... python3 tokens_cli.py create --label "charlee claude" --user charlee

    # 3. the MCP service
    sudo cp deploy/ledger-mcp.service /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl enable --now ledger-mcp

    # 4. alerts: pick a HARD-TO-GUESS ntfy topic (it is effectively a
    #    password), edit deploy/sync-notify-override.conf, then install it
    #    as a drop-in on your existing sync service (instructions inside).
    #    Subscribe to the topic in the ntfy app on both phones.
    #    Test first:  LEDGER_DB=... python3 notify.py --dry-run

Connect from Claude Code:

    claude mcp add ledger --transport http http://<pi-tailscale-name>:8091/mcp \
      --header "Authorization: Bearer lgr_..."

Claude Desktop: add the same URL + header as a custom connector. The
claude.ai website and mobile app cannot reach a Tailnet-only server —
that's deliberate for now (AGENT-DESIGN decision 1).

## Two gates before you trust two numbers

**1. The who-owes-whom balance is OFF until configured.** The two schema
variants use split columns with different meanings, and a silently wrong
balance is worse than none. Open your deployed `schema.sql` and check the
split column's comment:

- "% of the cost that is the **payer's** share" → set
  `LEDGER_SPLIT_MODE=payer_share`
- "% of the cost owed by **user 1**" → set `LEDGER_SPLIT_MODE=user1_share`

Uncomment the line in `deploy/ledger-mcp.service`, restart, then ask
Claude for the balance and **verify it against the app's dashboard once**
before believing it. (At an even 50/50 split the two modes agree, which
is exactly why a wrong setting would hide until the first uneven split.)

**2. Income tools self-activate later.** Until INCOME-DESIGN ships,
`ledger_get_income_summary` and the tagging queue honestly report that
the feature isn't built. The moment `direction`/`income_type` land in the
schema, the same server picks them up — no redeploy of this package.

## What this deliberately does not do

No writes to app tables, no transaction edits, no settle-up recording, no
money movement. The only writable surface is `household_context` (shared
notes both partners' agents can see — treat it as such) and the audit
trail behind it. The write tier (classify, propose-rule, two-phase
confirm via `pending_actions`, which the migration already creates) ships
with the income feature, wrapping Flask endpoints per AGENT-DESIGN.md.
