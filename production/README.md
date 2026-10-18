# VPS1 production runtime

This is the real host installer for seven independent apps. The `rebuild/`
rootfs simulations remain offline. Scene Builder, its Script Work integration,
the existing Storage Manager candidate and Auto Script have separate owners.

Initial installation uses the chat-selected canonical Drive Bridge agent. The
small carrier pins the bootstrap bytes and protected-main Git SHA. Bootstrap
installs FFmpeg, isolated per-app Python environments, immutable source releases,
systemd services, the shared application workspace and authenticated Nginx routes.

Every app passes a real subprocess startup and UI test before its live pointer
changes. Short Maker also renders and downloads a small MP4. Existing app SQLite
databases are backed up with the SQLite backup API, including WAL transactions.
A failed activation restores that app's previous unit and pointer. Media and
application data remain in place. Initial install failures preserve new state.

The initial workspace login is generated only on the host, in the mode-0600
`/root/vps1-production-login.json`; credentials never enter GitHub or Drive
reports. Gateway checks prove anonymous requests are denied and authenticated
app health and HTML pages work. Application ports stay on loopback.

After initial Bridge installation, the existing `vps1deploy` account can invoke
only the root-owned production command. It accepts an allowlisted operation and
app plus the exact current protected-main SHA. GitHub's production environment
approval and existing Tailscale/SSH identity remain in force. The six unrelated
legacy remote workflows remain disabled; the one-time SSH proof is unchanged.

Use the **VPS1 production applications** workflow for app updates or code rollback.
A main merge containing `[vps1-production-release]` requests one coordinated
release. Regular commits do not deploy. A request for an obsolete main SHA
stops; run against current main instead.

Service health and UI readiness are separate from external provider readiness.
YouTube OAuth channels, a live stream destination, GPU voice endpoints and model
assets must be configured in their app interfaces. No saved media, old queue or
credential is imported by this installer, and no live stream is started by it.
