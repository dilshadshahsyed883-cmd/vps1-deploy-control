# Voice UI guarded release following PR #595

Source: protected-main PR #595, new global all-user read-only Audio Downloader idle gate.

Target: only `voice-studio`, using the existing Song Studio `song-audio-selector-only` release selector. Avoid combined or conflicting release selectors. Do not restart independent synthesis workers. Do not edit, migrate, reset or delete Audio Downloader jobs, credentials, owner data or media.

A busy, retrying or unreadable Audio Downloader queue MUST block the release. Run only existing Tailscale/SSH production installer, SHA pinning, health/authenticated HTTPS readback and rollback. Keep Google Drive shared and Auto Script OFFLINE. Verify admin/non-admin shared-proxy visibility before declaring full completion.
