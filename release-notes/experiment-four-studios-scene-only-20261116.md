# Experiment Studio four-app guarded Scene release — 2026-11-16

Release target: production Scene Builder application only, using existing protected `vps1-production-deploy` helper and rollback. No Salad container or Docker image change.

Source code: merged PR #751, commit `856637e4ed2b5049c588dccfa59e5b0f44c5543b`.

User-approved UI changes:
- Rename the existing Cloud Render page to **Concat**, retaining its route `/cloud-render/`, APIs, queue data and canonical Drive Bridge.
- Keep **HeyGen Studio** owner-scoped and add its read-only GPU Models panel.
- Add **Imagen** owner-scoped production, generated image Gallery, jobs, models, and GPU Config.
- Add **Voice Clone** owner-scoped Qwen reference-WAV speech production, jobs, models, and GPU Config.
- Preserve Antidetect Browser and Upload Studio, other studios, login, permissions and all saved jobs.

Release eligibility: PR #751 completed 48/48 validation workflows including required offline-pilot and real Chromium visual audit. This release marker only authorizes the existing Scene Builder protected deploy lane, with its built-in health checks and rollback, on the merged commit.

Acceptance after release: the Scene service must pass standard production health, and protected navigation must show the approved six tabs. Worker readiness and GPU inference are separate from deploying VPS web UI; no auto image switch is authorized.