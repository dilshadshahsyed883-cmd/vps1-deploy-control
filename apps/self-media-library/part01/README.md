# Self Media Library V1.2.0 — Drive-Safe Library Builder

Standalone web-controlled media-library builder. Google Drive is the durable backend; the VPS keeps the fast local index/cache and temporary source during processing.

## V1.2 safety workflow

1. Upload a source video.
2. When Drive mode is enabled, processing does not start until the app's isolated runtime Drive config and a real read/write preflight are available.
3. FFmpeg detects shots and creates short clips (2–4 seconds by default).
4. Automatic metadata runs when enabled; manual keyword editing always remains available.
5. Every clip uploads as MP4 + thumbnail + JSON metadata.
6. Each upload is verified by byte size and MD5 when the backend exposes it.
7. Drive Retry skips clips already verified and retries only pending/failed clip assets.
8. Before automatic source deletion, the backend performs a fresh all-clips verification gate from SQLite. Every clip must have verified state, verification time and expected Drive paths.
9. Only after that gate passes can the original source be automatically removed.
10. If Drive sync fails, the source is marked failed and retained. The UI shows Retry failed Drive and Manual cleanup.

## Isolated runtime Drive config

Self Media Library uses:

```text
/home/ubuntu/.config/self-media-library/rclone.conf
```

This must remain separate from the Master deployment bridge and every other app's Drive credentials.

Configure it with:

```bash
/home/ubuntu/apps/self_media_library/configure-drive.sh
```

Recommended remote name: `gdrive`.

Then use the web UI: Settings → Google Drive backend → Enable Drive → Save → Test Drive.

The Settings page shows the runtime config path, whether the selected remote exists, and the last read/write Drive test result.

## Drive layout

```text
Self Media Library/
├── clips/
├── thumbnails/
├── metadata/
└── _system/
```

## Failure behavior

If one or more clips fail Drive upload/verification:

- source status becomes Failed
- source file is retained
- already verified clips are not re-uploaded on Retry
- failed/pending clips can be retried
- Manual cleanup removes only the local original source
- generated clips and Drive objects remain untouched

Permanent clip deletion remains safety-first: Drive objects are removed before the local record is destroyed. If Drive deletion fails, local deletion aborts.

## Metadata behavior

SQLite is the fast index. Every clip also has a JSON sidecar on Drive containing permanent clip ID, source provenance/timestamps, description, AI state/confidence, quality, editable keywords, review/trash state, and Drive paths/verification state.

Keyword/status/trash/AI changes resync the JSON sidecar when metadata sync is enabled.

## Run standalone

```bash
cd self_media_library_v1
./install.sh
./configure-drive.sh
./start.sh
```

Default port: `8090`.

## V1.1 → V1.2 upgrade

Database migrations are additive. Existing clips, source history, keywords and Drive paths are preserved when deployment uses the existing shared data directory.
