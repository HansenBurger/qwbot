#!/usr/bin/env bash
set -eu

# Local Docker deployment for QWBot.
#
# Unlike scripts/deploy.sh (server deployment, requires data/sample_status.json),
# this script is meant for a developer machine: it bootstraps .env and data/,
# works around a broken Docker credential helper, keeps the host port free and
# never needs sample data because the database seeds itself on first start.
#
# Usage:
#   scripts/local-deploy.sh <command> [args]

cd "$(dirname "$0")/.."

PROJECT="qwbot"
ENV_FILE=".env"
DB_FILE="data/qwbot.sqlite3"
WORKDIR="tmp"

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

usage() {
  cat <<EOF
用法: $(basename "$0") <command> [args]

部署
  deploy              构建镜像（如需要）并启动 web + scheduler
  rebuild             不使用缓存重新构建镜像，并重建容器
  restart             重启容器，不重新构建
  stop                停止并移除容器（保留 .env 和 data/）

排查
  logs [service] [N]  查看日志；默认跟随输出，给 N 则只打印最后 N 行后退出
                      service 可选 web | scheduler，默认两个都看
  status              容器、镜像、端口、数据库和通知配置概览
  url                 打印访问地址并探测连通性
  shell [service]     进入容器 shell，默认 web

自测
  dispatch [HH:MM]    在容器内手动执行一次定时派发，可指定时间模拟到点
  reset-db            停止容器并删除本地运行态数据库，下次启动按 .env 重新播种

  help                显示本帮助

说明
  - webhook 不再依赖 .env：首次启动播种一次，之后请在页面上维护。
  - 所有命令都可以反复执行，deploy 是幂等的。
EOF
  exit "${1:-1}"
}

log()  { echo "[*] $*"; }
warn() { echo "[!] $*" >&2; }
die()  { echo "[x] $*" >&2; exit 1; }

# `docker compose` (plugin) with a fallback to the standalone `docker-compose`.
if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  DC=(docker-compose)
else
  DC=()
fi

require_docker() {
  command -v docker >/dev/null 2>&1 || die "找不到 docker，请先安装 Docker。"
  docker info >/dev/null 2>&1 || die "docker 守护进程不可用，请先启动 Docker Desktop / dockerd。"
  if [ "${#DC[@]}" -eq 0 ]; then
    die "找不到 docker compose，请安装 compose 插件或 docker-compose。"
  fi
}

# Docker Desktop under WSL ships credsStore=desktop.exe, which the Linux daemon
# cannot execute ("exec format error"), so every build dies while pulling the
# base image. Detect exactly that failure and fall back to a credential-free
# config for this run only; the global ~/.docker/config.json is never modified.
fix_docker_credentials() {
  [ -n "${QWBOT_DOCKER_CONFIG_FORCE:-}" ] && return 0

  local config_dir="${DOCKER_CONFIG:-$HOME/.docker}"
  local config_file="$config_dir/config.json"
  [ -f "$config_file" ] || return 0

  local helper_name
  helper_name=$(sed -n 's/.*"credsStore"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$config_file" | head -1)
  [ -n "$helper_name" ] || return 0

  local helper
  helper=$(command -v "docker-credential-$helper_name" 2>/dev/null || true)
  [ -n "$helper" ] || return 0

  # Only bail out when the helper really cannot be executed on this platform.
  local err
  err=$("$helper" list 2>&1 >/dev/null || true)
  case "$err" in
    *"exec format error"* | *"cannot execute binary file"*) ;;
    *) return 0 ;;
  esac

  mkdir -p "$WORKDIR/docker-config"
  echo '{}' > "$WORKDIR/docker-config/config.json"
  export DOCKER_CONFIG="$PWD/$WORKDIR/docker-config"
  warn "docker 凭据助手 docker-credential-$helper_name 在本平台无法执行，"
  warn "本次改用 DOCKER_CONFIG=$DOCKER_CONFIG（匿名拉取公共镜像不受影响）。"
}

ensure_env_file() {
  if [ ! -f "$ENV_FILE" ]; then
    cp .env.example "$ENV_FILE"
    warn "已从 .env.example 创建 .env，里面是占位 webhook。"
    warn "可以先启动，然后在页面「消息通知」里新增真实的群机器人；"
    warn "也可以编辑 .env 后执行: $(basename "$0") reset-db && $(basename "$0") deploy"
  fi
}

env_value() {
  # Read a key from .env without sourcing it, keeping the caller's env clean.
  local key="$1" default="${2:-}" value
  value=$(sed -n "s/^[[:space:]]*${key}=[[:space:]]*//p" "$ENV_FILE" 2>/dev/null | tail -1 | tr -d '\r')
  if [ -n "$value" ]; then echo "$value"; else echo "$default"; fi
}

set_env_value() {
  local key="$1" value="$2" escaped
  escaped=$(printf '%s' "$value" | sed -e 's/[&|\\]/\\&/g')
  if grep -qE "^[[:space:]]*${key}=" "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^[[:space:]]*${key}=.*|${key}=${escaped}|" "$ENV_FILE"
  else
    echo "${key}=${value}" >> "$ENV_FILE"
  fi
}

port_in_use() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -tln 2>/dev/null | grep -qE "[:.]${port}[[:space:]]"
  elif command -v netstat >/dev/null 2>&1; then
    netstat -tln 2>/dev/null | grep -qE "[:.]${port}[[:space:]]"
  else
    (exec 3<>"/dev/tcp/127.0.0.1/${port}") 2>/dev/null
  fi
}

our_containers_running() {
  [ -n "$("${DC[@]}" ps --status running --services 2>/dev/null)" ]
}

# Pick a free host port and keep .env in sync, mirroring scripts/dev.sh.
ensure_port_free() {
  local port candidate frontend
  port=$(env_value QWBOT_PORT 5000)

  if ! port_in_use "$port"; then
    set_env_value QWBOT_PORT "$port"
    echo "$port"
    return 0
  fi
  if our_containers_running; then
    # Our own containers already hold the port, which is exactly what we want.
    echo "$port"
    return 0
  fi

  candidate=$port
  while port_in_use "$candidate"; do
    candidate=$((candidate + 1))
    if [ "$candidate" -gt $((port + 100)) ]; then
      die "端口 ${port}-$((port + 100)) 全部被占用，请手动设置 .env 的 QWBOT_PORT。"
    fi
  done

  warn "端口 $port 已被其他进程占用，改用 $candidate（已写入 .env）。"
  set_env_value QWBOT_PORT "$candidate"
  frontend=$(env_value FRONTEND_URL "")
  if [ -n "$frontend" ]; then
    set_env_value FRONTEND_URL "$(printf '%s' "$frontend" | sed -E "s|:[0-9]+(/?)$|:${candidate}\1|")"
  fi
  echo "$candidate"
}

web_url() {
  echo "http://127.0.0.1:$(env_value QWBOT_PORT 5000)"
}

require_running() {
  our_containers_running || die "容器没有在运行，先执行: $(basename "$0") deploy"
}

build_image() {
  local mode="$1"
  if [ "$mode" = "if-missing" ] && docker image inspect "${PROJECT}:latest" >/dev/null 2>&1; then
    log "镜像 ${PROJECT}:latest 已存在，跳过构建（需要强制重建请用 rebuild）。"
    return 0
  fi
  if [ "$mode" = "no-cache" ]; then
    log "重新构建镜像（不使用缓存）..."
    "${DC[@]}" build --no-cache
  else
    log "构建镜像..."
    "${DC[@]}" build
  fi
}

start_services() {
  local port
  port=$(ensure_port_free)
  log "启动容器（web 端口 $port）..."
  "${DC[@]}" up -d
  sleep 2
  "${DC[@]}" ps
  echo ""
  log "维护页面: $(web_url)/"
  log "通知配置: $(web_url)/notifications"
  log "查看日志: $(basename "$0") logs"
}

# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

cmd_deploy() {
  require_docker
  fix_docker_credentials
  ensure_env_file
  mkdir -p data
  build_image "if-missing"
  start_services
}

cmd_rebuild() {
  require_docker
  fix_docker_credentials
  ensure_env_file
  mkdir -p data
  build_image "no-cache"
  log "重建容器..."
  "${DC[@]}" up -d --force-recreate
  sleep 2
  "${DC[@]}" ps
  echo ""
  log "维护页面: $(web_url)/notifications"
}

cmd_restart() {
  require_docker
  fix_docker_credentials
  ensure_env_file
  if our_containers_running; then
    log "重启容器..."
    "${DC[@]}" restart
  else
    warn "容器没有在运行，改为启动。"
    build_image "if-missing"
    "${DC[@]}" up -d
  fi
  sleep 2
  "${DC[@]}" ps
}

cmd_stop() {
  require_docker
  fix_docker_credentials
  log "停止并移除容器（.env 和 data/ 保留）..."
  "${DC[@]}" down
  log "已停止。"
}

cmd_logs() {
  require_docker
  fix_docker_credentials
  local service="" lines="" arg
  for arg in "$@"; do
    case "$arg" in
      web | scheduler) service="$arg" ;;
      ''|*[!0-9]*)     die "无法识别的 logs 参数: $arg（可用: web | scheduler | 行数）" ;;
      *)               lines="$arg" ;;
    esac
  done

  if [ -n "$lines" ]; then
    # One-shot: print the last N lines and exit instead of following.
    "${DC[@]}" logs --tail "$lines" ${service:+"$service"}
  else
    "${DC[@]}" logs -f --tail 100 ${service:+"$service"}
  fi
}

cmd_status() {
  require_docker
  fix_docker_credentials

  echo "=== 容器 ==="
  "${DC[@]}" ps -a 2>/dev/null || true
  echo ""

  echo "=== 镜像 ==="
  # `docker images --format` is used instead of `docker image inspect --format`:
  # the containerd image store returns a different inspect shape.
  local image_line
  image_line=$(docker images "${PROJECT}:latest" \
    --format '{{.Repository}}:{{.Tag}}  id={{.ID}}  built={{.CreatedSince}}  size={{.Size}}' 2>/dev/null)
  if [ -n "$image_line" ]; then
    echo "  $image_line"
  else
    echo "  ${PROJECT}:latest 尚未构建，执行: $(basename "$0") deploy"
  fi
  echo ""

  echo "=== 配置 ==="
  printf '  .env           %s\n' "$([ -f "$ENV_FILE" ] && echo "存在" || echo "缺失（deploy 会自动创建）")"
  printf '  QWBOT_PORT     %s\n' "$(env_value QWBOT_PORT 5000)"
  printf '  FRONTEND_URL   %s\n' "$(env_value FRONTEND_URL '(未设置)')"
  printf '  WEBHOOK_TARGET %s\n' "$(env_value WECOM_WEBHOOK_TARGET '(未设置)')"
  if [ -f "$DB_FILE" ]; then
    printf '  数据库         %s (%s)\n' "$DB_FILE" "$(du -h "$DB_FILE" | cut -f1)"
  else
    printf '  数据库         %s 尚未创建\n' "$DB_FILE"
  fi
  echo ""

  if our_containers_running; then
    echo "=== 通知配置（读取容器内数据库） ==="
    "${DC[@]}" exec -T web python - <<'PY' || echo "  读取失败，容器可能还在启动中"
from pathlib import Path

from qwbot.store import load_webhook_tree
from qwbot.web import mask_webhook_url

for webhook in load_webhook_tree(Path("data/qwbot.sqlite3")):
    tags = []
    if webhook["is_default"]:
        tags.append("默认")
    if not webhook["is_enabled"]:
        tags.append("已停用")
    suffix = f" [{'/'.join(tags)}]" if tags else ""
    print(f"  [{webhook['id']}] {webhook['name']} ({webhook['target_label']}){suffix}")
    print(f"      {mask_webhook_url(webhook['url'])}")
    if not webhook["templates"]:
        print("      - 暂无消息模板")
    for template in webhook["templates"]:
        times = ", ".join(
            f"{s['send_time']}{'' if s['is_enabled'] else '(停用)'}" for s in template["schedules"]
        ) or "无通知时间"
        state = "" if template["is_enabled"] else "  [模板已停用]"
        print(f"      - {template['name']}: {times}{state}")
PY
    echo ""
    log "维护页面: $(web_url)/notifications"
  else
    log "容器未运行，执行 $(basename "$0") deploy 启动。"
  fi
}

cmd_url() {
  local url code
  url=$(web_url)
  echo "$url/"
  echo "$url/notifications"
  if command -v curl >/dev/null 2>&1; then
    code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$url/notifications" || echo "000")
    if [ "$code" = "200" ]; then
      log "web 服务可访问 (HTTP $code)"
    else
      warn "web 服务返回 HTTP $code，容器可能没在运行: $(basename "$0") status"
    fi
  fi
}

cmd_shell() {
  require_docker
  fix_docker_credentials
  require_running
  local service="${1:-web}"
  case "$service" in
    web | scheduler) ;;
    *) die "未知服务: $service（可用: web | scheduler）" ;;
  esac
  "${DC[@]}" exec "$service" bash
}

cmd_dispatch() {
  require_docker
  fix_docker_credentials
  require_running
  local args=(run-scheduled-once)
  if [ -n "${1:-}" ]; then
    printf '%s' "$1" | grep -qE '^[0-9]{1,2}:[0-9]{2}$' \
      || die "时间格式必须是 HH:MM，例如 09:00"
    args+=(--at "$1")
    log "以 $1 代替当前时间执行一次派发..."
  else
    log "以当前时间执行一次派发..."
  fi
  "${DC[@]}" exec -T scheduler python -m qwbot.cli "${args[@]}"
}

cmd_reset_db() {
  require_docker
  fix_docker_credentials

  if our_containers_running; then
    log "先停止容器..."
    "${DC[@]}" down
  fi

  if [ ! -f "$DB_FILE" ]; then
    log "数据库不存在，无需清理。"
    return 0
  fi

  if rm -f "$DB_FILE" 2>/dev/null; then
    log "已删除 $DB_FILE"
  else
    # The file is usually owned by root because the container creates it.
    warn "$DB_FILE 属于容器内 root 用户，用一次性容器删除..."
    docker run --rm -v "$PWD/data:/app/data" --entrypoint python "${PROJECT}:latest" \
      -c "import pathlib; pathlib.Path('/app/data/qwbot.sqlite3').unlink(missing_ok=True)"
    log "已删除 $DB_FILE"
  fi
  log "下次 deploy 会按 .env 重新播种机器人，页面上维护的配置需要重新添加。"
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

cmd="${1:-help}"
shift 2>/dev/null || true

case "$cmd" in
  deploy)   cmd_deploy "$@" ;;
  rebuild)  cmd_rebuild "$@" ;;
  restart)  cmd_restart "$@" ;;
  stop)     cmd_stop "$@" ;;
  logs)     cmd_logs "$@" ;;
  status)   cmd_status "$@" ;;
  url)      cmd_url "$@" ;;
  shell)    cmd_shell "$@" ;;
  dispatch) cmd_dispatch "$@" ;;
  reset-db) cmd_reset_db "$@" ;;
  help|-h|--help) usage 0 ;;
  *)
    warn "未知命令: $cmd"
    usage 1
    ;;
esac
