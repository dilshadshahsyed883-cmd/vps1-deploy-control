#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PY="python3"
[[ -x .venv/bin/python ]] && PY=".venv/bin/python"
"$PY" -m py_compile app.py
command -v ffmpeg >/dev/null
command -v ffprobe >/dev/null
command -v rclone >/dev/null || echo "WARNING: rclone missing; Drive backend cannot be enabled yet"
echo "Self Media Library V1.2.0 validation: PASS"
