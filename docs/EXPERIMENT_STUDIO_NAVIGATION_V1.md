# Experiment Studio — Navigation V1 (2026-10-20)

Requested navigation:
- Sidebar main tab: **Experiment Studio**, URL `/experiment-studio/`.
- First subtab: **Antidetect Browser**, URL `/experiment-studio/antidetect-browser/`.
- Existing shared sidebar owns the link, active state, and role-aware Admin menu.
- Scene session authentication remains required for both endpoints.
- Subtab uses the shared responsive design tokens; no new backend services.
- The landing route redirects to the first subtab, and the subtab renders an honest UI-only workspace.
- Antidetect browser/profile engine is **not installed or active**. Never claim profile, proxy, fingerprint, or automation functions are working yet.
- No modifications to other studio queues, sessions, OAuth, storage, or Auto Script (stays OFFLINE).

Offline checks:
`python3 -m unittest production.test_unified_sidebar_navigation -v`
`node --test tests/test_scene_native_sidebar_role.js`

Deployment must use the existing guarded GitHub → Tailscale release path; production verification separate.
