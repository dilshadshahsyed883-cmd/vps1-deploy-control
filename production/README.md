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

## Direct GitHub → Tailscale Nginx route-only release

**Operation:** In the existing **VPS1 production applications** GitHub Actions
workflow, select one independent app and `nginx-route-only` (or
`nginx-route-rollback`). Never select `all` or Scene Builder for these operations.
Normal `install` / `rollback` semantics are unchanged.

The production SSH helper now allows only the fixed route operation and exact
current protected-main SHA, then calls `production.nginx_route_only`. This never
installs app dependencies, rebuilds the shared gateway, restarts app services,
touches SQLite/queues or changes existing Scene login. It inserts one app-specific
Nginx include into the existing reviewed TLS vhost, only when existing routing
ownership and the app's authenticated internal gateway are confirmed. It verifies
a valid TLS/SNI certificate, anonymous denial, authenticated health and real HTML
on the same HTTPS prefix, then records a scoped rollback snapshot. Earlier Voice,
Caption and uploader routes remain owned by their original release. If a route
already exists and works, this operation is a verified no-op. A rollback restores
only the matching last recorded route and blocks if the Nginx config drifted.

**One-time host-helper upgrade prerequisite:** the existing installed
`/usr/local/sbin/vps1-production-deploy` may be an older binary/source script
which only understands install/rollback. GitHub cannot update a root-owned
sudo allowlisted executable merely by merging this PR. Perform a separately
authorized, hash-pinned installation of the reviewed new helper through an
already-permitted bootstrap transport, or report `HOST_HELPER_UPGRADE_REQUIRED`.
Do **not** add a general sudo shell, unpin host SSH, or re-enable disabled
legacy Actions workflows. Verify the installed helper hash/version before the
first live route-only dispatch; a failed invocation proves nothing was published.

The GitHub workflow keeps the existing `production` environment binding,
Tailscale OIDC, pinned SSH host key and host-wide deployment serialization.
PR tests are offline; they are **not** a VPS route-live proof. Report URL live
only after a successful authenticated HTTPS job and state readback.
