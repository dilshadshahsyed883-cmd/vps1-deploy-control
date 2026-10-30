# VPS_1 Song Studio — Audio Downloader app-scoped release intent

- VPS_1 / Agent 1 only. Production queues, files and credentials must remain intact.
- Source baseline protected main `098670d2459e64fbec66b3282d10787811735d9f` (Audio Downloader source PR #406; Song Maker #405; Studio navigation #399).
- Single production selection: `[vps1-song-studio-release] [vps1-song-audio-selector-only]`.
- Guarded Tailscale workflow must call only approved `voice-studio` app installer with host-wide serialization, idle admission, backups, authenticated route health, and rollback. No combined four-app Song Studio selector.
- Do not force if active/queued jobs, install helper drift, worker health, authorization, backup or immutable SHA check blocks.
- Verify `/audio-downloader/` authenticated and anonymous access, queue progress, 60s upload pacing, 180s retry and duplicate prevention with authorized QA inputs. Report any external Drive/YouTube provider uncertainty honestly. No live publishing as part of acceptance.
- Never equate a source merge with successful production release; record exact workflow result separately.
