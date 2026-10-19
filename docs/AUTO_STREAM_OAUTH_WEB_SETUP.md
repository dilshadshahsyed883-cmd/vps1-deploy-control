# Auto Stream — Browser-managed Google OAuth Client (admin only)

**URL:** https://app.scenebuilder.online/short-stream/engine/studio

Open the *OAuth Setup* tab while signed into Scene Builder as an **admin**.
Create a Google Cloud OAuth 2.0 **Web application** client in
https://console.cloud.google.com/apis/credentials and enable the
YouTube Data API v3 and Google Sheets API. Register the exact redirect URI:

https://app.scenebuilder.online/short-stream/engine/oauth/callback

Paste the new client ID and client secret into the two HTTPS form fields.
Select **Save OAuth Credentials**. Neither a VPS terminal command nor
restarting Short Stream is necessary. The backend encrypts credentials in
a mode-0600 file in Auto Stream's *own* persistent application directory,
using its previously provisioned stable Fernet vault key. The secret is
never returned through the browser status API. Admin-only role headers are
provided by Scene session SSO; POST/DELETE additionally require same-origin
request checks and a custom configuration header. Direct unauthenticated
requests to the local application port fail closed.

Legacy environment OAuth configuration remains usable unless a newer
browser-stored client is present. An admin can remove an app-stored client.
Replacing/removing is refused while authorized channel tokens or pending OAuth
consent exist. Existing Auto Uploader OAuth clients, channels and tokens are
not read, migrated or changed. All YouTube publishing/Go Live flags remain OFF.

After saving the Google client, use **Channels → Connect YouTube + Sheets**,
sign in to Google and provide the requested consent. OAuth consent is user
action. The current release does not itself authorize a channel or publish a
stream. Use the engine's read-only Live status and Sheets preview after Google
authorization. The owner performs UAT.
