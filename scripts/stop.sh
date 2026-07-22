#!/usr/bin/env bash
set -eu
cd "$(dirname "$0")/.."
exec bash scripts/deploy.sh stop
