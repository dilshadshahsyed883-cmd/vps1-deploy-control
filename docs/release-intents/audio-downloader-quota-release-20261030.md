# VPS_1 / Agent 1 — Audio Downloader Drive quota optimization release

Production intent only; NOT a live delivery receipt.

## Exact scope
Deploy the current protected-main Audio Downloader module via the existing thumbnail-only Voice overlay. This applies tested PR #389 duplicate Drive metadata stat removal while retaining PR #388's 60-second gap between verified audio uploads, upload-only retries every 180 seconds, thumbnail receipts, per-file remote size/MD5 verification, public link/anonymous byte verification, and local retention until verified.

## Safety
Use only `vps1-audio-thumbnail-release.yml` with both required release markers. Preserve installed Voice engine templates outside the reviewed Audio Downloader sections, existing proxy/session/OAuth, Kokoro/Edge workers, Song Maker and Song Uploader queues, current Drive contents and Auto Script.

The installer must acquire an idle queue fence, back up Audio Downloader SQLite, swap only its reviewed module and UI overlay, verify authenticated HTTPS and scoped smoke, then report `PASS_AUDIO_THUMBNAIL_ONLY`. Fail or roll back if any gate does not pass. No cleanup of preexisting Drive files and no synthetic upload into the shared folder. Tests alone are not live Drive quota evidence.

Do not run the full Song Studio release workflow or bypass host deployment locks.
