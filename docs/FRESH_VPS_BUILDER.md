# Fresh VPS Builder — ten active app targets

Auto Script is **OFFLINE**, excluded from all active release, route, worker, functional
readiness, deploy and cutover gates. Its source and dormant helpers are retained.

The isolated rootfs assembly from PR #7 installs ten managed groups and generates
candidate Nginx/systemd files but never enables live units or production routes.
The builder keeps ten independent per-app data, db, queue, uploads and work roots.
Per-app atomic pointers and offline rollback are tested in CI.

## Safe existing commands

```bash
python3 -m unittest discover -s rebuild/tests -v
python3 rebuild/builder.py state
python3 rebuild/builder.py plan
python3 -m rebuild.fresh_vps assemble --root /tmp/fresh-vps-sandbox --sha "$(git rev-parse HEAD)"
python3 -m rebuild.fresh_vps verify --root /tmp/fresh-vps-sandbox
```

Build independent app artifacts with the GitHub `fresh-vps-app-artifacts.yml`
workflow. Archived Auto Script source is not a deployable app.

## Remaining non-CI prerequisites

- Verify the completed OVH snapshot and tested current-state rollback/restore.
- Confirm exact runtime dependencies, systemd worker ownership, per-app health checks,
  application queues and media processing on the **post-snapshot** VPS.
- Configure secrets outside GitHub source; verify OAuth/external integrations separately.
- Require fresh explicit approval to reboot, deploy, switch routing or cut over.

A passing source/build CI or a local synthetic health endpoint never proves all ten
applications production ready. Existing production remains untouched before approval.
