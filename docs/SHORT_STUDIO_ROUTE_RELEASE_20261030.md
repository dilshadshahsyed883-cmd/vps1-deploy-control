# VPS_1 — Short Studio HTTPS route guarded release (30 October 2026)

## Incident
Owner's authenticated browser at `https://app.scenebuilder.online/short-uploader` returned FastAPI `{"detail":"Not Found"}` on 30 October 2026. Source PR #399 is already merged at `098670d2459e64fbec66b3282d10787811735d9f`, but that squash merge was explicitly source-only: its commit message did not include the exact `[vps1-short-studio-release]` selector, so source merge alone did not schedule the guarded Short Studio production route installation.

## Release objective and exact mechanism
Trigger the **already-reviewed**, existing `.github/workflows/vps1-short-studio-release.yml` on a **protected main** squash merge with exact commit-title selector `[vps1-short-studio-release]`. Do not alter its Tailscale identity, approved SSH command, environment, host-wide serialized app release gate, idle checks, backup, or rollback. Do not trigger directly from a feature branch.

The selected workflow sequentially invokes the existing `vps1-production-deploy install <immutable main SHA> <app>` helper for:
1. `local-upload-manager` — provision independent `lum-shortstudio` web/worker/scheduler on loopback 8019 (scheduling stays OFF until authorized configuration); install authenticated/reversible Nginx `/short-uploader` -> `/short-uploader/` redirect and `/short-uploader/` reverse proxy using the existing Scene session auth; verify public anonymous 401 and authorized HTML 200.
2. `short-maker` — preserve the existing project/render queue and canonical `/short-maker` service; refuse non-idle upgrade.
3. `video-merger` — preserve workers/imports, refuse non-idle upgrade.
4. `scene-builder` — publish the coordinated Song/Short/Experiment navigation and canonical Song Uploader route only through guarded app-scoped release.

## Release acceptance (do not mark completed without proof)
- GitHub required PR checks pass and protected main is preserved.
- Recorded workflow exists and all four installer steps report successful scoped installs. On busy queues, drift, unreachable host, failed auth, unhealthy service, disk reserve or credential issues: fail closed and leave existing production untouched; report exact blocker.
- HTTPS `GET /short-uploader` returns expected 308 redirect to `/short-uploader/` without returning the owner's previous JSON 404.
- Anonymous `/short-uploader/app` and `/short-uploader/health` are denied; authorized `/short-uploader/app?tab=auto_engine` loads real Short Uploader HTML with correct application identity.
- On mobile and desktop, Short Studio lists Short Maker and Short Uploader; Short Maker existing drafts, queue, and route remain preserved.
- Uploader uses **isolated** data, OAuth, scheduler, workers and the Short Auto Engine workflow (thumbnail optional in this Short Studio instance); do not log in channels, activate schedule, or publish YouTube videos as part of release acceptance.
- Confirm Song Studio and Experiment Studio navigation are intact. Preserve current Auto Script runtime unchanged; do not toggle its state.
- Record commit SHA, workflow run and live readback separately. A GitHub merge or successful offline CI alone is **not** production success.

## Coordination
Only one host-wide production release at a time. Other active Audio Downloader/Song Maker/Song Uploader release efforts must keep their own separate idle and service boundaries. The owner screenshot authorizes resolving this missing Short Uploader page, **not** YouTube publishing or overriding any production safety fence.
