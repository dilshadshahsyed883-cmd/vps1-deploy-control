# Song Maker PC upload folder selection

Use the canonical Song Maker folder registry for PC single and batch uploads.
The Input page adds one shared VPS output folder dropdown, Create Folder and
Refresh controls. Selection persists per account in an additive intake record;
refreshing the registry does not change it. Empty selection preserves the saved
legacy delivery mode. A selected folder pins local Song Uploader delivery and
skips Drive for that project without changing saved export or Drive settings.

Both intake paths reuse the existing multipart /jobs endpoint. Batch destinations
are snapshotted per pair and retained across retries. Existing downloader JPEG
matching/retention rules, rendering, overlays, queues and verified folder cleanup
remain unchanged. No other Studio source or application is deployed.

Validation: 60 Song Maker media/API tests PASS, including four new PC folder
regressions; 11 UI/deployment governance tests PASS. Real Chromium at 360, 390,
768, 1024 and 1440 px verifies folder creation, selection persistence after
reload, manual render/download, batch folder mapping and duplicate safety.
JavaScript syntax and diff checks PASS; no real YouTube publishing.

Release through the existing serialized Tailscale Song Maker-only lane:
[vps1-song-studio-release] [vps1-song-maker-only] [vps1-song-maker-pc-folders]
Retain idle/submission fences, backups, health checks and rollback. Authenticated
production readback must confirm folder controls and selection/registry APIs
before the installer records success. Auto Script stays OFFLINE.
