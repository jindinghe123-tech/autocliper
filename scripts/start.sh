#!/usr/bin/env bash
# AutoStudio launcher for macOS / Linux (double-click AutoStudio.command on macOS, or run this script).
# First run installs everything into this folder: uv, a private Python 3.12 (only if no Python 3.10+
# is found) and all packages. Later runs start immediately. Arguments go to `python -m autostudio`.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
RT="$ROOT/.runtime"
VPY="$ROOT/.venv/bin/python"
STAMP="$ROOT/.venv/autostudio-setup.txt"
SETUP_VERSION=1

hash_file() { if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1"; else sha256sum "$1"; fi | cut -d' ' -f1; }
FINGERPRINT="v$SETUP_VERSION $(hash_file pyproject.toml)"

venv_ok() { [ -x "$VPY" ] && "$VPY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; }

if [ -f "$STAMP" ] && [ "$(cat "$STAMP")" = "$FINGERPRINT" ] && venv_ok; then
  exec "$VPY" -m autostudio "$@"
fi

echo "[AutoStudio] 正在安装所需组件（全部装在本文件夹里，首次需要几分钟）…"

# Mainland China: try the domestic mirrors first. Override with AUTOSTUDIO_MIRROR=1 / 0.
PREFER_MIRROR=0
case "${AUTOSTUDIO_MIRROR:-}" in
  0|off|false|no) ;;
  "") case "$(readlink /etc/localtime 2>/dev/null || cat /etc/timezone 2>/dev/null || true)" in
        *Asia/Shanghai*|*Asia/Chongqing*|*Asia/Harbin*|*Asia/Urumqi*|*PRC*) PREFER_MIRROR=1 ;;
      esac ;;
  *) PREFER_MIRROR=1 ;;
esac
OFFICIAL="https://pypi.org/simple|"
MIRROR1="https://pypi.tuna.tsinghua.edu.cn/simple|https://registry.npmmirror.com/-/binary/python-build-standalone"
MIRROR2="https://mirrors.aliyun.com/pypi/simple/|https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone"
if [ "$PREFER_MIRROR" = 1 ]; then SOURCES=("$MIRROR1" "$MIRROR2" "$OFFICIAL"); else SOURCES=("$OFFICIAL" "$MIRROR1" "$MIRROR2"); fi

export UV_CACHE_DIR="$RT/cache" UV_PYTHON_INSTALL_DIR="$RT/python" UV_PYTHON_INSTALL_BIN=0 UV_HTTP_TIMEOUT=120

# uv: reuse one on PATH, otherwise install a private copy (official installer, then PyPI mirrors).
UV="$(command -v uv || true)"
if [ -z "$UV" ] && [ -x "$RT/uv/uv" ]; then UV="$RT/uv/uv"; fi
if [ -z "$UV" ]; then
  mkdir -p "$RT/uv"
  if curl -LsSf --connect-timeout 20 https://astral.sh/uv/install.sh | env UV_UNMANAGED_INSTALL="$RT/uv" sh >/dev/null 2>&1; then
    UV="$RT/uv/uv"
  else
    for src in "${SOURCES[@]}"; do
      idx="${src%%|*}"
      for py in python3 python; do
        if command -v "$py" >/dev/null 2>&1 && "$py" -m pip --version >/dev/null 2>&1 \
          && "$py" -m pip install -q --target "$RT/uv-pip" --index-url "$idx" uv >/dev/null 2>&1; then
          UV="$(find "$RT/uv-pip" -path '*/bin/uv' -type f | head -n1)"; break 2
        fi
      done
    done
  fi
fi

if [ -n "$UV" ]; then
  if ! venv_ok; then
    rm -rf .venv
    made=0
    for src in "${SOURCES[@]}"; do
      mirror="${src#*|}"
      if UV_PYTHON_INSTALL_MIRROR="$mirror" "$UV" venv .venv --python 3.12 --no-project; then made=1; break; fi
      rm -rf .venv
    done
    [ "$made" = 1 ] || { echo "[AutoStudio] 无法下载 Python，请检查网络后重试" >&2; exit 1; }
  fi
  ok=0
  for src in "${SOURCES[@]}"; do
    if "$UV" pip install --python "$VPY" --default-index "${src%%|*}" -r pyproject.toml --extra all; then ok=1; break; fi
  done
  if [ "$ok" = 0 ]; then
    for src in "${SOURCES[@]}"; do
      if "$UV" pip install --python "$VPY" --default-index "${src%%|*}" -r pyproject.toml; then ok=1; break; fi
    done
  fi
  [ "$ok" = 1 ] || { echo "[AutoStudio] 依赖安装失败，请检查网络后重试" >&2; exit 1; }
else
  # No uv available at all: fall back to a system Python 3.10+ with venv + pip.
  PY=""
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then PY="$c"; break; fi
  done
  [ -n "$PY" ] || { echo "[AutoStudio] 需要 Python 3.10+，或者能访问网络以自动下载" >&2; exit 1; }
  venv_ok || { rm -rf .venv; "$PY" -m venv .venv; }
  "$VPY" -m pip install -q --upgrade pip
  "$VPY" -m pip install -e ".[all]"
fi

"$VPY" scripts/doctor.py
printf '%s' "$FINGERPRINT" > "$STAMP"
exec "$VPY" -m autostudio "$@"
