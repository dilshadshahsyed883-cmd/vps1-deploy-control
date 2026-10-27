# Scene Builder scoped protected-main release intent — 2026-10-27

Release reason: apply worker SQLite FD lifecycle correction from PR #357 to the owned Scene production worker, resolving incident 3327eb5e931e417f9daf5bca9af526da at final FFprobe validation.

Safety: select only scene-builder, not all; use existing Tailscale OIDC production environment with pinned host key and exact current protected-main SHA. Respect the worker's _require_idle() fail-closed guard, durable SQLite queue, source-refresh rollback. Do not force a restart of active render or delete any project data.

Required postrelease evidence: worker source revision equals selected main SHA; authenticated health; real FFmpeg canaries PASS; incident job is resumed from checkpoints only after safe worker recovery; validate final MP4, verified Drive delivery, local cleanup after upload. Auto Script stays OFFLINE.

Existing read-only ownership check PASS: https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/actions/runs/40479128074
Code correction: PR #357.