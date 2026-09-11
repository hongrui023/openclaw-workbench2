#!/usr/bin/env bash
# ============================================================
# 本地开发启动（macOS / Linux / Git Bash）
#
#   bash scripts/dev_run.sh
#   PORT=8080 OPENCLAW_BASE_URL=http://192.168.1.20:18789/v1 bash scripts/dev_run.sh
#
# 开发模式下用明文口令，省去反复生成哈希。
# 绝不能把 WORKBENCH_DEV=1 带到 NAS 上。
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PORT="${PORT:-8080}"
DATA_DIR="${ROOT}/var/dev-data"
mkdir -p "${DATA_DIR}/literature" "${DATA_DIR}/life_notes" "${ROOT}/var/jobs"

export WORKBENCH_APP_ROOT="${ROOT}"
export WORKBENCH_DEV=1

# 开发口令：默认每次启动随机生成；想固定就自己 export WORKBENCH_PASSWORD。
# 刻意【不】留固定默认值——固定口令一旦随代码进了仓库，
# 就等于把一串"可以直接拿来用的弱口令"一起发布了。
if [ -z "${WORKBENCH_PASSWORD:-}" ]; then
  WORKBENCH_PASSWORD="$(head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | cut -c1-32)"
fi
export WORKBENCH_PASSWORD
export LITERATURE_DIR="${DATA_DIR}/literature"
export LIFE_NOTES_DIR="${DATA_DIR}/life_notes"
export WORKBENCH_WORK_DIR="${ROOT}/var/jobs"
export OPENCLAW_BASE_URL="${OPENCLAW_BASE_URL:-http://127.0.0.1:18789/v1}"
export OPENCLAW_MODEL="${OPENCLAW_MODEL:-openclaw:main}"
export PYTHONPATH="${ROOT}"
export PYTHONUTF8=1
export TZ="${TZ:-Asia/Shanghai}"

cat <<INFO

本地开发模式
  访问地址     : http://127.0.0.1:${PORT}
  登录口令     : ${WORKBENCH_PASSWORD}
  文献目录     : ${LITERATURE_DIR}
  记录目录     : ${LIFE_NOTES_DIR}
  AI 服务地址  : ${OPENCLAW_BASE_URL}
  接口文档     : http://127.0.0.1:${PORT}/docs （仅开发模式可用）

INFO

exec python -m uvicorn app.main:app --host 127.0.0.1 --port "${PORT}" --reload --no-server-header
