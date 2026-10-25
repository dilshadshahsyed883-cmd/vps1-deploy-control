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

## Second subtab — Self Media Library (2026-10-20)

- Experiment Studio sidebar remains the single parent destination `/experiment-studio/`.
- Subtabs ordered **Antidetect Browser** then **Self Media Library**.
- The second navigable entry `/experiment-studio/self-media-library/` is Scene-auth-guarded and redirects (303) to the existing independent app at `/self-media-library/`, without an iframe or new app backend.
- The independent Self Media Library HTML loads the shared native Scene shell as workspace `library`. The canonical left sidebar highlights Experiment Studio, and its native Experiment Studio tab rail highlights Self Media Library. Returning to Antidetect Browser is a normal same-origin link.
- Self Media Library's existing five inner controls (Dashboard, Input, Process, Library & Preview, API & Settings) remain in a horizontally scrollable app-local rail. Original metadata, Drive settings, worker, queues and app-specific API remain unchanged.
- Shared sidebar Admin link remains visible only after positive Scene session identity. All unrelated native app layouts remain untouched by scoped CSS.
- New standalone production workflow `vps1-experiment-library-nav-release.yml` stages Scene Builder then Self Media Library under existing authorized Tailscale identity, with idle preflight, local source/HTML readback, app-only rollback, and anonymous gateway controls. Only triggered by merge message marker `[vps1-experiment-library-nav-release]`.
- Automated tests: `python3 -m unittest production.test_unified_sidebar_navigation tests.test_self_media_library_navigation -v`, `node --test tests/test_scene_native_sidebar_role.js`; browser owner acceptance remains separate.

## Third subtab — Auto Script (2026-10-25)

- Experiment Studio navigation now has three subtabs: Antidetect Browser, Self Media Library, Auto Script.
- The authenticated URL `/experiment-studio/auto-script/` redirects (303) to the already-deployed `/auto-script` UI. It returns 404 if the fresh runtime is not explicitly enabled. No iframe, duplicate scheduler, new database, or changed API.
- The existing Auto Script UI uses the Experiment Studio shared header/sidebar and highlights Auto Script in its subtab rail. SML's independent native rail links to the same route; the SML HTML cache-busts only the shared navigation script.
- Existing data, channels, queue, credentials, auth and scheduler are not modified by this navigation work. Two independent apps (Scene and SML) are the only guarded release targets, with SML idle preflight and app-specific rollback.
- Release on protected main only with `[vps1-experiment-auto-script-nav]` and confirm public JS/readback and authenticated UI status. Offline tests exercise native and legacy shell navigation.
