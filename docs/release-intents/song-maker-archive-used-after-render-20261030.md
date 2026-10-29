# VPS_1 Song Maker — verified render input archiving rollout

Source: merged PR #381, commit `faf155cb55d0e42504082518e8854b42e4badf9d`.

## Owner requirement
Upon **successful render and complete MP4 validation**, move exactly that project's Drive image + audio from `Song Studio/Audio downloader/` into `Song Studio/Audio downloader/used/`. **Do not wait for MP4 Drive upload.**

The MP4 is queued separately for delivery under `Song Studio/song maker/`.

## Production scope
- Use the existing root-authorized Song Maker app lane only: `install <current protected main SHA> song-maker`.
- Stop the scanner first, hold the submission/claim admission lock, refuse active/queued jobs, back up Song Maker SQLite, update only its own immutable pointer and four systemd units.
- Preserve scene renderer, Nginx, Voice TTS, Audio Downloader, Song Uploader, OAuth, existing jobs, prior Drive objects, and owner-reported Auto Script ONLINE instance.
- Verify authenticated HTTPS, worker heartbeats and neighboring app health. Automatically restore exact Song Maker pointer/units on any failure; don't bypass owner controls.

## Test and release evidence
The source PR passed 14/14 protected CI workflows including disposable real media rendering and archive timing/collision/retry tests. The tested fake Drive move is not evidence of real Google Drive mutation. Only a successful guarded production run and an isolated real Drive image/audio pair can establish remote behavior.

## Activation
Merge this docs-only release intent after protected CI with both explicit markers:
`[vps1-song-maker-update-release] [vps1-song-maker-only]`.

Never call other-app release lanes, avoid rerunning a failed installer after it has started, and don't declare production success without a passed run.
