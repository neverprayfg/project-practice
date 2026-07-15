#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

docker version >/dev/null 2>&1 || { printf '关闭失败：Docker Engine 不可用\n' >&2; exit 1; }
docker compose stop backend docker-api
printf '服务已停止，模型配置、LangGraph checkpoint 和业务数据均已保留。\n'
