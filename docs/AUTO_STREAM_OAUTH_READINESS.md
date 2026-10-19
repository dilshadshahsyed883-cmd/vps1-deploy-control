# Auto Stream — isolated OAuth readiness and encryption vault bootstrap

For existing VPS_1 short-stream installations, the scoped production installer
now generates an independent, stable Auto Stream Fernet vault key in its
existing root-owned /etc/fresh-website/production-short-stream.env only if no
key exists and no encrypted Auto Stream OAuth/broadcast secrets already exist.
It never reads, changes or copies Auto Uploader OAuth credentials. An existing
key is preserved; an invalid key or undecryptable legacy state prevents install
rather than silently rotating it. No key value appears in logs or the web UI.

GET /short-stream/engine/integrations/health provides safe booleans:
- vault_ready
- google_client_ready
- oauth_configured
- channels_connected

OAuth client credentials must still come from a separate Google Cloud OAuth
2.0 Web application under user control. Configure its exact authorized redirect:

https://app.scenebuilder.online/short-stream/engine/oauth/callback

Google Cloud console: https://console.cloud.google.com/apis/credentials
Required enabled APIs: YouTube Data API v3 and Google Sheets API. The user
must authorize the chosen channel through the Auto Stream Connect UI. The
client ID/secret must be provisioned by the VPS operator through a restricted
secret channel directly to the VPS environment file; never paste the secret
into ChatGPT, commit it to GitHub or put it in Sheets.

Production OAuth, Sheets readback and private YouTube Live E2E tests cannot be
claimed until owner OAuth consent and Google client provisioning complete.
YouTube publishing, broadcast mutations, transitions and scheduler remain OFF.
