#!/usr/bin/env bash
# deploy.sh — one-command Phase 0 deploy for the Ledger agent layer.
# Wraps walkthrough steps 1-5; prints exact instructions for 6-7.
# Safe to re-run. Nothing here touches app tables.
#
#   LEDGER_DB=/home/pi/financeapp/finance.db ./deploy.sh
#   Flags: --skip-ntfy   --skip-service
set -euo pipefail
cd "$(dirname "$0")"

LEDGER_DB="${LEDGER_DB:-$HOME/financeapp/finance.db}"
export LEDGER_DB
SKIP_NTFY=false; SKIP_SERVICE=false
for a in "$@"; do
  [ "$a" = "--skip-ntfy" ] && SKIP_NTFY=true
  [ "$a" = "--skip-service" ] && SKIP_SERVICE=true
done
say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

say "0/5 preflight"
python3 preflight.py || { echo "Preflight failed — fix the above, re-run."; exit 1; }
PF=$(python3 preflight.py --json)
SPLIT_MODE=$(python3 - "$PF" <<'EOF'
import json,sys
d=json.loads(sys.argv[1]).get("split_mode") or {}
print(d.get("mode") or "" if d.get("confidence")=="high" else "")
EOF
)

say "1/5 dependencies"
python3 -c "import mcp, uvicorn" 2>/dev/null \
  || pip install "mcp[cli]" uvicorn --break-system-packages
python3 -c "import mcp, uvicorn; print('deps ok')"

say "2/5 migration"
python3 migrate_agent.py

say "3/5 tokens"
if python3 tokens_cli.py list | grep -q "active"; then
  echo "Active tokens already exist — skipping creation (tokens_cli.py list):"
  python3 tokens_cli.py list
else
  echo "Creating one token per person. COPY EACH lgr_... LINE INTO A"
  echo "PASSWORD MANAGER NOW — they are shown once and never stored."
  for u in $(sqlite3 "$LEDGER_DB" "SELECT username FROM users"); do
    python3 tokens_cli.py create --label "$u claude" --user "$u"
  done
fi

if ! $SKIP_SERVICE; then
  say "4/5 MCP service"
  sed -e "s|^User=.*|User=$(whoami)|" \
      -e "s|^WorkingDirectory=.*|WorkingDirectory=$(pwd)|" \
      -e "s|^Environment=LEDGER_DB=.*|Environment=LEDGER_DB=$LEDGER_DB|" \
      -e "s|^ExecStart=.*|ExecStart=$(command -v python3) $(pwd)/ledger_mcp.py|" \
      deploy/ledger-mcp.service > /tmp/ledger-mcp.service
  if [ -n "$SPLIT_MODE" ]; then
    sed -i "s|^# Environment=LEDGER_SPLIT_MODE=.*|Environment=LEDGER_SPLIT_MODE=$SPLIT_MODE|" /tmp/ledger-mcp.service
    echo "Split mode auto-detected from your schema: $SPLIT_MODE"
  else
    echo "Split mode NOT auto-detected — balance tool stays gated (step 7 is manual)."
  fi
  sudo cp /tmp/ledger-mcp.service /etc/systemd/system/ledger-mcp.service
  sudo systemctl daemon-reload
  sudo systemctl enable --now ledger-mcp
  sleep 2
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
    http://127.0.0.1:8091/mcp -H 'Content-Type: application/json' -d '{}' || true)
  if [ "$CODE" = "401" ]; then
    echo "Server up, anonymous requests rejected (401) — correct."
  else
    echo "Expected 401, got '$CODE'. Inspect: journalctl -u ledger-mcp -n 30"
    exit 1
  fi
fi

if ! $SKIP_NTFY; then
  say "5/5 ntfy alerts"
  DEFAULT_TOPIC="ledger-$(openssl rand -hex 6)"
  read -rp "ntfy topic [$DEFAULT_TOPIC]: " TOPIC
  TOPIC="${TOPIC:-$DEFAULT_TOPIC}"
  echo "Subscribe BOTH phones to '$TOPIC' in the ntfy app, then press enter."
  read -r
  curl -sf -d "Ledger alerts connected" "ntfy.sh/$TOPIC" >/dev/null \
    && echo "Test push sent — both phones should have buzzed." \
    || echo "Test push failed — check connectivity; continuing anyway."
  SYNC_UNIT=$(python3 - "$PF" <<'EOF'
import json,sys
c=json.loads(sys.argv[1]).get("sync_services") or []
print(next((s for s in c if s.endswith(".service")), c[0] if c else ""))
EOF
)
  read -rp "Sync service to hook [$SYNC_UNIT]: " UNIT
  UNIT="${UNIT:-$SYNC_UNIT}"
  if [ -n "$UNIT" ]; then
    UNIT="${UNIT%.service}"
    sudo mkdir -p "/etc/systemd/system/${UNIT}.service.d"
    sed -e "s|^Environment=NTFY_TOPIC=.*|Environment=NTFY_TOPIC=$TOPIC|" \
        -e "s|^Environment=LEDGER_DB=.*|Environment=LEDGER_DB=$LEDGER_DB|" \
        -e "s|/home/pi/ledger-agent|$(pwd)|" \
        deploy/sync-notify-override.conf \
      | sudo tee "/etc/systemd/system/${UNIT}.service.d/override.conf" >/dev/null
    sudo systemctl daemon-reload
    echo "Hook installed on ${UNIT}.service. Dry run:"
    python3 notify.py --dry-run
  else
    echo "No sync service named — install deploy/sync-notify-override.conf manually later."
  fi
fi

say "done — two steps remain, from your laptop"
HOST=$(command -v tailscale >/dev/null && tailscale status --self --peers=false 2>/dev/null | awk 'NR==1{print $2}' || hostname)
cat <<EOF
6) Connect Claude Code (per person, with their own token):
     claude mcp add ledger --transport http http://${HOST}:8091/mcp \\
       --header "Authorization: Bearer lgr_..."
   Then ask: "How's our spending this month?"

7) Balance gate:
$(if [ -n "$SPLIT_MODE" ]; then
  echo "   Split mode was set to '$SPLIT_MODE' from your schema's own comment."
  echo "   Ask Claude 'who owes whom right now?' and confirm it MATCHES the"
  echo "   app dashboard once before trusting it."
else
  echo "   Not auto-detected. Run: sqlite3 \$LEDGER_DB '.schema transactions',"
  echo "   read the split column comment, set LEDGER_SPLIT_MODE in"
  echo "   /etc/systemd/system/ledger-mcp.service, restart, verify vs dashboard."
fi)
EOF
