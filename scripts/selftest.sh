#!/usr/bin/env bash
set -eu

# Local self-test for the webhook -> message template -> notification time model.
#
#   scripts/selftest.sh            run unit tests + live end-to-end test
#   scripts/selftest.sh --unit     run only the pytest suite
#   scripts/selftest.sh --live     run only the live end-to-end test
#
# The live part starts a mock WeCom webhook and a throwaway QWBot web service on a
# temporary database, so it never touches data/qwbot.sqlite3 or a real group.

cd "$(dirname "$0")/.."

MODE="${1:-all}"
MOCK_PORT="${MOCK_PORT:-8089}"
WEB_PORT="${WEB_PORT:-5055}"
PYTHON="${PYTHON:-.venv/bin/python}"
WORKDIR="tmp/selftest"

if [ ! -x "$PYTHON" ]; then
  echo "[!] $PYTHON not found. Create it with: python3 -m venv .venv && pip install -e '.[dev]'"
  exit 1
fi

cleanup() {
  if [ -n "${WEB_PID:-}" ]; then kill "$WEB_PID" 2>/dev/null || true; fi
  if [ -n "${MOCK_PID:-}" ]; then kill "$MOCK_PID" 2>/dev/null || true; fi
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

wait_for_http() {
  local url="$1" tries=0
  until curl -fsS -m 2 "$url" > /dev/null 2>&1; do
    tries=$((tries + 1))
    if [ "$tries" -gt 60 ]; then
      echo "[!] $url did not come up in time"
      return 1
    fi
    sleep 0.25
  done
}

run_unit() {
  echo "========================================="
  echo " pytest"
  echo "========================================="
  "$PYTHON" -m pytest
}

run_live() {
  echo ""
  echo "========================================="
  echo " live end-to-end (mock webhook)"
  echo "========================================="
  rm -rf "$WORKDIR"
  mkdir -p "$WORKDIR"

  cat > "$WORKDIR/.env" <<EOF
WECOM_WEBHOOK_URL_PROD=http://127.0.0.1:$MOCK_PORT/cgi-bin/webhook/send?key=MOCKPROD0001
WECOM_WEBHOOK_URL_TEST=http://127.0.0.1:$MOCK_PORT/cgi-bin/webhook/send?key=MOCKTEST0001
WECOM_WEBHOOK_TARGET=test
TIMEZONE=Asia/Shanghai
PROGRESS_DOC_URL=https://doc.weixin.qq.com/sheet/selftest-progress
BATCH_REGISTER_DOC_URL=https://doc.weixin.qq.com/sheet/selftest-batch
CASE_ASSIGNMENT_DOC_URL=https://doc.weixin.qq.com/sheet/selftest-cases
AGENDA_DOC_URL=https://doc.weixin.qq.com/sheet/selftest-agenda
FRONTEND_URL=http://127.0.0.1:$WEB_PORT
LOCAL_STATUS_FILE=$WORKDIR/sample_status.json
QWBOT_DB_PATH=$WORKDIR/qwbot.sqlite3
EOF

  echo "[*] starting mock webhook on :$MOCK_PORT"
  python3 scripts/mock-webhook.py --host 127.0.0.1 --port "$MOCK_PORT" \
    > "$WORKDIR/mock-webhook.log" 2>&1 &
  MOCK_PID=$!
  wait_for_http "http://127.0.0.1:$MOCK_PORT/__health"

  echo "[*] starting QWBot web on :$WEB_PORT"
  # The self-test .env must win over a developer's own .env file.
  set -a
  # shellcheck disable=SC1090
  . "$WORKDIR/.env"
  set +a
  "$PYTHON" -m qwbot.cli web --host 127.0.0.1 --port "$WEB_PORT" \
    > "$WORKDIR/web.log" 2>&1 &
  WEB_PID=$!
  wait_for_http "http://127.0.0.1:$WEB_PORT/notifications"

  "$PYTHON" scripts/selftest.py \
    --base-url "http://127.0.0.1:$WEB_PORT" \
    --mock-url "http://127.0.0.1:$MOCK_PORT" \
    --db "$WORKDIR/qwbot.sqlite3" \
    --cli-command "$PYTHON -m qwbot.cli"
}

case "$MODE" in
  --unit) run_unit ;;
  --live) run_live ;;
  all)    run_unit; run_live ;;
  *)
    echo "Usage: scripts/selftest.sh [--unit|--live]"
    exit 1
    ;;
esac

echo ""
echo "Self-test finished."
