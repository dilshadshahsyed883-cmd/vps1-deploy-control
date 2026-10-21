# Universal Shared Shell V2 — implementation register

Extends `UI_UX_MASTER_RULES_V1.md`; does not replace the canonical native
sidebar or independently owned application engines.

## Current boundary — canonical Dashboard/Storage batch

`/` and `/admin/storage` now mount app-owned Dashboard and Storage adapters
inside one persistent host using the existing base template and native sidebar.
`/workspace/` remains the application directory; `/workspace/storage/` remains
a supported alias. These routes use the same no-store host and hashed module/CSS
manifest. `?native=1` selects the original server-rendered page for rollback or
compatibility diagnosis without bypassing backend permissions.

Dashboard is authenticated-user accessible; Storage is admin-only. The backend
checks permissions before rendering and the host checks a fresh backend identity
before mounting. Non-admin HTML excludes the Storage seed. Both adapters use
isolated Shadow DOM and reuse macros in their original app-owned templates;
no fetched HTML/script injection or iframe is involved.

Unmount aborts requests/listeners and clears polling. Dashboard serializes only
its validated refresh-rate/auto-refresh preferences into per-user sessionStorage;
no credentials, role decisions, jobs or media state are stored there. Back, refresh,
aliases, API recovery and role revocation have dedicated browser checks. All other
apps retain native-document compatibility boundaries and are not called migrated.

## Ownership and gates

- Native navigation/theme/geometry: existing `vps1_native_site_shell.css/.js`.
- Registration: `static/shell_v2_registry.json`; schema 2, shell 2, design 1.
- Adapter: app-owned `static/storage_manager.js` and `dashboard_control_center.js`; shell only calls lifecycle.
- Assets: `python -m production.build_shell_v2`; check mode blocks stale output.
- Required offline gate: `production.shell_v2_contract`, negative contract tests.
- Required Chromium gate: `production.shell_v2_browser`; actual committed host
  template/module/styles, synthetic API identities and responses.
- Protected production transaction: same browser test with `--production`,
  existing root-protected session credentials, no screenshot/data capture.
  Exit/result marker is checked and printed; failure raises within existing immutable Scene release rollback transaction.
- Existing deployment lock serializes host changes. Deploy with the existing
  Scene-only protected main release lane; no independent engine reinstall.

The viewport gate covers 360/390/768/1024/1440 CSS px and a 720 CSS px / DPR 2
layout equivalent to 200% zoom on a 1440px display. It does not claim to operate
browser toolbar zoom. Tests check DOM identity, singleton sidebar, shadow root,
Back/forward, hard refresh, focus, overflow, role revocation and zero writes.

## Conflict matrix

| Class | Implementation | Remaining |
|---|---|---|
| C01 | Reuse canonical sidebar; pilot cannot create outer chrome | Native legacy documents still reload |
| C02 | Shadow boundary for pilot CSS | Legacy broad global styles remain outside pilot |
| C03 | Existing 214/760/1400/62 baselines preserved | Full actual-app visual matrix |
| C04 | Versioned frontend registration with compatibility boundaries | Consolidate existing gateway/Nginx inventories and aliases |
| C05 | Persistent workspace/storage navigation | Other apps retain native documents |
| C06 | Explicit teardown/abort/timer lifecycle | Legacy app scripts need own adapters |
| C07 | Fresh identity check before mount; independent backend guards | Broader role matrix |
| C08 | Pilot performs GET only; no job actions | Actual media app continuity gates |
| C09 | Content-addressed pilot assets | Legacy shared assets still query-versioned |
| C10 | Existing serialized immutable release/rollback | All-app shared ownership enforcement |
| C11 | Required fixture + production pilot browser gates | All tabs/subtabs authenticated coverage |
| C12 | Fail-closed supported adapter/registration versions | Each new adapter requires smoke implementation |

## Migration exceptions and removal condition

All registry `native-document` entries retain their existing backend/API/static/
WebSocket routing; this registry does not claim to replace the Nginx source.
Exception owner is the named application. Remove its boundary only after its
forms, inline/global scripts, event listeners, polling, identity and recovery
are converted to an app-owned lifecycle with authenticated regression evidence.
No automatic raw-document importer is permitted as a migration shortcut.

No universal completion claim: Scene/Video, Voice, Upload/Stream, Experiment,
ACS and remaining app adapters, global CSS retirement and gateway consolidation
remain implementation work. Auto Script stays OFFLINE.

## Canonical batch verification

The fixture/production Chromium gate now measures Dashboard → Storage → Back
DOM identity, one document navigation, exact live interval counts (1/2), repeated
unmounts, polling preferences, direct refresh, legacy alias and zero API writes.
Fixtures additionally cover ordinary-user access, exclusion of admin template
content, admin revocation, logout and dashboard API failure/recovery. Six viewport
layouts are checked. Actual toolbar zoom remains unverified. Production results
are emitted as a sanitized `SHELL_V2_BROWSER=PASS ... auth=PRODUCTION` marker.
