#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
ACTION_NAME="清除"
# shellcheck source=scripts/runtime-common.sh
source "$ROOT/scripts/runtime-common.sh"

require_runtime_layout
require_docker
validate_compose_config

ASSUME_YES=0
case "${1:-}" in
  -y|--yes) ASSUME_YES=1 ;;
  "") ;;
  *) fail "仅支持可选参数 --yes" ;;
esac

if [ "$ASSUME_YES" != "1" ]; then
  printf '将永久删除所有项目、阶段草稿、预览、生成数据和导出 zip。\n'
  printf '模型配置会保留，LangGraph checkpoint 与项目数据将一并删除。\n'
  printf '请输入 CLEAR 确认：'
  IFS= read -r CONFIRMATION
  [ "$CONFIRMATION" = "CLEAR" ] || {
    printf '已取消。\n'
    exit 0
  }
fi

assert_application_idle
assert_no_active_runners

if docker volume inspect "$STORAGE_VOLUME_NAME" >/dev/null 2>&1; then
  docker image inspect "$BACKEND_IMAGE" >/dev/null 2>&1 \
    || fail "缺少镜像 $BACKEND_IMAGE，无法安全保留模型配置"
fi

WAS_RUNNING=0
if backend_is_running; then
  WAS_RUNNING=1
  require_health_client
fi

printf '[1/4] 停止并移除应用容器\n'
assert_application_idle
assert_no_active_runners
compose_cmd stop --timeout 30 backend >/dev/null \
  || fail "后端容器停止失败，未删除任何数据"
assert_no_active_runners
compose_cmd stop --timeout 30 docker-api >/dev/null \
  || fail "Docker 代理容器停止失败，未删除任何数据"
compose_cmd rm -sf backend docker-api workspace-init >/dev/null \
  || fail "应用容器移除失败，未删除任何数据"
assert_no_active_runners

printf '[2/4] 删除业务数据与 LangGraph checkpoint\n'
if docker volume inspect "$STORAGE_VOLUME_NAME" >/dev/null 2>&1; then
  docker run --rm --user 0:0 --network none --read-only \
    --security-opt no-new-privileges \
    -v "$STORAGE_VOLUME_NAME:/workspace" \
    "$BACKEND_IMAGE" \
    sh -ceu '
      find /workspace -mindepth 1 -maxdepth 1 ! -name _system -exec rm -rf -- {} +
      if [ -d /workspace/_system ]; then
        find /workspace/_system -mindepth 1 -maxdepth 1 ! -name model_config.json -exec rm -rf -- {} +
      fi
    ' \
    || fail "业务数据清理失败"
fi

printf '[3/4] 删除宿主机导出文件\n'
mkdir -p "$ROOT/exports" || fail "无法创建导出目录"
find "$ROOT/exports" -depth -mindepth 1 \
  ! -path "$ROOT/exports/.gitkeep" -delete \
  || fail "宿主机导出文件清理失败"

printf '[4/4] 恢复原运行状态\n'
if [ "$WAS_RUNNING" = "1" ]; then
  start_application
  wait_for_backend 60 || fail "清除完成，但应用未在 60 秒内恢复就绪"
  printf '应用后端已重新启动。\n'
else
  printf '应用此前未运行，数据已清除但未自动启动。\n'
fi

printf '\n应用历史与 LangGraph checkpoint 已清除，.env 与前端保存的模型配置均未改动。刷新浏览器后旧项目入口会自动失效。\n'
