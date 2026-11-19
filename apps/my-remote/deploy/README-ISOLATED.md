# Standalone Linux relay deployment on an isolated host

**This is an operator runbook, not evidence of a live deployment.**
The Linux artifact is self-contained: no Docker or system-wide .NET runtime required.
Do NOT install on existing VPS_1 without full resource/rollback checks.

## Required before install
- Dedicated owned Linux x64 server, suitable free disk (>5 GB plus deployment/rollback reserve), root/operator access.
- DNS A/AAAA for a dedicated subdomain pointing at that host. Verify authoritative resolution; do not assume remote.scenebuilder.online already exists.
- Dedicated TLS certificate obtained using the authorized DNS/provider route (e.g. certbot).
- Current target service/port status, existing proxy ownership, rollback strategy, and confirmed source SHA.
- Two distinct random secrets: browser login password (>=20 chars) and agent token (>=32 chars). Generate on server, never in GitHub or logs.
- HTTPS ingress to Nginx only; bind application to loopback port 5123.

## Deployment sequence (isolated host only)
1. Download the successful CI artifact `MyRemote-Linux-x64`, verify checksums against GitHub Actions logs, and stage it at `/opt/my-remote/releases/<SHA>`.
2. Create a locked-down `myremote` system user; set binary/read-only release directory permissions and a stable `/opt/my-remote/current` symlink. Save the *previous* symlink target for rollback.
3. Create `/etc/my-remote/relay.env` owned by root with mode 0600; supply `REMOTE_BROWSER_PASSWORD`, `REMOTE_AGENT_TOKEN`, `REMOTE_PUBLIC_ORIGIN=https://<DNS-HOST>` and `ASPNETCORE_URLS=http://127.0.0.1:5123`. Do not log values.
4. Install `my-remote.service.template` as `my-remote.service` after reviewing paths, account and resource limits; reload systemd, enable/start.
5. Verify `curl http://127.0.0.1:5123/healthz`, anonymous `/api/status` -> 401, and no public listener on 5123.
6. Install Nginx virtual-host config **only after the TLS cert is valid**; test `nginx -t` before reload. Do not replace or edit existing unrelated sites.
7. Verify HTTPS health, anonymous denial, wrong password rejection, correct login, WSS upgrade, single viewer/agent and all existing production routes remaining healthy.
8. If any check fails, restore the last symlink/service/proxy configuration; remove only the new isolated configuration and restart only the affected service.

The internet URL is unusable until DNS and TLS are correctly configured. Do not use raw-IP HTTP for control.

## Windows + Android
- Windows: download `MyRemote-Windows-x64`, double-click `Start-MyRemote.cmd` and pair with HTTPS URL + private agent token.
- Android: open the HTTPS origin in Chrome and log in with the separate browser password.
- Real end-to-end test on user's devices is required before claiming the app is working.

## Current known limitations
Initial relay uses JPEG at ~4 FPS rather than WebRTC, no multi-screen, no native APK, no Windows lock/UAC handling, no file transfers, and no end-to-end encryption beyond TLS/WSS transport.
