#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
ACTION_NAME="关闭"
# shellcheck source=scripts/runtime-common.sh
source "$ROOT/scripts/runtime-common.sh"

require_runtime_layout
require_docker
validate_compose_config

if ! backend_is_running && ! compose_service_is_running docker-api; then
  printf '服务已经停止，模型配置、LangGraph checkpoint 和业务数据均已保留。\n'
  exit 0
fi

assert_application_idle
assert_no_active_runners
compose_cmd stop --timeout 30 backend || fail "后端容器停止失败"
assert_no_active_runners
compose_cmd stop --timeout 30 docker-api || fail "Docker 代理容器停止失败"
printf '服务已停止，模型配置、LangGraph checkpoint 和业务数据均已保留。\n'
