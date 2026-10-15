#!/usr/bin/env bash
set -Eeuo pipefail
command -v rclone >/dev/null || { echo "rclone is not installed. Run ./install.sh first."; exit 1; }
CFG="${SML_RCLONE_CONFIG:-$HOME/.config/self-media-library/rclone.conf}"
mkdir -p "$(dirname "$CFG")"
chmod 700 "$(dirname "$CFG")"
export RCLONE_CONFIG="$CFG"
echo "Self Media Library runtime Drive setup"
echo "Isolated config: $CFG"
echo "Recommended Google Drive remote name: gdrive"
echo "This config is separate from the Master deployment bridge and other apps."
exec rclone --config "$CFG" config
