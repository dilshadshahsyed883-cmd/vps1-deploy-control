# My Remote — Android → Windows PC (isolated MVP)

**Status: source prototype, not deployed or validated on a real phone/PC.** This directory is independent of every VPS_1 production app. The parent repo is **public**, so NEVER commit credentials, media, user data, .env files, TLS keys or session cookies. Do not merge/deploy without CI and manual connection testing.

## What exists

- Android-first HTTPS browser UI, home-screen manifest, password sign-in and 8-hour HttpOnly session cookie.
- ASP.NET Core relay with two authenticated WSS endpoints, one Windows agent and one browser viewer at a time; outbound-only connection from the Windows PC.
- Windows 10/11 x64 .NET agent: primary-screen JPEG at ~4 fps (max 1280px wide) only while a viewer is connected; mouse, click/drag, right/double-click, wheel, typing, basic keys.
- The relay transports control packets and screen JPEGs; authentication, rate limiting, message-size limits, fixed command vocabulary, origin check and single-viewer policy are applied at the server.
- GitHub Actions creates a self-contained portable Windows x64 agent with a one-click DPAPI pairing launcher, builds the relay container, and performs login + authenticated WebSocket integration tests. No automatic production deployment.

## Architecture

Android Chrome/PWA --HTTPS/WSS--> isolated Linux ASP.NET Core relay --WSS outbound--> Windows agent.

The MVP forwards JPEG frames over the server (not yet WebRTC, direct peer-to-peer, or hardware video encoding). The VPS therefore consumes bandwidth during viewing. Screen contents are visible to the relay operator; WebSockets encryption is **transport encryption**, not end-to-end encryption.

## Server setup (isolated test host, not VPS_1 production)

Requires .NET 10 ASP.NET Core runtime on Ubuntu and an HTTPS reverse proxy on a **separate hostname**. Never expose the loopback ASP.NET Core port to the internet.

Set these environment variables in a private protected environment file (e.g. mode 0600, owned by service user):

- REMOTE_PUBLIC_ORIGIN=https://remote.example.com  (no path or trailing slash)
- REMOTE_BROWSER_PASSWORD=long-random-owner-password  (at least 20 characters)
- REMOTE_AGENT_TOKEN=independent-random-agent-token  (at least 32 characters)
- ASPNETCORE_URLS=http://127.0.0.1:5123

Generate *two distinct* random secrets, for example with openssl rand -hex 32 twice; do not paste the values into GitHub, scripts, or logs.

Run relay with dotnet MyRemote.Server.dll from the published server directory. Under Nginx, terminate TLS and proxy both HTTP and WebSocket traffic:

    location / {
      proxy_pass http://127.0.0.1:5123;
      proxy_http_version 1.1;
      proxy_set_header Host $host;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection "upgrade";
      proxy_read_timeout 3600s;
      proxy_send_timeout 3600s;
      proxy_buffering off;
    }

Serve only over HTTPS, block public port 5123 in firewall, isolate process and filesystem from production workloads, use an unprivileged service user, and verify rollback before deployment. This is an example, not an approved production configuration.

## Windows PC

1. Download MyRemote-Windows-x64 from a successful My Remote MVP GitHub Actions run and unzip all files to a folder that you control.
2. Double-click Start-MyRemote.cmd in that folder. The first run asks for your relay HTTPS URL and your private agent token. Do not enter the Android login password here.
3. The pairing token is stored encrypted with **Windows DPAPI** for the current Windows account, in LocalApplicationData/MyRemote (not in the repository or plaintext script). Double-click the same CMD on subsequent runs.
4. Keep the window running and Windows signed in/unlocked. Ctrl+C stops control; the agent reconnects automatically after transient disconnects. No inbound Windows port forwarding required.

To clear saved pairing, run Reset-MyRemote.ps1 under the same Windows user. This does not revoke the token on the relay: rotate REMOTE_AGENT_TOKEN separately when needed.

You can also run the EXE manually from PowerShell by providing REMOTE_SERVER_URL (wss://) and REMOTE_AGENT_TOKEN as process environment variables. Do **not** paste secrets in shared chat, terminal histories, public repos or screenshots.

## Isolated relay container

From apps/my-remote, prepare a secrets/relay.env file from secrets/relay.env.example with mode 0600. Generate two independent cryptographic random strings (at least 32 characters), never reuse the Android password as the agent token. For example: openssl rand -hex 32. Configure REMOTE_PUBLIC_ORIGIN as the actual dedicated HTTPS origin.

Run docker compose up -d --build in an isolated test environment. The relay binds **only 127.0.0.1:5123** on the host. Configure a dedicated HTTPS/WSS hostname in a reverse proxy, ensure TLS certificate and WebSocket Upgrade forwarding, and test the full public hostname before any external use.

This does not configure DNS or Nginx automatically. **Do not run these commands on the existing VPS_1 production host** without an independently checked, approved isolated deployment and rollback path.

## Android phone

1. Open https://remote.example.com in Android Chrome (not HTTP).
2. Sign in with the owner password.
3. Tap the screen to click, drag to move objects, or use the right/double-click, scroll and keyboard buttons.
4. Chrome menu → Add to Home screen for convenient access. Browser install experience varies depending on icon support.

## Current limits

This is **not a full AnyDesk alternative yet**. There is no native APK, OS-level unattended startup, file transfer, clipboard sync, audio, multi-monitor, adaptive video/WebRTC, 60fps encoding, Android background wake, system lock-screen capture, UAC/secure desktop control, or reconnecting browser socket. A PC must be on, connected to the internet, have an unlocked interactive desktop, and run the visible agent. Screen capture is GDI/JPEG, so performance is not comparable with hardware-encoded remote desktop software.

## Local builds

Server: dotnet build apps/my-remote/server/MyRemote.Server.csproj -c Release

Agent (Windows): dotnet publish apps/my-remote/agent/MyRemote.Agent.csproj -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true

## Next gates

1. CI successful on the exact commit SHA.
2. End-to-end phone + Windows PC test over HTTPS/WSS with own credentials.
3. Test disconnect, invalid login, mouse up on disconnect, pointer mapping and screen lock behavior.
4. Independent review of security, network and installation before a dedicated VPS deployment. **No changes to the canonical Drive Bridge or existing VPS_1 release workflow.**
