#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

command -v docker >/dev/null 2>&1 || { printf '启动失败：未找到 Docker\n' >&2; exit 1; }
docker version >/dev/null 2>&1 || { printf '启动失败：Docker Engine 不可用\n' >&2; exit 1; }
for image in \
  contest-dataset-backend:0.1.0 \
  contest-dataset-runner-compiler:0.2.0 \
  contest-dataset-runner-executor:0.2.0
do
  docker image inspect "$image" >/dev/null 2>&1 \
    || { printf '启动失败：缺少镜像 %s，请先运行 ./deploy.sh\n' "$image" >&2; exit 1; }
done

docker compose up -d --no-build docker-api workspace-init backend
printf '等待应用就绪'
for _ in $(seq 1 60); do
  if curl -fsS http://localhost:8000/health >/dev/null 2>&1; then
    printf ' 完成\n\n应用工作台：http://localhost:8000\n'
    exit 0
  fi
  printf '.'
  sleep 1
done
printf '\n启动失败：应用未在 60 秒内就绪\n' >&2
exit 1
