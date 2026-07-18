# Ledger — a two-person household finance app

Self-hosted finance tracker for exactly two people, built to run on a
Raspberry Pi (Pi 3 or newer). Flask + SQLite backend, plain HTML/CSS/JS
frontend served by Flask — no build step, no npm, no external database.

**Features**

- Transactions with category, who-paid, shared flag, and a custom split
  (default 50/50), editable and deletable. Automated entries are tagged
  by source (`manual`, `bill`, `simplefin`, `settlement`).
- Who-owes-whom balance computed across all shared transactions, with a
  one-tap "Settle up" that records the repayment.
- Recurring bills with a due day; "Mark paid" logs the month's payment
  **and** creates the matching transaction automatically (Undo removes both).
- Savings goals with target amount, optional target date, contribution
  log (withdrawals = negative amounts), and progress bars.
- Dashboard: month total, spend by category, balance, unpaid bills,
  goal progress, recent activity.
- Optional daily bank sync via SimpleFIN Bridge (see below).

**Files**

```
app.py                     Flask app (all API routes + static serving)
static/                    index.html, style.css, app.js (the whole UI)
simplefin_sync.py          optional bank-sync script
requirements.txt           flask, python-dotenv, requests, gunicorn
.env.example               template for your .env
deploy/pifinance.service   systemd unit for the web app
deploy/pifinance-sync.service + .timer   daily sync job
```

---

## 1. Copy the code to the Pi

From the machine where you unzipped this:

```bash
scp -r pifinance pi@raspberrypi.local:/home/pi/pifinance
```

(Adjust the username/host if yours differ. Everything below assumes
`/home/pi/pifinance` — if you use another user or path, change the same
paths inside the two `deploy/*.service` files.)

## 2. Install dependencies

On the Pi:

```bash
sudo apt update && sudo apt install -y python3-venv
cd /home/pi/pifinance
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

## 3. Create your .env

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_hex(32))"   # copy the output
nano .env    # paste it as SECRET_KEY; the other defaults are fine
chmod 600 .env
```

## 4. First run and account creation

```bash
venv/bin/python app.py
```

Open `http://<pi-ip>:8080` from any device on your network. The first
visit shows a one-time setup screen — create both accounts (names,
usernames, passwords of 8+ characters). That screen never appears
again; afterwards it's normal username/password sign-in. Ctrl-C the
test run once you've confirmed login works.

## 5. Enable the systemd service (survives reboots, restarts on crash)

```bash
sudo cp deploy/pifinance.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pifinance
systemctl status pifinance        # should say "active (running)"
```

Logs: `journalctl -u pifinance -f`

## 6. Remote access from your phones — Tailscale

Tailscale puts the Pi and both phones on a private WireGuard mesh; no
port-forwarding, nothing exposed to the internet.

1. On the Pi:
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   ```
   It prints a login URL — open it and sign in (one free account for
   the household; the free plan covers many devices).
2. Install the Tailscale app on both phones and sign in to the **same**
   account (or use Tailscale's user-sharing if you prefer separate
   accounts).
3. Find the Pi's Tailscale address: `tailscale ip -4` (a `100.x.y.z`
   address), or use its MagicDNS name (e.g. `raspberrypi`).
4. On each phone, visit `http://100.x.y.z:8080` (or
   `http://raspberrypi:8080`) and add it to the home screen — it
   behaves like an app. The session cookie lasts 90 days, so you won't
   sign in often.

The app itself never leaves the tailnet. Don't port-forward it from
your router — it's a small home app, not hardened for the open
internet.

## 7. Optional: automatic bank sync with SimpleFIN

[SimpleFIN Bridge](https://beta-bridge.simplefin.org/) is a small,
individual-friendly bank aggregator (~$1.50/mo or $15/yr) — unlike
Plaid, it's built for personal projects. One-time setup:

1. Create a SimpleFIN Bridge account and connect your bank(s).
2. In the Bridge dashboard, create a **setup token** (a long base64
   string). It's single-use.
3. On the Pi:
   ```bash
   cd /home/pi/pifinance
   venv/bin/python simplefin_sync.py --claim "PASTE_TOKEN_HERE"
   ```
   This exchanges the token for a permanent access URL, saved to
   `simplefin_access.url` with `600` permissions. The URL contains
   credentials — the script never prints it; don't commit or share
   that file.
4. Test a sync: `venv/bin/python simplefin_sync.py`
   You should see inserted transactions (or "0 inserted" if none are
   new). Behavior: deposits are skipped (only money out is tracked),
   re-runs never double-insert (deduped on SimpleFIN's transaction id),
   and new rows land as **shared 50/50, category "Other"**, attributed
   to the user id in `SYNC_PAID_BY` — recategorize/resplit them in the
   app whenever you like.
5. Enable the daily timer:
   ```bash
   sudo cp deploy/pifinance-sync.service deploy/pifinance-sync.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now pifinance-sync.timer
   systemctl list-timers pifinance-sync.timer   # next run time
   ```
   It runs daily at ~06:30 (and catches up after a power-off, thanks to
   `Persistent=true`). Sync logs: `journalctl -u pifinance-sync`.

If both of you have accounts to sync, run two claims into two access
files and duplicate the service/timer with a different
`SIMPLEFIN_ACCESS_FILE` and `SYNC_PAID_BY` per copy.

---

## How the balance math works

For every **shared** transaction, the payer fronted 100% but is only
responsible for their share (`payer_share_pct`, default 50%). So the
other person owes `amount × (100 − payer_share_pct) / 100`. Summing
that over all shared transactions gives the running balance.

Worked example (verified by the test run in development):

| # | Paid by | Amount | Split | Effect |
|---|---------|--------|-------|--------|
| 1 | Alta  | $120.00 groceries | 50/50 | Riley owes Alta $60.00 |
| 2 | Riley | $80.00 internet | 50/50 | Alta owes Riley $40.00 → net: Riley owes Alta $20.00 |
| 3 | Alta  | $90.00 dinner | Alta covers 25% | Riley owes Alta $67.50 → net: Riley owes Alta $87.50 |
| 4 | Riley | $35.00 personal | not shared | no effect |

Result: **Riley owes Alta $87.50.** "Settle up" records that repayment
as a transaction paid by the ower with a 0% payer share, which offsets
the balance exactly back to zero (and is excluded from spending totals).

All money is stored as integer cents, so there are no floating-point
drift issues.

## Everyday notes

- **Backups**: the entire dataset is one file. `cp finance.db
  finance-backup-$(date +%F).db` (do it while the app is idle, or use
  `sqlite3 finance.db ".backup backup.db"` anytime). Copy it off the Pi
  occasionally — SD cards die.
- **Password change / reset**: no UI for it (two users, keep it
  simple). From the Pi:
  ```bash
  cd /home/pi/pifinance && venv/bin/python -c "
  from werkzeug.security import generate_password_hash; import sqlite3
  db = sqlite3.connect('finance.db')
  db.execute('UPDATE users SET password_hash=? WHERE username=?',
             (generate_password_hash('NEW-PASSWORD'), 'USERNAME'))
  db.commit()"
  ```
- **Update the code**: copy new files over, then
  `sudo systemctl restart pifinance`.

## Troubleshooting

- *Service won't start*: `journalctl -u pifinance -e`. Most common
  causes are a wrong path in the unit file or a missing `.env`.
- *Sync says HTTP 403*: the access URL was revoked — generate a fresh
  setup token on the Bridge site and re-run `--claim`.
- *Can't reach the app from a phone*: check both devices show as
  connected in the Tailscale app, and that you're using the `100.x`
  address, not the LAN one.
