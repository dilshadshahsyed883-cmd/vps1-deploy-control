# Coordinated fresh-website clean-install test

Runs only from the existing PR #7 source. This is a **single combined**
isolated Ubuntu job that assembles source releases, verifies integrity,
executes application-native fresh SQLite initialization, validates queues
are empty, produces candidate worker and web units, tests authenticated
Nginx reverse proxy, starts actual locally isolated application services,
and exercises a previous-revision rollback.

Neither old databases nor historical queues, uploads, backups or media are
imported. Algrow Voice stays excluded. Auto Script's parallel branch stays
unchanged. Production SSH/Tailscale, GitHub environment production and
VPS_1 live services are never accessed.

The test report has two deliberately distinct statuses: the subset exercised
successfully in the isolated build; and the **full 11-app readiness gate**.
Do not treat a passing subset as authorization to merge, wipe, cut over,
enable automatic production deployments, or claim all integrations tested.

Commands on an isolated Ubuntu 24.04 filesystem with dependencies installed:

    python3 -m rebuild.full_site_test --root /tmp/fresh11-ci --revision "$(git rev-parse HEAD)"

The script fails immediately if the native schemas cannot initialize empty.
The readiness report is written within the isolated test rootfs. Full 11-app
cutover remains BLOCKED until all external integrations and workers pass.
