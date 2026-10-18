# VPS_1 Tailscale host migration — 18 October 2026

Scope: GitHub **offline/disabled** workflow target and pinned SSH known-host update only.

- Retired Tailscale IP (offline original machine): `100.113.253.99`
- Fresh VPS Tailscale IP (Agent 1 read-only verified): `100.87.55.92`
- Fresh VPS SSH ED25519 fingerprint: `SHA256:lpe4LGyWUajX4+UbUYbt5U+4ddtHFjcyEAKBlR7Bfog`
- Fresh VPS SSH public host key: `ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGpN+j9QsIP11NqypSCoYdFZ0Bvy65j608M4EyjC+OlJ`
- Agent 1 read-only proof: https://drive.google.com/file/d/1CWOsG23eepq4GeNov4gT4ioKbKqOLpu0/view
- Production SSH and `tailscaled` active; `TAILNET_CONNECTED=YES`.
- `vps1deploy` remote account: **CREATED / least-privilege public key installed (Agent 1 PASS on 2026-10-18)**.
- GitHub production SSH credential compatibility: **NOT VERIFIED**. Previous private-key fingerprint pin is preserved and not replaced.
- Tailscale OIDC production environment and `tag:ci` grant: **NOT RETESTED**.
- All seven credential-consuming remote jobs remain **hard-disabled**, including proof. No live GitHub SSH performed.
- Unchanged: SSH service, firewall, GitHub secrets, Drive Bridge, applications, `main` deployment control.

## Before any separately approved GitHub SSH proof

1. ~~Establish a least-privilege `vps1deploy` account through canonical Drive Bridge~~ — **DONE**, verified by Agent 1.
2. Confirm production-environment secret *presence*, restricted OIDC `tag:ci` and exact private identity match without exposing credentials.
3. Perform a separately authorized read-only proof of the new host IP, pinned ED25519 host key and deploy account.
4. Explicitly authorize any future remote-workflow enablement; **this PR keeps all remote jobs disabled**.

## Least-privilege deployment SSH identity restored (Agent 1, 18 October 2026)

- Job ID: `VPS1_AGENT1_DEPLOY_IDENTITY_RESTORE_20261018_T6M9` via canonical Drive Bridge V3, Agent 1.
- **PASS:** created dedicated non-root `vps1deploy` user, restricted home `/home/vps1deploy`, without sudo/wheel or other supplementary groups.
- **PASS:** installed the **historically verified A7K2 production GitHub public key**, matching the existing GitHub pinned private-identity fingerprint `SHA256:oeX4vWPvVspKOTrvbMgUa+uWPGyXLz3531TQEy5wp1E`. No SSH private key placed on the VPS.
- **PASS:** authorized key options `from="100.64.0.0/10",restrict`, SSH folder 0700, authorized_keys 0600; password authentication for the account locked.
- **PASS:** SSH public key authentication enabled in the effective server config, SSH/Tailscale/Agent 1 services stayed active, new tailnet IP `100.87.55.92` unchanged.
- **PASS:** job report `EXIT_CODE=0`, `RESULT=PASS`, restored account/key only; no changes to SSH daemon config, firewall, GitHub secrets, apps or deployment state.
- **NOT TESTED:** live GitHub Actions OIDC `tag:ci` connection and private-identity SSH authentication; GitHub production secret provenance remains unverified.

[Verified Agent 1 report](https://drive.google.com/file/d/1pyAmas7RQb-ZMHzeb6IWV2QP_lC1NbWk/view).

**Account creation blocker is closed; independent GitHub OIDC/SSH live proof remains held.** All seven credential-consuming jobs remain hard-disabled.
