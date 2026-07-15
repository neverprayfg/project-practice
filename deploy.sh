#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
API_KEY=""
MODEL_CONFIGURED=0
trap 'stty echo 2>/dev/null || true; unset API_KEY' EXIT

fail() {
  printf '部署失败：%s\n' "$1" >&2
  exit 1
}

run() {
  "$@" || fail "命令执行失败：$*"
}

command -v docker >/dev/null 2>&1 || fail "未找到 Docker，请先安装并启动 Docker Desktop"
command -v tar >/dev/null 2>&1 || fail "未找到 tar，无法创建跨路径 Docker 构建上下文"
docker version >/dev/null 2>&1 || fail "Docker Engine 不可用，请先启动 Docker Desktop"
docker compose version >/dev/null 2>&1 || fail "当前 Docker 未提供 Compose v2"

printf '模型名称 [deepseek-chat]：'
IFS= read -r MODEL
MODEL="${MODEL:-deepseek-chat}"
BASE_URL="${MODEL_BASE_URL:-https://api.deepseek.com/v1}"
printf '模型 API Key（可留空，之后在前端配置；输入不会显示）：'
stty -echo
IFS= read -r API_KEY
stty echo
printf '\n'
[ -n "$API_KEY" ] && MODEL_CONFIGURED=1

printf '\n[1/5] 初始化固定版本依赖\n'
run git submodule sync --recursive
run git submodule update --init --recursive testlib jngen

printf '\n[2/5] 拉取并核验锁定镜像\n'
while IFS='|' read -r name image; do
  case "$name" in ''|'#'*) continue ;; esac
  case "$image" in *@sha256:*) ;; *) fail "$name 未使用 digest 锁定" ;; esac
  printf '  - %s\n' "$name"
  run docker pull "$image"
done < docker/images.lock

printf '\n[3/5] 写入 LangGraph 后端模型配置\n'
umask 077
{
  printf 'MODEL_BASE_URL=%s\n' "$BASE_URL"
  printf 'MODEL_API_KEY=%s\n' "$API_KEY"
  printf 'MODEL_NAME=%s\n' "$MODEL"
  printf 'MODEL_TIMEOUT_SECONDS=300\n'
  printf 'AGENT_MAX_ITERATIONS=4\n'
} > .env
unset API_KEY

printf '\n[4/5] 构建后端与受限运行器\n'
tar -cf - \
  backend/pyproject.toml \
  backend/uv.lock \
  backend/app \
  demo前端样式设计 \
  docker/backend.Dockerfile \
  | docker build --pull=false --progress=plain \
      -t contest-dataset-backend:0.1.0 -f docker/backend.Dockerfile - \
  || fail "后端镜像构建失败"
tar -cf - \
  docker/runner/runner.cpp \
  docker/runner.Dockerfile \
  testlib/testlib.h \
  jngen/jngen.h \
| docker build --pull=false --progress=plain \
      --target compiler \
      -t contest-dataset-runner-compiler:0.3.0 -f docker/runner.Dockerfile - \
  || fail "runner 编译镜像构建失败"
tar -cf - \
  docker/runner/runner.cpp \
  docker/runner.Dockerfile \
  testlib/testlib.h \
  jngen/jngen.h \
| docker build --pull=false --progress=plain \
      --target executor \
      -t contest-dataset-runner-executor:0.3.0 -f docker/runner.Dockerfile - \
  || fail "runner 执行镜像构建失败"

printf '\n[5/5] 启动应用并等待就绪\n'
run docker compose up -d --no-build docker-api workspace-init backend
READY=0
for _ in $(seq 1 60); do
  if curl -fsS http://localhost:8000/health >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 1
done
[ "$READY" = "1" ] || fail "应用未在 60 秒内就绪，请运行 docker compose logs backend"

printf '\n部署完成。\n'
printf '应用工作台：http://localhost:8000\n'
printf 'API 文档：http://localhost:8000/docs\n'
if [ "$MODEL_CONFIGURED" = "0" ]; then
  printf '提示：当前未配置模型，普通页面可正常使用，AI 操作需配置模型后运行。\n'
fi
