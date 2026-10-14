#!/usr/bin/env bash
set -Eeuo pipefail
# Local/non-mutating preflight; no service/config changes, no secret content.
printf 'PREFLIGHT_MODE=READ_ONLY\n'
printf 'KERNEL=%s\n' "$(uname -s)"
for cmd in git python3 sha256sum ssh bash; do
  if command -v "$cmd" >/dev/null 2>&1; then echo "TOOL_${cmd}=PRESENT"; else echo "TOOL_${cmd}=ABSENT"; fi
done
if command -v tailscale >/dev/null 2>&1; then echo 'TAILSCALE_CLI=PRESENT'; else echo 'TAILSCALE_CLI=ABSENT'; fi
echo 'DEPLOYMENT_EXECUTED=NO'
