# VPS_1 Unified UI/UX Master Rules V1 — OWNER APPROVED (2026-10-20)

Status: **APPROVED; source-level enforcement rollout in progress.** This document is the canonical design/governance contract for website UI work in the existing VPS_1 deployment repository. It does not by itself certify every app's rendered layout.

## Golden architecture
**One Website Shell + Independent Application UIs.** The shared shell owns navigation, sidebar, top-level studio tabs, identity display, responsive breakpoints, global spacing, typography, theme and loading transitions. Each application's own code owns its forms, editing controls, specialized media displays, APIs, credentials, database, workers and jobs. Never merge application engines or use an outer iframe to imitate a single shell. Preserve Auto Script OFFLINE.

## Permanent rules (UI-01 to UI-10)
1. **Single ownership:** all shared layout variables and components have one canonical owner. Application CSS is scoped to app content and must not redefine global navigation or sidebar geometry.
2. **Design tokens and geometry:** one versioned token layer governs fonts, color, contrast, spacing, radii, controls, and viewport breakpoints. Current production-verified source baseline is 214px desktop sidebar, 1400px max Video Studio content width, 26px desktop top and 34px side video padding, 62px video tab rail, and 760px mobile breakpoint. A future migration to 32px uniform desktop content gutters or 48px tab controls must be coordinated across all apps, not patched individually. Mobile tab rails may scroll horizontally but must not wrap or shift.
3. **Canonical shared components:** use common nav tabs, buttons, cards, inputs, tables, tooltips, alerts, status badges, skeletons and empty/error states. App-specific complex editors can be bespoke, but keep global component appearance.
4. **Stable navigation:** shared sidebar must never change width/position during app switch. Studio group subtabs share identical x/y baselines at the same viewport. No duplicated sidebar, conflicting headings, or API/embedded page URL as the user-facing app location. Native internal tabs change in place; safe selections restore after refresh without restarting backend work.
5. **Responsive and accessible:** verify at 360, 390, 768, 1024 and 1440 CSS px, including 100% and 200% zoom where applicable. No overlap, clipping, unusable horizontal page overflow, broken keyboard tab order, inaccessible focus indication, or mislabeled controls. Honor reduced-motion settings; meet WCAG AA contrast targets.
6. **State and data honesty:** show authentic backend states, clear loading/retry, empty, partial and error modes. No fabricated metrics. Preserve jobs, OAuth, queues, uploaded media and history. Never put secrets in localStorage/sessionStorage; avoid duplicate submissions, unnecessary app reload and queue polling while hidden.
7. **CSS and JavaScript boundaries:** do not introduce duplicate hard-coded global dimensions, broad selectors overwriting every app's cards/forms, or escalating !important patches. Existing historical shims are compatibility debt to retire incrementally; each approved exception must have a reason and removal plan. Asset versioning must prevent mixed CSS/JS releases.
8. **GitHub UI Quality Gate:** every changed site shell or app UI must pass static design contracts AND (as authenticated-browser infrastructure becomes available) viewport screenshot/geometry comparison, navigation, mobile, accessibility and performance checks. Baselines may change only for consciously approved redesigns. UI contract is added to required offline-pilot workflow; production browser visual proof is a separate follow-on gate, not falsely asserted by source checks.
9. **Deploy/rollback:** GitHub-first PR -> tests -> guarded Tailscale app-scoped installation -> health/route readback -> browser UAT. Global shell releases require compatibility verification of all affected apps; app-only releases must not restart other app engines. Do not trigger production deployment for policy-only changes.
10. **Traceability:** record the issue, changed application, before/after screenshots or explicitly unverified visuals, screenshots' viewport, test results, PR and deployment SHA/links, and concise outcome in the matching VPS_1 Master Google Doc application subtab. Preserve document history. Owner performs final functional UAT after automated delivery.

## UI source ownership
- Current shared native shell: apps/scene-builder/part01/static/vps1_native_site_shell.css and vps1_native_site_shell.js.
- Current four-app Video Studio layout: apps/scene-builder/part01/static/vps1_video_studio_geometry.css.
- Scene template: apps/scene-builder/part01/templates/base.html.
- Native app adapters: independent ACS, Voice, Local Upload Manager, Short Maker, Video Merger and Auto Stream templates.
- Required, fail-closed *source* contract: production/ui_design_contract.py and tests/test_ui_design_governance.py (offline-pilot).
- Authenticated rendered geometry audit: production/ui_visual_audit.py. Run with a legitimate authenticated browser storage state on a protected machine, never commit a session or screenshots containing user data.

## Required UI PR checklist
- [ ] Shared shell change versus app-local change identified; list affected apps and ownership.
- [ ] No unauthorized global CSS overrides, duplicate navigation or hard-coded dimensions.
- [ ] Same viewport positioning across destination pages documented.
- [ ] Responsive/mobile, keyboard focus, high zoom, reduced motion and contrast tested.
- [ ] Tab switching, F5/back, login/role/route behaviour and absence of unintended full reload checked.
- [ ] Static contract and relevant application tests PASS; browser screenshot comparison attached or explicitly marked NOT VERIFIED.
- [ ] Queues, databases, credentials, OAuth, publishing, Auto Script and other app services untouched unless expressly approved.
- [ ] PR/CI link, exact SHA, guarded deployment/readback and Master Doc entry recorded.

## Rollout contract
Phase A (this PR): ratify a single standard, add conservative fail-closed static governance to the protected offline-pilot CI, and add an opt-in authenticated browser geometry auditor. **No site CSS, live app code, Nginx, or production services changed.**
Phase B: centralize tokens/components, remove conflicting global overrides and retire duplicated shell elements one app at a time; compare baseline screenshots at each viewport.
Phase C: run authenticated multi-app Playwright screenshot/geometry/a11y/performance checks automatically in a protected CI environment and make the result a required merge/deploy gate when reliable credentials and reference baselines are installed.
Phase D: evaluate a truly persistent shell if the no-iframe, independent-app model needs more than cross-document view transitions. This is a separate migration with regression and rollback.

## Known limitations at approval
The current shared shell still supports full HTML document navigation between independent apps; it is not an SPA. Some app-local CSS contains historical overrides. Static source validation cannot prove pixel-level layout, a login session, smooth navigation or accessibility. Do not claim those checks PASS until an authenticated browser completes them.
