# VPS_1 Nginx, routing and systemd as versioned infrastructure

This adds **inert Git-managed configuration**; it does not discover or modify VPS_1 live routing. Production remains under the canonical Drive Bridge V3 agent rules. No GitHub Actions workflow can write to /etc/nginx or systemd.

## State as introduced

- Source of truth: infrastructure/registry/apps.json, deliberately EMPTY until an authorized read-only live audit confirms actual location paths, upstream ports, WebSocket behavior, prefix stripping, authentication, dependencies and service unit names.
- Compiler: tools/render_nginx_routes.py (Python stdlib only). It emits a deterministic **server-context location snippet**, not a full site or certificate configuration. Every upstream is hardcoded to 127.0.0.1 with a validated numeric port.
- Synthetic CI registry: tests/fixtures/nginx-apps.json; never apply this fixture to production.
- Service template: infrastructure/systemd/vps1-app.service.template is an inert document. Real, sanitized per-app unit files must be reviewed and committed separately, after live discovery. Do not blindly replace user, ExecStart, secret paths, dependencies or production service definitions.
- CI: .github/workflows/infra-nginx-validate.yml runs unit tests and Nginx syntax checks in an isolated network-disabled container. GitHub-hosted CI cannot reach the VPS from this workflow.
- Agent-only apply helper: scripts/apply_nginx_routes.sh is guarded and defaults to --check. This is NOT called by CI and must not run before approved Agent execution, exact server-context include wiring and rollback evidence.

## One-shot read-only live discovery (future, through locked VPS Agent only)

Capture sanitized nginx -T site structure (never publish certificates, private keys, auth directives, tokens, passwords or credentials), /etc/nginx/sites-enabled/ logical includes, domain route collisions, /etc/systemd/system service metadata, app upstream listeners, service ownership and known-good HTTP behavior. Collect enough detail to validate all routes before activating a single registry entry. Do not change DNS, TLS, Auth, HTTP root paths, media/upload routing, or other existing apps.

## Safe adoption

1. Compare discovered live Nginx and service inventory with registry entries. Keep existing config as authoritative until the reviewed Git snapshot is complete.
2. Populate real app entries with id, exact /id/ path, actual upstream_port, prefix_mode preserve/strip, websocket bool, and actual service_unit name. The strict schema intentionally disallows arbitrary proxy host, free-form directives and injected headers.
3. Run Python tests; build the config; run Nginx syntax checks. Inspect diff against the sanitized, effective live site and test equivalent auth paths, uploads, WebSockets and static assets.
4. Once authorized under the locked Drive Bridge agent, one-time bootstrap: create /etc/nginx/vps1-managed and add a single 'include /etc/nginx/vps1-managed/*.conf;' line **inside the correct app.scenebuilder.online server block**, never at http/global level. Snapshot and test that site change separately; roll back if nginx -t or real HTTP checks fail. Do not overwrite the existing site block.
5. Only after that bootstrap and explicit approved config change, use the agent to run:
   - python3 tools/render_nginx_routes.py --registry infrastructure/registry/apps.json --output build/nginx/vps1-managed-routes.conf --require-nonempty
   - bash scripts/apply_nginx_routes.sh --check build/nginx/vps1-managed-routes.conf
   - VPS1_AGENT_EXECUTION=DriveBridge VPS1_CONFIG_CHANGE_APPROVED=yes bash scripts/apply_nginx_routes.sh --apply build/nginx/vps1-managed-routes.conf
   Agent must then test public HTTPS per affected route with appropriate authentication and report evidence to Automation_Master_System/VPS_1/02_REPORTS/. If app health fails despite successful reload, restore the saved backup with the locked agent. The script automatically attempts rollback when Nginx syntax/reload fails.
6. Never remove a route from the live registry just because an app was removed from Git. Verify user authorization, persistent data backup and dependency inventory before any deletion, and preserve previous known-good config for rollback.

## Prefix and identity behavior

- preserve: 'proxy_pass http://127.0.0.1:PORT;' leaves the '/app/' prefix unchanged.
- strip: 'proxy_pass http://127.0.0.1:PORT/;' strips the matched '/app/' prefix.
- Switching modes can break absolute URLs, authentication cookies, CSRF, uploads and WebSockets; verify with browser tests.
- The website shell owns navigation, app UIs stay independent, and a route edit need not rebuild unrelated applications.
- SSL certificates, .env files, SSH keys and cookies are *never* committed. Keep TLS renewal on VPS.

## Security limitations

The managed include requires a one-time manual integration into the correct existing server block. A generic Nginx syntax pass alone cannot prove production route correctness, ownership or endpoint health. The environment-variable gates in the helper are defense-in-depth only, not a replacement for Drive Bridge's privileged execution control. No CI production deploy is enabled by this change.
