# TAB 3: Immutable offline shadow deployment and N7G3 route inventory

State: SOURCE-ONLY; candidate branch; NO VPS install, SSH, Nginx change, merge or secret access.

## Implementation

Existing rebuild/builder.py packages per-app immutable source SHA artifacts with tar SHA-256 and per-file hashes. Existing rebuild/deploy_engine.py provides persistent host-wide FIFO, idempotence, lease-based crash freeze and simulator; no production adapter is wired. New rebuild/shadow_release.py adds a strictly /tmp-root release transaction with host-wide fcntl lock, SHA-256 authentication of artifact *bytes* (not origin), required dependency readiness, per-app recovery/capacity/security/health evidence, loopback port admission, bounded safe unpack, journaled transitions, atomic SHADOW-ONLY pointer, simulated health probe, abort and verified rollback. Missing evidence FAILS CLOSED. Offline test tokens are synthetic and never evidence of live health or restore.

## Nginx route compatibility

The full 43 sanitized file-scoped entries from Agent 1 N7G3 are preserved verbatim as paths and metadata, including duplicated /auto-caption/ path, UNKNOWN upstream/auth fields and four legacy WebSocket routes where observed. N7G3 is not effective nginx -T and cannot establish TLS, auth_request, callback, CSRF/cookie, SSO, location/include precedence or live upstream ownership. rebuild/nginx_route_compat.py classifies all 43 and returns SHADOW_ONLY_NO_ROUTE_CHANGE. No production route snippet is emitted, no config is applied. Storage Manager has zero observed N7G3 app routes.

For any future per-app switch, compare sanitized effective nginx -T for precise server/location/include order and capture a protected old config; require exact app upstream ownership, listener and worker health; preserve upstream URI/prefix semantics, TLS/HSTS, auth_request, SSO, OAuth callback paths, CSRF, Secure/SameSite/cookie scope, WebSocket upgrades/timeouts and upload/media behavior. Test allowed and denied auth and WebSocket paths; rollback app-only route to preserved upstream if a check fails. No Nginx reload or route change allowed in this PR.

## Admission from parallel tabs

TAB 1 must provide working non-root service startup and workers, app health/auth positive+negative and dedicated data/state ownership, exact immutable commit/artifact and verified independent shadow port. TAB 2 must provide measured *current* free, actual peak application/release/temp/model/FFmpeg bytes and minimum host/rollback reserves, recoverable consistent off-host DB/WAL/queue/uploads/auth/media/secret/config backups with isolated restore evidence, OVH snapshot coverage and restore path. No Docker escalation authorized. Issue #21 provider/secret provenance unresolved.

Source contracts still declare runtime_verified=false and health.enabled=false for all active apps. Synthetic boolean fixtures must NOT be reused as admission tokens.

## First future VPS Storage Manager pilot (NOT EXECUTED)

Use exact commit and verified source tar.gz SHA-256 for vps-storage-manager; TAB 1 verify part01/storage_manager.py serve startup command; candidate 127.0.0.1:8149 is UNRESERVED until Agent 1 listener/ownership readback. First perform offline /tmp shadow transaction (tests included). After independent external gates and separately scoped owner approval, Agent 1 Drive Bridge V3 must implement a reviewed *different production-safe adapter* for an isolated non-root shadow release directory and distinct data tree, never touching protected legacy /opt/vps-storage-manager and /var/lib/vps-storage-manager, no public routing and no queue imports. Measure pre/post peak bytes, verify positive/negative auth health, no unwanted legacy service effects, and prove candidate-only rollback. Global flags remain OFF.

Exact admission fields: app_id, source_sha, verified_dependencies, sha256 manifest, loopback_only, shadow_port, occupied_ports, backup_restore_proven, capacity_current, runtime_verified, auth_negative_tested, worker_ownership_verified, rollback_proven, port_ownership_verified, security_reviewed, free_bytes, peak_new_bytes, host_reserve_bytes, rollback_bytes.

Future VPS work must use the locked Agent 1 canonical Drive Bridge V3, with execution packages only in Automation_Master_System/VPS_1/01_COMMANDS/Agent 1/PACKAGES/ and reports only in VPS_1/02_REPORTS/. No Bridge job was created by this source-only PR.
