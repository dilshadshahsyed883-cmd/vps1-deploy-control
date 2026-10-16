#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# NOT a general SSH shell. Install root-owned through Agent 1 Drive Bridge only
# after exact service/permissions/baseline preflight. sudoers must allow only this path.
[[ "$(id -u)" -eq 0 ]] || { echo 'BLOCKED=NONROOT'; exit 20; }
[[ "$#" -ge 2 && "$#" -le 3 ]] || exit 20
COMMIT="$1"; DIGEST="$2"; MODE="${3:-deploy}"
[[ "$COMMIT" =~ ^[0-9a-f]{40}$ && "$DIGEST" =~ ^[0-9a-f]{64}$ ]] || exit 20
[[ "$MODE" == deploy || "$MODE" == rollback-probe ]] || exit 20
APP=/opt/scene-builder-web
CFG=/etc/auto-script-cicd
STATE=/var/lib/auto-script-cicd/current.sha256
BACKUP_ROOT=/var/backups/auto-script-cicd
INCOMING="/home/vps1deploy/auto-script-incoming/$COMMIT"
ARCHIVE="$INCOMING/auto-script.tar.gz"
META="$INCOMING/release.json"
for path in "$APP" "$CFG" "$STATE" "$ARCHIVE" "$META" "$CFG/service-name" "$CFG/health-url"; do
  [[ -e "$path" && ! -L "$path" ]] || { echo "BLOCKED=REQUIRED_BOUNDARY:$path"; exit 21; }
done
[[ "$(stat -c '%u' "$CFG/service-name")" -eq 0 && "$(stat -c '%u' "$CFG/health-url")" -eq 0 && "$(stat -c '%u' "$STATE")" -eq 0 ]] || exit 21
[[ ! -w "$CFG/service-name" || "$(stat -c '%a' "$CFG/service-name")" =~ ^[46][0-4][0-4]$ ]] || exit 21
read -r SERVICE < "$CFG/service-name"
read -r HEALTH_URL < "$CFG/health-url"
[[ "$SERVICE" =~ ^[a-zA-Z0-9_.@-]+\.service$ ]] || exit 21
[[ "$HEALTH_URL" == http://127.0.0.1:*/* || "$HEALTH_URL" == http://localhost:*/* ]] || { echo 'BLOCKED=HEALTH_URL_MUST_BE_LOCAL'; exit 21; }
[[ -f "$APP/auto_script.py" && -f "$APP/templates/auto_script.html" && -f "$APP/static/auto_script.js" && -f "$APP/static/auto_script.css" ]] || exit 21
# Detect out-of-band edits before touching any source; bootstrap must capture initial hashes.
(cd / && sha256sum --check --status "$STATE") || { echo 'BLOCKED=SOURCE_DRIFT'; exit 22; }
systemctl is-active --quiet "$SERVICE" || { echo 'BLOCKED=BASELINE_SERVICE_DOWN'; exit 22; }
[[ "$(sha256sum "$ARCHIVE" | cut -d' ' -f1)" == "$DIGEST" ]] || { echo 'BLOCKED=CHECKSUM_MISMATCH'; exit 23; }

STAGE="$(mktemp -d /var/tmp/auto-script-release.XXXXXXXX)"
BACKUP=''
SWITCHED=0
FILES=( auto_script.py templates/auto_script.html static/auto_script.js static/auto_script.css )
rollback() {
  local rc="$?"
  trap - EXIT
  if [[ "$rc" -ne 0 && "$SWITCHED" -eq 1 && -n "$BACKUP" ]]; then
    for file in "${FILES[@]}"; do
      local live="$APP/$file" restore="$APP/$file.rollback.$$"
      cp -a -- "$BACKUP/$file" "$restore" && mv -fT -- "$restore" "$live" || echo "ROLLBACK_FILE_FAILED=$file"
    done
    cp -a -- "$BACKUP/current.sha256" "$STATE" || true
    timeout 45s systemctl restart "$SERVICE" || true
    echo 'AUTO_SCRIPT_ROLLBACK_ATTEMPTED=YES'
    if ( cd / && sha256sum --check --status "$STATE" ) && systemctl is-active --quiet "$SERVICE"; then
      echo 'AUTO_SCRIPT_ROLLBACK_BASELINE=PASS'
    else
      echo 'AUTO_SCRIPT_ROLLBACK_BASELINE=BLOCKED'
    fi
  fi
  rm -rf -- "$STAGE"
  exit "$rc"
}
trap rollback EXIT

python3 - "$ARCHIVE" "$META" "$DIGEST" "$STAGE" <<'PY'
import ast,hashlib,json,pathlib,sys,tarfile
archive,meta,digest,stage=sys.argv[1:]
m=json.load(open(meta))
assert set(m)=={'app_id','artifact','schema_version','sha256'}
assert m=={'app_id':'auto-script','artifact':'auto-script.tar.gz','schema_version':1,'sha256':digest}
allowed=['auto_script.py','templates/auto_script.html','static/auto_script.js','static/auto_script.css']
with tarfile.open(archive,'r:gz') as tar:
    members=tar.getmembers()
    assert [i.name for i in members]==['auto-script/'+x for x in allowed]+['auto-script/release-files.json']
    assert all(i.isfile() and i.size <= 2_000_000 for i in members)
    filemap={}
    for i in members:
        contents=tar.extractfile(i).read()
        assert len(contents)==i.size
        filemap[i.name.removeprefix('auto-script/')]=contents
catalog=json.loads(filemap.pop('release-files.json'))
assert catalog['app_id']=='auto-script' and catalog['schema_version']==1
assert [i['path'] for i in catalog['files']]==allowed
for f in catalog['files']:
    blob=filemap[f['path']]
    assert f['bytes']==len(blob) and f['sha256']==hashlib.sha256(blob).hexdigest()
    if f['path'].endswith('.py'): ast.parse(blob.decode('utf8'))
    dest=pathlib.Path(stage)/f['path']
    dest.parent.mkdir(parents=True,exist_ok=True)
    dest.write_bytes(blob)
print('AUTO_SCRIPT_REMOTE_ARTIFACT_VALIDATION=PASS')
PY

mkdir -p -- "$BACKUP_ROOT" /var/lib/auto-script-cicd
BACKUP="$(mktemp -d "$BACKUP_ROOT/$COMMIT.XXXXXXXX")"
for file in "${FILES[@]}"; do
  [[ ! -L "$APP/$file" ]] || exit 24
  mkdir -p "$BACKUP/$(dirname "$file")"
  cp -a -- "$APP/$file" "$BACKUP/$file"
done
cp -a -- "$STATE" "$BACKUP/current.sha256"
echo "AUTO_SCRIPT_BACKUP_PATH=$BACKUP"

SWITCHED=1
for file in "${FILES[@]}"; do
  live="$APP/$file"
  next="$APP/$file.cicd.$$"
  cp -- "$STAGE/$file" "$next"
  chown --reference="$live" "$next"
  chmod --reference="$live" "$next"
  mv -fT -- "$next" "$live"
done
sha256sum "$APP/auto_script.py" "$APP/templates/auto_script.html" "$APP/static/auto_script.js" "$APP/static/auto_script.css" > "$STATE"
timeout 45s systemctl restart "$SERVICE"
systemctl is-active --quiet "$SERVICE"
OK=NO
for i in 1 2 3 4 5; do
  STATUS="$(curl --silent --output /dev/null --max-time 8 --write-out '%{http_code}' "$HEALTH_URL" || true)"
  if [[ "$STATUS" == 200 ]]; then OK=YES; break; fi
  sleep 2
done
[[ "$OK" == YES ]] || { echo "BLOCKED=HEALTH_CHECK_HTTP_${STATUS:-NONE}"; exit 25; }
if [[ "$MODE" == rollback-probe ]]; then
  echo 'CONTROLLED_FAILURE_INJECTED=YES'
  exit 26
fi
SWITCHED=0
echo "AUTO_SCRIPT_RELEASE_SHA256=$DIGEST"
echo "AUTO_SCRIPT_RELEASE_COMMIT=$COMMIT"
echo 'AUTO_SCRIPT_SERVICE_HEALTH=PASS'
echo 'AUTO_SCRIPT_DEPLOY_RESULT=PASS'
