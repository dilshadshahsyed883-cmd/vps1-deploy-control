# Scene → Concat Web SSO issuer — review-only

Scope: VPS_1 issue #772; Concat Web PR #6. Scene source: apps/scene-builder/part01/concat_sso_issuer.py. **No merge, deployment or production changes.**

## Exact protocol agreed with Agent 5

1. Concat GET https://concat.scenebuilder.online/auth/scene/start generates the Secure HttpOnly host-only state cookie and redirects to Scene GET https://app.scenebuilder.online/concat-sso/authorize with exact client_id=concat-web, pinned redirect_uri=https://concat.scenebuilder.online/auth/scene/callback, and 32-200-character URL-safe state.
2. The Scene endpoint requires the signed existing Scene session cookie, authenticated active user, canonical username and exact app.scenebuilder.online Host. Existing /login?next=... safe-login redirect preserves resume. It rejects unknown clients, extra/duplicate query keys, arbitrary callback URLs, malformed state, inactive account and legacy ownerless __default__ identity.
3. Existing Scene login generates a random 256-bit scene_sid, stores its SHA-256 in Scene web.sqlite3 table concat_sso_sessions(sid_hash TEXT PRIMARY KEY, username_key TEXT, expires_at INTEGER, revoked_at INTEGER NULL), and places the raw SID only inside its signed Scene cookie. Existing signed pre-issuer Scene cookies can be enrolled once without exposing identity headers. Invalid/expired/revoked SIDs cannot silently re-enroll.
4. Ticket is base64url(compact JSON) + dot + base64url(HMAC_SHA256("scene-concat-v1." + encoded JSON, CONCAT_SCENE_SSO_SECRET)). Claims: v=1, iss=https://app.scenebuilder.online, aud=concat-web, sub=<canonical Scene username>, sid=<Scene SID>, state=<exact opaque state>, jti=<fresh 256-bit unique token>, iat=<seconds>, exp=iat+60. No cached is_admin or grant claims. The independent secret must be at least 32 UTF-8 bytes, never SESSION_SECRET or WEB_ACCESS_KEY.
5. Redirect is always 303 to pinned Concat callback with ticket and state query params, Cache-Control: private, no-store and Referrer-Policy: no-referrer. Concat PR #6 checks signature/state/claims, live Scene users/roles and SID hash, Concat-local grants, and atomically consumes JTI exactly once. Concat requests continue to recheck Scene session and grant. A signed ticket by itself never authorizes jobs, media, or uploads.
6. Scene logout, re-login, account disable/delete, and inactive-session handling revoke SID. Rows expire at most 12h after issuance; existing signed Scene cookie lifespan and Cloud Render admin gate remain unchanged. A role demotion is reflected in live Scene users, not trusted from the ticket. Concat grants belong in Concat's private DB; no duplicate grants are stored by Scene.

## Hard blockers prior to any production rollout

- Provision a separate random CONCAT_SCENE_SSO_SECRET in both services outside source control; verify unchanged Scene SESSION_SECRET. SSO endpoint fails closed without it.
- Safely provision a dedicated Concat service account with *read-only* access to the existing /opt/scene-builder-web/data/web.sqlite3 and any required WAL/SHM visibility; verify actual SQLite mode=ro account and SID queries. No copied Scene passwords, no writable access from Concat, no runtime cross-repository imports.
- **Prevent ticket and state query disclosure**: default Nginx request access logging often includes full query strings. On both Scene /concat-sso/authorize and Concat /auth/scene/callback, use sanitizing log format based on path-only URI (not full request/query/Location), or disable those sensitive-path access logs; disable upstream tracing of the URL and Location header. Check configured logging paths in real preproduction before enabling public ingress. This PR intentionally does not alter production Nginx.
- Run Scene schema migration at startup, verify active session signing, Host forwarding, secure cookie settings, legacy login, and unchanged Cloud Render/Studio route guards on staging. Plan rollback that leaves original Scene authentication, source database and production services intact.
- Agent 5 and integration agent must verify real two-account login to callback, forged ticket/state, replay, Scene logout revocation, inactive and demoted users, role reactivation, Concat access grant removal, CSRF, ownerless/foreign jobs and upload media, and SQLite read-only behavior. Until then Concat Web must fail closed; isolated issuer unit tests do not prove end-to-end security.

## Offline checks

python -m unittest discover -s tests -p test_concat_scene_sso_issuer.py -v

python -m unittest discover -s tests -p test_scene_user_roles.py -v

python -m unittest discover -s tests -p test_scene_session_sso.py -v

Focused CI: .github/workflows/vps1-concat-sso-issuer-ci.yml. No Nginx, DNS, Cloud Render, Salad GPU, Drive Bridge, OAuth, worker, or deployment changes.
