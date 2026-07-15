#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
ACTION_NAME="更新"
# shellcheck source=scripts/runtime-common.sh
source "$ROOT/scripts/runtime-common.sh"

require_runtime_layout
require_docker
validate_compose_config
require_health_client
require_command tar
require_command git
assert_no_active_runners

printf '[1/6] 同步 testlib/jngen 固定版本\n'
sync_fixed_dependencies

printf '[2/6] 使用当前源码重建后端镜像\n'
build_backend_image

printf '[3/6] 暂停任务入口\n'
assert_application_idle
assert_no_active_runners
compose_cmd stop --timeout 30 backend \
  || fail "后端停止失败，未重建 runner 镜像"

printf '[4/6] 使用固定依赖重建受限 runner 双镜像\n'
build_runner_images

printf '[5/6] 重建应用容器\n'
assert_no_active_runners
compose_cmd up -d --no-build --force-recreate --remove-orphans \
  docker-api workspace-init backend \
  || fail "应用容器重建失败"

printf '[6/6] '
wait_for_backend 60 || fail "应用未在 60 秒内就绪"
printf '\n更新已生效，业务状态、模型配置与 LangGraph checkpoint 已保留。\n'
