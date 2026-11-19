#!/usr/bin/env bash
# Manual, pinned Concat Outputs upgrade. Never changes VPS1 Scene, OAuth or Nginx.
set -Eeuo pipefail
set +x
umask 077
[[ "$(id -u)" == 0 ]] || { echo "BLOCKED: sudo required"; exit 1; }

BRIDGE_SHA="d9c422423aeecccf55ced49f63d7b8b35448840d"
CONCAT_SHA="2555fe9a7c01d0915ca994587e0bcfc4531ad5b8"
BRIDGE_REPO="dilshadshahsyed883-cmd/vps1-deploy-control"
CONCAT_REPO="dilshadshahsyed883-cmd/concat-web"
BRIDGE_FILE="/opt/concat-drive-bridge/concat_drive_bridge.py"
BRIDGE_UNIT="concat-drive-bridge.service"
CONCAT_UNIT="concat-web.service"
SCENE_UNIT="vps1-scene-candidate.service"
SOCKET="/run/concat-drive-bridge/intake.sock"
TMP="$(mktemp -d)"
BRIDGE_CHANGED=0
CONCAT_CHANGED=0
DONE=0
OLD_CONCAT=""
SAVED_BRIDGE=""

cleanup() {
  rc=$?
  trap - EXIT
  if [[ "$DONE" != 1 && "$rc" != 0 ]]; then
    if [[ "$CONCAT_CHANGED" == 1 && -d "$OLD_CONCAT" ]]; then
      ln -s "$OLD_CONCAT" "/srv/concat-web/.outputs-rollback-$$" || true
      mv -Tf "/srv/concat-web/.outputs-rollback-$$" /srv/concat-web/current || true
      systemctl restart "$CONCAT_UNIT" || true
      echo "CONCAT_ROLLBACK=ATTEMPTED"
    fi
    if [[ "$BRIDGE_CHANGED" == 1 && -f "$SAVED_BRIDGE" ]]; then
      install -o root -g root -m 0644 "$SAVED_BRIDGE" "$BRIDGE_FILE" || true
      systemctl restart "$BRIDGE_UNIT" || true
      echo "BRIDGE_ROLLBACK=ATTEMPTED"
    fi
  fi
  rm -rf -- "$TMP"
  exit "$rc"
}
trap cleanup EXIT

echo "=== VERIFY PINNED RELEASES ==="
for unit in "$BRIDGE_UNIT" "$CONCAT_UNIT" "$SCENE_UNIT"; do
  systemctl is-active --quiet "$unit"
done
[[ -f "$BRIDGE_FILE" && ! -L "$BRIDGE_FILE" &&
   "$(stat -c %U "$BRIDGE_FILE")" == root ]] || {
  echo "BLOCKED: Existing Bridge installation unverified"; exit 1;
}
[[ -x /opt/concat-drive-bridge/venv/bin/python && -S "$SOCKET" ]] || {
  echo "BLOCKED: Existing Bridge runtime unavailable"; exit 1;
}
[[ -L /srv/concat-web/current ]] || {
  echo "BLOCKED: Existing Concat release not linked"; exit 1;
}
OLD_CONCAT="$(readlink -f /srv/concat-web/current)"
[[ "$OLD_CONCAT" == /srv/concat-web/releases/* && -d "$OLD_CONCAT" ]] || {
  echo "BLOCKED: Unexpected previous Concat release"; exit 1;
}
HEAD="$(git ls-remote \
  "https://github.com/$CONCAT_REPO.git" \
  refs/heads/integration/personal-release-20261119 | awk '{print $1}')"
[[ "$HEAD" == "$CONCAT_SHA" ]] || {
  echo "BLOCKED: Concat integration HEAD changed"; exit 1;
}
curl -fsSL --retry 3 \
  "https://raw.githubusercontent.com/$BRIDGE_REPO/$BRIDGE_SHA/production/concat_drive_bridge.py" \
  -o "$TMP/bridge.py"
curl -fsSL --retry 3 \
  "https://raw.githubusercontent.com/$CONCAT_REPO/$CONCAT_SHA/deploy/manual-integration-release.sh" \
  -o "$TMP/release.sh"
python3 -m py_compile "$TMP/bridge.py"
bash -n "$TMP/release.sh"
grep -q 'def upload_output(self):' "$TMP/bridge.py"
grep -q 'OUTPUT_FOLDER = "Concat Outputs"' "$TMP/bridge.py"
echo "PINNED_OUTPUT_RELEASES=PASS"

echo "=== UPDATE EXISTING BRIDGE ONLY ==="
BACKUP="/var/backups/concat-drive-bridge"
[[ ! -L "$BACKUP" ]] || exit 1
install -d -o root -g root -m 0700 "$BACKUP"
SAVED_DIR="$(mktemp -d "$BACKUP/upgrade.XXXXXXXX")"
SAVED_BRIDGE="$SAVED_DIR/previous_bridge.py"
cp -p "$BRIDGE_FILE" "$SAVED_BRIDGE"
install -o root -g root -m 0644 "$TMP/bridge.py" "$BRIDGE_FILE.new"
BRIDGE_CHANGED=1
mv -f "$BRIDGE_FILE.new" "$BRIDGE_FILE"
systemctl restart "$BRIDGE_UNIT"

OWNER="$(printf 'concat-outputs-health' | sha256sum | awk '{print $1}')"
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
[[ "$READY" == 1 ]] || {
  echo "BRIDGE_HEALTH=FAIL"
  journalctl -u "$BRIDGE_UNIT" -n 12 --no-pager
  exit 1;
}
echo "OUTPUT_BRIDGE_HEALTH=PASS"

echo "=== STAGE CONCAT WEB ==="
CONCAT_CHANGED=1
if ! bash "$TMP/release.sh" "$CONCAT_SHA" stage >"$TMP/release.log" 2>&1; then
  tail -n 35 "$TMP/release.log"
  exit 1
fi
grep -E 'PASS|STAGED_SHA|BACKUP_PATH' "$TMP/release.log" | tail -n 12
[[ "$(readlink -f /srv/concat-web/current)" == "/srv/concat-web/releases/$CONCAT_SHA" ]]
systemctl is-active --quiet "$BRIDGE_UNIT"
systemctl is-active --quiet "$CONCAT_UNIT"
systemctl is-active --quiet "$SCENE_UNIT"
curl --noproxy '*' -fsS --max-time 8 http://127.0.0.1:18873/healthz

DONE=1
echo
echo "CONCAT_OUTPUTS_UPLOAD_RELEASE=STAGED"
echo "RELEASE_SHA=$CONCAT_SHA"
echo "DRIVE_DESTINATION=Concat Outputs"
echo "MANUAL_RETRY_TEST=PENDING"
