#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")"
PY="python3"
[[ -x .venv/bin/python ]] && PY=".venv/bin/python"
export RCLONE_CONFIG="${SML_RCLONE_CONFIG:-$HOME/.config/self-media-library/rclone.conf}"
exec "$PY" -m uvicorn app:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8090}"
