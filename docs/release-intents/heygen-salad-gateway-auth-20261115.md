# HeyGen Studio — guarded SaladCloud gateway authentication release

Date: 2026-11-15
Source feature: PR #744, merged commit `1255ce7804276a0c0bb85126e9cd542f808f3140`

## Intended production change
Release the HeyGen Studio Scene Builder bridge support for SaladCloud
`Salad-Api-Key` gateway authentication independently of the application
`AVATAR_API_KEY` Bearer token. No GPU image or worker settings are changed.

This is a Scene Builder scoped deployment only. It preserves existing
per-owner saved endpoints and tokens via SQLite schema migration; retained keys
are never returned to browsers. Host allowlisting, session authentication,
CSRF protection, queue data and media remain unchanged.

## Gate and rollback
Use the existing `VPS1 HeyGen Studio guarded production release` workflow
triggered only by a main-branch commit with
`[vps1-heygen-studio-release]`. The workflow pins the exact source SHA,
deploys scene-builder only, validates public static assets and protected
anonymous API access, and attempts rollback on failure. No secrets are
committed or echoed.

Acceptance of this code change does not prove the Salad protected endpoint is
already reachable: the VPS must separately permit the exact SaladCloud domain
in `HEYGEN_WORKER_ALLOWED_HOSTS` or `HEYGEN_WORKER_URL`, and the owner must
provide the correct private Salad gateway key via the HeyGen GPU Config UI.
Live authenticated worker validation and GPU MP4 rendering remain separate.
