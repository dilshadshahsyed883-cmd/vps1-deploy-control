# Fresh VPS rebuild — 10 active applications, Auto Script OFFLINE

The application registry retains eleven historical names, but **only ten are deployment targets**.
**Auto Script is intentionally OFFLINE by user request**: do not install, start, schedule,
route, test as a functional gate, or include it in cutover. Its existing sanitized source
is preserved in `apps/auto-script/` for future separately authorized work. Do not delete
or change any currently running Auto Script production service as part of this GitHub-only work.

The existing VPS_1 production must remain unchanged while the OVH Cloud snapshot is
being saved. No reboot, production deployment, database import, or cutover is authorized.

The existing private GitHub PR #7 controls the other ten app source groups.
The safe build lane uses `rebuild/builder.py` and `rebuild/fresh_vps.py`:
`python3 rebuild/builder.py plan`, `python3 rebuild/builder.py state`,
`python3 -m rebuild.fresh_vps assemble --root /tmp/fresh-rehearsal --sha <40-character-git-sha>`,
and `python3 -m rebuild.fresh_vps verify --root /tmp/fresh-rehearsal`.
Only a directory strictly under /tmp is accepted in this lane.

GitHub CI is not proof that all ten independent web/worker stacks run end-to-end on VPS_1.
The existing Tailscale/SSH isolated pilot is separate from production authorization.
Use the per-app source artifact workflow for independent tar.gz + SHA256 manifests;
artifact integrity checks do not imply external services are configured.

## Before a future post-snapshot VPS_1 rebuild

1. Verify the OVH snapshot completed, identify its snapshot ID and check restore feasibility.
2. Confirm a fresh recovery/rollback boundary for current production, credentials and
   non-reproducible uploads/databases. Historical August master packages are not current-state backups.
3. Obtain separate explicit authorization for reboot, installation, routing and production cutover.
4. Run an isolated ten-app install and verify process startups, correct environment variables,
   independent SQLite/queues, worker media jobs, auth boundaries, and per-app rollback.
5. Defer external OAuth, YouTube publishing and other unavailable credentials without calling
   the affected external integration PASS.

Auto Script must remain OFFLINE unless the user explicitly changes its deployment state.
