#!/usr/bin/env bash
# Install guarded 12h lifecycle cleaner on an approved VPS1 host.
# Run only via an existing approved root deployment helper.
set -Eeuo pipefail
umask 077

[[ "${EUID}" -eq 0 ]] || { echo 'REFUSE: requires approved root helper'; exit 21; }
[[ -f production/storage_gc.py && -f infrastructure/systemd/vps1-storage-deployment-gc.timer ]] || {
  echo 'REFUSE: run from signed/deployed Git checkout'; exit 22;
}
[[ -d /srv/fresh-website/releases && -d /srv/fresh-website/control/state ]] || {
  echo 'REFUSE: production release layout not found'; exit 23;
}
# Require clean read-only inventory before any daemon or deletion is enabled.
/usr/bin/python3 -m py_compile production/storage_gc.py
/usr/bin/python3 -m unittest production.test_storage_gc -q
/usr/bin/python3 production/storage_gc.py --json >/var/tmp/vps1-storage-gc-dry-run.json
/usr/bin/python3 - <<'PY'
import json
from pathlib import Path
result=json.loads(Path('/var/tmp/vps1-storage-gc-dry-run.json').read_text())
if result['inventory'].get('blocked'):
    raise SystemExit('REFUSE: read-only inventory failed')
if result['inventory']['retention_hours'] != 12:
    raise SystemExit('REFUSE: age guard missing')
print('DRY_RUN_PASS candidates=',len(result['inventory']['candidates']))
PY

install -d -m 0755 /usr/local/libexec
install -d -m 0750 /var/lib/vps-storage-manager
install -m 0755 production/storage_gc.py /usr/local/libexec/vps1-storage-gc.py
install -m 0644 infrastructure/systemd/vps1-storage-deployment-gc.service /etc/systemd/system/vps1-storage-deployment-gc.service
install -m 0644 infrastructure/systemd/vps1-storage-deployment-gc.timer /etc/systemd/system/vps1-storage-deployment-gc.timer
systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/vps1-storage-deployment-gc.service /etc/systemd/system/vps1-storage-deployment-gc.timer
systemctl enable --now vps1-storage-deployment-gc.timer
systemctl is-enabled --quiet vps1-storage-deployment-gc.timer
systemctl is-active --quiet vps1-storage-deployment-gc.timer
echo 'VPS1_STORAGE_DEPLOYMENT_GC_TIMER=ACTIVE'
echo 'USER_MEDIA=UNCHANGED'
echo 'AUTO_SCRIPT=OFFLINE'
