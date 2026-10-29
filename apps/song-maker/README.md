# Song Maker V1

Independent Video Studio app at `/song-maker/`. Source and release belong to
`dilshadshahsyed883-cmd/vps1-deploy-control`; VPS_1 / Agent 1. Auto Script OFFLINE.

One JPG/JPEG/PNG/WEBP plus one MP3/WAV/M4A becomes a static-image H.264/AAC MP4.
The filename comes from the audio basename, with Unicode retained and unsafe path
characters replaced. 1080p 16:9 default; 720p 16:9 and 1080p 9:16 available.
Fit contains the whole image; Blur adds a blurred background behind the same
uncropped foreground. No captions, effects, animation or separate compression pass.

## Runtime ownership

* Web: `songmaker.app:app`, loopback `127.0.0.1:8150`.
* Workers: `python -m songmaker.worker render|upload|sync`, separate processes.
* Exactly one process per lane holds a lifetime `flock`; SQLite transactional FIFO
  claims and version keys prevent duplicate jobs. Recovery occurs only after the
  old lane process has exited and released its lock. systemd kills that process's
  own cgroup on forced shutdown, including its FFmpeg child.
* `SONG_MAKER_DATA` defaults to `/var/lib/fresh-website/song-maker`.
* Dedicated `inputs`, `temp`, `outputs`, `staging`, `logs`, `state` directories;
  `state/jobs.sqlite3` holds WAL-persistent jobs, settings, sync snapshots and
  worker heartbeats. No existing application's DB or queue is opened by the app.
* Render progress and live FPS / processing speed come from FFmpeg progress output.
  Elapsed time is measured while rendering and ETA is derived only when FFmpeg
  reports speed. Upload remains stage-based (no fabricated byte percentages).
  ffprobe plus complete error-failing decode validate codecs/durations before download.

## Drive configuration and source ownership

Use the existing rclone config selected by server `RCLONE_CONFIG`. Default is
`/home/ubuntu/.config/masterctl/rclone.conf`. No credentials are copied into the
Song Maker DB, browser or repository; rclone remains responsible for token refresh.

New installations default to the existing `gdrive` remote and relative paths:
`Song Studio/Audio downloader` for **both** images and audio (matching Video ID
filenames) and `Song Studio/song maker` for rendered MP4s. An administrator may
change the remote or use legacy Drive folder IDs in the **Settings** tab.
The output folder must differ from the input folder; image/audio folders may be the
same. An explicit existing administrator configuration is never overwritten.
After FFmpeg finishes and MP4 validation succeeds, a Drive-synced pair from the default
shared input folder immediately moves its image and audio into
`Song Studio/Audio downloader/used` (before MP4 upload). The move is verified
against each original Drive file ID/size/hash, collision-safe and restartable.
For each archive decision Song Maker uses shared, fresh source/destination metadata snapshots;
any server-side move invalidates both snapshots and triggers a fresh read before receipts.
No cached result can replace a post-mutation verification.
Failed/cancelled renders do not move inputs. If archiving fails, the verified local
MP4 stays available while the job reports Needs Attention; retry resumes archive
without rerendering. Manual PC uploads and older custom-folder/ID jobs are
unchanged. The last administrator-saved destination
is the default for manual uploads. Any SSO-authorized Song Maker user can request Sync Now for the fixed approved
default Song Studio folders; only administrators can change Drive profiles or
folder IDs. Every scan and resulting job remains strictly owner-scoped.

Automatic scan runs every 300 seconds (manual Sync remains immediate), with a persistent metadata snapshot and
source-version job ledger. Matching image/audio folders reuse one metadata listing per scan. rclone exposes metadata listing rather than a Drive
change cursor here; unchanged pairs do not download again. Inputs download only
when their queued job runs, with uniqueness, ID, size/modtime/hash checks before
and after. Input versions use Drive IDs, checksums/modtime/size where exposed.
NFC/casefold basename matching supports Unicode and case-insensitive extensions.
Conflicts require explicit image/audio ID selection. Exact duplicate Drive names
remain blocked because filename-addressed rclone cannot safely disambiguate them;
rename the duplicate in Drive and sync again. Default Drive input files are
only moved after a successfully validated render, never on discovery or failure.

Upload reserves a noncolliding name `(2)`, `(3)`, etc. in SQLite before transfer,
uses `--ignore-existing`, then queries the exact output file (not the whole output folder) to verify
its remote ID, size and MD5.
A crash or upload-only retry retains the same reservation. A mismatching external
file becomes Needs Attention; it is never overwritten. Completed requires verified
receipt persistence. Local MP4 stays available for preview/download until explicit
Delete. After verified completion only job temp/staging and imported Drive copies
are cleaned; manual uploaded originals are retained until explicit project Delete.
Delete is blocked while any worker owns the job and retains an idempotency tombstone.

The backend trusts Scene SSO headers only from the protected loopback proxy.
Nginx overwrites client identity/admin headers. Jobs, media and settings are owner
scoped; only Drive credential/folder settings require admin. Sync and pair
selection use own authorized workspace and owner-scoped snapshots. Mutations
require same-origin application headers. No iframe; shared header/sidebar tokens
and Video Studio rail.

## Tests

```sh
python -m pip install -r apps/song-maker/part01/requirements.txt httpx jinja2 playwright
python -m unittest discover -s tests -p 'test_song_maker*.py' -v
python -m playwright install chromium
python production/song_maker_browser.py
```

The browser harness starts disposable real web/render/upload/sync processes and
verifies five viewport widths, refresh/tab persistence, real MP4 render/download,
no iframe and one shared sidebar. Test identity headers and Drive fakes are explicitly
not production authentication or real Google Drive evidence.

## Release gate

`production/song_maker_release.py` is a dedicated new-app lane, outside generic
`all` deployment. It stages immutable source, runs real-media tests, backs up affected
Scene SQLite files, overlays only Scene SSO allowlist/shared navigation, starts bounded
Song Maker services and adds one trusted SSO Nginx include. Failure restores scoped
units/pointers/routes and preserves all persistent state. First installation creates the app and route. Existing-instance updates use an
independent idle-only admission fence: stop scanner, hold the submission/claim lock,
refuse queued or active jobs, back up the Song Maker SQLite database, and swap only
Song Maker's immutable code pointer and four service units. Authenticated HTTPS and
worker health must pass; any failure rolls back those exact Song Maker units/pointer
while leaving all persistent state intact. Manual rollback is also idle-fenced.
Scene renderers, Nginx, other apps and their queues are never restarted.

The installed root-owned helper must first receive the reviewed Song Maker capability
through an already-approved maintenance transport. A GitHub source merge cannot
modify that privileged installed helper. Never invoke another app's lane to evade
this gate. Then use **VPS1 Song Maker guarded release** on protected main with the
existing production environment, owner actor, Tailscale OIDC, pinned SSH identity,
host serialization and exact-current-main check. No automatic deployment on merge.

Acceptance is not complete until an isolated real Drive pair is detected by the
running sync worker, rendered, and its remote ID/size/hash independently read back.
Health/CI alone does not establish that result. Current live configuration and
production evidence are recorded separately in `docs/song-maker-v1-20261029.md`.

## Song Maker queue and UI (30 Oct 2026)

The workspace has four tabs: Input & Drive Sync, Pair Matching, Queue and Settings.
There is no separate Output tab: the Queue exposes preview, local MP4 download and
verified Drive link. The Settings tab persists each owner's 1080p/720p/vertical
resolution and fit/blur defaults; these affect only newly admitted manual and
Drive-matched projects. The Queue shows real, throttled FPS, speed, elapsed time,
estimated render time remaining and stage/progress. A per-owner durable Pause/Resume
blocks new queue claims without interrupting active jobs. Batch retry, cancel
pending, and clear completed are owner-scoped; clear completed deletes local MP4
copies after explicit confirmation but never remotely uploaded Drive files.
