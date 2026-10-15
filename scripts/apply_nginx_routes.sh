#!/usr/bin/env bash
# Guarded, agent-operated only. NEVER invoked by GitHub Actions.
set -Eeuo pipefail
umask 077

usage() {
  echo "Usage: bash scripts/apply_nginx_routes.sh --check|--apply PATH_TO_GENERATED_CONF" >&2
  exit 2
}
[[ $# -eq 2 ]] || usage
mode="$1"
candidate="$2"
[[ "$mode" == "--check" || "$mode" == "--apply" ]] || usage
[[ -f "$candidate" && ! -L "$candidate" ]] || { echo "BLOCKED: candidate absent or symlink"; exit 1; }
grep -Fxq '# managed-by: vps1-deploy-control; renderer: v1' "$candidate" || { echo "BLOCKED: not generated config"; exit 1; }
if [[ "$mode" == "--check" ]]; then
  echo "NGINX_FRAGMENT_MARKER_CHECK=PASS"
  echo "NO_VPS_CHANGES=YES"
  exit 0
fi

# No production routing may be changed until canonical Drive Bridge agent approval,
# live-site inventory and exact server-context include are independently verified.
[[ "$(id -u)" -eq 0 ]] || { echo "BLOCKED: privileged agent required"; exit 1; }
[[ "$(printenv VPS1_CONFIG_CHANGE_APPROVED 2>/dev/null || true)" == "yes" ]] || { echo "BLOCKED: no explicit config change approval"; exit 1; }
[[ "$(printenv VPS1_AGENT_EXECUTION 2>/dev/null || true)" == "DriveBridge" ]] || { echo "BLOCKED: Drive Bridge-only production mutation"; exit 1; }
grep -q '^location \^~ /' "$candidate" || { echo "BLOCKED: refusing empty route manifest"; exit 1; }
command -v nginx >/dev/null && command -v systemctl >/dev/null || { echo "BLOCKED: nginx/systemd unavailable"; exit 1; }

target_dir="/etc/nginx/vps1-managed"
target="$target_dir/routes.conf"
backup_dir="/var/lib/vps1-deploy-control/nginx-backups"
[[ -d "$target_dir" && ! -L "$target_dir" ]] || { echo "BLOCKED: managed include directory not bootstrapped"; exit 1; }
[[ ! -L "$target" ]] || { echo "BLOCKED: symlink destination"; exit 1; }
if [[ -e "$target" ]]; then
  grep -Fxq '# managed-by: vps1-deploy-control; renderer: v1' "$target" || { echo "BLOCKED: refusing to overwrite unmanaged config"; exit 1; }
fi
nginx -t >/dev/null 2>&1 || { echo "BLOCKED: existing Nginx config is unhealthy"; exit 1; }
nginx -T 2>/dev/null | grep -F 'include /etc/nginx/vps1-managed/*.conf;' >/dev/null || { echo "BLOCKED: managed include not verified"; exit 1; }

mkdir -p "$backup_dir"
chmod 700 "$backup_dir"
backup="$(mktemp "$backup_dir/routes.XXXXXXXX.bak")"
prior=0
if [[ -e "$target" ]]; then
  cp -p -- "$target" "$backup"
  prior=1
else
  rm -f -- "$backup"
fi

switched=0
restore_on_error() {
  rc="$?"
  trap - ERR
  if [[ "$switched" -eq 1 ]]; then
    if [[ "$prior" -eq 1 ]]; then
      cp -p -- "$backup" "$target" || true
    else
      rm -f -- "$target" || true
    fi
    nginx -t >/dev/null 2>&1 && systemctl reload nginx >/dev/null 2>&1 || true
    echo "NGINX_ROLLBACK_ATTEMPTED=YES" >&2
  fi
  exit "$rc"
}
trap restore_on_error ERR
staged="$(mktemp "$target_dir/.routes.XXXXXXXX")"
trap 'rm -f -- "$staged"' EXIT
install -m 0644 -- "$candidate" "$staged"
mv -f -- "$staged" "$target"
switched=1
nginx -t >/dev/null 2>&1
systemctl reload nginx >/dev/null 2>&1
switched=0
echo "NGINX_CONFIG_SYNTAX=PASS"
echo "NGINX_RELOAD=PASS"
echo "AGENT_MUST_VERIFY_HTTP_ROUTES=YES"
echo "BACKUP_PATH=$backup"
