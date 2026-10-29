# Song Uploader V1 — guarded Local Upload Manager release intent

Date: 2026-10-29 PKT
Scope: VPS_1 / Song Studio / Song Uploader; depends on merged PR #366 and #367.
Source baseline: protected main with Song Uploader's independent loopback 8018 web/worker/scheduler.
Expected workflow on approved squash merge: [vps1-lum-engines]
Deployment target: existing local-upload-manager only (not all apps).
Preflight: installed web/worker status, immutable commit identity, all Engine 1/2/Shorts/Song uploader in-flight queues idle or fail closed.
Backup & rollback: existing app-scoped runtime transaction, SQLite backup, systemd unit snapshots, worker restoration.
Acceptance: exact 8015/8016/8017/8018 health, authenticated five-tab Song Uploader HTML via same-origin mode gateway, app-specific logs.
Publishing policy: new Song scheduler OFF and no OAuth migration or actual YouTube upload in this release.
Preserve: existing uploads, queues, channels, OAuth, users, original Auto Uploader, Scene Builder, Song Maker, and Auto Script.
Block on: any active or indeterminate upload, failing smoke test/CI, unavailable authorized Tailscale, protected environment or failed health probe.
Do not claim LIVE until observed protected workflow and authenticated HTTPS readback succeed.
