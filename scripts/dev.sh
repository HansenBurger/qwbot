#!/usr/bin/env bash
set -eu

# Start QWBot for local Linux/macOS development and testing.
# Usage:
#   scripts/dev.sh [command] [test|prod] [port] [host]
#
# Commands:
#   web          Start web service (default).
#   preview      Print scheduled reminder without sending.
#   send         Force send scheduled reminder.
#   test         Send webhook connectivity test message.
#   scheduled    Run scheduled logic once with business-day checks.
#   schedule     Start the persistent scheduler daemon.

cd "$(dirname "$0")/.."

COMMAND="${1:-web}"
WEBHOOK_TARGET="${2:-test}"
HOST="${4:-0.0.0.0}"

# Map shorthand to CLI subcommand.
case "$COMMAND" in
  web)       CLI_CMD="web" ;;
  preview)   CLI_CMD="preview" ;;
  send)      CLI_CMD="send-now" ;;
  test)      CLI_CMD="test-webhook" ;;
  scheduled) CLI_CMD="run-scheduled-once" ;;
  schedule)  CLI_CMD="schedule" ;;
  *)
    echo "Unknown command: $COMMAND"
    echo "Usage: scripts/dev.sh [web|preview|send|test|scheduled|schedule] [test|prod] [port] [host]"
    exit 1
    ;;
esac

# Activate virtual environment.
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
else
  echo "Virtual environment not found. Run: python3 -m venv .venv && pip install -e ."
  exit 1
fi

# Copy example .env if missing.
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example — edit it with real webhook keys before sending."
fi

export WECOM_WEBHOOK_TARGET="$WEBHOOK_TARGET"
export QWBOT_DB_PATH="${QWBOT_DB_PATH:-data/qwbot.sqlite3}"

# --- Port management ---
# Read QWBOT_PORT from .env (fallback 5000), CLI arg overrides it.
ENV_PORT=$(grep -E '^QWBOT_PORT=' .env 2>/dev/null | cut -d= -f2 | tr -d '[:space:]')
ENV_PORT="${ENV_PORT:-5000}"
PORT="${3:-$ENV_PORT}"

is_port_free() {
  ! ss -tln 2>/dev/null | grep -qE ":${1}\b"
}

find_free_port() {
  local p=$1
  while ! is_port_free "$p"; do
    p=$((p + 1))
  done
  echo "$p"
}

if [ "$CLI_CMD" = "web" ]; then
  if ! is_port_free "$PORT"; then
    NEW_PORT=$(find_free_port "$PORT")
    echo "Port $PORT is occupied, switching to $NEW_PORT."
    # Update QWBOT_PORT in .env.
    if grep -qE '^QWBOT_PORT=' .env 2>/dev/null; then
      sed -i "s/^QWBOT_PORT=.*/QWBOT_PORT=$NEW_PORT/" .env
    else
      echo "QWBOT_PORT=$NEW_PORT" >> .env
    fi
    # Update FRONTEND_URL port in .env to match.
    if grep -qE '^FRONTEND_URL=' .env 2>/dev/null; then
      sed -i -E "s|^(FRONTEND_URL=http://[^:]*):[0-9]+|\1:$NEW_PORT|" .env
    fi
    PORT="$NEW_PORT"
  else
    # Make sure .env stays in sync when port is free.
    if grep -qE '^QWBOT_PORT=' .env 2>/dev/null; then
      sed -i "s/^QWBOT_PORT=.*/QWBOT_PORT=$PORT/" .env
    else
      echo "QWBOT_PORT=$PORT" >> .env
    fi
  fi
fi

# Show the first LAN IP for convenience when binding to 0.0.0.0.
LAN_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
DISPLAY_HOST="${LAN_IP:-$HOST}"
# Also sync FRONTEND_URL in .env with LAN IP so pushed links work for teammates.
if [ "$CLI_CMD" = "web" ] && [ -n "$LAN_IP" ]; then
  sed -i "s|^FRONTEND_URL=.*|FRONTEND_URL=http://$LAN_IP:$PORT|" .env
fi

echo "========================================="
echo " QWBot local dev"
echo "========================================="
echo " Command:  $COMMAND ($CLI_CMD)"
echo " Webhook:  $WEBHOOK_TARGET"
if [ "$CLI_CMD" = "web" ]; then
  echo " Listen:   $HOST:$PORT"
  echo " URL:      http://$DISPLAY_HOST:$PORT"
fi
echo " DB:       $QWBOT_DB_PATH"
echo "========================================="
echo ""

EXTRA_ARGS=()
if [ "$CLI_CMD" = "web" ]; then
  EXTRA_ARGS+=(--host "$HOST" --port "$PORT")
elif [ "$CLI_CMD" = "test-webhook" ] || [ "$CLI_CMD" = "preview" ] || [ "$CLI_CMD" = "send-now" ] || [ "$CLI_CMD" = "run-scheduled-once" ]; then
  EXTRA_ARGS+=(--webhook "$WEBHOOK_TARGET")
fi

exec python -m qwbot.cli "$CLI_CMD" "${EXTRA_ARGS[@]}"
