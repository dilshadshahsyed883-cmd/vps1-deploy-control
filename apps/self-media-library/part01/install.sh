#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }
missing=()
command -v ffmpeg >/dev/null || missing+=(ffmpeg)
if ((${#missing[@]})); then
  echo "Installing: ${missing[*]}"
  sudo apt-get update
  sudo apt-get install -y "${missing[@]}"
fi
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
chmod +x start.sh validate.sh
printf '\nInstalled Self Media Library — VPS Local.\n'
printf '1) Start: ./start.sh\n'
printf '2) Open: http://SERVER_IP:8090\n'
