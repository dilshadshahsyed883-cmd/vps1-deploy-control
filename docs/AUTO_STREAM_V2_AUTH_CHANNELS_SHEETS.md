# Auto Stream V2 — OAuth, independent Channels, read-only Sheets Sync

Auto Stream V2 extends the existing V1 backend without changing uploader code,
legacy short-stream sessions, publishing switches, scheduler or existing credentials.

## VPS configuration (required before connecting Google accounts)

Provision an independent Google Cloud OAuth web client with registered callback:

https://app.scenebuilder.online/short-stream/engine/oauth/callback

Provide these through the existing restricted server environment secret mechanism
for the short-stream runtime (never GitHub source/Actions secrets logs):

AUTO_STREAM_GOOGLE_CLIENT_ID
AUTO_STREAM_GOOGLE_CLIENT_SECRET
AUTO_STREAM_GOOGLE_REDIRECT_URI
AUTO_STREAM_FERNET_KEY  (one stable cryptography Fernet key; back up securely)

The V2 application is usable with **no** OAuth credentials configured; it
responds oauth_configured=false and prevents authorization. Never guess, read
or copy credentials from Auto Uploader.

Scopes: youtube.force-ssl for YouTube identity/read-only live status and
spreadsheets.readonly for approved Google Sheet values. Browser-facing OAuth
uses 10-minute one-use state and PKCE-S256 verifier; callbacks validate
the chosen YouTube channel against channels.list(mine=true). A channel may be
manually metadata-imported without OAuth; this grants NO upload/stream access.

Refresh tokens are encrypted using Fernet before being written to a
0600 standalone SQLite file under Short Stream's own persistent data root.
Refresh on demand only. Disconnect immediately removes local grant. Revocation
of consent at Google is separate and can be managed in Google account settings.
No token or RTMPS stream key appears in Sheets, URL params, APIs, or logs.

## API endpoints (existing Scene SSO gateway only)

GET /engine/integrations/health
GET /engine/channels
POST /engine/channels/import  {channel_id,title} — metadata only
POST /engine/channels/{channel_id}/disconnect
GET /engine/oauth/start -> authorization_url; opened by signed-in user
GET /engine/oauth/callback?code=...&state=...  (dedicated Google redirect)
GET /engine/sheets
POST /engine/sheets {channel_id,spreadsheet_id,a1_range}
POST /engine/sheets/{id}/sync?dry_run=true (default: preview; false: create DRAFT)
GET /engine/channels/{channel_id}/live (Google API-backed statuses)

Sheets columns: channel_id,media_path,title,scheduled_at,enabled;
optional continuous. Only read-only approved Sheets through the
authorized connection. Rows must match the linked authorized channel,
reference Auto Stream media and include timezone-aware schedule.
Sync can create DRAFT jobs only and never start FFmpeg/broadcast. Idempotency
key includes sheet id, row and source/schedule identifiers. No overwrite of
active jobs. Maximum 200 data rows per sync; invalid rows returned as per-row
errors.

YouTube Live status is read-only, comes directly from Google Live API;
`youtube_verified_live` only when YouTube reports lifeCycleStatus=live.
No broadcast creation, RTMPS keys, start/publish/transition or 24x7 scheduler
are automatically enabled. Existing V1 streaming API remains disabled by
default. Actual authorization needs owner-controlled Google Cloud credentials
and consent; no account connection is possible without them.

Deployment through guarded Short Stream GitHub–Tailscale release with active
legacy session=0 preflight, rollback and strict authenticated gateway checks.
