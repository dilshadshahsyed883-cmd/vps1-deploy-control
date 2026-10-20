# Experiment Studio — Browser Engine V1 (2026-10-20)

A real Chromium-based engine runs behind the existing Experiment Studio > Antidetect Browser tab, using the Scene service already authenticated on VPS_1.

## Implemented
- Profile create/list/delete isolated by the logged-in Scene username.
- Persistent Chrome profiles store their independent cookies, local storage, and browsing data only under WEB_DATA_DIR/experiment-browser.
- Optional per-profile HTTP, HTTPS, or SOCKS5 proxy (authenticated SOCKS5 is excluded); secrets never returned by list APIs.
- Configurable locale, IANA timezone, viewport, optional user agent, initial URL.
- Start/stop, navigation, image preview, click/type/keyboard/scroll/reload/back controls.
- Private Playwright pipe rather than public CDP/VNC; per-user authorization and session-bound CSRF on mutations.
- Max 24 profiles per user, 2 simultaneous browser instances globally, stop after 30 min idle when next queried.
- No alterations to existing Scene/Video/Voice/Upload/Auto Script jobs or their storage.
- Chrome startup and missing runtime dependencies are reported as explicit errors, never a fake success.

## Boundaries
This is a usable browser isolation/profile manager, not equivalent to ixBrowser's proprietary engine or a guaranteed undetectable browser. No synthetic fingerprint camouflage is claimed. Default Chrome/Playwright headless behavior may be detectable; avoid using this to circumvent website policies. Credential/password fields should be managed only through the existing authenticated site.

## Verification & rollout
- Browser profile/URL policy unit tests and narrow candidate-write policy tests.
- Existing guarded CI; guarded GitHub Actions > Tailscale > Scene candidate release, with rollback.
- Report Chrome executable and Playwright startup status independently of route availability.
- Owner's authenticated visual UAT remains a separate step.


## CW-028 / EXP-PRIV-001 — Privacy V2 (scoped implementation)

- Optional strict mode blocks service workers and constrains non-proxied WebRTC UDP; standard mode retains original compatibility. These controls are policies, **not** proof of DNS/WebRTC leak prevention.
- Per-profile configurable Do Not Track HTTP header, explicitly granted fixed-location geolocation, device pixel ratio, color scheme and reduced-motion preferences; all applied at persistent Chrome launch. Existing profiles are migrated in-place with standard defaults, preserving cookies and session data.
- Non-destructive profile-settings clone starts with a fresh empty Chrome directory. Proxy usernames and passwords and browsing state are intentionally not copied. CSRF and owner checks apply to all new mutation routes.
- Authenticated on-demand inspection reports actual navigator settings; the user-triggered exit-IP check loads `https://api.ipify.org?format=json` inside the selected browser context (via configured proxy when present). This discloses an IP to that external provider; it does **not** test DNS, IPv6 or WebRTC leaks.
- Safe limitations: no Canvas/WebGL/Audio fingerprint forgery, no cookies/extensions import-export, no mass profile jobs, no encrypted-at-rest migration for existing SQLite proxy credentials. Full ixBrowser parity remains **OPEN** and must not be advertised as complete. Never use these controls to circumvent third-party site policies.
- Acceptance: `python3 -m unittest tests.test_experiment_browser_engine` and existing release smoke, protected PR CI, guarded Scene-only deployment; no unrelated services, users, OAuth, media, Auto Script or active jobs affected.
