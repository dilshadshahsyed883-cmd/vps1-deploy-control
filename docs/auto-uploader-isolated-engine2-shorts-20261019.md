# Auto Uploader — Engine 2 and Shorts isolated app release (19 October 2026)

Owner scope: work only on app and engines. Never migrate, reset, import, delete or modify existing upload history, OAuth, channels, Google Sheets, schedules, old queues or existing media.

- Existing four-mode UI preserved. Engine 1 and Short Stream are not replaced.
- Engine 2 app runs only on loopback 127.0.0.1:8016; Shorts app on 127.0.0.1:8017. Both reuse real LUM FastAPI and Auto Engine V6 code.
- Existing web shell embeds each engine app through a protected same-origin reverse proxy with server-derived identity headers.
- Each new mode uses a separate new root: lum2 or lum3. No existing data roots, tokens or histories are imported.
- New engine defaults are disarmed; worker processes may be healthy but will not initiate uploads or scheduled jobs without configuration.
- Unit installation refuses any existing unowned/nonempty destination root. Failed releases use existing app rollback and leave existing data alone.
- New scoped owner-only GitHub→Tailscale workflow runs solely the existing allowlisted local-upload-manager install operation using marker [vps1-lum-engines].

Source CI: production.test_uploader_four_modes, production.test_uploader_mode_runtime, production.test_uploader_mode_gateway, production.test_uploader_mode_services and production runtime smoke. CI success is not proof of live VPS install.

Final live evidence: app-scoped release PASS with both engine-specific loopback HTTP 200/identity readbacks, existing 8015 root UI working and original Scene shell intact. No YouTube publish triggered for verification.

Dormant PR #91 history coordinator stays inactive/out-of-scope. Auto Script remains OFFLINE.
