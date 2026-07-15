#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
ACTION_NAME="部署"
# shellcheck source=scripts/runtime-common.sh
source "$ROOT/scripts/runtime-common.sh"

API_KEY=""
MODEL_CONFIGURED=0
ENV_TEMP=""

cleanup() {
  stty echo 2>/dev/null || true
  unset API_KEY
  [ -z "$ENV_TEMP" ] || rm -f "$ENV_TEMP"
}
trap cleanup EXIT

require_runtime_layout
require_docker
require_command tar
require_command git
require_command curl
require_command mktemp

if [ -t 0 ]; then
  printf '模型 Base URL [%s]：' "${MODEL_BASE_URL:-https://api.deepseek.com/v1}"
  IFS= read -r BASE_URL || true
  BASE_URL="${BASE_URL:-${MODEL_BASE_URL:-https://api.deepseek.com/v1}}"
  printf '模型名称 [%s]：' "${MODEL_NAME:-deepseek-chat}"
  IFS= read -r MODEL || true
  MODEL="${MODEL:-${MODEL_NAME:-deepseek-chat}}"
  printf '模型 API Key（可留空，之后在前端配置；输入不会显示）：'
  stty -echo
  IFS= read -r API_KEY || true
  stty echo
  printf '\n'
else
  BASE_URL="${MODEL_BASE_URL:-https://api.deepseek.com/v1}"
  MODEL="${MODEL_NAME:-deepseek-chat}"
  API_KEY="${MODEL_API_KEY:-}"
  printf '检测到非交互环境，使用 MODEL_BASE_URL、MODEL_NAME 和 MODEL_API_KEY。\n'
fi

case "$BASE_URL" in
  http://*|https://*) ;;
  *) fail "MODEL_BASE_URL 必须使用 http:// 或 https://" ;;
esac
[ -n "$MODEL" ] || fail "MODEL_NAME 不能为空"
[ -n "$API_KEY" ] && MODEL_CONFIGURED=1

assert_no_active_runners
assert_backend_stopped

printf '\n[1/5] 初始化固定版本依赖\n'
sync_fixed_dependencies

printf '\n[2/5] 拉取并核验锁定镜像\n'
while IFS='|' read -r name image; do
  case "$name" in ''|'#'*) continue ;; esac
  case "$image" in *@sha256:*) ;; *) fail "$name 未使用 digest 锁定" ;; esac
  printf '  - %s\n' "$name"
  docker pull "$image" || fail "镜像拉取失败：$name"
done < docker/images.lock

printf '\n[3/5] 写入当前后端运行配置\n'
umask 077
ENV_TEMP="$(mktemp "$ROOT/.env.tmp.XXXXXX")" || fail "无法创建临时配置文件"
{
  write_dotenv_entry MODEL_BASE_URL "$BASE_URL"
  write_dotenv_entry MODEL_API_KEY "$API_KEY"
  write_dotenv_entry MODEL_NAME "$MODEL"
  write_dotenv_entry MODEL_TIMEOUT_SECONDS "${MODEL_TIMEOUT_SECONDS:-300}"
  write_dotenv_entry MODEL_MAX_OUTPUT_TOKENS "${MODEL_MAX_OUTPUT_TOKENS:-32768}"
  write_dotenv_entry AGENT_JNGEN_DOCUMENT_CONTEXT_CHARS "${AGENT_JNGEN_DOCUMENT_CONTEXT_CHARS:-64000}"
  write_dotenv_entry RUNNER_CONCURRENCY "${RUNNER_CONCURRENCY:-2}"
  write_dotenv_entry RUNNER_BATCH_SIZE "${RUNNER_BATCH_SIZE:-16}"
  write_dotenv_entry MANIFEST_CHECKPOINT_INTERVAL "${MANIFEST_CHECKPOINT_INTERVAL:-10}"
  write_dotenv_entry DOCKER_PROXY_TIMEOUT_SECONDS "${DOCKER_PROXY_TIMEOUT_SECONDS:-1200}"
} > "$ENV_TEMP"
validate_compose_env_file "$ENV_TEMP"
chmod 600 "$ENV_TEMP"
mv "$ENV_TEMP" "$ROOT/.env"
ENV_TEMP=""
unset API_KEY

printf '\n[4/5] 构建后端与受限运行器\n'
build_backend_image
build_runner_images

printf '\n[5/5] 启动应用并等待就绪\n'
assert_no_active_runners
start_application
wait_for_backend 60 || fail "应用未在 60 秒内就绪"

printf '\n部署完成。\n'
printf '应用工作台：%s\n' "$APP_URL"
printf 'API 文档：%s/docs\n' "$APP_URL"
if [ "$MODEL_CONFIGURED" = "0" ]; then
  printf '提示：当前未配置模型，普通页面可正常使用，AI 操作需配置模型后运行。\n'
fi
