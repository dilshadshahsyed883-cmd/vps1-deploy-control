# Song Studio folder workflow — coordinated release intent

Includes the Group-only Song Uploader work from PR #473, reconciled with the
current Short Uploader changes. Existing individual profiles, queues, records,
OAuth, manual Song Maker and Drive workflows remain intact.

Song Maker owns one persistent account-scoped folder registry. UUID folder IDs
and server-derived owner identities are snapshotted into download jobs, rendering
jobs, profile mappings and upload-source receipts. Audio Downloader requires a
folder confirmation before auto-queue admission; source Drive upload stays off.
The existing multipart handoff streams original Video-ID-matched media. The
existing FFmpeg worker renders full audio duration with saved export/overlay
settings, copies the original JPEG unchanged and atomically publishes the pair.

Song Uploader can select Google Drive, manual Song Maker local import, or Song
Maker VPS Folder, independently of Sheets / Manual on Website metadata. Folder
scans import only completed pairs from the selected owner/folder. Existing group
schedules, quotas, eligibility and durable duplicate guards remain authoritative.

The canonical registry reserves dependent consumers before transfer. Cleanup
runs only after independent YouTube verification, required thumbnail success and
persisted upload history. Pending dependencies retain originals; consumption
receipts are committed before idempotent file cleanup. No public route exposes
the internal destructive receipt endpoint. In-use rendered jobs cannot be
manually deleted through Song Maker. Raw downloader originals retain their
existing retention policy; consumed rendered pairs and Song Maker input copies
are cleaned after verification.

Deploy through exactly one host-locked Tailscale invocation:
[vps1-song-studio-release] [vps1-song-uploader-only] [vps1-song-folder-flow]

The coordinator preflights all queues, upgrades Song Maker, then LUM, then Audio
Downloader admission last. Existing app installers keep backups, idle fences,
health checks and rollback. The Audio installer overlays only folder admission,
account access and handoff definitions; it proves all other installed engine
functions remain bytecode-equivalent in AST. It never upgrades yt-dlp, changes
proxy settings, enables Auto Script, or publishes to YouTube during verification.
On failure, rollback runs in reverse order with idle/identity guards and preserves
all databases and media if active work prevents a safe rollback.

Tests include real FFmpeg, actual multipart transfer into the existing render
API, all four prior group source combinations, new folder auto-scan, exact JPEG
hashes, failed render retention, owner/path/symlink rejection, persistent folder
preferences, duplicate prevention and simulated verified/dependent cleanup.
Protected CI and authenticated readback must pass before declaring the release
live. Real production YouTube publishing and owner UAT are not run by tests.
