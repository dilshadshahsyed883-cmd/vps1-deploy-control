# VPS1 UI Architecture — One Website Shell + Independent App UIs

**Scope:** existing private `vps1-deploy-control`, fresh rebuild branch only.
**Production:** unchanged; cutover, service enablement and deployment remain disabled.

## Implemented shell contract

The isolated builder now creates one central static website shell:

- `/srv/fresh-website/shell/index.html` — shared header, app sidebar and content host.
- `/srv/fresh-website/shell/assets/shell.css` — responsive theme tokens and common chrome.
- `/srv/fresh-website/shell/assets/shell.js` — client-side navigation using an allowlisted JSON registry.
- `/srv/fresh-website/shell/assets/apps.json` — generated from `rebuild/groups.json`
  and `rebuild/install-profiles.json`; offline apps are excluded.

The **candidate-only** Nginx configuration serves the central shell at `/`,
its assets at `/shell-assets/`, and each configured application's independent
UI through `/<app-id>/` with its own existing upstream. Site-level Basic
Auth remains on the Nginx server. No app's HTML/CSS/JavaScript is replaced.

Apps without a configured web service appear in the shared navigation but
display a "Route pending" panel instead of a broken iframe. Configured routes
are labeled **runtime not verified**, never falsely shown as healthy.
In this historical fresh-rebuild candidate shell, Auto Script is excluded from the candidate navigation. The independently activated production Auto Script is ONLINE per owner as of 29 Oct 2026 and available through the authenticated Experiment Studio > Auto Script navigation (merged PRs #314/#315); do not use this candidate-only registry as a live-status report.

## Future changes

- **Add app:** Add a validated `groups.json` registration, matching
  `install-profiles.json` entry, app source, service contract and tests.
  The next offline build generates its navigation entry. An enabled web
  route is not automatic production authorization.
- **Edit app UI:** Modify only `apps/<app-id>/` source. Its independent
  frontend and versioned release remain app-owned; shared shell is unchanged.
- **Remove app:** Remove registration deliberately and validate dependent
  groups; next shell build removes the menu entry. Do not delete runtime
  databases, queues, uploads or other apps automatically.
- **Theme:** Use shell tokens as a reference and optionally port them into
  each app's stylesheet. If an app runs in an iframe, its CSS does **not**
  inherit shell CSS automatically. Cross-app stylesheet overrides are avoided.

## Important incomplete gates

Per-app prefix/static routes, authenticated sessions, iframe policies,
service health and external integrations **must be browser-tested** on a
post-snapshot staging host. A configured proxy target does not prove an
application works at `/<app-id>/`. Some existing app pages use absolute
`/static` references and may require their own base-path adjustments.

Applications share a same-origin shell only if they are within the same trust
boundary; user-supplied/untrusted apps should use isolated origins. Shell
navigation does not replace backend authorization. Production is not enabled.

## Offline checks

```bash
python3 -m unittest rebuild.tests.test_website_shell -v
python3 -m unittest discover -s rebuild/tests -v
python3 -m rebuild.fresh_vps assemble --root /tmp/fresh-shell-check --sha "$(git rev-parse HEAD)"
python3 -m rebuild.fresh_vps verify --root /tmp/fresh-shell-check
```


## 2026-10-21 Universal Shell V2 — source patch, production not yet verified

The legacy preview index embedded apps in an iframe with a separate 240px
sidebar and 820px breakpoint. That conflicts with the approved native shared
navigation geometry (214px / 760px). The root now becomes a navigation
overview only: links open app-owned documents at their canonical top-level
paths, where the single shared native sidebar is already installed. No iframe
may be reintroduced as a competing production shell.

Configured routes and verified apps are distinct. The gateway writes the
manifest as unverified, runs authenticated health+HTML checks, then marks only
directly probed app routes verified. Scene-derived aliases stay unverified.
Pending routes are non-clickable. Invalid registry entries fail closed.

Shell assets are emitted to a new immutable per-attempt directory, so a failed
Nginx cutover can restore the previous Nginx config **and** its original shell
assets. This does not touch running jobs, stored media, OAuth, app databases
or Auto Script (excluded from this legacy rebuild, currently ONLINE). Offline CI covers native links, geometry, and
verification transition.

Deferred app-specific work: remove historical broad !important selectors,
eliminate response-body Nginx sub_filter by migrating apps to canonical base
paths, reconcile global theme across native pages, and complete authenticated
end-to-end browser coverage. These limitations must not be represented as
fully resolved by this source patch.