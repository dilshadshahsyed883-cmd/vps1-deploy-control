# VPS_1 — Auto Script status correction (29 October 2026)

**Current owner-reported status: ONLINE.** The owner explicitly confirmed that Auto Script has been online for some time. The protected activation source was merged as PR #314 on 25 October 2026, followed by the Experiment Studio Auto Script navigation PR #315. Do not treat old `Auto Script OFFLINE` documentation as today's service state.

## Operational rule
- Preserve the existing Auto Script runtime, activation, scheduler, queue, SQLite data, credentials and authenticated UI through unrelated VPS_1 app deployments.
- Do not stop, disable, re-install, replace, or restart Auto Script as part of an Audio Downloader, Song Maker, Song Uploader, Scene, SML, or fresh-rebuild documentation update. Follow normal production safety gates and isolated rollback.
- `rebuild/groups.json` deliberately retains `deployment_mode: offline` and `offline_applications: ["auto-script"]` **only for legacy fresh-rebuild exclusion**. These configuration values should not be interpreted as the live service state. Changing them would expand migration/deployment scope and is NOT authorized by the documentation correction.
- Names such as `offline-pilot`, offline CI tests and historical deployment logs are test identifiers or immutable as-at records, not assertions about current Auto Script runtime.

## Evidence / confidence
- Owner: ONLINE, 29 October 2026 (current operational status source).
- PR #314 (merged 25 October 2026): scoped online activation, gated health/scheduler/rollback logic.
- PR #315 (merged 25 October 2026): authenticated Experiment Studio Auto Script navigation.
- This documentation update does **not** independently verify the present live scheduler, production API, or active process. For an operational release, run a read-only authenticated health check and preserve any existing active jobs.

## Supersession
This record supersedes any older *current directive* saying "Auto Script OFFLINE" in the README, Master Progress document, Song Studio/Song Maker requirements, and newly generated prompts after 25 October 2026. Preserve the exact original words in **dated historical incident reports, workflow names, old PRs and CI evidence**, which document prior states. Future instructions must refer to Auto Script as ONLINE unless current production evidence/owner instructions explicitly change that.

GitHub: https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/pull/314 and https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/pull/315
