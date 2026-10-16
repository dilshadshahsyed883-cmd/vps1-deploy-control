# VPS_1 Tab 3 — Application & deployment compatibility (16 October 2026)

Base: migration/fresh-vps-11groups-20261015 @ `93a50f9556223efe41b12211cac5f57231cf18be`.
Review scope: GitHub-only. No Agent 1 package, VPS command, production edit or main merge.

## Scope and acceptance
- Ten active app groups have matching source, group ownership, install profiles, and fail-closed deployment contracts. Auto Script is OFFLINE (source preserved).
- A single app-owned `apps/<app-id>/` source change selects only that app for release-artifact work. Shared/infrastructure/control-plane changes prohibit automatic promotion and require a broader review. A source artifact is NOT a deployed web service.
- Per-app runtime health, workers, persistent application databases/queues, authenticated routes, uploads, external APIs and actual rollback are NOT proven by a successful synthetic workflow.
- GitHub `rebuild/deploy-contracts.json` currently marks **all** `runtime_verified=false`, `health.enabled=false`, automatic migrations disabled, and pending worker lists empty. These are honest blockers, not automatic install instructions.
- `infrastructure/registry/apps.json` has **zero** confirmed live entries; install-profile ports are proposed candidate ports, never authoritative live endpoints.

## Per-app compatibility matrix

| App | Source/profile | Known proposed service | Outstanding deployment contract | Independent release |
| --- | --- | --- | --- | --- |
| Scene Builder | part01 / `app:app` | proposed 8080 | Optional Algrow Voice/Auto Script coupling; session/static/prefix; current DB and worker ownership | Source CI yes; runtime blocked |
| Script Work | part01 `start.sh` / no service declared | unknown | Chrome/Xvfb lifecycle, user auth, sessions, shared Scene Builder ownership, WebSockets if used | Source CI yes; runtime blocked |
| Voice Studio | part01 / `vps1_omnivoice_app:app` | proposed 8143 | OmniVoice/Kokoro/Edge workers, models, voice endpoints, async queues and auth | Source CI yes; runtime blocked |
| Auto Caption Studio | part01 / `web_backend.app:app` | proposed 8144 | Render/prefetch/compress workers, queue/DB migration and storage, media uploads | Source CI yes; runtime blocked |
| Local Upload Manager | part01 / `lum_engine.standalone_app:app` | proposed 8015 | Engine2/3 workers, schedulers, OAuth/token persistence, DB state | Source CI yes; runtime blocked |
| Short Stream | part01 / `app:app` | proposed 8145 | FFmpeg/live publisher, streaming auth, WebSocket/media route and worker ownership | Source CI yes; runtime blocked |
| Short Maker | part01 / `app:app` | proposed 8146 | Render queue, FFmpeg, Google Drive delivery and persistent job data | Source CI yes; runtime blocked |
| Video Merger | part01 / `app.py` script | proposed 8148 | External Drive/media worker integration; startup and auth | Source CI yes; runtime blocked |
| Self Media Library | part01 / `app:app` | proposed 8147 | SQLite schema/bootstrap, real file paths, Drive credentials, uploads | Source CI yes; runtime blocked |
| VPS Storage Manager | part01 / `storage_manager.py serve` script | proposed 8149 | Scoped admin credentials and storage privileges, deletion/backup safety | Source CI yes; runtime blocked |

No proposed port was checked against the real VPS here. The known nine observed listeners and seven proposed-port mismatches remain a **Tab 2** reconciliation task; Script Work is the additional unresolved process owner.

## Browser and reverse proxy contract
- Shared shell is `/`, shell assets `/shell-assets/`, and app candidate prefix is `/<app-id>/`; site-level Basic Auth and each app's authorization must both be verified.
- Validate public URL, root-relative `/static/` assets, proxy-prefix preserve/strip, cookies/redirect URLs, CSRF, same-origin iframe framing policy, upstream WebSockets and streaming requests app-by-app. Do not assume iframe styling reaches each UI.
- Do not register or enable routes until Tab 2 provides read-only verified paths and safe port mappings.

## Durable queue safety review and scoped fix
- Prior issue: `enqueue_verified` inserted a bare QUEUED row via `enqueue` and then attached its artifact digest in a separate transaction. A concurrent backend could claim that intermediate row as unverified, creating a false FAILED record. New code inserts verified artifact digest atomically with the queue intent and refuses retroactive verification of already processed unverified jobs.
- Prior issue: `Engine.once` could return VERIFIED solely because `backend.activate()` returned without throwing. New code independently checks the backend's active Git SHA and health after activation; mismatch/unhealthy freezes at NEEDS_ATTENTION.
- Additional tests cover all ten app change classifiers, shell OFFLINE/route state, all runtime gates, atomic intake, processed-intent safety, and mismatched/unhealthy active releases.
- This does NOT supply a production backend, host-wide persistent VPS queue handoff, independent provenance verification or actual service/worker rollback; those remain blockers.

## Storage compatibility
- Single system Python 3.12 plus app-specific venvs; shared hardlinked uv cache on the same filesystem; one FFmpeg; shared models only for checksum/version/license-compatible assets.
- Admission target 30,000,000,000 bytes, preserving at least 5,000,000,000 free bytes. Planner does not enforce an ongoing hard quota or demonstrate ten real apps fit; Chrome/heavy models and dynamic uploads still require measured live capacity.
- Databases, upload directories, models, external/mounted data and SQLite queues must not be automatically deleted or overwritten.

## Tab 2 handoff: read-only evidence to return
1. Mapping of nine observed listeners to live systemd/process owners, the seven mismatched profile port proposals, and Script Work's Chrome/Xvfb ownership; return sanitized exact file locations and the Agent 1 evidence report ID.
2. Nginx server/location/routes, site/app auth, WS and static prefix behavior; app-specific real health/API smoke endpoints, legacy cookie/redirect policies and iframe policy.
3. Per-app DB file paths, persistent SQLite queue location, uploads/Drive spool, workers/timers, users/service environment, model paths, data-retention and rollback requirements. Do not disclose secrets.
4. Actual disk `df`/application `du`/uv/model/Chrome measurements, filesystem identity, 30GB budget remaining, 5GB reserve, snapshot/restore and external backup evidence.
5. No deployment or mutation from this Tab 3 PR; use the preexisting locked Agent 1 bridge and canonical `Automation_Master_System/VPS_1/02_REPORTS/` evidence, not a new independently executed package.

## Validation
CI: `python -m unittest discover -s rebuild/tests -v` (includes the new source-only guards), plus existing `vps1-deploy-engine-ci.yml` Python 3.11/3.12 matrix. GitHub Actions status for this feature head must be verified separately; test code being committed is not evidence of PASS.

**Release decision:** GitHub source-level compatibility is testable; real deployment of all ten apps remains **BLOCKED** by incomplete runtime ownership, route registry, full storage measurements, and verified snapshot/restore and rollback evidence. Preserve Auto Script OFFLINE; do not merge to main.
