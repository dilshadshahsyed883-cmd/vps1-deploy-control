#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }
missing=()
command -v ffmpeg >/dev/null || missing+=(ffmpeg)
command -v rclone >/dev/null || missing+=(rclone)
if ((${#missing[@]})); then
  echo "Installing: ${missing[*]}"
  sudo apt-get update
  sudo apt-get install -y "${missing[@]}"
fi
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
chmod +x start.sh validate.sh configure-drive.sh
printf '\nInstalled Self Media Library V1.2.\n'
printf '1) Configure Google Drive once: ./configure-drive.sh\n'
printf '2) Start: ./start.sh\n'
printf '3) Open: http://SERVER_IP:8090\n'
