#!/usr/bin/env bash
set -Eeuo pipefail
STATE=/var/lib/vps-storage-manager
STATUS="$STATE/full-production-clean-status.json"
LOG="$STATE/full-production-clean.log"
LOCK="$STATE/full-production-clean.lock"
GC=/opt/local-upload-manager-engine2/maintenance/lum2_staging_gc.py
CLEANER=/opt/vps-storage-manager/full_production_clean_v18.sh
mkdir -p "$STATE"

if [[ -e "$LOCK" ]]; then
  echo 'ERROR: full production clean lock already exists' >&2
  exit 1
fi
: > "$LOCK"
trap 'rm -f "$LOCK"' EXIT
: > "$LOG"

BEFORE="$(df -B1 --output=avail / | tail -1 | tr -d ' ')"
write_status(){
  local state="$1" running="$2" code="$3" message="$4" reclaimed="${5:-0}"
  python3 - "$STATUS" "$state" "$running" "$code" "$message" "$reclaimed" <<'PY'
import json, os, sys, tempfile
from datetime import datetime, timezone
p,state,running,code,message,reclaimed=sys.argv[1:]
old={}
try: old=json.load(open(p,encoding='utf-8'))
except Exception: pass
value={
 'ok': state != 'failed',
 'service':'vps-production-cleaner.service',
 'running':running=='1',
 'status':state,
 'request_id':old.get('request_id',''),
 'started_at':old.get('started_at') or datetime.now(timezone.utc).isoformat(),
 'completed_at':datetime.now(timezone.utc).isoformat() if state in {'completed','failed'} else '',
 'exit_code':None if code=='' else int(code),
 'verified':state=='completed',
 'reclaimed_bytes':int(reclaimed or 0),
 'message':message,
}
fd,tmp=tempfile.mkstemp(prefix='.full-clean-status.',dir=str(os.path.dirname(p)))
with os.fdopen(fd,'w',encoding='utf-8') as f: json.dump(value,f,separators=(',',':'))
os.replace(tmp,p)
PY
}

write_status running 1 '' 'Full Production Clean V18 is running. Current live production is protected and verified after cleanup.' 0

set +e
{
  echo "FULL_CLEAN_V18_BEGIN"
  echo "FULL_CLEAN_LUM2_GC_BEGIN"
  /usr/bin/python3 "$GC" --reason full-production-clean-v18
  GC_RC=$?
  echo "FULL_CLEAN_LUM2_GC_RC=$GC_RC"
  if [[ "$GC_RC" -ne 0 ]]; then
    exit "$GC_RC"
  fi
  echo "FULL_CLEAN_LUM2_GC_DONE"
  bash "$CLEANER" --execute
} >>"$LOG" 2>&1
RC=$?
set -e

AFTER="$(df -B1 --output=avail / | tail -1 | tr -d ' ')"
RECLAIMED=$(( AFTER > BEFORE ? AFTER - BEFORE : 0 ))
if [[ "$RC" -eq 0 ]]; then
  write_status completed 0 0 'Full Production Clean V18 completed and current-live verification passed.' "$RECLAIMED"
else
  write_status failed 0 "$RC" 'Full Production Clean V18 failed or verification did not pass. Review the latest log.' "$RECLAIMED"
fi
exit "$RC"
