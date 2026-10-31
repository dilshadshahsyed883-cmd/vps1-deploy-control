# PR #587 — guarded production activation

Source: protected-main merged PR #587 (commit `7fd860277a9040c4c76b27087bc2b6c2e49dc1d8`).

Scope: server-rendered shared-control visibility only for Song/Short/Copyright Uploader and Audio Downloader (Scene/Voice views). This intent does not authorize data migration, queue reset, OAuth replacement, media changes, or new system creation.

Release lanes (existing workflows only):
- Song Studio settings-fix scoped release: local-upload-manager and scene-builder, preserving workers and active jobs.
- Copyright Studio isolated UI-only release: copyright-uploader only.
- Audio Downloader proxy-only release: voice-studio path with existing safety and readback contract.

All releases must use the existing Tailscale deployment identity, SHA pinning, VPS host lock, idle checks, health/auth smoke tests and rollback. Treat a busy uploader as a safe block, not permission to restart it. Keep Auto Script OFFLINE and Google Drive shared/unchanged. Final live admin/non-admin smoke test required before declaring production verified.
