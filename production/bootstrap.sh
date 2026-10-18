#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$(id -u)" == 0 ]]
[[ "$(hostname)" == vps-01a23801 ]]
revision="${1:?Exact approved Git SHA required}"
shift
[[ "$revision" =~ ^[0-9a-f]{40}$ ]]
export DEBIAN_FRONTEND=noninteractive
apt-get -o DPkg::Lock::Timeout=180 update -qq
apt-get -o DPkg::Lock::Timeout=180 install -y --no-install-recommends \
  ca-certificates git python3 python3-venv ffmpeg nginx apache2-utils espeak-ng libespeak-ng1 fonts-dejavu-core
checkout="/srv/fresh-website/control/releases/$revision"
if [[ ! -d "$checkout/.git" ]]; then
  install -d -m 755 "$checkout"
  git -C "$checkout" init -q
  git -C "$checkout" remote add origin https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control.git
  git -C "$checkout" fetch --depth=1 origin "$revision"
  git -C "$checkout" checkout --detach FETCH_HEAD
fi
[[ "$(git -C "$checkout" rev-parse HEAD)" == "$revision" ]]
cd "$checkout"
python3 -m production.runtime install --revision "$revision" "$@"
python3 - <<'PY'
import subprocess
from production.runtime import CATALOG, deployment_lock
from production.gateway import install
active = [app for app in CATALOG if subprocess.run(
    ['systemctl', 'is-active', '--quiet', 'vps1-production-' + app + '.service']).returncode == 0]
with deployment_lock():
    install(active)
PY
# The existing dedicated SSH account can invoke this one validated command.
# No sudo group, unrestricted shell or GitHub credential change is introduced.
id vps1deploy >/dev/null
install -m 0755 "$checkout/production/remote_deploy.py" /usr/local/sbin/vps1-production-deploy
sudoers_file="$(mktemp)"
trap 'rm -f "$sudoers_file"' EXIT
printf '%s\n' 'vps1deploy ALL=(root) NOPASSWD: /usr/local/sbin/vps1-production-deploy' > "$sudoers_file"
visudo -cf "$sudoers_file"
install -m 0440 "$sudoers_file" /etc/sudoers.d/vps1-production
echo 'VPS1_PRODUCTION_HELPER=INSTALLED'
