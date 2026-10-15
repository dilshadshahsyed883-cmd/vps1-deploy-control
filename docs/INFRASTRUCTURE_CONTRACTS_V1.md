# VPS_1 — Infrastructure contracts / Tab 3 handoff

**Base:** `migration/fresh-vps-11groups-20261015`. GitHub-only, draft PR. No
VPS operations, Nginx reload, service installation or main merge authorized.

## Ownership boundaries

- Tab 1: reconcile branches and maintain the authoritative migration registry.
- Tab 2: durable, serialized per-app release executor and rollback.
- Tab 3: read-only route validation, independent shell UI, service contracts,
  shared-dependency/storage admission.
- Tab 4: sanitized **verified live** Nginx/auth/upstream/systemd/browser
  inventory and authorized activation. All actual reports go to
  `Automation_Master_System/VPS_1/02_REPORTS/` via locked Drive Bridge Agent.
  Tab 3 does NOT author any job package or apply live config.

## Routing contract

`infrastructure/registry/apps.json` intentionally has **zero** live entries.
No candidate port in `rebuild/install-profiles.json` is proof of a live VPS port.

1. Tab 4 supplies live observed route, service unit, WebSocket endpoint,
   static-asset paths, site vs application authentication and browser-tested
   prefix behavior. Never invent or default real ports. Sanitized inventory
   refers back to its Agent 2_REPORTS evidence using the canonical report path.
2. `tools/review_nginx_contracts.py` validates the existing strict renderer
   registry against the externally verified inventory and prints a unified diff
   between existing reviewed and proposed managed snippets. It is **read-only**.
   Any missing inventory, mismatched upstream, WS path, static prefix, auth
   policy, unsupported prefix stripping or unexpected route fails closed.
3. CI uses `tests/fixtures/nginx-inventory.json` with
   `--fixture`. This mock cannot prove a live route, and fixture origin
   cannot be used to pass real production review.
4. `tools/render_nginx_routes.py` retains the strict loopback-only
   allowlist. The agent-only apply helper requires explicit approval,
   bootstrap of the exact server-context include, root privileges and Drive
   Bridge. It saves a prior config and attempts rollback on syntax/reload
   failures. Production HTTPS/login/static assets/WS must still be checked by
   Tab 4; CI only tests syntactic correctness, negative guards and the diff.

**Important:** Site Basic Auth/cookies, CSRF, callback redirects, iframe
`X-Frame-Options`/`frame-ancestors`, arbitrary root-relative `/static/`
URLs and WebSocket upgrade behavior cannot be inferred by automation. Live
inventory must contain app-owned routes only. Shared static paths need explicit
separate adoption review and conflict detection; the validator rejects them
by default. A valid Nginx `-t` is not a runtime acceptance test.

## Website shell

The shell controls header, responsive sidebar, accessible light/dark switch,
navigation and the same-origin iframe host. Each app owns its UI, routes,
processes, database, workers and independent release. The CSS theme switch
changes only shell chrome; iframe apps must opt in to matching theme tokens.

`rebuild/website_shell.py` no longer considers a service install profile a
confirmed public route. If the protected live route registry is empty,
all ten apps can appear in the menu but remain `Route pending`. Auto Script
is excluded. A route entry only enables navigation; runtime health remains
unverified until Tab 4 browser/API checks.

Shared shell changes require shell release review, app source edits should
rebuild only the changed app, and shared infrastructure edits require the
Tab 2 broader-impact release classifier. This PR does not modify Tab 2 queue.

## Systemd, Python and app isolation

- `infrastructure/systemd/vps1-app.service.template` and
  `vps1-worker.service.template` are **inert**, not installable without
  exact unprivileged user, verified startup arguments and writable state paths.
- One **system** Python interpreter, individual app venvs, a single uv cache
  using hardlinks on the same device, common OS FFmpeg/libraries. Venvs are
  separate environments; `uv` cache and read-only compatible model content
  may be shared. Model reuse requires same exact version, checksum and license.
- `rebuild/dependency_budget.py` already checks the 30,000,000,000 byte
  application budget and 5,000,000,000 byte host reserve after staged
  installs, counting unique hardlinked inodes once. It is **admission only**,
  not a recurring storage quota, and does not measure unknown live VPS usage.
- `rebuild/storage_admission.py` produces **read-only** add/remove plans,
  counts shared uv and models only once and never automatically deletes shared
  cache/models. App removal requires backup review, agent authorization and
  data retention decision. Installing may require reserving estimated cache
  and model growth; no destructive operations are implemented.

## CI and activation gates

`infra-architecture-contracts.yml` runs unit tests for malformed routes,
missing evidence, static/WebSocket/prefix/auth conditions, rendered Nginx
syntax in a network-disabled disposable container, shell route assertions and
storage install/remove planning. `test_nginx_apply_guard.py` checks the
non-mutating --check mode and static rollback guards; it does **not** inject
failures into the real agent apply path. CI success is not proof of production
rollback or a 30 GB ten-app measured installation.

Do not merge/activate until Tab 1 reconciles migration conflicts, CI passes,
Tab 4 reports a verified route manifest and Tab 2 production gates exist.
Existing OS, Tailscale, SSH, real Nginx sites, databases and services are
untouched by this PR.
