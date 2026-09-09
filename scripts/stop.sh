#!/usr/bin/env bash
set -eu

# Stop QWBot containers, picking the driver that matches this checkout.
# .env and data/ are kept either way.
#
# Force the driver with QWBOT_DEPLOY_DRIVER=local|server.

cd "$(dirname "$0")/.."

driver=$(sh scripts/deploy-driver.sh)
echo "[*] 使用 $driver stop"
exec bash "$driver" stop
