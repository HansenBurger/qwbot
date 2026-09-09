#!/usr/bin/env sh
set -eu

# Print the deployment driver that matches this checkout:
#
#   scripts/local-deploy.sh   developer machine (WSL / Linux / macOS)
#   scripts/deploy.sh         server
#
# scripts/logs.sh and scripts/stop.sh delegate through this so the same command
# works on a laptop and on the server. Force the choice when auto-detection is
# wrong:
#
#   QWBOT_DEPLOY_DRIVER=local  sh scripts/logs.sh
#   QWBOT_DEPLOY_DRIVER=server sh scripts/stop.sh
#
# Run it directly to see what would be picked:
#
#   sh scripts/deploy-driver.sh

cd "$(dirname "$0")/.."

if [ -n "${QWBOT_DEPLOY_DRIVER:-}" ]; then
  case "$QWBOT_DEPLOY_DRIVER" in
    local)  echo "scripts/local-deploy.sh"; exit 0 ;;
    server) echo "scripts/deploy.sh"; exit 0 ;;
    *)
      echo "QWBOT_DEPLOY_DRIVER 只能是 local 或 server，当前是: $QWBOT_DEPLOY_DRIVER" >&2
      exit 1
      ;;
  esac
fi

# A server checkout is bootstrapped by scripts/bootstrap-server.sh, which uploads
# data/sample_status.json (deploy.sh hard-requires it) and has no virtualenv.
# A developer checkout has .venv and registers batch plans through the web UI.
if [ -f data/sample_status.json ] && [ ! -d .venv ]; then
  echo "scripts/deploy.sh"
else
  echo "scripts/local-deploy.sh"
fi
