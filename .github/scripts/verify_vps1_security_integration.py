#!/usr/bin/env python3
"""Offline fail-closed GitHub workflow and migration security gate."""
import json
import re
from pathlib import Path

root = Path('.github/workflows')
legacy = {'vps1-audio-404-probe.yml':'probe-audio-only', 'vps1-connection-check.yml':'check-vps', 'vps1-pilot-deploy.yml':'isolated-deploy'}
staging = {'fresh-vps-post-snapshot-stage.yml':'stage-only', 'fresh-vps-source-of-truth.yml':'auto-stage', 'vps1-staging-drift.yml':'audit'}
proof_name = 'vps1-main-ssh-proof-once.yml'
pin = 'SHA256:oeX4vWPvVspKOTrvbMgUa+uWPGyXLz3531TQEy5wp1E'
obsolete = 'SHA256:pHVIA8i/zuowqpDCaGbKc4bWGNcp/DRziKemWZS0ZdQ'
credentials = ('secrets.TS_OAUTH_CLIENT_ID','secrets.TS_AUDIENCE','secrets.VPS1_SSH_PRIVATE_KEY')

def job(text, name):
    m = re.search(r'(?m)^  ' + re.escape(name) + r':\s*$', text)
    assert m, 'missing job: ' + name
    tail = text[m.end():]
    end = re.search(r'(?m)^  [\w-]+:\s*$', tail)
    return tail[:end.start()] if end else tail

def disabled(block, label):
    m = re.search(r'(?m)^    if:\s*(.*)$', block)
    assert m, 'missing if for ' + label
    val = m.group(1).strip()
    if val in ('>-','|','>'):
        val = next((s.strip() for s in block[m.end():].splitlines() if s.strip()), '')
    assert val.startswith('false &&') or val.startswith('${{ false &&'), 'remote job enabled: ' + label

proof = (root/proof_name).read_text()
assert 'workflow_dispatch:' not in proof.split('\njobs:',1)[0]
assert 'branches: [main]' in proof.split('\njobs:',1)[0]
assert pin in proof and obsolete not in proof
pj = job(proof, 'main-readonly-ssh')
assert 'environment: production' in pj
disabled(pj, proof_name)
# Fail closed until a separate explicitly authorized commit pins a real prior-main SHA.
# This draft preparation MUST NOT create an executable job.
guard = re.search(r"(?m)^    if: >-\n((?:      .*\n)+)", pj)
assert guard, "SSH proof job guard missing"
guard_lines = [line.strip() for line in guard.group(1).splitlines()]
assert guard_lines == [
    "false &&",
    "github.event_name == 'push' &&",
    "github.ref == 'refs/heads/main' &&",
    "github.run_attempt == 1 &&",
    "github.event.before == 'PENDING_SEPARATE_AUTHORIZATION' &&",
    "contains(github.event.head_commit.message, '[vps1-ssh-proof-authorized]')",
], "SSH proof conditions changed or accidentally enabled"
preflight_name = "- name: Reject replay or unexpected ref before OIDC"
join_action = "uses: tailscale/github-action@v4"
assert pj.count(preflight_name) == 1
assert pj.index(preflight_name) < pj.index(join_action), "OIDC starts before preflight"
preflight = pj[pj.index(preflight_name):pj.index(join_action)]
for check in ('test "$GITHUB_RUN_ATTEMPT" = "1"', 'test "$GITHUB_EVENT_NAME" = "push"', 'test "$GITHUB_REF" = "refs/heads/main"'):
    assert check in preflight, "missing fail-closed preflight: " + check
for term in ("github.event_name == 'push'", "github.ref == 'refs/heads/main'", '[vps1-ssh-proof-authorized]', "expected='" + pin + "'", 'StrictHostKeyChecking=yes','UpdateHostKeys=no','vps1deploy@100.87.55.92','VPS1_DEPLOYMENT=NOT_ATTEMPTED'):
    assert term in pj, term
for file,name in legacy.items():
    part = job((root/file).read_text(),name)
    assert 'environment: production' in part, file
    disabled(part,file)
for file,name in staging.items():
    part = job((root/file).read_text(),name)
    assert 'environment: staging' in part, file
    disabled(part,file)
    assert all(s in part for s in credentials),file
found = {f.name for f in root.glob('*.yml') if any(c in f.read_text() for c in credentials)}
expected = set(legacy)|set(staging)|{proof_name}
assert found == expected, 'unreviewed credential consumers: '+repr(found ^ expected)

# Fresh VPS identity verified by Agent 1 through Drive Bridge on 2026-10-18.
fresh_tailnet_ip = '100.87.55.92'
fresh_host_public_key = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGpN+j9QsIP11NqypSCoYdFZ0Bvy65j608M4EyjC+OlJ'
retired_tailnet_ip = '100.113.253.99'
for fname in sorted(set(legacy) | set(staging) | {proof_name}):
    source = (root/fname).read_text()
    assert retired_tailnet_ip not in source, 'old Tailscale IP remains: '+fname
    assert fresh_tailnet_ip in source, 'fresh Tailscale target missing: '+fname
    if fname != 'vps1-connection-check.yml':
        assert fresh_tailnet_ip + ' ' + fresh_host_public_key in source, 'verified SSH host pin missing: '+fname
assert proof.count('false &&') >= 1, 'SSH proof must remain fail-closed'
groups=json.loads(Path('rebuild/groups.json').read_text())
assert len(groups['groups']) == 11 and len({g['id'] for g in groups['groups']}) == 11
assert [g['id'] for g in groups['groups'] if g.get('deployment_mode')=='offline']==['auto-script']
assert len([g for g in groups['groups'] if g.get('deployment_mode')!='offline'])==10
for f in ('production_deploy_enabled','fresh_install_enabled','automatic_database_migrations','legacy_database_import','legacy_queue_import','legacy_media_import'):
    assert groups[f] is False, f
print('PROTECTED_FOUR_WORKFLOWS=PASS')
print('CORRECTED_SSH_PIN_AND_MAIN_ONLY=PASS')
print('THREE_LEGACY_GUARDS=PASS')
print('SEVEN_CREDENTIAL_CONSUMERS_STATICALLY_DISABLED=PASS')
print('SSH_PROOF_REPLAY_AND_PRIOR_SHA_HOLDS=PASS')
print('NO_REPOSITORY_SECRET_RUNTIME_FALLBACK=PASS_BY_FAIL_CLOSED_JOBS')
print('ELEVEN_GROUPS_AUTO_SCRIPT_OFFLINE=PASS')
