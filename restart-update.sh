#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

command -v docker >/dev/null 2>&1 || { printf '更新失败：未找到 Docker\n' >&2; exit 1; }
command -v tar >/dev/null 2>&1 || { printf '更新失败：未找到 tar\n' >&2; exit 1; }
docker version >/dev/null 2>&1 || { printf '更新失败：Docker Engine 不可用\n' >&2; exit 1; }

printf '[1/4] 使用当前源码重建后端镜像\n'
tar -cf - \
  backend/pyproject.toml \
  backend/uv.lock \
  backend/app \
  demo前端样式设计 \
  docker/backend.Dockerfile \
  | docker build --pull=false --progress=plain \
      -t contest-dataset-backend:0.1.0 -f docker/backend.Dockerfile -

printf '[2/4] 使用固定依赖重建受限 runner 双镜像\n'
tar -cf - \
  docker/runner/runner.cpp \
  docker/runner.Dockerfile \
  testlib/testlib.h \
  jngen/jngen.h \
| docker build --pull=false --progress=plain \
      --target compiler \
      -t contest-dataset-runner-compiler:0.2.0 -f docker/runner.Dockerfile -
tar -cf - \
  docker/runner/runner.cpp \
  docker/runner.Dockerfile \
  testlib/testlib.h \
  jngen/jngen.h \
| docker build --pull=false --progress=plain \
      --target executor \
      -t contest-dataset-runner-executor:0.2.0 -f docker/runner.Dockerfile -

printf '[3/4] 重建应用容器\n'
docker compose up -d --no-build --force-recreate docker-api workspace-init backend

printf '[4/4] 等待应用就绪'
for _ in $(seq 1 60); do
  if curl -fsS http://localhost:8000/health >/dev/null 2>&1; then
    printf ' 完成\n\n更新已生效，业务状态与 LangGraph checkpoint 已保留。\n'
    exit 0
  fi
  printf '.'
  sleep 1
done
printf '\n更新失败：应用未在 60 秒内就绪\n' >&2
exit 1
