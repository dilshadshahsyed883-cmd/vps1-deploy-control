# Audio Downloader proxy and authorized YouTube sessions

Issue VS-ISS-20261021-AD-01. Scope: Audio Downloader only.

The Proxy Test reads YouTube robots.txt through the specified profile. Its result
is **connectivity only**, never proof of media extraction, authentication, or
Drive delivery. `/tts/api/audio-downloader/status` reports the enabled toggle,
selected ID, worker profile ID, explicit execution contract and session state.
The authenticated production prefix is `/voice-studio`.

Each download snapshots the enabled selection into a private temporary yt-dlp
config. An enabled missing/invalid profile fails before network access. Disabled
means explicit direct mode, with inherited proxy/bypass environment removed.
Proxy credentials stay out of process arguments and user-visible errors.
Existing jobs are not automatically retried on terminal provider/auth failures.
Transport retries are bounded (1); extractor retries are disabled (0).

An owner may configure an authorized Netscape-format export at
`$AUDIO_DOWNLOADER_ROOT/data/youtube-cookies.txt`, or set
`AUDIO_DOWNLOADER_COOKIES_FILE` in the existing Voice service environment to a
private owner-readable file. The file must belong to the Voice service user,
have mode 0600, be a regular file (no symlink) and contain unexpired YouTube
cookies. Never commit, log, publicly share or paste session cookies into issues.
The worker uses a private temporary copy and never rewrites the original export.
Status shows configured/not configured/invalid, never cookie contents or paths.
No cookie collection, OAuth extraction, token generation or challenge bypass is
performed. Cookies can expire; YouTube may still demand provider verification.

Upstream guidance:
https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp
https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies

The release overlays only `part01/audio_downloader_api.py` onto the existing
immutable Voice release. Current Voice UI and independent TTS workers are
preserved. An Audio SQLite write fence rejects active/pending work before web
shutdown; an integrity-checked SQLite backup is made before switching. Failed
health/authenticated API checks restore the prior symlink and web service.

Disposable smoke uses yt-dlp's short test video `BaW_jenozKc` through the deployed
worker with the current app proxy and optional private session. It uses a
temporary SQLite/media root, never owner jobs. Drive delivery uses the existing
verified workflow when configured; only this uniquely named delivered QA object
is removed. A provider challenge is reported as a blocker, not download success.
Audio extraction and Drive verification are reported independently.
