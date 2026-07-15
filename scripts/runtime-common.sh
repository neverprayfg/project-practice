#!/usr/bin/env bash

# Root scripts source this file after defining ROOT and ACTION_NAME.
: "${ROOT:?ROOT must be set before sourcing runtime-common.sh}"
: "${ACTION_NAME:=操作}"

BACKEND_IMAGE="contest-dataset-backend:0.1.0"
RUNNER_COMPILER_IMAGE="contest-dataset-runner-compiler:0.3.0"
RUNNER_EXECUTOR_IMAGE="contest-dataset-runner-executor:0.3.0"
STORAGE_VOLUME_NAME="contest_dataset_storage"
COMPOSE_PROJECT_NAME="contest-dataset-mvp"
APP_URL="${APP_URL:-http://localhost:8000}"
HEALTH_URL="${HEALTH_URL:-${APP_URL%/}/health}"

fail() {
  printf '%s失败：%s\n' "$ACTION_NAME" "$1" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "未找到 $1"
}

require_docker() {
  require_command docker
  docker info >/dev/null 2>&1 || fail "Docker Engine 不可用，请先启动 Docker Desktop"
  docker compose version >/dev/null 2>&1 || fail "当前 Docker 未提供 Compose v2"
}

compose_cmd() {
  docker compose \
    -p "$COMPOSE_PROJECT_NAME" \
    --project-directory "$ROOT" \
    -f "$ROOT/compose.yaml" \
    "$@"
}

require_runtime_layout() {
  [ -f "$ROOT/compose.yaml" ] || fail "缺少 compose.yaml"
}

validate_compose_config() {
  compose_cmd config --quiet >/dev/null \
    || fail "compose.yaml 或 .env 配置无效"
}

validate_compose_env_file() {
  local env_file="$1"
  docker compose \
    -p "$COMPOSE_PROJECT_NAME" \
    --project-directory "$ROOT" \
    --env-file "$env_file" \
    -f "$ROOT/compose.yaml" \
    config --quiet >/dev/null \
    || fail "新生成的 .env 配置无效，原配置未改动"
}

write_dotenv_entry() {
  local key="$1"
  local value="$2"
  local single_quote="'"
  local escaped_single_quote="\\'"

  case "$key" in
    ""|[0-9]*|*[!A-Z0-9_]*) fail "无效的 .env 配置项名称：$key" ;;
  esac
  case "$value" in
    *$'\n'*|*$'\r'*) fail "$key 不能包含换行符" ;;
  esac

  # Compose 对单引号值不做变量插值；只需转义值中的单引号。
  value="${value//$single_quote/$escaped_single_quote}"
  printf "%s='%s'\n" "$key" "$value"
}

active_runner_ids() {
  local rows
  local container_id
  local compose_project
  local compose_service

  rows="$(
    docker ps \
      --filter "volume=$STORAGE_VOLUME_NAME" \
      --format '{{.ID}}|{{.Label "com.docker.compose.project"}}|{{.Label "com.docker.compose.service"}}'
  )" || return 1

  while IFS='|' read -r container_id compose_project compose_service; do
    [ -n "$container_id" ] || continue
    if [ "$compose_project" = "$COMPOSE_PROJECT_NAME" ]; then
      case "$compose_service" in
        backend|workspace-init) continue ;;
      esac
    fi
    printf '%s\n' "$container_id"
  done <<< "$rows"
}

assert_no_active_runners() {
  local active
  active="$(active_runner_ids)" \
    || fail "无法检查是否存在运行中的 Sandbox 容器"
  [ -z "$active" ] \
    || fail "仍有 Sandbox/runner 容器占用业务卷（$active），请等待当前任务结束后重试"
}

assert_application_idle() {
  local health_payload
  local compact_payload

  backend_is_running || return 0
  require_health_client
  health_payload="$(curl --max-time 3 -fsS "$HEALTH_URL")" \
    || fail "后端正在运行但健康接口不可用；为避免中断任务，拒绝继续"
  compact_payload="${health_payload//[[:space:]]/}"
  case "$compact_payload" in
    *'"active_tasks":false'*) return 0 ;;
    *'"active_tasks":true'*)
      fail "仍有 Agent/数据集任务正在运行，请等待任务结束后重试"
      ;;
    *) fail "健康接口未返回 active_tasks，当前后端版本与管理脚本不匹配" ;;
  esac
}

compose_service_is_running() {
  local service="$1"
  local active
  active="$(compose_cmd ps --status running --quiet "$service" 2>/dev/null)" \
    || fail "无法读取 $service 容器状态"
  [ -n "$active" ]
}

backend_is_running() {
  compose_service_is_running backend
}

assert_backend_stopped() {
  backend_is_running \
    && fail "应用仍在运行。为避免任务跨版本执行，请先运行 ./stop.sh"
  return 0
}

require_health_client() {
  require_command curl
}

wait_for_backend() {
  local attempts="${1:-60}"
  local attempt=0
  local container_id
  local container_state

  printf '等待应用就绪'
  while [ "$attempt" -lt "$attempts" ]; do
    if curl --max-time 2 -fsS "$HEALTH_URL" >/dev/null 2>&1; then
      printf ' 完成\n'
      return 0
    fi
    if [ "$attempt" -ge 3 ]; then
      container_id="$(compose_cmd ps --all --quiet backend 2>/dev/null)" || container_id=""
      if [ -n "$container_id" ]; then
        container_state="$(
          docker inspect --format '{{.State.Status}}' "$container_id" 2>/dev/null
        )" || container_state=""
        case "$container_state" in
          exited|dead|restarting)
            printf ' 失败（backend 容器状态：%s）\n' "$container_state" >&2
            compose_cmd logs --no-color --tail=80 backend >&2 2>/dev/null || true
            return 1
            ;;
        esac
      fi
    fi
    printf '.'
    sleep 1
    attempt=$((attempt + 1))
  done

  printf '\n' >&2
  compose_cmd logs --no-color --tail=80 backend >&2 2>/dev/null || true
  return 1
}

ensure_runtime_images() {
  local image
  for image in "$BACKEND_IMAGE" "$RUNNER_COMPILER_IMAGE" "$RUNNER_EXECUTOR_IMAGE"; do
    docker image inspect "$image" >/dev/null 2>&1 \
      || fail "缺少镜像 $image，请先运行 ./deploy.sh"
  done
}

sync_fixed_dependencies() {
  require_command git
  git submodule sync --recursive \
    || fail "固定依赖地址同步失败"
  git submodule update --init --recursive testlib jngen \
    || fail "testlib/jngen 固定版本初始化失败"
  [ -f testlib/testlib.h ] || fail "缺少 testlib/testlib.h"
  [ -f jngen/jngen.h ] || fail "缺少 jngen/jngen.h"
}

build_backend_image() {
  require_command tar
  if ! tar -cf - \
    backend/pyproject.toml \
    backend/uv.lock \
    backend/app \
    demo前端样式设计 \
    docker/backend.Dockerfile \
    | docker build --pull=false --progress=plain \
        -t "$BACKEND_IMAGE" -f docker/backend.Dockerfile -
  then
    fail "后端镜像构建失败"
  fi
}

build_runner_images() {
  local context_archive
  require_command tar
  require_command mktemp
  context_archive="$(mktemp "${TMPDIR:-/tmp}/contest-runner.XXXXXX")" \
    || fail "无法创建 runner 临时构建上下文"

  if ! tar -cf "$context_archive" \
    docker/runner/runner.cpp \
    docker/runner.Dockerfile \
    testlib/testlib.h \
    jngen/jngen.h
  then
    rm -f "$context_archive"
    fail "runner 构建上下文创建失败"
  fi

  if ! docker build --pull=false --progress=plain \
    --target compiler \
    -t "$RUNNER_COMPILER_IMAGE" -f docker/runner.Dockerfile - \
    < "$context_archive"
  then
    rm -f "$context_archive"
    fail "runner 编译镜像构建失败"
  fi

  if ! docker build --pull=false --progress=plain \
    --target executor \
    -t "$RUNNER_EXECUTOR_IMAGE" -f docker/runner.Dockerfile - \
    < "$context_archive"
  then
    rm -f "$context_archive"
    fail "runner 执行镜像构建失败"
  fi

  rm -f "$context_archive"
}

start_application() {
  assert_no_active_runners
  mkdir -p "$ROOT/exports"
  compose_cmd up -d --no-build docker-api workspace-init backend \
    || fail "应用容器启动失败"
}
