#!/usr/bin/env sh
set -eu

service="${1:-}"

if [ -n "$service" ]; then
  docker compose logs -f "$service"
else
  docker compose logs -f
fi
