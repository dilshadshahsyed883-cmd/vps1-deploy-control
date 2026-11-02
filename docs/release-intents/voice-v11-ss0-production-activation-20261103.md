# Voice Studio Frozen V1/V1.1 — protected production activation

Status: release-intent only; no application-source changes or manual VPS commands.

## Evidence and owner authorization

- Owner approved guarded PR #633 and frozen Voice V1/V1.1; current request is to proceed with the remaining approved production activation, not to modify the frozen plan.
- Live user-provided production audit: `vps1-production-voice-studio.service` active; `127.0.0.1:8143` listening; `/healthz` HTTP 200; public anonymous `/voice-studio/service/tts/api/kokoro/status` HTTP 401.
- Live pointer is the older `f3156fc3...-voice-recovery` overlay. Its `part01/vps1_omnivoice_app.py` has **no** `voice_session_and_permissions` / `scene_identity` marker. Anonymous direct-localhost Kokoro/Edge/clone status endpoints returned 200. Public gateway alone does **not** establish upstream owner isolation.
- Canonical protected-main `apps/voice-studio/part01/vps1_omnivoice_app.py` implements live Scene session middleware for all non-health routes, allowed-studio grants, and request owner context; the corresponding `voice_tenant.py` uses canonical Scene loopback identity. Deploy the approved **complete** Voice source rather than patch an unverified old bundle in place.

## One narrowly scoped release

- Only approved trigger: `[vps1-production-release:voice-studio]`, which selects `APP_SELECTION=voice-studio` in `.github/workflows/vps1-production.yml`. No `[vps1-song-studio-release]`, independent Song cutover, generic `all`, or Scene release marker.
- Protected GitHub main + required workflows first; Tailscale SSH authenticated actor, host lock/serialized deploy, identity and source checks, all-owner active-job preflight, SQLite/WAL backups, immutable release pointer, Voice-only service health and rollback.
- Candidate runtime must have matching `voice_tenant.py`, Kokoro, Edge, OmniVoice modules, HTML, and the approved per-user backend; no changes to Song files or Google Drive originals, credentials, queues, Auto Script OFFLINE.
- If Song Audio independent worker is not verified after the failed cutover, retain the fail-closed Voice queue preflight. Never force the release, edit handoff flags or delete history.
- Verify resulting immutable current pointer, localhost 8143 health, **direct anonymous API returns 401/403** (not just gateway denial), signed-in Scene SSO route and admin/non-admin isolation. Examine all 3 engine status endpoints. OmniVoice worker not configured in previous read-only audit; do not silently claim it is online or set credentials.
- Roll back on any missing auth, startup error or unexpected health failure. No production success assertion without completed GitHub release receipts.
- Run real admin and non-admin acceptance only after successful guarded production activation.

*Document is source control release intent and evidence; it does not change Frozen V1/V1.1 product behavior.*
