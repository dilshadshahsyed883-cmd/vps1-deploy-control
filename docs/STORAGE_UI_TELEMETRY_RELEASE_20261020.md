# Storage Manager UI telemetry release — 2026-10-20

- Source PR #180 merged, checks 34 successful/2 non-applicable.
- Root producer via GC stage-1 run #39420340590 SUCCESS; verified redacted record last_cleanup deleted=7 and freed=409755648 bytes, current safe inventory 0.
- This stage deploys only Storage Manager isolated backend and Scene Builder Storage UI through existing protected GitHub/Tailscale production lane.
- Re-check candidate health, safe snapshot, seven app gateways, backups and rollback.
- No action to delete app media or Drive data; Auto Script stays OFFLINE.
