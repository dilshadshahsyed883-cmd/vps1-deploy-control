# Song Studio Audio Downloader — independent 8152 guarded activation

## Live prerequisite proved
Protected Tailscale release run 41474246433, merge f3156fc3c267e6f03109948286beb43c9b969e1c:
- VOICE_RECOVERY=PASS, Voice 8143 HEALTHY, authenticated Scene SSO PASS
- Anonymous access DENIED, original SQLite and media PRESERVED
- Voice-only recovery rollback journal recorded, independent Song 8152 NOT_STARTED
- AUTO_SCRIPT=OFFLINE

## Exact cutover authorization
On a protected-main merge by the approved actor, use only:
- [vps1-song-studio-release]
- [vps1-song-audio-separate]

No other Song Studio release selectors are authorized in that merge commit.

The existing serialized Tailscale root transaction production.song_audio_cutover.install must preserve all-owner Voice synthesis and downloader idle preflight, retain read-only proof of one historical inert orphan without changing it, and require every genuine Song Maker handoff naturally complete. Stage immutable Song/Voice/Scene source; back up and integrity-check original SQLite without modifying the source database. Stop the legacy worker, independently recheck no new jobs, start Voice synthesis-only with verified split=1, then start exactly ONE Song worker on loopback 8152. Check independent authenticated API/health, flip Scene routing LAST, verify a real Scene admin session and anonymous denial, and preserve SQLite/media/Drive originals and Auto Script OFFLINE.

Fail closed on any busy user job, unknown missing module, data drift, duplicate worker, storage failure, health/SSO failure, or rollback inability; record exact redacted evidence. No manual force, deletion, spoofed session or security-gate bypass.

## Acceptance
- GitHub protected release run SUCCESS and receipt song_audio_cutover=PASS
- Voice 8143 healthy synthesis-only
- Song 8152 healthy sole downloader worker and correct canonical original DB
- Scene Song-only route 8152; Voice routes remain 8143
- Song Maker handoff history and originals unchanged
- Admin/non-admin real-account acceptance separately recorded when credentials are available
- Repository PUBLIC and frozen Voice V1/V1.1 unchanged.
