#!/usr/bin/env sh
set -eu

mkdir -p data

if [ ! -f .env ]; then
  cp .env.deploy.example .env
  echo "Created .env from .env.deploy.example. Please edit .env before starting services."
fi

if [ ! -f data/sample_status.json ]; then
  echo "Missing data/sample_status.json."
  echo "Please manually upload the production sample_status.json to ./data/sample_status.json."
fi

echo "Bootstrap finished."
