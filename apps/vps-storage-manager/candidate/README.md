# Storage Manager source-only shadow candidate

- Proposed port 127.0.0.1:8149, different from old 8799. Requires new port-owner check.
- Non-root vps1storage, isolated /var/lib/fresh-website/vps-storage-manager state; no legacy volume access.
- GET /health public on loopback, GET /v1/policy needs X-Storage-Token, every POST is blocked.
- Scheduler remains off; no cleanup, no systemd service start or installation authorized.
- Supply private token in mode 0600 environment file. Tab 3 reviews unit and rollback before use.
