#!/usr/bin/env bash
# One-command launcher for macOS / Linux: creates a venv on first run, then starts AutoStudio.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "[AutoStudio] 未找到 python3，请先安装 Python 3.10+" >&2
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "[AutoStudio] 首次运行：创建虚拟环境并安装依赖，需要几分钟…"
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --upgrade pip -q
  .venv/bin/python -m pip install -e ".[all]"
fi

exec .venv/bin/python -m autostudio "$@"
