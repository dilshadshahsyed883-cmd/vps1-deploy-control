# Fresh VPS Builder — executable isolated implementation

The current builder consumes already-migrated source in existing private GitHub PR #7. No additional VPS source discovery or Drive source transfers are required.

## Tested commands on a clean checkout

    python3 -m unittest discover -s rebuild/tests -v
    python3 -m rebuild.fresh_vps plan
    python3 -m rebuild.fresh_vps assemble --root /tmp/fresh-vps-sandbox --sha "$(git rev-parse HEAD)"
    python3 -m rebuild.fresh_vps verify --root /tmp/fresh-vps-sandbox
    python3 -m pip install -r rebuild/runtime-smoke-requirements.txt
    python3 rebuild/smoke_services.py --root /tmp/fresh-vps-sandbox

All tests are automated in the independent GitHub Actions workflow fresh-vps-builder-ci.yml.

## What the builder really does

- Materializes 10 selected, sanitized groups into immutable revisioned releases.
- Uses atomic current/<app> symlinks and records the previous revision for rollback.
- Creates genuinely empty db, queue, uploads and work directories per app.
- Generates candidate systemd unit files, a disabled Nginx site and per-app environment templates.
- SHA256-verifies each staged source file and detects tampering or unauthorized service enablement.
- Starts the actual Short Stream and Short Maker FastAPI applications from the isolated new release tree and checks their health endpoints.
- Excludes Algrow-specific source paths, destructive reset/cleanup scripts and historical rollback source directories.

No systemctl, apt, Nginx reload, production database, DNS, external API, YouTube upload or VPS_1 production operation occurs.

## Important: what is not yet an authorized working live installation

The current milestone is a working isolated release assembler with two functional app smoke tests, NOT a verified 11-group clean-VPS deploy. Candidate systemd units remain disabled.

Unresolved runtime contracts that must be implemented and verified on a separately authorized clean Ubuntu host: Scene Builder's hard imports of Algrow Voice and separately owned Auto Script; independent ACS, LUM and Voice Studio workers; Video Merger's hardcoded old /opt path and fresh SQLite bootstrap; Chrome/Xvfb for Script Work; media worker dependencies, package locking, credentials, exact Nginx routing, integrations, and health/rollback at the systemd level. Auto Script remains parallel-owned and untouched.

Do not run generated units or Nginx candidates on VPS_1. Production auto-deploy, wipe and cutover remain disabled pending explicit authorization.
