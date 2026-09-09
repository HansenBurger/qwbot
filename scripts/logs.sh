#!/usr/bin/env bash
set -eu

# Tail QWBot logs, picking the driver that matches this checkout.
#
#   sh scripts/logs.sh                 # web + scheduler, follow
#   sh scripts/logs.sh web             # one service
#   sh scripts/logs.sh scheduler 50    # last 50 lines, no follow (local driver)
#
# Force the driver with QWBOT_DEPLOY_DRIVER=local|server.

cd "$(dirname "$0")/.."

driver=$(sh scripts/deploy-driver.sh)
echo "[*] 使用 $driver logs $*"
exec bash "$driver" logs "$@"
