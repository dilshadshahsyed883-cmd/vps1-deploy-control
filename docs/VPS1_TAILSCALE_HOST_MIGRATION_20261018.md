# VPS_1 Tailscale host migration — 18 October 2026

Scope: GitHub **offline/disabled** workflow target and pinned SSH known-host update only.

- Retired Tailscale IP (offline original machine): `100.113.253.99`
- Fresh VPS Tailscale IP (Agent 1 read-only verified): `100.87.55.92`
- Fresh VPS SSH ED25519 fingerprint: `SHA256:lpe4LGyWUajX4+UbUYbt5U+4ddtHFjcyEAKBlR7Bfog`
- Fresh VPS SSH public host key: `ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGpN+j9QsIP11NqypSCoYdFZ0Bvy65j608M4EyjC+OlJ`
- Agent 1 read-only proof: https://drive.google.com/file/d/1CWOsG23eepq4GeNov4gT4ioKbKqOLpu0/view
- Production SSH and `tailscaled` active; `TAILNET_CONNECTED=YES`.
- `vps1deploy` remote account: **ABSENT on fresh VPS** at verification.
- GitHub production SSH credential compatibility: **NOT VERIFIED**. Previous private-key fingerprint pin is preserved and not replaced.
- Tailscale OIDC production environment and `tag:ci` grant: **NOT RETESTED**.
- All seven credential-consuming remote jobs remain **hard-disabled**, including proof. No live GitHub SSH performed.
- Unchanged: SSH service, firewall, GitHub secrets, Drive Bridge, applications, `main` deployment control.

## Before any separately approved GitHub SSH proof

1. Establish a least-privilege `vps1deploy` account through canonical Drive Bridge with safeguard/rollback. No sudo or wheel.
2. Confirm production-environment secret *presence*, restricted OIDC `tag:ci` and SSH public key match without exposing credentials.
3. Perform a separately authorized read-only proof of the new host IP, pinned ED25519 host key and deploy account.
4. Explicitly authorize any future remote-workflow enablement; **this PR keeps all remote jobs disabled**.
