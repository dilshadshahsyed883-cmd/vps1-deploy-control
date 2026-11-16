# Imagen & Voice Clone GPU Config HTTP 403 — Scene-only guarded release

Date: 2026-11-16

## Verified source
- Bugfix merged to protected `main` by PR #756, merge SHA `a1dc625f4af2f446231dbc5b256b625c99efddde`.
- Candidate write guard now admits only exact owner-scoped `POST /api/experiment-gpu/{imagen,voice-clone}/{config,jobs}` and Imagen job cancellation with a validated ID.
- Existing session authentication, active account checks, owner isolation, studio permission checks and independent CSRF verification remain enforced.
- Prior PR #756 completed 31 successful checks, 2 intentional skips, 0 failures, including Experiment GPU Controls CI.
- No changes to Salad images, access tokens, Drive Bridge, Cloud Render/Concat, HeyGen or unrelated studios.

## Deployment intent
Use the existing protected-main `VPS1 production applications` workflow, with *only* `scene-builder` selected. The release commit's `[vps1-production-release:scene-builder]` marker activates the exact-source, protected Tailscale/SSH deployment helper, serialized host operations and rollback checks.

Release is not evidence of live authenticated user GPU inference. After guarded deployment, the owner should save rotated API and gateway keys in Imagen GPU Config, run Test Connection, and confirm one image-generation job and output. Secrets must not be committed or logged.

## Safety and acceptance
- Keep Auto Script offline and the other application routes unchanged.
- If guarded deploy fails, do not claim the 403 is resolved on production; use the existing rollback procedure.
- Expected UI: `Save GPU Config` succeeds instead of candidate-write HTTP 403 for an authenticated owner with a valid per-session CSRF token.
