#!/usr/bin/env bash
set -Eeuo pipefail

STATE_ROOT="/var/lib/vps-storage-manager"
STATUS="$STATE_ROOT/production-reset-status.json"
LOG="$STATE_ROOT/production-reset.log"
LOCK="$STATE_ROOT/production-reset.lock"
SCRIPT="/opt/vps-storage-manager/vps_production_clean_reset_VERIFIED.sh"

mkdir -p "$STATE_ROOT"
touch "$LOCK"
chmod 600 "$LOCK" 2>/dev/null || true

write_status() {
  local state="$1" code="$2" message="$3" started="$4" completed="$5"
  STATE="$state" CODE="$code" MESSAGE="$message" STARTED="$started" COMPLETED="$completed" STATUS_PATH="$STATUS" python3 - <<'PY'
import json, os, tempfile
from pathlib import Path
p=Path(os.environ['STATUS_PATH'])
value={
    'ok': os.environ['STATE'] == 'completed',
    'status': os.environ['STATE'],
    'started_at': os.environ['STARTED'],
    'completed_at': os.environ['COMPLETED'],
    'exit_code': int(os.environ['CODE']) if os.environ['CODE'] not in ('', 'None') else None,
    'message': os.environ['MESSAGE'],
}
fd,tmp=tempfile.mkstemp(prefix='.production-reset-status.', dir=str(p.parent))
with os.fdopen(fd,'w',encoding='utf-8') as f:
    json.dump(value,f,separators=(',',':'))
os.replace(tmp,p)
PY
}

STARTED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
: > "$LOG"
chmod 640 "$LOG" 2>/dev/null || true
write_status running "" "Production reset is running. Control Web services may restart briefly." "$STARTED" ""

cleanup_lock() {
  rm -f "$LOCK"
}
trap cleanup_lock EXIT

set +e
{
  echo "============================================================"
  echo " WEB-TRIGGERED VERIFIED PRODUCTION RESET"
  echo " Started: $STARTED"
  echo " Script:  $SCRIPT"
  echo "============================================================"
  "$SCRIPT" --execute
} >> "$LOG" 2>&1
RC=$?
set -e

COMPLETED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
if [[ "$RC" -eq 0 ]]; then
  write_status completed "$RC" "Production reset completed successfully." "$STARTED" "$COMPLETED"
else
  write_status failed "$RC" "Production reset failed. Review the Storage Manager reset log." "$STARTED" "$COMPLETED"
fi

exit "$RC"
