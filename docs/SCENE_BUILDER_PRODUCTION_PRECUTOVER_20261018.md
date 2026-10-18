# Scene Builder Production Implementation — Pre-cutover (18 Oct 2026)

**Status: GITHUB IMPLEMENTATION / OFFLINE CI ONLY. No public release authorized.**

## Verified starting point
- Storage Manager: `vps1-storage-candidate.service`, `127.0.0.1:8149`, Agent 1 S1P1 PASS.
- Scene Builder: `vps1-scene-candidate.service`, `127.0.0.1:18080`, Agent 1 S2H1 PASS **read-only**.
- GitHub protected-main source for successful Scene pilot: `608ffe99293c8aaca400a8c328e05f7090ff8b9c`.
- No Nginx/TLS, FFmpeg, external worker or public website release has been verified.
- Auto Script remains OFFLINE; no legacy database/queue/media imports.

## New capability (disabled by default)
- `VPS1_SCENE_PRECUTOVER_EDITING=0` (default) preserves existing strict read-only candidate behavior.
- After a **separately approved** service update, `=1` allows only the login/logout form and user custom-preset create/duplicate/rename. Authentication middleware and HTTPS-only session cookies still apply. Normal SessionMiddleware rejects unauthorized access to preset mutations.
- HTTP POST to queues, media processor, Storage Manager cleanup/service activation, users, global settings and project operations remains rejected with HTTP 403 before backend handlers. `DELETE`/`PUT` also remain denied. Runtime never starts Google outbox, Script Work extraction, Script Processor, or Auto Script worker loops in candidate mode.
- The feature gate **cannot** operate outside `VPS1_RUNTIME_CANDIDATE=1`. Guard requires non-root, loopback `18080`, strong credentials, absolute isolated state and `FRESH_AUTO_SCRIPT_READY=0`; no state in legacy roots.
- CI executes real HTTPS-cookie login, preset CRUD subset, unauthorized redirects, runtime probe allow/deny and denied side effects with a fresh CI-only SQLite state.

## Next implementation and release gates (explicit approvals, no defaults)
1. **Internal controlled editing upgrade**: Review exact merged commit, app-only rollback, isolated SQLite snapshot/restore, existing local 18080 service, known admin bootstrap method, authenticated HTTPS session path. Owner authorization required to change existing service and environment.
2. **Job engine & workers**: Map actual worker and Chrome/FFmpeg/Drive dependencies; independent job ownership, queuing, authorization, retries and rollback/backup tests. **Never** enable workers merely by switching off candidate mode. Requires separate owner approval.
3. **Public `app.scenebuilder.online` cutover**: Verify domain/DNS, web certificate, Nginx `Host`, TLS and WebSocket, auth routing, firewall exposure and recovery plan; document app-only rollback and independent end-user browser smoke. **Separate explicit public-cutover approval** required.
4. **Functional end-to-end**: Only claim full production after actual login, saved editor/preset, queue submission, worker processing, media output, external integrations and recoverability tests PASS on a fully authorized stack.

Do **not** disable any of the seven credential-consuming GitHub workflows or modify protected-main release gates. Keep source immutable via GitHub; Agent 1 Bridge V3 is the only approved channel for on-host actions, and existing installations must not be blindly reinstalled.

**Security note:** The existing S2H1 installer generated a random initial admin key exclusively on VPS and did not expose it in GitHub or reports. A secure owner credential provisioning/handoff method must be implemented and verified before public login/cutover. Do not print secrets to logs, Drive reports or PRs.
