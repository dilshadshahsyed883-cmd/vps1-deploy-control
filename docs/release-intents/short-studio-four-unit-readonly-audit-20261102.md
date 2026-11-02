# Short Studio 4-process cooperation and drain — read-only VPS audit
Date: 2026-11-02 (PKT)
Source: protected main 5a98321b572a521efb5bd8f4e2fe11f2034aab16

Scope: frozen Short Maker approved plan only. Phase-1 production install was verified in GitHub Actions run 41479741406, 8146 health PASS, 7 authenticated gateway routes PASS, Voice 8143 PASS.

Run **only** the existing sanitized readonly diagnostic:
`[vps1-short-studio-release] [vps1-shortstudio-readonly-diagnose]`

Existing production/shortstudio_readonly_diagnostic.py reports Maker+Uploader web/worker/scheduler unit config, root device/inode identity, running-PID environment and same mount, plus bounded read-only upload/claim idle snapshots. It deliberately never establishes a continuous drain/lease or gives permission to delete.

Absolutely no app source install, service stop/restart, environment edits, account/OAuth access, data copy, media removal, queue manipulation, Drive uploads or script activation.

The diagnostic result may be BLOCKED; do not convert an all-flags-OFF reading into a PASS. Keep daily Short Profiles, cooperative locks and UNUSED-ONLY delete OFF pending the separately approved coordinated activation, all-owner idle proof and post-deploy two-user acceptance. Auto Script OFFLINE. Originals preserved.
