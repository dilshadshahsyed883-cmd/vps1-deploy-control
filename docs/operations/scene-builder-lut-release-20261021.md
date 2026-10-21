# Scene Builder built-in LUT repair — production release intent

- Date: 2026-10-21
- App: `scene-builder` only; VPS_1 existing protected GitHub/Tailscale production workflow.
- Prerequisites already merged: PR #240 (Whisper/PyAV compatibility), PR #247 (Scene worker writable HOME), PR #251 (23 built-in .cube LUT presets and real FFmpeg filter validation).
- Current blocker: PR #251 merged without a release marker; the production workflow skipped on that push.
- Trigger: merge this operations-only PR with commit title containing `[vps1-production-release:scene-builder]` after required checks pass.
- Scope: use the existing idempotent `vps1-production-deploy install <merge SHA> scene-builder` flow and its automatic rollback; no app code overwrite, no new credentials, no independent app deployment.
- Safety: installer preserves project/queue data and does not restart any failed job. Verify worker's 23 LUTs and `SCENE_001` real FFmpeg proof from the protected deployment log.
- Recovery: only after deployment pass should the affected owner resume the preserved failed project through existing authenticated Scene Builder **Resume** or **Restart Stage** control; never automatically reset the entire project.
- End-to-end success: a completed real render and Google Drive delivery are separate observations, not implied by this release marker.
