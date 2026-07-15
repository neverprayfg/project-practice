#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

fail() {
  printf '清除失败：%s\n' "$1" >&2
  exit 1
}

command -v docker >/dev/null 2>&1 || fail "未找到 Docker"
docker version >/dev/null 2>&1 || fail "Docker Engine 不可用，请先启动 Docker Desktop"

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

WAS_RUNNING=0
if docker compose ps --status running --services 2>/dev/null | grep -qx backend; then
  WAS_RUNNING=1
fi

printf '[1/4] 停止并移除应用后端容器\n'
docker compose stop backend >/dev/null 2>&1 || true
docker compose rm -sf backend workspace-init >/dev/null 2>&1 || true

printf '[2/4] 删除业务数据卷\n'
if docker volume inspect contest_dataset_storage >/dev/null 2>&1; then
  docker volume rm contest_dataset_storage >/dev/null
fi

printf '[3/4] 删除宿主机导出文件\n'
mkdir -p exports
find exports -mindepth 1 -maxdepth 1 -type f ! -name '.gitkeep' -delete

printf '[4/4] 恢复原运行状态\n'
if [ "$WAS_RUNNING" = "1" ]; then
  docker compose up -d --no-build workspace-init backend
  printf '应用后端已重新启动。\n'
else
  printf '应用此前未运行，数据已清除但未自动启动。\n'
fi

printf '\n应用历史与 LangGraph checkpoint 已清除，模型配置未改动。刷新浏览器后旧项目入口会自动失效。\n'
