# Fresh VPS rebuild (11 selected application groups)

This migration is **not a cutover**. VPS_1 remains intact; no old databases,
queues, media, uploads, releases or backups are imported into the new system.
Algrow Voice is excluded and must not be pulled in as a transitive runtime.

This branch owns `rebuild/**`, the new `fresh-vps-*` workflows and any new
source subfolders agreed after sanitization. It never rewrites the existing
Auto Script CI/CD branch, `apps/auto-script/**`, the existing pilot or Kokoro
source ownership.

`python3 rebuild/builder.py state` reports missing group sources; that result
is not installation-ready until **all 11 groups** have real sanitized source
and Auto Script's separate branch is reconciled. `plan` prints dependency
ordering. `package --app <id> --sha <40-hex> --output ...` builds source-only
SHA256 verified artifacts from registered applications. `stage-rootfs --output
/tmp/safe-path` will populate a local offline test tree, only after source
coverage is complete. It does not touch systemd/nginx or production.

Fresh installation MUST additionally obtain and test the actual current
reproducible Python/Node/Docker dependencies, Nginx exact routes, systemd
service ownership, fresh schema initialization, empty queue setup, external
configuration values and per-app rollback health gates. Their definitions must
come from the selected live source manifests, **not inferred from a service
name**. Production workflow is deliberately blocked until that evidence and
explicit user authorization exist. No production credential values belong in
GitHub. GitHub Actions/CI status is independent from VPS functional testing.
