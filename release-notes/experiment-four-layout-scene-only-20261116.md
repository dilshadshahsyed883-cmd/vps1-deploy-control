# Experiment Studio four-page layout guarded Scene Builder release — 2026-11-16

Deployment target: **Scene Builder application only** via the existing protected `vps1-production-deploy` guarded workflow and rollback. This release does not deploy other applications, GPU workers, Salad containers or model images.

Reviewed source: PR #754, merged as `22c0fbbc2e21d9fd2407f78d37ffa49aab46ed48`, with **30/30 GitHub Actions checks passing** before merge.

## User-approved scoped change
- Match Concat, HeyGen Studio, Voice Clone and Imagen top navigation and workspace width/centering to the existing Upload Studio reference.
- Preserve the `/cloud-render/`, `/heygen-studio/`, `/voice-clone-studio/`, and `/imagen-studio/` routes.
- Add dedicated four-page CSS and page-scoped main class; retain existing mobile overflow, sidebar and role-controlled navigation.
- Upload Studio, Antidetect Browser, other apps, API paths, authentication, persistent jobs, GPU configurations and canonical Drive Bridge remain unchanged.

## Validation and production acceptance
- Source PR #754: Experiment Studio GPU CI and applicable site/UI/security tests **30/30 PASS**.
- Release PR CI must pass; use the exact protected Scene Builder-only release marker `[vps1-production-release:scene-builder]` on the merge commit.
- On the deployed exact SHA, verify Scene Builder health, asset `/static/experiment_four_studio_layout.css?v=20261116-1`, authenticated access to the four URLs, their aligned rails and navigation, and unchanged Upload Studio.
- Never claim authenticated live layout was validated until a browser check succeeds. Do not touch gateway routing or Salad infrastructure. Any deployment failure must invoke existing guarded rollback.
