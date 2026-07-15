#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
ACTION_NAME="启动"
# shellcheck source=scripts/runtime-common.sh
source "$ROOT/scripts/runtime-common.sh"

require_runtime_layout
require_docker
validate_compose_config
require_health_client
ensure_runtime_images

if backend_is_running; then
  compose_service_is_running docker-api \
    || fail "后端仍在运行，但 Docker 代理已停止；请先运行 ./stop.sh 再重新启动"
  wait_for_backend 15 || fail "后端容器正在运行，但健康检查未通过"
  printf '\n应用已经在运行：%s\n' "$APP_URL"
  exit 0
fi

start_application
wait_for_backend 60 || fail "应用未在 60 秒内就绪"
printf '\n应用工作台：%s\n' "$APP_URL"
