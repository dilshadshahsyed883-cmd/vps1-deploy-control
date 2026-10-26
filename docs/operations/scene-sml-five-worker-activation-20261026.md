# Scene Builder — activate merged SML five-download worker

Release intent date: 2026-10-26.
Source feature: merged PR #330, commit `0fadbf9899bc0a1ab8a183db6122201ce4d27dc9`.
Target application: `scene-builder` only, under the current protected `main` source revision.

The validated `apps/scene-builder/part02/api_server.py` already includes bounded
`SCENE_SML_DOWNLOAD_WORKERS`, `_SMLParallelReservations`, and
`_parallel_sml_scene_download`. Do not change that validated implementation.

This is a release-intent-only change to trigger the existing protected-main
Scene production release with the existing Tailscale SSH helper and transactional
source-refresh implementation. Unlike the preceding web-policy-only release,
this release deliberately exercises the existing independent writable worker
source refresh; no web-only intent marker is used.

Acceptance evidence from release logs: `SCENE_WRITABLE_WORKER_SOURCE=REFRESHED`
or a matching already-current worker release, with Scene worker health,
real FFmpeg render proof and rollback guard intact. If queued/in-flight jobs
block source refresh, defer without restarting or clearing them. Never touch
other apps, approved media, SML masters, credentials or the existing user queue.
