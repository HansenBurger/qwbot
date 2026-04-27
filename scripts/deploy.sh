#!/usr/bin/env sh
set -eu

if [ ! -f .env ]; then
  echo "Missing .env. Run scripts/bootstrap-server.sh and fill .env first."
  exit 1
fi

if [ ! -f data/sample_status.json ]; then
  echo "Missing data/sample_status.json. Upload it before deployment."
  exit 1
fi

docker compose build
docker compose up -d
docker compose ps
