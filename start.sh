#!/usr/bin/env bash
# SnowLink 启动器：不传参数等于 `run`。
set -euo pipefail
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo "没找到 python3，请先装 Python 3.10+" >&2
  exit 1
fi

exec "$PY" -m snowlink "$@"
