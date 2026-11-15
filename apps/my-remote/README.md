# My Remote — Android → Windows PC (isolated MVP)

**Status: source prototype, not deployed or validated on a real phone/PC.** This directory is independent of every VPS_1 production app. The parent repo is **public**, so NEVER commit credentials, media, user data, .env files, TLS keys or session cookies. Do not merge/deploy without CI and manual connection testing.

## What exists

- Android-first HTTPS browser UI, home-screen manifest, password sign-in and 8-hour HttpOnly session cookie.
- ASP.NET Core relay with two authenticated WSS endpoints, one Windows agent and one browser viewer at a time; outbound-only connection from the Windows PC.
- Windows 10/11 x64 .NET agent: primary-screen JPEG at ~4 fps (max 1280px wide); mouse, click/drag, right/double-click, wheel, typing, basic keys.
- The relay transports control packets and screen JPEGs; authentication, rate limiting, message-size limits, fixed command vocabulary, origin check and single-viewer policy are applied at the server.
- GitHub Actions creates a self-contained portable Windows x64 agent and performs relay build + unauthenticated/login smoke checks. No automatic production deployment.

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

1. Download the MyRemote-Windows-x64 artifact from a successful My Remote MVP GitHub Actions run.
2. Unzip to a folder controlled by the PC owner. Verify the workflow's printed SHA-256 checksum.
3. Open PowerShell and configure the current session (use your actual HTTPS hostname and the server's agent token):

       $env:REMOTE_SERVER_URL = "wss://remote.example.com"
       $env:REMOTE_AGENT_TOKEN = "<private agent token>"
       .\MyRemote.Agent.exe

4. Keep the agent window open and Windows signed in/unlocked. Ctrl+C stops remote access. It reconnects automatically after a transient disconnect. No Windows firewall inbound port forwarding is required.

Be careful entering secrets in shell history; use a protected secret store for longer-term operation. Never ask someone to run an unknown agent or share credentials.

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
