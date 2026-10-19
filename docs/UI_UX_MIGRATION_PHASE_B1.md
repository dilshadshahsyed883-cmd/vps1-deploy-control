# VPS1 Unified UI — Phase B1 (shared native navigation + Chromium gating)

**Scope:** production shared shell geometry only, 20 October 2026. Approved by owner as next step after PR #131 policy. App engines, queues, databases, OAuth, worker configuration and Auto Script remain untouched.

## Findings from repository source
- Native sidebar originally declared 205px and also a later canonical 214px width; two independent CSS ranges could disagree during loading.
- Mobile drawer in legacy CSS used max-width 800px, while canonical overlay/sidebar used max-width 760px; the 761–800px window could display inconsistent navigation.
- Upload Studio tab rail used 16px top margin and 32px total outer gutter while Video Studio used 26px and 34px per side. Switching from Video to Upload visibly shifts top-level tabs.
- Auto Stream tabs were inserted **after** its own header, whereas Auto Uploader tabs were inserted after the shared mobile navigation button. This makes two subtabs use different vertical starting positions.
- Older Scene, ACS, Voice and uploader app-local CSS contains broad element-level !important overrides. These remain compatibility debt: no one-shot removal without full-content screenshots and app-specific functional smoke.

## Changes in this phase
- Shared 214px sidebar and 760px mobile breakpoint are now consistent across early fallback and canonical CSS.
- One versioned `--vps1-ui-*` token contract governs shared top-level Studio rail max width (1400px), desktop X/Y padding (34px/26px), 62px rail, 40px links, color/border/focus and mobile (14px/72px).
- Upload Studio uses a nonwrapping scrollable top rail matching Video Studio geometry. Auto Stream and uploader place it before their individual application headers. No embed wrapper or app state modification.
- Actual source stylesheet and JS are used by **real headless Chromium** at 1440, 1024, 768, 390 and 360 CSS px. Shared sidebar width, tab coordinates (2px tolerance), overflow, tab counts, stream header ordering and cross-app **rendered screenshot pixel** equivalence are checked. Tests have deterministic synthetic empty bodies; no login secrets/media are stored.
- `offline-pilot`, an already required status, now depends on `ui-chromium-visual`. A browser fail therefore blocks protected main PR merge; screenshots are saved as sanitized CI artifacts. Source tests continue separately.

## Proven and not proven
- CI passing proves the committed global shell/nav rendering and synthetic cross-app visual regression in Chromium. It does **not** by itself prove all live application contents, user sessions, complex forms, Voice panels, OAuth, media previews, or full end-to-end app navigation.
- The earlier `production/ui_visual_audit.py` protected authenticated browser probe remains separate and must still be run for pixel-level *production* verification with valid auth state. Never check such a state file or live screenshots containing private data into Git.
- This phase does not rewrite app-owned templates or remove all historical broad CSS overrides in Scene/Voice/ACS; do those per app behind the same visual gate, respecting existing functionality.

## Deployment procedure
1. Merge only after required offline, source, and new Chromium checks pass.
2. Use guarded main-only GitHub/Tailscale UI-scoped rollout with host deployment lock/automatic rollback. Publish Scene shared CSS/JS before verifying pages. If other app templates or cache-busting links are changed later, include them app-wise.
3. Confirm authenticated app route/health readback plus Chrome visual navigation after deploy. User handles final UAT.
4. Record issue/fix/results/PR/run/revision in VPS_1 Master Google Doc, retain existing history.

**Note:** A cached CSS/JS asset from a prior release may remain in an already-open browser until its cache refreshes. A subsequent coordinated cache-key migration is needed if fresh asset activation is delayed.
