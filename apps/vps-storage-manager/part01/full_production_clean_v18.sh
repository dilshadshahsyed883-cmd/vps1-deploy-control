#!/usr/bin/env bash
set -Eeuo pipefail

MODE="${1:---preflight}"
case "$MODE" in
  --preflight|--execute) ;;
  *) echo "Usage: $0 [--preflight|--execute]" >&2; exit 2 ;;
esac

section(){ echo; echo "================================================================"; echo "$*"; echo "================================================================"; }
bytes(){ du -sb "$1" 2>/dev/null | awk '{print $1}' || echo 0; }

ACS="/home/ubuntu/apps/auto_caption_studio"
ALG="/home/ubuntu/apps/algrow_voice_bridge"
SB="/var/lib/scene-builder"
SCENE_CONTAINER="scene-builder-worker"

[[ ${EUID:-1} -eq 0 ]] || { echo "REFUSE: V18 cleaner requires root"; exit 20; }

section "V18 CURRENT-LIVE PREFLIGHT"
echo "UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "MODE=$MODE"
CURRENT_ACS="$(readlink -f "$ACS/current" 2>/dev/null || true)"
CURRENT_ALG="$(readlink -f "$ALG/current" 2>/dev/null || true)"
CURRENT_SCENE_IMAGE="$(docker inspect -f '{{.Config.Image}}' "$SCENE_CONTAINER" 2>/dev/null || true)"
SCENE_RUNNING="$(docker inspect -f '{{.State.Running}}' "$SCENE_CONTAINER" 2>/dev/null || true)"

echo "CURRENT_ACS=$CURRENT_ACS"
echo "CURRENT_ALG=$CURRENT_ALG"
echo "CURRENT_SCENE_IMAGE=$CURRENT_SCENE_IMAGE"
echo "SCENE_RUNNING=$SCENE_RUNNING"

[[ -n "$CURRENT_ACS" && -d "$CURRENT_ACS" ]] || { echo "REFUSE: ACS current target unresolved"; exit 21; }
[[ -n "$CURRENT_ALG" && -d "$CURRENT_ALG" ]] || { echo "REFUSE: Algrow current target unresolved"; exit 22; }
[[ -n "$CURRENT_SCENE_IMAGE" ]] || { echo "REFUSE: Scene Builder image unresolved"; exit 23; }
[[ "$SCENE_RUNNING" == "true" ]] || { echo "REFUSE: Scene Builder production container is not running"; exit 24; }

PROTECTED_PATHS=(
  "/opt/scene-builder-web"
  "/opt/vps-storage-manager"
  "/var/lib/scene-builder/permanent"
  "/opt/vps1-voice-studio-omnivoice"
  "/var/lib/vps1-voice-studio-omnivoice"
  "/var/lib/local-upload-manager"
  "/var/lib/local-upload-manager-engine2"
  "/home/ubuntu/master_tools"
  "/home/ubuntu/.config/masterctl"
  "$CURRENT_ACS"
  "$ACS/data/database"
  "$ACS/data/models"
  "$CURRENT_ALG"
  "$ALG/shared/vps-chrome-profile"
  "$ALG/shared/vps-extension"
)
for p in "${PROTECTED_PATHS[@]}"; do
  [[ -e "$p" ]] || { echo "REFUSE: protected live path missing: $p"; exit 25; }
  echo "PROTECTED_OK=$p"
done

PERM_BEFORE_BYTES="$(bytes "$SB/permanent")"
PERM_BEFORE_FILES="$(find "$SB/permanent" -type f 2>/dev/null | wc -l || true)"
BEFORE_FREE="$(df -B1 --output=avail / | tail -1 | tr -d ' ')"

# Historical/obsolete roots are cleared only when not referenced by active services/processes.
mapfile -t OBSOLETE_ROOTS < <(
  {
    printf '%s\n' \
      /opt/scene-builder-web/backups \
      /home/ubuntu/archive \
      /home/ubuntu/agent-transfers \
      /opt/vps1-voice-studio-omnivoice/backups
    find /var/lib -maxdepth 1 -type d -name 'local-upload-manager-engine2.pre-activate-*' -print 2>/dev/null || true
  } | awk '!seen[$0]++'
)

LIVE_TEXT="/tmp/v18-full-clean-live-refs.$$"
{
  systemctl list-units --type=service --state=active --no-legend 2>/dev/null | awk '{print $1}' | while read -r svc; do
    systemctl show "$svc" -p ExecStart -p WorkingDirectory -p EnvironmentFiles --no-pager 2>/dev/null || true
  done
  ps -eo args= 2>/dev/null || true
} > "$LIVE_TEXT"
trap 'rm -f "$LIVE_TEXT"' EXIT

for p in "${OBSOLETE_ROOTS[@]}"; do
  [[ -e "$p" ]] || { echo "OBSOLETE_ABSENT=$p"; continue; }
  if grep -Fq -- "$p" "$LIVE_TEXT"; then
    echo "REFUSE: obsolete candidate is referenced by live service/process: $p"
    exit 26
  fi
  echo "OBSOLETE_ELIGIBLE=$p bytes=$(bytes "$p")"
done

# Capture all currently-active protected/control services; they must still be active afterwards.
mapfile -t CONTROL_SERVICES < <(
  for s in \
    local-upload-manager.service \
    local-upload-manager-engine2.service \
    local-upload-manager-engine2-staging-gc.timer \
    vps1-voice-studio-omnivoice-web.service \
    vps1-voice-studio-omnivoice-dispatcher.service \
    vps1-voice-studio-omnivoice-drive-spooler.service
  do
    systemctl is-active --quiet "$s" 2>/dev/null && echo "$s" || true
  done
)
printf 'CONTROL_ACTIVE_BEFORE=%s\n' "${CONTROL_SERVICES[@]:-}"

if [[ "$MODE" == "--preflight" ]]; then
  echo "V18_PREFLIGHT=PASS"
  echo "MUTATION_PERFORMED=NO"
  exit 0
fi

rm -f "$LIVE_TEXT"
trap - EXIT

section "CAPTURE ACTIVE CLEAN-TARGET SERVICES"
mapfile -t ACTIVE_SERVICES < <(
  systemctl list-units --type=service --state=active --no-legend 2>/dev/null |
  awk '{print $1}' |
  grep -E '^(scene-builder-web|auto-caption-|algrow-)' || true
)
printf 'ACTIVE_BEFORE=%s\n' "${ACTIVE_SERVICES[@]:-}"

RESTORE_NEEDED=0
restore_services(){
  set +e
  docker start "$SCENE_CONTAINER" >/dev/null 2>&1 || true
  local svc
  for svc in "${ACTIVE_SERVICES[@]:-}"; do
    [[ -n "$svc" ]] && systemctl start "$svc" >/dev/null 2>&1 || true
  done
}
on_exit(){
  local rc=$?
  if [[ "$RESTORE_NEEDED" == "1" ]]; then restore_services; fi
  exit "$rc"
}
trap on_exit EXIT INT TERM

section "STOP ONLY CLEAN-TARGET WRITERS"
for svc in "${ACTIVE_SERVICES[@]:-}"; do
  [[ -n "$svc" ]] || continue
  echo "STOP=$svc"
  systemctl stop "$svc" 2>/dev/null || true
done
docker stop -t 30 "$SCENE_CONTAINER" >/dev/null 2>&1 || true
RESTORE_NEEDED=1

section "CLEAR ACS RUNTIME DATABASE ROWS - CONFIG/MODELS PRESERVED"
python3 - <<'PY'
import sqlite3
from pathlib import Path
p=Path('/home/ubuntu/apps/auto_caption_studio/data/database/auto_caption.db')
targets=[
'batch_items','batch_upload_chunks','batch_project_plans','job_attempts','job_events','jobs','projects',
'upload_batches','drive_ingress','app_events','scheduler_state','acs_control_audit','drive_batch_queue'
]
if p.exists():
    con=sqlite3.connect(str(p),timeout=30)
    try:
        con.execute('PRAGMA busy_timeout=30000')
        tables={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        existing=[t for t in targets if t in tables]
        for t in existing:
            print(f'ACS_BEFORE|{t}|{con.execute(f"SELECT COUNT(*) FROM \"{t}\"").fetchone()[0]}')
        con.execute('PRAGMA foreign_keys=OFF')
        con.execute('BEGIN IMMEDIATE')
        for t in existing: con.execute(f'DELETE FROM "{t}"')
        if 'sqlite_sequence' in tables:
            for t in existing: con.execute('DELETE FROM sqlite_sequence WHERE name=?',(t,))
        con.commit()
        con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        con.execute('VACUUM')
        for t in existing:
            print(f'ACS_AFTER|{t}|{con.execute(f"SELECT COUNT(*) FROM \"{t}\"").fetchone()[0]}')
    except Exception:
        con.rollback(); raise
    finally:
        con.close()
PY

section "CLEAR ALGROW RUNTIME DATABASE ROWS - CONTROLS/LOGIN PRESERVED"
python3 - <<'PY'
import sqlite3
from pathlib import Path
p=Path('/home/ubuntu/apps/algrow_voice_bridge/shared/data/algrow_voice.sqlite3')
targets=['events','voice_jobs','projects']
if p.exists():
    con=sqlite3.connect(str(p),timeout=30)
    try:
        tables={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        existing=[t for t in targets if t in tables]
        for t in existing: print(f'ALG_BEFORE|{t}|{con.execute(f"SELECT COUNT(*) FROM \"{t}\"").fetchone()[0]}')
        con.execute('PRAGMA foreign_keys=OFF'); con.execute('BEGIN IMMEDIATE')
        for t in existing: con.execute(f'DELETE FROM "{t}"')
        con.commit(); con.execute('PRAGMA wal_checkpoint(TRUNCATE)'); con.execute('VACUUM')
        for t in existing: print(f'ALG_AFTER|{t}|{con.execute(f"SELECT COUNT(*) FROM \"{t}\"").fetchone()[0]}')
    except Exception:
        con.rollback(); raise
    finally: con.close()
PY

section "CLEAR SCENE BUILDER RUNTIME DATABASE ROWS - PERMANENT PRESERVED"
python3 - <<'PY'
import sqlite3
from pathlib import Path
p=Path('/var/lib/scene-builder/queue.sqlite3')
targets=['jobs','job_events','job_attempts','drive_ingress','batch_items','voice_jobs','projects','upload_batches','scheduler_state']
if p.exists() and p.stat().st_size>0:
    con=sqlite3.connect(str(p),timeout=30)
    try:
        tables={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        existing=[t for t in targets if t in tables]
        for t in existing: print(f'SCENE_BEFORE|{t}|{con.execute(f"SELECT COUNT(*) FROM \"{t}\"").fetchone()[0]}')
        con.execute('PRAGMA foreign_keys=OFF'); con.execute('BEGIN IMMEDIATE')
        for t in existing: con.execute(f'DELETE FROM "{t}"')
        con.commit(); con.execute('PRAGMA wal_checkpoint(TRUNCATE)'); con.execute('VACUUM')
        for t in existing: print(f'SCENE_AFTER|{t}|{con.execute(f"SELECT COUNT(*) FROM \"{t}\"").fetchone()[0]}')
    except Exception:
        con.rollback(); raise
    finally: con.close()
PY

section "CLEAR EXPLICIT RUNTIME DIRECTORIES"
clear_contents(){ local d="$1"; [[ -d "$d" ]] || return 0; echo "CLEAR_CONTENTS=$d bytes=$(bytes "$d")"; find "$d" -mindepth 1 -delete 2>/dev/null || true; }
for d in \
  "$ACS/data/projects" "$ACS/data/uploads" "$ACS/data/outputs" "$ACS/data/previews" "$ACS/data/pexels" \
  "$ACS/data/shared/pexels" "$ACS/data/shared_media" "$ACS/data/staging" "$ACS/data/tmp" "$ACS/data/temp" \
  "$ACS/data/cache" "$ACS/data/queue" "$ACS/data/queues" \
  "$ALG/shared/projects" "$ALG/shared/downloads" "$ALG/shared/incoming" "$ALG/shared/output" "$ALG/shared/tmp" \
  "$ALG/shared/temp" "$ALG/shared/queue" "$ALG/shared/queues" \
  "$SB/jobs" "$SB/media" "$SB/uploads" "$SB/outputs" "$SB/cache" "$SB/tmp" "$SB/temp" "$SB/queue" "$SB/queues" "$SB/pexels_library"; do
  clear_contents "$d"
done

section "REMOVE OLD RELEASES - KEEP EXACT CURRENT TARGETS"
clean_old(){
  local dir="$1" keep="$2" x r
  [[ -d "$dir" ]] || return 0
  for x in "$dir"/*; do
    [[ -e "$x" ]] || continue
    r="$(readlink -f "$x" 2>/dev/null || true)"
    if [[ "$r" == "$keep" ]]; then echo "KEEP_CURRENT=$x"; else echo "REMOVE_OLD_RELEASE=$x"; rm -rf --one-file-system -- "$x"; fi
  done
}
clean_old "$ACS/releases" "$CURRENT_ACS"
clean_old "$ALG/releases" "$CURRENT_ALG"

section "REMOVE VERIFIED OBSOLETE/HISTORICAL MATERIAL"
for p in "${OBSOLETE_ROOTS[@]}"; do
  [[ -e "$p" ]] || continue
  echo "REMOVE_OBSOLETE=$p"
  rm -rf --one-file-system -- "$p"
done
rm -rf --one-file-system -- \
  "$ACS/deployments" "$ACS/rollback_snapshots" "$ACS/integration_audit" "$ACS/data/backups" \
  /home/ubuntu/apps/purge_db_backups 2>/dev/null || true
find "$ACS/data/database" -maxdepth 1 -type f \( -name '*.before_*' -o -name '*backup*' -o -name '*.bak' \) -delete 2>/dev/null || true
find "$ALG/shared/data" -maxdepth 1 -type f \( -name '*.before_*' -o -name '*backup*' -o -name '*.bak' \) -delete 2>/dev/null || true

section "CLEAR ALGROW BROWSER CACHE ONLY"
PROFILE="$ALG/shared/vps-chrome-profile"
rm -rf --one-file-system -- \
  "$PROFILE/Default/Cache" "$PROFILE/Default/Code Cache" "$PROFILE/Default/GPUCache" \
  "$PROFILE/GPUCache" "$PROFILE/GrShaderCache" "$PROFILE/ShaderCache" 2>/dev/null || true

section "SAFE SYSTEM CACHE CLEANUP"
apt-get clean 2>/dev/null || true
find /var/lib/snapd/cache -mindepth 1 -maxdepth 1 -type f -delete 2>/dev/null || true
journalctl --vacuum-size=300M >/dev/null 2>&1 || true
find /tmp -xdev -type f -mtime +1 -delete 2>/dev/null || true
if [[ -d /home/ubuntu/.local/share/Trash ]]; then find /home/ubuntu/.local/share/Trash -mindepth 1 -delete 2>/dev/null || true; fi
if [[ -d /home/ubuntu/.cache ]]; then
  find /home/ubuntu/.cache -mindepth 1 -maxdepth 1 ! -name doc ! -name pip -exec rm -rf --one-file-system -- {} + 2>/dev/null || true
fi
mkdir -p /home/ubuntu/.cache/pip
chown ubuntu:ubuntu /home/ubuntu/.cache /home/ubuntu/.cache/pip 2>/dev/null || true
find /home/ubuntu/.cache/pip -mindepth 1 -maxdepth 1 -exec rm -rf --one-file-system -- {} + 2>/dev/null || true

section "SAFE DOCKER CLEANUP - NO BROAD IMAGE PRUNE"
docker start "$SCENE_CONTAINER" >/dev/null
sleep 1
[[ "$(docker inspect -f '{{.Config.Image}}' "$SCENE_CONTAINER")" == "$CURRENT_SCENE_IMAGE" ]] || { echo "REFUSE: Scene image changed before Docker cleanup"; exit 27; }
docker container prune -f || true
docker image prune -f || true
docker builder prune -a -f || true

section "RESTORE PREVIOUSLY ACTIVE CLEAN-TARGET SERVICES"
restore_services
RESTORE_NEEDED=0
sleep 5

section "POST-CLEAN CURRENT-LIVE VERIFICATION"
FINAL_ACS="$(readlink -f "$ACS/current" 2>/dev/null || true)"
FINAL_ALG="$(readlink -f "$ALG/current" 2>/dev/null || true)"
FINAL_SCENE_IMAGE="$(docker inspect -f '{{.Config.Image}}' "$SCENE_CONTAINER" 2>/dev/null || true)"
FINAL_SCENE_RUNNING="$(docker inspect -f '{{.State.Running}}' "$SCENE_CONTAINER" 2>/dev/null || true)"
PERM_AFTER_BYTES="$(bytes "$SB/permanent")"
PERM_AFTER_FILES="$(find "$SB/permanent" -type f 2>/dev/null | wc -l || true)"

echo "FINAL_ACS=$FINAL_ACS"
echo "FINAL_ALG=$FINAL_ALG"
echo "FINAL_SCENE_IMAGE=$FINAL_SCENE_IMAGE"
echo "FINAL_SCENE_RUNNING=$FINAL_SCENE_RUNNING"
echo "PERMANENT_BEFORE bytes=$PERM_BEFORE_BYTES files=$PERM_BEFORE_FILES"
echo "PERMANENT_AFTER bytes=$PERM_AFTER_BYTES files=$PERM_AFTER_FILES"

[[ "$FINAL_ACS" == "$CURRENT_ACS" ]] || { echo "VERIFY_FAIL: ACS current changed"; exit 50; }
[[ "$FINAL_ALG" == "$CURRENT_ALG" ]] || { echo "VERIFY_FAIL: Algrow current changed"; exit 51; }
[[ "$FINAL_SCENE_IMAGE" == "$CURRENT_SCENE_IMAGE" ]] || { echo "VERIFY_FAIL: Scene image changed"; exit 52; }
[[ "$FINAL_SCENE_RUNNING" == "true" ]] || { echo "VERIFY_FAIL: Scene container not running"; exit 53; }
[[ "$PERM_AFTER_BYTES" == "$PERM_BEFORE_BYTES" && "$PERM_AFTER_FILES" == "$PERM_BEFORE_FILES" ]] || { echo "VERIFY_FAIL: Scene permanent area changed"; exit 54; }

for svc in "${ACTIVE_SERVICES[@]:-}"; do
  [[ -n "$svc" ]] || continue
  systemctl is-active --quiet "$svc" || { echo "VERIFY_FAIL: clean-target service inactive $svc"; exit 55; }
  echo "SERVICE_OK=$svc"
done
for svc in "${CONTROL_SERVICES[@]:-}"; do
  [[ -n "$svc" ]] || continue
  systemctl is-active --quiet "$svc" || { echo "VERIFY_FAIL: protected/control service inactive $svc"; exit 56; }
  echo "CONTROL_SERVICE_OK=$svc"
done

curl -fsS --max-time 15 http://127.0.0.1:8000/health >/dev/null && echo "SCENE_HEALTH_OK=YES" || { echo "VERIFY_FAIL: Scene health unavailable"; exit 57; }
curl -fsS --max-time 15 http://127.0.0.1:8130/healthz >/dev/null && echo "OMNIVOICE_HEALTH_OK=YES" || { echo "VERIFY_FAIL: OmniVoice health unavailable"; exit 58; }

python3 - <<'PY'
import sqlite3
from pathlib import Path
checks=[
(Path('/home/ubuntu/apps/auto_caption_studio/data/database/auto_caption.db'), ['projects','jobs','job_events','job_attempts','batch_items','upload_batches','drive_batch_queue']),
(Path('/home/ubuntu/apps/algrow_voice_bridge/shared/data/algrow_voice.sqlite3'), ['projects','voice_jobs','events']),
(Path('/var/lib/scene-builder/queue.sqlite3'), ['jobs','job_events','job_attempts']),
]
for p,tables in checks:
    if not p.exists() or p.stat().st_size==0: continue
    con=sqlite3.connect(str(p),timeout=10)
    try:
        existing={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in tables:
            if t in existing:
                n=con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                print(f'VERIFY_RUNTIME_ROWS|{p}|{t}|{n}')
                if n != 0: raise SystemExit(f'Runtime table not empty: {p}:{t}={n}')
    finally:
        con.close()
PY

AFTER_FREE="$(df -B1 --output=avail / | tail -1 | tr -d ' ')"
RECLAIMED=$(( AFTER_FREE > BEFORE_FREE ? AFTER_FREE - BEFORE_FREE : 0 ))
section "V18 CLEAN RESULT"
echo "BEFORE_FREE_BYTES=$BEFORE_FREE"
echo "AFTER_FREE_BYTES=$AFTER_FREE"
echo "RECLAIMED_BYTES=$RECLAIMED"
df -h /
echo "MUTATION_PERFORMED=YES"
echo "BACKUP_CREATED=NO"
echo "CURRENT_LIVE_SYSTEM_VERIFIED=YES"
echo "V18_EXECUTION_COMPLETE=YES"
