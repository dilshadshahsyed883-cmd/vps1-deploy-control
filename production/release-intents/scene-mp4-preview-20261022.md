# Scene Builder local MP4 View/Download release intent — 2026-10-22

Source: https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/pull/302
Merged source SHA: 2a61d488f68c789904488da157c743d9593640b6

Purpose: use the existing protected GitHub→Tailscale Scene-only workflow to install the merged Queue and Scene worker source, making authenticated View Video and Download MP4 available after rendering during ready_to_upload / drive_uploading.

No approval or hold before Google Drive upload. Preserve automatic Drive upload, verified size/hash checks and cleanup, SQLite queue history, existing media, other apps, and Auto Script OFFLINE.

Production gate: the existing Scene-only production installer must abort and preserve the running system if there are active Scene jobs or other preflight failures. Never force release over active rendering. Require deployment job success and subsequent manual media acceptance before declaring the feature live.
