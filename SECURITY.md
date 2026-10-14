# Security boundaries

- Until a verified, separately authorized cutover, **only Google Drive Bridge + locked Agent 1** executes on VPS_1.
- CI is a GitHub-hosted workflow. Do not register a broad production self-hosted runner.
- Do not accept arbitrary shell scripts, user-supplied commands, GitHub issues, PR bodies or commit messages as deployment input.
- Use a dedicated least-privilege non-root deployment identity and an explicit per-app allowlist. Never grant blanket sudo or access to other app roots.
- CI and deployments must verify immutable release hashes, signatures/identity when configured, trusted branch origin, and protected environment gates.
- Never allow a PR-triggered untrusted workflow to access Tailscale OIDC credentials or protected deployment secrets.
- Establish `tag:ci` and a host-only ACL; do not expose public SSH ports or widen existing firewall rules.
- Keep an exact known-good release, atomic switch mechanism and demonstrated rollback before touching live app routes.
- No secret material in logs, files, GitHub artifacts, Drive reports or manifests; do not print SSH config contents.
- No unattended migration, deletion, channel publishing, authentication/permission alteration, or irreversible action.
- The pilot has no deployment secret and cannot deploy to VPS_1 by design.
