# Historical fresh VPS rebuild — 10 migration targets, Auto Script excluded (current runtime ONLINE)

The application registry retains eleven historical names, but **only ten are deployment targets**.
**Auto Script was excluded from this legacy fresh-rebuild workflow by the earlier owner instruction**: do not install, start, schedule, route, or re-create it **through this rebuild flow**. This is not the current runtime status: Auto Script is already ONLINE per owner on 29 Oct 2026 after the scoped activation (PR #314). Preserve existing production Auto Script services, scheduler, jobs, SQLite, auth and credentials. The rebuild exclusion protects against accidental replacement.

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

Auto Script must remain excluded from this legacy fresh-rebuild deployment unless separately authorized. Its independently existing runtime is ONLINE per owner on 29 Oct 2026.
