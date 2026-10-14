#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# Run only as the non-root pilot account. Never inspect or mutate production apps.
[[ "$(id -un)" == "vps1deploy" ]] || { echo "BLOCKED: unexpected SSH account"; exit 1; }
[[ "$(id -u)" -ne 0 ]] || { echo "BLOCKED: root account"; exit 1; }

root="$HOME/vps1-ci-pilot"
[[ -d "$root" && ! -L "$root" ]] || { echo "BLOCKED: pilot root missing/unsafe"; exit 1; }
archive="$root/incoming/pilot-site.tar.gz"
manifest="$root/incoming/release.json"
[[ -f "$archive" && ! -L "$archive" && -f "$manifest" && ! -L "$manifest" ]] || { echo "BLOCKED: missing artifact"; exit 1; }

digest="$(python3 - "$manifest" <<'PY'
import json, sys
p=sys.argv[1]
with open(p,encoding="utf-8") as f: d=json.load(f)
assert set(d)=={"app_id","artifact","schema_version","sha256"}
assert d["app_id"]=="pilot-canary" and d["artifact"]=="pilot-site.tar.gz" and d["schema_version"]==1
s=d["sha256"]
assert isinstance(s,str) and len(s)==64 and all(c in "0123456789abcdef" for c in s)
print(s)
PY
)"
actual="$(sha256sum "$archive" | cut -d' ' -f1)"
[[ "$actual" == "$digest" ]] || { echo "BLOCKED: archive SHA256 mismatch"; exit 1; }
members="$(tar -tzf "$archive")"
[[ "$members" == "pilot/index.html" ]] || { echo "BLOCKED: unsupported tar contents"; exit 1; }

mkdir -p "$root/releases"
[[ ! -e "$root/current" || -L "$root/current" ]] || { echo "BLOCKED: current is not a symlink"; exit 1; }

stage="$(mktemp -d "$root/.stage.XXXXXXXX")"
old_target=""
current_switched=0
atomic_link() {
  local target="$1"
  local tmp="$root/.next.$$"
  [[ "$target" == releases/* && "$target" != *..* ]] || return 1
  [[ -f "$root/$target/index.html" ]] || return 1
  ln -s "$target" "$tmp"
  mv -Tf "$tmp" "$root/current"
}
recover() {
  local code="$?"
  if [[ "$code" -ne 0 && "$current_switched" -eq 1 && -n "$old_target" ]]; then
    atomic_link "$old_target" || true
    echo "AUTO_ROLLBACK_ATTEMPTED=YES"
  fi
  rm -rf -- "$stage"
}
trap recover EXIT

# A brand new, isolated account gets a synthetic baseline release for rollback testing.
if [[ ! -L "$root/current" ]]; then
  mkdir -p "$root/releases/baseline"
  printf '%s\n' '<!doctype html><title>Canary baseline</title><h1>BASELINE</h1>' > "$root/releases/baseline/index.html"
  atomic_link "releases/baseline"
fi
old_target="$(readlink "$root/current")"
[[ "$old_target" == releases/* && "$old_target" != *..* && -f "$root/$old_target/index.html" ]] || {
  echo "BLOCKED: existing current target invalid"; exit 1;
}

tar -xzf "$archive" -C "$stage" --no-same-owner --no-same-permissions
[[ -f "$stage/pilot/index.html" && ! -L "$stage/pilot/index.html" ]] || { echo "BLOCKED: pilot index missing"; exit 1; }
grep -Fq 'id="pilot-test"' "$stage/pilot/index.html"
grep -Fq 'Pilot UI PASS' "$stage/pilot/index.html"
release="releases/pilot-$(printf '%s' "$digest" | cut -c1-20)"
if [[ -d "$root/$release" ]]; then
  cmp -s "$stage/pilot/index.html" "$root/$release/index.html" || { echo "BLOCKED: release collision"; exit 1; }
else
  mv -- "$stage/pilot" "$root/$release"
fi
echo "RELEASE_SHA256_VERIFIED=PASS"
echo "ISOLATED_RELEASE_STAGED=PASS"

# Simulate a broken switch, restore prior known-good release, then promote the tested one.
atomic_link "$release"
current_switched=1
[[ "$(readlink "$root/current")" == "$release" ]]
echo "SYNTHETIC_FAILURE_INJECTED=YES"
atomic_link "$old_target"
[[ "$(readlink "$root/current")" == "$old_target" ]]
cmp -s "$root/current/index.html" "$root/$old_target/index.html"
echo "ROLLBACK_AFTER_SYNTHETIC_FAILURE=PASS"

atomic_link "$release"
cmp -s "$root/current/index.html" "$root/$release/index.html"
grep -Fq 'Pilot UI PASS' "$root/current/index.html"
current_switched=0
echo "FINAL_RELEASE_ACTIVATED=PASS"
echo "PILOT_PATH=$root/current/index.html"
echo "PRODUCTION_APPS_MODIFIED=NO"
echo "VPS1_ISOLATED_SSH_DEPLOYMENT=PASS"
