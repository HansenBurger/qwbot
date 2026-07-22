#!/usr/bin/env bash
set -eu

cd "$(dirname "$0")/.."

usage() {
  cat <<EOF
用法: $(basename "$0") <command>

Commands:
  start          Build image (if needed) and start all services
  stop           Stop all services
  restart        Restart all services
  rebuild        Rebuild image and restart all services
  logs [service] Tail logs (optionally for a specific service)
  status         Show service and container status
  shell          Open a shell in the web container

EOF
  exit 1
}

check_env() {
  if [ ! -f .env ]; then
    echo "[!] Missing .env file. Run scripts/bootstrap-server.sh first."
    exit 1
  fi
}

check_data() {
  if [ ! -f data/sample_status.json ]; then
    echo "[!] Missing data/sample_status.json. Upload it before deployment."
    exit 1
  fi
}

cmd_start() {
  check_env
  check_data
  echo "[*] Building image..."
  docker compose build
  echo "[*] Starting services..."
  docker compose up -d
  echo "[*] Done. Service status:"
  docker compose ps
}

cmd_stop() {
  echo "[*] Stopping services..."
  docker compose down
  echo "[*] Services stopped."
}

cmd_restart() {
  echo "[*] Restarting services..."
  docker compose restart
  echo "[*] Services restarted."
  docker compose ps
}

cmd_rebuild() {
  check_env
  check_data
  echo "[*] Rebuilding image (no cache)..."
  docker compose build --no-cache
  echo "[*] Recreating containers..."
  docker compose up -d --force-recreate
  echo "[*] Done. Service status:"
  docker compose ps
}

cmd_logs() {
  local service="${1:-}"
  if [ -n "$service" ]; then
    docker compose logs -f "$service"
  else
    docker compose logs -f
  fi
}

cmd_status() {
  echo "=== Docker Compose Services ==="
  docker compose ps
  echo ""
  echo "=== Docker Images ==="
  docker images qwbot:latest --format "table {{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.CreatedAt}}\t{{.Size}}"
  echo ""
  echo "=== Disk Usage ==="
  docker system df 2>/dev/null || true
}

cmd_shell() {
  docker compose exec web bash
}

# --- Main ---
cmd="${1:-}"
shift 2>/dev/null || true

case "$cmd" in
  start)    cmd_start "$@" ;;
  stop)     cmd_stop "$@" ;;
  restart)  cmd_restart "$@" ;;
  rebuild)  cmd_rebuild "$@" ;;
  logs)     cmd_logs "$@" ;;
  status)   cmd_status "$@" ;;
  shell)    cmd_shell "$@" ;;
  *)        usage ;;
esac
