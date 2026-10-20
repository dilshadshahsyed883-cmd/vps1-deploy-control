# CW-009 — Isolated Uploader Engine health release intent

Date: 2026-10-20 (PKT)
Source fix: PR #164, merged into protected main at e2d31cf5250696a721b5f18efcef27d1f18c54c1.
Affected app: local-upload-manager only. Engine 2 (8016), Short Uploader (8017).

The independent web services were installed under mode-specific LUM_BASE_PATH
prefixes, while the Engine 1 read-only dashboard checked the wrong generic
health URL and hid the independent uploader controls as WEB NOT CONNECTED.

PR #164 repairs both health probe URLs, extends local response timeout, adds
regressions and requires a read-only active-job preflight before installer
stops any uploader services. All 16 PR workflows passed before merge.

The generic production release workflow was cancelled by GitHub concurrency
before any deployment job started:
https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/actions/runs/39384544233

This document records a new **dedicated isolated-engines release intent**.
A protected-main merge commit with [vps1-lum-engines] must start the dedicated
GitHub→Tailscale release job, install only Local Upload Manager using the
existing guarded installer and check exact engine health identity. Failure
to prove engine idle must abort before service stop. Successful installer
and health endpoints do not constitute end-to-end YouTube uploads.

Protected: Engine 1/2/Shorts SQLite records, OAuth, channels, Sheets,
scheduled jobs, source media and Auto Script (OFFLINE). No publishing enable.
Status: INTENT ONLY until matching GitHub Actions result exists.
