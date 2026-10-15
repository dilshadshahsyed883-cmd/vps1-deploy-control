#!/usr/bin/env bash
set -Eeuo pipefail

display_number=":100"
profile_dir="/home/ubuntu/.local/share/script-work-browser/profile"
mkdir -p "$profile_dir"
chmod 700 "$profile_dir"

cleanup() {
  local code=$?
  trap - EXIT INT TERM
  for pid in "${chrome_pid:-}" "${websockify_pid:-}" "${vnc_pid:-}" "${xvfb_pid:-}"; do
    if [[ -n "$pid" ]]; then kill "$pid" 2>/dev/null || true; fi
  done
  wait 2>/dev/null || true
  exit "$code"
}
trap cleanup EXIT INT TERM

/usr/bin/Xvfb "$display_number" -screen 0 1440x900x24 -nolisten tcp -ac &
xvfb_pid=$!
for _ in {1..40}; do
  [[ -S /tmp/.X11-unix/X100 ]] && break
  sleep 0.25
done
[[ -S /tmp/.X11-unix/X100 ]]

export DISPLAY="$display_number"
/usr/bin/x11vnc -display "$display_number" -rfbport 5901 -localhost -forever -shared -nopw -quiet &
vnc_pid=$!
/usr/bin/websockify --web /usr/share/novnc 127.0.0.1:6081 127.0.0.1:5901 &
websockify_pid=$!

/usr/bin/google-chrome +  --user-data-dir="$profile_dir" +  --remote-debugging-address=127.0.0.1 +  --remote-debugging-port=9223 +  --no-first-run +  --no-default-browser-check +  --disable-dev-shm-usage +  --disable-blink-features=AutomationControlled +  --password-store=basic +  --window-size=1440,900 +  --start-maximized +  "https://www.youtube.com/" &
chrome_pid=$!

wait -n "$xvfb_pid" "$vnc_pid" "$websockify_pid" "$chrome_pid"
