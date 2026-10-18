# VPS_1 — Verified five-route HTTPS release (19 October 2026 local)

**Release status: HTTPS routing LIVE / PASS.** This records successful automated deployment and authenticated API/UI checks, **not owner functional UAT**. No application release, queue, data, or Auto Script change is implied.

## Evidence of successful execution

- Protected main release SHA: `38c1a07a07a6a9dade2d8cfafe6ca6d6c85e0ed8`.
- [GitHub Actions run #39167302988](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/actions/runs/39167302988): **completed / success**; VPS job `117792817922`; `FIVE_ROUTE_CUTOVER=PASS`.
- VPS_1 Agent 1 helper upgrade `VPS1_AGENT1_REMOTE_DEPLOY_HELPER_UPGRADE_20261019_U1H1`: PASS; exact root-owned helper Git blob `21f69a74f909278a70356c74e625570c2fcf93f8`; [Drive evidence](https://drive.google.com/file/d/1TmQH7AYGLT8bq7O2KRWe86_2aRbZFb0n/view).
- Checked after all Nginx reloads: anonymous requests are denied with HTTP 401; requests with existing on-host workspace credentials yield HTTP 200 and healthy JSON or HTML, over trusted signed TLS/SNI.
- Existing Scene login HTTP 200, health, private endpoint denial, Voice Studio, and Auto Caption Studio protections preserved.
- `OWNER_FUNCTIONAL_UAT=PENDING`. No secrets, session tokens or credentials stored in GitHub.
- The [earlier failed attempt](https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/actions/runs/39166757775) rolled back its Short Maker change; corrected bounded Nginx handover passed in the successful release.

## HTTPS applications

| Route | Runtime loopback port | Deployment result |
| --- | ---: | --- |
| [Short Maker](https://app.scenebuilder.online/short-maker/) | 8146 | New route PASS |
| [Short Stream](https://app.scenebuilder.online/short-stream/) | 8145 | New route PASS |
| [Self Media Library](https://app.scenebuilder.online/self-media-library/) | 8147 | New route PASS |
| [Video Merger](https://app.scenebuilder.online/video-merger/) | 8148 | New route PASS |
| [Local Upload Manager](https://app.scenebuilder.online/local-upload-manager/) | 8015 | New route PASS |
| [Voice Studio](https://app.scenebuilder.online/voice-studio/) | 8143 | Existing route preserved; verified |
| [Auto Caption Studio](https://app.scenebuilder.online/auto-caption-studio/) | 8144 | Existing route preserved; verified |

**Scene Builder:** [https://app.scenebuilder.online/](https://app.scenebuilder.online/), login `/login` HTTP 200, health JSON PASS. **Storage Manager:** internal loopback service on port 8149; not claimed as a separately published public route.

## Scope and controls

The root-owned helper made Nginx-only app-scoped changes with strict ownership guards, atomic site and snippet writes, Nginx syntax checks, graceful reloads and app-specific automatic rollback. The GitHub workflow published one app at a time, then rechecked each route before checking the pre-existing two. It did not restart app services, change SQLite databases, touch media, or enable Auto Script.

**Source of truth for verifiable route claims:** `production/verified_https_routes.json`. This is a historical evidence record pinned to the successful production run. Do not overwrite it with a future unverified runtime assumption. The unrelated `infrastructure/registry/apps.json` is an offline design registry with different admission semantics and must not be automatically converted into a live-app registry.

## Remaining separate acceptance

The owner will test the actual browser workflows and external integrations (editing, rendering, stream destinations, voice outputs, OAuth/channel operations and uploads). Those features are **not** covered by the verified HTTPS route smoke checks; keep their status `PENDING_UAT` rather than claiming full functional completion.
