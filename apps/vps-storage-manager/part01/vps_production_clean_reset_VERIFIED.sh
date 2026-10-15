#!/usr/bin/env bash
# ============================================================================
# VPS PRODUCTION CLEAN RESET - VERIFIED
# ============================================================================
#
# Goal:
#   Keep the CURRENT working production system and remove old project/job data,
#   runtime media, old releases/backups/packages, caches, stopped Docker
#   containers and unused Docker images.
#
# Preserves:
#   - Current ACS release (resolved from /home/ubuntu/apps/auto_caption_studio/current)
#   - Current Algrow release (resolved from /home/ubuntu/apps/algrow_voice_bridge/current)
#   - ACS live database structure/config/control/model data
#   - Algrow controls + worker_runtime + Chrome login/profile
#   - Scene Builder application source/config
#   - Running Scene Builder Docker image/container
#   - Current secrets/config/models
#
# Removes/resets:
#   - queued/failed/completed/cancelled project/job history
#   - ACS/Algrow/Scene Builder runtime project media
#   - old ACS/Algrow releases and deployment/rollback copies
#   - /home/ubuntu/archive and purge_db_backups by default
#   - Algrow browser CACHE only (not login/profile)
#   - user/browser cache and Trash
#   - stopped Docker containers, unused Docker images, Docker build cache
#
# IMPORTANT:
#   Default is DRY RUN. Nothing is deleted unless --execute is supplied.
#
# Usage:
#   sudo bash vps_production_clean_reset_VERIFIED.sh
#   sudo bash vps_production_clean_reset_VERIFIED.sh --execute
#
# Optional:
#   KEEP_ARCHIVE=1 sudo bash vps_production_clean_reset_VERIFIED.sh --execute
#
# ============================================================================
set -Eeuo pipefail

MODE="dry-run"
case "${1:-}" in
  "")          MODE="dry-run" ;;
  --dry-run)   MODE="dry-run" ;;
  --execute)   MODE="execute" ;;
  *)
    echo "Usage: sudo bash $0 [--dry-run|--execute]"
    exit 2
    ;;
esac

if [[ "${EUID}" -ne 0 ]]; then
  echo "ERROR: Run with sudo/root."
  exit 1
fi

BASE="/home/ubuntu"
ACS="$BASE/apps/auto_caption_studio"
ALGROW="$BASE/apps/algrow_voice_bridge"
SB="/var/lib/scene-builder"
SCENE_CONTAINER="scene-builder-worker"

[[ -d "$ACS" ]] || { echo "ERROR: Missing $ACS"; exit 1; }
[[ -d "$ALGROW" ]] || { echo "ERROR: Missing $ALGROW"; exit 1; }
[[ -d "$SB" ]] || { echo "ERROR: Missing $SB"; exit 1; }

CURRENT_ACS="$(readlink -f "$ACS/current" 2>/dev/null || true)"
CURRENT_ALGROW="$(readlink -f "$ALGROW/current" 2>/dev/null || true)"
CURRENT_SCENE_IMAGE="$(docker inspect -f '{{.Config.Image}}' "$SCENE_CONTAINER" 2>/dev/null || true)"

[[ -n "$CURRENT_ACS" && -d "$CURRENT_ACS" ]] || {
  echo "ERROR: Could not resolve current ACS release."
  exit 1
}

[[ -n "$CURRENT_ALGROW" && -d "$CURRENT_ALGROW" ]] || {
  echo "ERROR: Could not resolve current Algrow release."
  exit 1
}

[[ -n "$CURRENT_SCENE_IMAGE" ]] || {
  echo "ERROR: Could not resolve Scene Builder container/image."
  exit 1
}

section() {
  echo
  echo "============================================================"
  echo " $*"
  echo "============================================================"
}

run() {
  if [[ "$MODE" == "dry-run" ]]; then
    printf '[DRY RUN] '
    printf '%q ' "$@"
    printf '\n'
  else
    "$@"
  fi
}

section "CURRENT PRODUCTION / SAFETY CHECK"
echo "Mode:                $MODE"
echo "ACS current:         $CURRENT_ACS"
echo "Algrow current:      $CURRENT_ALGROW"
echo "Scene Builder image: $CURRENT_SCENE_IMAGE"
echo
df -h /

if [[ "$MODE" == "dry-run" ]]; then
  echo
  echo "NO DATA WILL BE DELETED IN THIS RUN."
fi

# ---------------------------------------------------------------------------
# Record only services that are active now.
# On completion/error, restart only those services.
# ---------------------------------------------------------------------------
SERVICES=(
  auto-caption-web
  auto-caption-worker
  auto-caption-renderer
  auto-caption-compressor
  auto-caption-drive-uploader
  algrow-voice-sidecar
  algrow-vps-chrome
  algrow-vps-viewer
  algrow-vps-x11vnc
  algrow-vps-xvfb
  scene-builder-web
)

ACTIVE_SERVICES=()

for svc in "${SERVICES[@]}"; do
  if systemctl is-active --quiet "$svc" 2>/dev/null; then
    ACTIVE_SERVICES+=("$svc")
  fi
done

SCENE_WAS_RUNNING=0
if [[ "$(docker inspect -f '{{.State.Running}}' "$SCENE_CONTAINER" 2>/dev/null || true)" == "true" ]]; then
  SCENE_WAS_RUNNING=1
fi

was_active() {
  local wanted="$1"
  local x
  for x in "${ACTIVE_SERVICES[@]}"; do
    [[ "$x" == "$wanted" ]] && return 0
  done
  return 1
}

start_original_services() {
  # Dependency-friendly order that matches the working VPS setup.
  local restart_order=(
    algrow-vps-xvfb
    algrow-vps-chrome
    algrow-vps-x11vnc
    algrow-vps-viewer
    algrow-voice-sidecar
    auto-caption-worker
    auto-caption-renderer
    auto-caption-compressor
    auto-caption-drive-uploader
    scene-builder-web
    auto-caption-web
  )

  if (( SCENE_WAS_RUNNING )); then
    docker start "$SCENE_CONTAINER" >/dev/null 2>&1 || true
  fi

  local svc
  for svc in "${restart_order[@]}"; do
    if was_active "$svc"; then
      systemctl start "$svc" 2>/dev/null || true
    fi
  done
}

stop_active_services() {
  local svc
  for svc in "${ACTIVE_SERVICES[@]}"; do
    echo "STOP $svc"
    systemctl stop "$svc" 2>/dev/null || true
  done

  if (( SCENE_WAS_RUNNING )); then
    echo "STOP Docker $SCENE_CONTAINER"
    docker stop -t 30 "$SCENE_CONTAINER" >/dev/null 2>&1 || true
  fi
}

if [[ "$MODE" == "execute" ]]; then
  section "STOP ACTIVE PROJECT / QUEUE WRITERS"
  trap 'start_original_services' EXIT
  stop_active_services
else
  section "SERVICES THAT WOULD BE TEMPORARILY STOPPED"
  printf '%s\n' "${ACTIVE_SERVICES[@]:-NONE}"
  (( SCENE_WAS_RUNNING )) && echo "$SCENE_CONTAINER (Docker)" || true
fi

# ---------------------------------------------------------------------------
# SQLite reset.
#
# Uses Python stdlib sqlite3 because the VPS does not require the sqlite3 CLI.
# The table sets are the same project/queue/history families used by the
# successful reset workflow, plus the current ACS/Algrow history tables that
# were successfully cleared during the production cleanup.
# ---------------------------------------------------------------------------
section "RESET PROJECT / QUEUE DATABASE ROWS"

if [[ "$MODE" == "dry-run" ]]; then
  echo "[DRY RUN] Would scan:"
  echo "  $ACS/data"
  echo "  $ALGROW/shared (excluding vps-chrome-profile/backups)"
  echo "  $SB"
  echo
  echo "[DRY RUN] ACS target tables:"
  echo "  batch_items batch_upload_chunks batch_project_plans"
  echo "  job_attempts job_events jobs projects upload_batches"
  echo "  drive_ingress app_events scheduler_state acs_control_audit"
  echo
  echo "[DRY RUN] Algrow target tables:"
  echo "  events voice_jobs projects"
  echo
  echo "[DRY RUN] Scene Builder target tables:"
  echo "  jobs job_events job_attempts drive_ingress batch_items"
  echo "  voice_jobs projects upload_batches scheduler_state"
else
  python3 - <<'PY'
import os
import sqlite3
from pathlib import Path

ACS_ROOT = Path("/home/ubuntu/apps/auto_caption_studio/data")
ALG_ROOT = Path("/home/ubuntu/apps/algrow_voice_bridge/shared")
SB_ROOT = Path("/var/lib/scene-builder")

ACS_TABLES = [
    "batch_items",
    "batch_upload_chunks",
    "batch_project_plans",
    "job_attempts",
    "job_events",
    "jobs",
    "projects",
    "upload_batches",
    "drive_ingress",
    "app_events",
    "scheduler_state",
    "acs_control_audit",
]

ALG_TABLES = [
    "events",
    "voice_jobs",
    "projects",
]

SB_TABLES = [
    "jobs",
    "job_events",
    "job_attempts",
    "drive_ingress",
    "batch_items",
    "voice_jobs",
    "projects",
    "upload_batches",
    "scheduler_state",
]

def is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except Exception:
        return False

def iter_sqlite(root: Path, max_depth: int, exclude_parts=()):
    if not root.exists():
        return
    root_depth = len(root.parts)
    for base, dirs, files in os.walk(root):
        base_path = Path(base)
        depth = len(base_path.parts) - root_depth

        dirs[:] = [
            d for d in dirs
            if depth < max_depth
            and d not in {"__pycache__", "venv"}
            and not any(x in (base_path / d).parts for x in exclude_parts)
        ]

        for name in files:
            if not name.lower().endswith((".db", ".sqlite", ".sqlite3")):
                continue
            p = base_path / name
            if any(x in p.parts for x in exclude_parts):
                continue
            if is_sqlite(p):
                yield p

def table_exists(con, table):
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,)
    ).fetchone() is not None

def clear_tables(path: Path, targets):
    con = sqlite3.connect(str(path), timeout=30)
    try:
        con.execute("PRAGMA busy_timeout=30000")
        existing = [t for t in targets if table_exists(con, t)]
        if not existing:
            return False

        print(f"\nRESET DB: {path}")
        print("Before:")
        for t in existing:
            n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            print(f"  {t}: {n}")

        # The previous full reset used foreign_keys=OFF for a complete
        # queue/history purge, preventing delete-order FK conflicts.
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("BEGIN IMMEDIATE")

        for t in existing:
            con.execute(f'DELETE FROM "{t}"')

        if table_exists(con, "sqlite_sequence"):
            for t in existing:
                con.execute(
                    "DELETE FROM sqlite_sequence WHERE name=?",
                    (t,)
                )

        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("VACUUM")

        print("After:")
        for t in existing:
            n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            print(f"  {t}: {n}")
        return True

    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

seen = set()

for p in iter_sqlite(ACS_ROOT, 4, exclude_parts=("models", "backups")):
    rp = str(p.resolve())
    if rp not in seen:
        clear_tables(p, ACS_TABLES)
        seen.add(rp)

for p in iter_sqlite(
    ALG_ROOT,
    5,
    exclude_parts=("vps-chrome-profile", "backups"),
):
    rp = str(p.resolve())
    if rp not in seen:
        clear_tables(p, ALG_TABLES)
        seen.add(rp)

for p in iter_sqlite(SB_ROOT, 5):
    rp = str(p.resolve())
    if rp not in seen:
        clear_tables(p, SB_TABLES)
        seen.add(rp)
PY
fi

# ---------------------------------------------------------------------------
# Runtime directories successfully targeted by the prior reset workflow.
# ---------------------------------------------------------------------------
section "CLEAR PROJECT / RUNTIME DIRECTORIES"

ACS_RUNTIME_DIRS=(
  "$ACS/data/projects"
  "$ACS/data/uploads"
  "$ACS/data/outputs"
  "$ACS/data/previews"
  "$ACS/data/pexels"
  "$ACS/data/shared/pexels"
  "$ACS/data/shared_media"
  "$ACS/data/staging"
  "$ACS/data/tmp"
  "$ACS/data/temp"
  "$ACS/data/cache"
  "$ACS/data/queue"
  "$ACS/data/queues"
)

ALG_RUNTIME_DIRS=(
  "$ALGROW/shared/projects"
  "$ALGROW/shared/downloads"
  "$ALGROW/shared/incoming"
  "$ALGROW/shared/output"
  "$ALGROW/shared/tmp"
  "$ALGROW/shared/temp"
  "$ALGROW/shared/queue"
  "$ALGROW/shared/queues"
)

SB_RUNTIME_DIRS=(
  "$SB/jobs"
  "$SB/media"
  "$SB/uploads"
  "$SB/outputs"
  "$SB/cache"
  "$SB/tmp"
  "$SB/temp"
  "$SB/queue"
  "$SB/queues"
  "$SB/pexels_library"
)

clear_dir_contents() {
  local d="$1"
  [[ -d "$d" ]] || return 0

  if [[ "$MODE" == "dry-run" ]]; then
    echo "[DRY RUN] CLEAR $d"
  else
    echo "CLEAR $d"
    find "$d" -mindepth 1 -delete 2>/dev/null || true
  fi
}

for d in "${ACS_RUNTIME_DIRS[@]}"; do clear_dir_contents "$d"; done
for d in "${ALG_RUNTIME_DIRS[@]}"; do clear_dir_contents "$d"; done
for d in "${SB_RUNTIME_DIRS[@]}"; do clear_dir_contents "$d"; done

# ---------------------------------------------------------------------------
# Leftover runtime media cleanup, with the same important exclusions:
#   ACS models/database are preserved.
#   Algrow Chrome profile/extension are preserved.
# ---------------------------------------------------------------------------
section "REMOVE LEFTOVER PROJECT MEDIA"

if [[ "$MODE" == "dry-run" ]]; then
  echo "[DRY RUN] Remove runtime media under ACS data excluding models/database."
  echo "[DRY RUN] Remove runtime media under Algrow shared excluding Chrome profile/extension."
  echo "[DRY RUN] Remove runtime media under Scene Builder runtime."
else
  if [[ -d "$ACS/data" ]]; then
    find "$ACS/data" \
      \( -path "$ACS/data/models" -o -path "$ACS/data/models/*" \
         -o -path "$ACS/data/database" -o -path "$ACS/data/database/*" \
         -o -path "$ACS/data/backups" -o -path "$ACS/data/backups/*" \) -prune -o \
      -type f \( \
        -iname '*.mp4' -o -iname '*.mkv' -o -iname '*.mov' -o -iname '*.webm' -o -iname '*.avi' -o \
        -iname '*.mp3' -o -iname '*.m4a' -o -iname '*.wav' -o -iname '*.aac' -o -iname '*.ogg' -o -iname '*.flac' -o \
        -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.webp' -o -iname '*.gif' \
      \) -delete 2>/dev/null || true
  fi

  if [[ -d "$ALGROW/shared" ]]; then
    find "$ALGROW/shared" \
      \( -path "$ALGROW/shared/vps-chrome-profile" -o -path "$ALGROW/shared/vps-chrome-profile/*" \
         -o -path "$ALGROW/shared/vps-extension" -o -path "$ALGROW/shared/vps-extension/*" \) -prune -o \
      -type f \( \
        -iname '*.mp4' -o -iname '*.mkv' -o -iname '*.mov' -o -iname '*.webm' -o -iname '*.avi' -o \
        -iname '*.mp3' -o -iname '*.m4a' -o -iname '*.wav' -o -iname '*.aac' -o -iname '*.ogg' -o -iname '*.flac' -o \
        -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.webp' -o -iname '*.gif' \
      \) -delete 2>/dev/null || true
  fi

  if [[ -d "$SB" ]]; then
    find "$SB" -type f \( \
      -iname '*.mp4' -o -iname '*.mkv' -o -iname '*.mov' -o -iname '*.webm' -o -iname '*.avi' -o \
      -iname '*.mp3' -o -iname '*.m4a' -o -iname '*.wav' -o -iname '*.aac' -o -iname '*.ogg' -o -iname '*.flac' -o \
      -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.webp' -o -iname '*.gif' \
    \) -delete 2>/dev/null || true
  fi
fi

# ---------------------------------------------------------------------------
# Old releases: preserve exactly the current symlink targets.
# ---------------------------------------------------------------------------
section "REMOVE OLD APPLICATION RELEASES"

clean_old_releases() {
  local releases_dir="$1"
  local keep="$2"
  [[ -d "$releases_dir" ]] || return 0

  local item real
  for item in "$releases_dir"/*; do
    [[ -e "$item" ]] || continue
    real="$(readlink -f "$item" 2>/dev/null || true)"

    if [[ "$real" != "$keep" ]]; then
      run rm -rf -- "$item"
    else
      echo "KEEP current: $item"
    fi
  done
}

clean_old_releases "$ACS/releases" "$CURRENT_ACS"
clean_old_releases "$ALGROW/releases" "$CURRENT_ALGROW"

# Old deployment/rollback copies that were removed successfully in the
# production-only cleanup.
run rm -rf -- \
  "$ACS/deployments" \
  "$ACS/rollback_snapshots" \
  "$ACS/integration_audit" \
  "$ACS/data/backups"

if [[ "$MODE" == "execute" ]]; then
  mkdir -p "$ACS/logs"
  find "$ACS/logs" -mindepth 1 -type f -delete 2>/dev/null || true

  find "$ACS/data/database" -maxdepth 1 -type f \
    \( -name '*.before_*' -o -name '*backup*' -o -name '*.bak' \) \
    -delete 2>/dev/null || true

  find "$ALGROW/shared/data" -maxdepth 1 -type f \
    \( -name '*.before_*' -o -name '*backup*' -o -name '*.bak' \) \
    -delete 2>/dev/null || true
else
  echo "[DRY RUN] Clear ACS logs and old ACS/Algrow DB-copy files."
fi

# ---------------------------------------------------------------------------
# Browser cache only. Do NOT touch login/session/profile data.
# ---------------------------------------------------------------------------
section "CLEAR ALGROW CHROME CACHE - PRESERVE LOGIN PROFILE"

PROFILE="$ALGROW/shared/vps-chrome-profile"

run rm -rf -- \
  "$PROFILE/Default/Cache" \
  "$PROFILE/Default/Code Cache" \
  "$PROFILE/Default/GPUCache" \
  "$PROFILE/GPUCache" \
  "$PROFILE/GrShaderCache" \
  "$PROFILE/ShaderCache"

# ---------------------------------------------------------------------------
# Old archives/packages. The user requested production-only storage.
# ---------------------------------------------------------------------------
section "REMOVE OLD ARCHIVES / BACKUPS / INSTALL PACKAGES"

run rm -rf -- "$BASE/apps/purge_db_backups"

if [[ "${KEEP_ARCHIVE:-0}" == "1" ]]; then
  echo "KEEP_ARCHIVE=1 -> KEEP $BASE/archive"
else
  run rm -rf -- "$BASE/archive"
fi

if [[ "$MODE" == "execute" ]]; then
  # Known root-level Scene Builder deployment workspaces.
  find "$BASE" -maxdepth 1 -type d \
    \( -name 'v*-xfade-safe' -o -name 'v*-xfade-*' \) \
    -exec rm -rf -- {} + 2>/dev/null || true

  # Root-level Scene Builder install archives, not application source.
  find "$BASE" -maxdepth 1 -type f \
    \( -iname 'Scene_Builder_*.zip' -o \
       -iname 'Scene Builder *.zip' -o \
       -iname 'Scene_Builder_*.tar.gz' -o \
       -iname 'Scene Builder *.tar.gz' \) \
    -delete 2>/dev/null || true

  rm -rf "$BASE/.scene_builder_deploy"
  rm -f "$BASE/.scene_builder_deployment2_last_backup"

  find "$BASE" -maxdepth 1 -type f \
    \( -name 'HOME_STORAGE_AUDIT_*.txt' -o \
       -name 'VPS_PROJECT_RESET*.log' -o \
       -name 'FINAL_QUEUE_PEXELS_CLEAN_*.log' \) \
    -delete 2>/dev/null || true
else
  echo "[DRY RUN] Remove root-level old Scene Builder install workspace/archive files."
  echo "[DRY RUN] Remove old reset/audit logs."
fi

# ---------------------------------------------------------------------------
# Safe caches/Trash.
# .cache/doc is explicitly excluded because it produced a permission/mount
# error during the successful cleanup session.
# ---------------------------------------------------------------------------
section "CLEAR SAFE CACHE / TRASH"

if [[ "$MODE" == "execute" ]]; then
  if [[ -d "$BASE/.cache" ]]; then
    find "$BASE/.cache" \
      -mindepth 1 -maxdepth 1 \
      ! -name doc \
      ! -name pip \
      -exec rm -rf -- {} + 2>/dev/null || true
  fi

  # Keep the pip cache DIRECTORY itself because vps-storage-manager.service
  # lists /home/ubuntu/.cache/pip in ReadWritePaths. Deleting the directory
  # makes systemd fail with status=226/NAMESPACE on the next service restart.
  mkdir -p "$BASE/.cache/pip"
  chown ubuntu:ubuntu "$BASE/.cache" "$BASE/.cache/pip" 2>/dev/null || true
  if [[ -d "$BASE/.cache/pip" ]]; then
    find "$BASE/.cache/pip" \
      -mindepth 1 -maxdepth 1 \
      -exec rm -rf -- {} + 2>/dev/null || true
  fi

  if [[ -d "$BASE/snap/firefox/common/.cache" ]]; then
    find "$BASE/snap/firefox/common/.cache" \
      -mindepth 1 -maxdepth 1 \
      -exec rm -rf -- {} + 2>/dev/null || true
  fi

  if [[ -d "$BASE/.local/share/Trash" ]]; then
    find "$BASE/.local/share/Trash" \
      -mindepth 1 -maxdepth 1 \
      -exec rm -rf -- {} + 2>/dev/null || true
  fi

  apt-get clean 2>/dev/null || true
  find /var/lib/snapd/cache -mindepth 1 -maxdepth 1 -type f -delete 2>/dev/null || true
  journalctl --vacuum-size=300M >/dev/null 2>&1 || true
  find /tmp -xdev -type f -mtime +1 -delete 2>/dev/null || true
else
  echo "[DRY RUN] Clear $BASE/.cache except $BASE/.cache/doc and the $BASE/.cache/pip directory"
  echo "[DRY RUN] Clear Firefox cache"
  echo "[DRY RUN] Clear Trash"
  echo "[DRY RUN] apt-get clean / snap cache / old tmp / journal vacuum"
fi

# ---------------------------------------------------------------------------
# Docker cleanup.
#
# IMPORTANT: Start the production container BEFORE image prune so its image
# is protected by Docker as an in-use image.
# ---------------------------------------------------------------------------
section "DOCKER CLEANUP"

if [[ "$MODE" == "execute" ]]; then
  if (( SCENE_WAS_RUNNING )); then
    docker start "$SCENE_CONTAINER" >/dev/null
  fi

  docker container prune -f
  docker image prune -a -f
  docker builder prune -a -f || true
else
  echo "[DRY RUN] Stopped Docker containers:"
  docker ps -a --filter status=exited \
    --format '  {{.Names}} -> {{.Image}}' || true
  echo
  echo "[DRY RUN] Would run:"
  echo "  docker container prune -f"
  echo "  docker image prune -a -f"
  echo "  docker builder prune -a -f"
  echo "Running-container images remain protected."
fi

# ---------------------------------------------------------------------------
# Restart only what was active at the beginning.
# ---------------------------------------------------------------------------
if [[ "$MODE" == "execute" ]]; then
  section "RESTART ORIGINAL PRODUCTION SERVICES"
  start_original_services
  trap - EXIT

  sleep 5

  section "FINAL VERIFICATION"

  FINAL_ACS="$(readlink -f "$ACS/current" 2>/dev/null || true)"
  FINAL_ALGROW="$(readlink -f "$ALGROW/current" 2>/dev/null || true)"
  FINAL_SCENE_IMAGE="$(docker inspect -f '{{.Config.Image}}' "$SCENE_CONTAINER" 2>/dev/null || true)"

  echo "ACS current:"
  echo "  $FINAL_ACS"
  echo "Algrow current:"
  echo "  $FINAL_ALGROW"
  echo "Scene Builder image:"
  echo "  $FINAL_SCENE_IMAGE"

  if [[ "$FINAL_ACS" != "$CURRENT_ACS" ]]; then
    echo "ERROR: ACS current release changed unexpectedly."
    exit 1
  fi

  if [[ "$FINAL_ALGROW" != "$CURRENT_ALGROW" ]]; then
    echo "ERROR: Algrow current release changed unexpectedly."
    exit 1
  fi

  if [[ "$FINAL_SCENE_IMAGE" != "$CURRENT_SCENE_IMAGE" ]]; then
    echo "ERROR: Scene Builder image changed unexpectedly."
    exit 1
  fi

  echo
  echo "--- Scene Builder container ---"
  docker ps --filter "name=$SCENE_CONTAINER" \
    --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'

  echo
  echo "--- Scene Builder health ---"
  curl -fsS http://127.0.0.1:8000/health | python3 -m json.tool || true

  echo
  echo "--- Remaining project/history rows ---"
  python3 - <<'PY'
import os
import sqlite3
from pathlib import Path

checks = [
    (
        "ACS",
        Path("/home/ubuntu/apps/auto_caption_studio/data/database/auto_caption.db"),
        [
            "batch_items",
            "batch_upload_chunks",
            "batch_project_plans",
            "job_attempts",
            "job_events",
            "jobs",
            "projects",
            "upload_batches",
            "drive_ingress",
            "app_events",
            "scheduler_state",
            "acs_control_audit",
            "acs_pipeline_controls",
            "acs_worker_runtime",
            "shared_fg_assets",
        ],
    ),
    (
        "ALGROW",
        Path("/home/ubuntu/apps/algrow_voice_bridge/shared/data/algrow_voice.sqlite3"),
        [
            "projects",
            "voice_jobs",
            "events",
            "controls",
            "worker_runtime",
        ],
    ),
    (
        "SCENE BUILDER",
        Path("/var/lib/scene-builder/queue.sqlite3"),
        ["jobs"],
    ),
]

for title, db, tables in checks:
    print(f"\n{title}:")
    if not db.exists():
        print(f"  database not found: {db}")
        continue

    con = sqlite3.connect(str(db), timeout=10)
    try:
        for table in tables:
            present = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,)
            ).fetchone()
            if present:
                n = con.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0]
                print(f"  {table}: {n}")
    finally:
        con.close()
PY

  echo
  echo "--- Active production services ---"
  for svc in "${SERVICES[@]}"; do
    if was_active "$svc"; then
      printf '%-40s ' "$svc"
      systemctl is-active "$svc" 2>/dev/null || true
    fi
  done

  echo
  echo "--- Docker storage ---"
  docker system df

  echo
  echo "--- Home/apps storage ---"
  du -xsh "$BASE" 2>/dev/null || true
  du -xhd2 "$BASE/apps" 2>/dev/null | sort -hr | head -40 || true

  echo
  echo "--- Root disk ---"
  df -h /

  section "PRODUCTION CLEAN RESET COMPLETE"
else
  section "DRY RUN COMPLETE"
  echo "Nothing was deleted."
  echo
  echo "If the displayed CURRENT PRODUCTION values are correct,"
  echo "run the real cleanup with:"
  echo
  echo "  sudo bash $0 --execute"
fi
