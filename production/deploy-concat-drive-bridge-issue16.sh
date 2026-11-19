#!/usr/bin/env bash
# Pinned, guarded manual installer for Concat Drive Issue #16.
# Only the new optional bridge and isolated Concat release may be modified.
set -Eeuo pipefail
umask 077
test "$(id -u)" = 0 || { echo "Run as root via sudo"; exit 1; }

VPS_REPO="dilshadshahsyed883-cmd/vps1-deploy-control"
VPS_SHA="07e214614300d05917acc49a2d133f1f9887c930"
CONCAT_REPO="dilshadshahsyed883-cmd/concat-web"
CONCAT_SHA="6d891f952ee23b557dc2878cc2dcf298111ab48b"
SCENE="vps1-scene-candidate.service"
BRIDGE="concat-drive-bridge.service"
ROOT="/opt/concat-drive-bridge"
SOCKET="/run/concat-drive-bridge/intake.sock"
TMP="$(mktemp -d)"
CREATED=0

cleanup() {
  rc=$?
  if [ "$rc" -ne 0 ] && [ "$CREATED" = 1 ]; then
    systemctl disable --now "$BRIDGE" >/dev/null 2>&1 || true
    rm -f /etc/systemd/system/concat-drive-bridge.service
    rm -rf -- /etc/concat-drive-bridge /opt/concat-drive-bridge
    systemctl daemon-reload
    echo "BRIDGE_ROLLBACK=PERFORMED"
  fi
  rm -rf -- "$TMP"
}
trap cleanup EXIT

echo "=== CHECK PINNED RELEASE ==="
HEAD="$(git ls-remote "https://github.com/$CONCAT_REPO.git" refs/heads/integration/personal-release-20261119 | awk '{print $1}')"
test "$HEAD" = "$CONCAT_SHA" || { echo "BLOCKED: Concat integration branch changed"; exit 1; }

echo "=== VERIFY CANONICAL SCENE ==="
systemctl is-active --quiet "$SCENE"
test "$(systemctl show -p User --value "$SCENE")" = "vps1scene"
id vps1scene >/dev/null
id concatweb >/dev/null
test -x /usr/bin/rclone
PID="$(systemctl show -p MainPID --value "$SCENE")"
test "$PID" -gt 1
SOURCE=""
for base in "$(readlink -f "/proc/$PID/cwd")" "$(systemctl show -p WorkingDirectory --value "$SCENE")"; do
  for path in "$base" "$base/apps/scene-builder/part01"; do
    if [ -f "$path/cloud_render.py" ] && [ -f "$path/cloud_render_production.py" ]; then
      SOURCE="$(readlink -f "$path")"
      break 2
    fi
  done
done
test -n "$SOURCE" || { echo "BLOCKED: Scene source not found"; exit 1; }
[[ "$SOURCE" =~ ^/[A-Za-z0-9._/-]+$ ]] || { echo "BLOCKED: Unexpected Scene source path"; exit 1; }
runuser -u vps1scene -- test -r "$SOURCE/cloud_render.py"
runuser -u vps1scene -- test -r "$SOURCE/cloud_render_production.py"
REMOTE="$(python3 - "$PID" <<'PY'
import pathlib, re, sys
values = {}
for entry in pathlib.Path(f"/proc/{sys.argv[1]}/environ").read_bytes().split(b"\0"):
    if b"=" in entry:
        key, val = entry.split(b"=", 1)
        if key == b"CLOUD_RENDER_DRIVE_REMOTE": values[key] = val
remote = values.get(b"CLOUD_RENDER_DRIVE_REMOTE", b"gdrive").decode()
if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", remote): raise SystemExit("BLOCKED: Invalid remote name")
print(remote)
PY
)"
for path in /etc/systemd/system/concat-drive-bridge.service "$ROOT" /etc/concat-drive-bridge "$SOCKET"; do
  test ! -e "$path" && test ! -L "$path" || { echo "BLOCKED: Bridge already installed"; exit 1; }
done
echo "CANONICAL_SCENE=VERIFIED"

echo "=== INSTALL EXECUTABLE ISOLATED VENV ==="
BASE="https://raw.githubusercontent.com/$VPS_REPO/$VPS_SHA/production"
curl -fsSL --retry 3 "$BASE/concat_drive_bridge.py" -o "$TMP/bridge.py"
curl -fsSL --retry 3 "$BASE/concat-drive-bridge.service" -o "$TMP/bridge.service"
python3 -m py_compile "$TMP/bridge.py"
CREATED=1
install -d -m 0755 "$ROOT"
install -d -m 0700 /etc/concat-drive-bridge
install -m 0644 "$TMP/bridge.py" "$ROOT/concat_drive_bridge.py"
# Do not use umask 077 for the executable venv: Scene service is vps1scene.
(umask 022; python3 -m venv "$ROOT/venv")
if ! (umask 022; "$ROOT/venv/bin/pip" install --disable-pip-version-check --no-input \
    "fastapi==0.115.12" "requests==2.32.3" >"$TMP/pip.log" 2>&1); then
  tail -n 25 "$TMP/pip.log"
  exit 1
fi
# Only public import/runtime directories. Private Scene HOME remains untouched.
find "$ROOT/venv" -type d -exec chmod 0755 {} +
runuser -u vps1scene -- "$ROOT/venv/bin/python" -B -I - "$SOURCE" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from cloud_render import CloudRender
from cloud_render_production import DriveIntake
print("BRIDGE_VENV_EXEC_AND_SCENE_IMPORT=PASS")
PY

printf 'SCENE_SOURCE=%s\nCLOUD_RENDER_DRIVE_REMOTE=%s\n' "$SOURCE" "$REMOTE" \
  > /etc/concat-drive-bridge/bridge.env
chmod 0600 /etc/concat-drive-bridge/bridge.env
install -m 0644 "$TMP/bridge.service" /etc/systemd/system/concat-drive-bridge.service
systemctl daemon-reload
systemctl enable --now "$BRIDGE"

OWNER="$(printf 'concat-bridge-health' | sha256sum | awk '{print $1}')"
READY=0
for i in $(seq 1 15); do
  if runuser -u concatweb -- curl --noproxy '*' -fsS --max-time 5 \
      --unix-socket "$SOCKET" -H "Content-Type: application/json" \
      --data "{\"owner\":\"$OWNER\"}" http://localhost/v1/health \
      2>/dev/null | grep -q '"ok": true'; then
    READY=1
    break
  fi
  sleep 1
done
if [ "$READY" != 1 ]; then
  echo "BRIDGE_HEALTH=FAIL"
  journalctl -u "$BRIDGE" -n 18 --no-pager
  exit 1
fi
echo "CANONICAL_DRIVE_BRIDGE=PASS"

echo "=== STAGE VERIFIED CONCAT ISSUE #16 ==="
curl -fsSL --retry 3 \
 "https://raw.githubusercontent.com/$CONCAT_REPO/$CONCAT_SHA/deploy/manual-integration-release.sh" \
 -o "$TMP/release.sh"
bash -n "$TMP/release.sh"
if ! bash "$TMP/release.sh" "$CONCAT_SHA" stage >"$TMP/release.log" 2>&1; then
  tail -n 35 "$TMP/release.log"
  exit 1
fi
grep -E 'PASS|STAGED_SHA|BACKUP_PATH' "$TMP/release.log" | tail -n 12
test "$(readlink -f /srv/concat-web/current)" = "/srv/concat-web/releases/$CONCAT_SHA"
systemctl is-active --quiet concat-web.service
systemctl is-active --quiet "$SCENE"
systemctl is-active --quiet "$BRIDGE"
curl --noproxy '*' -fsS --max-time 8 http://127.0.0.1:18873/healthz
echo
echo "CANONICAL_DRIVE_BRIDGE=PASS"
echo "CONCAT_ISSUE16_RELEASE=STAGED"
echo "RELEASE_SHA=$CONCAT_SHA"
echo "LIVE_DRIVE_SCAN=NOT_YET_TESTED"
