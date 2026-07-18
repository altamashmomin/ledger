# START HERE — Ledger × Claude Code setup

From zero to Claude Code building migration #001, in about fifteen
minutes. Do the steps in order; each one says what success looks like.

## What's in this kit

```
CLAUDE.md                  → repo root (Claude Code auto-reads it every session)
docs/CORE-DESIGN.md        → the constitution
docs/INCOME-DESIGN.md      → income feature spec
docs/AGENT-DESIGN.md       → MCP agent layer spec
.gitignore                 → protects secrets + db files (hidden file!)
prompts/first-session.txt  → paste this into Claude Code, step 5
START-HERE.md              → this file (doesn't need to be committed)
```

## Step 1 — Install Claude Code

Requires a Claude subscription (Pro/Max/Team/Enterprise) or a Console
account with credits — the free plan doesn't include Claude Code.

macOS / Linux / WSL, in a terminal:

    curl -fsSL https://claude.ai/install.sh | bash

Windows, in PowerShell:

    irm https://claude.ai/install.ps1 | iex

(Alternative if you already have Node.js 18+:
`npm install -g @anthropic-ai/claude-code`)

**Expected result:** `claude --version` prints a version number.
**If it fails:** close and reopen the terminal first (PATH refresh);
full instructions live at https://code.claude.com/docs/en/quickstart

## Step 2 — Put the kit in the repo

Extract this zip **into the repo root** — the folder with app.py in it.
CLAUDE.md, docs/, .gitignore, and prompts/ should land at the top level.

**Expected result:** `ls` in the repo shows CLAUDE.md and docs/
alongside your code. Note that .gitignore is a hidden file — `ls -a`
shows it. If the repo already had a .gitignore, don't worry about
merging by hand; the first prompt tells Claude Code to handle it.

## Step 3 — Sanity-check the repo

    cd <your-repo-folder>
    git status

**Expected result:** you're on `main`, and the only untracked files are
the ones from this kit. If there are uncommitted code changes you care
about, commit them now — the rework branches from this point, and step
0 of the sequence says main gets tagged v1.0 at the deployed state.

## Step 4 — Launch and log in

    claude

**Expected result:** first run opens the browser to sign in with your
Anthropic account, then drops you at a prompt inside the repo.
**If login misbehaves:** type `/login` inside the session to retry.

## Step 5 — Paste the first prompt

Open `prompts/first-session.txt`, copy the whole thing, paste it into
Claude Code, hit enter. It will read CLAUDE.md and CORE-DESIGN.md, commit
the docs, cut the `rework` branch, and build the migration runner and the
balance gate against a synthetic seed database — asking approval before
commands and showing diffs before writes. You are the approval gate;
that's by design.

## Step 6 — Review like an owner

When it finishes, read two files line by line before anything else
happens: the migration runner and the gate script. They're small, and
everything else in the rework rides on them. Ask Claude Code to explain
any line you don't follow — "walk me through the runner" is a fine
prompt. Only after you're satisfied does the next session get to build
migration #002 (members + splits).

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `claude: command not found` | PATH not refreshed | Reopen terminal; reinstall if it persists |
| npm install errors | Node.js below 18 | `node --version`; update from nodejs.org, or use the curl installer instead |
| Login loops or wrong account | Stale credentials | `/login` inside the session |
| "Plan doesn't include Claude Code" | Free plan | Needs Pro/Max/Team/Enterprise or Console credits |
| Claude Code ignores the rules | CLAUDE.md not at repo root | `ls` — it must sit next to app.py, not in docs/ |

## What comes after (the map)

Casing gets built → Pi gets deployed with current main → **tag v1.0** →
apply migrations #001–#003 via the per-increment loop in CLAUDE.md →
verb extraction, one per session → income build → agent write tier.
The order and the reasons live in docs/CORE-DESIGN.md; when in doubt,
that document wins.
