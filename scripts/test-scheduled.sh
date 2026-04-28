#!/usr/bin/env sh
set -eu

docker compose run --rm scheduler python -m qwbot.cli run-scheduled-once
