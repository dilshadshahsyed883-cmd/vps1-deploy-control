# SML Face Recognition PR #308 — isolated production release intent

Source: https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/pull/308
Merged commit: decae995a92b1ead28c0b14eb254d297755796d3

- Deploy **self-media-library only**, using existing Tailscale-protected, immutable app-only installer with idle preflight and automatic rollback.
- Preserve all existing media, manual person tags, clip workflows, Drive settings, other applications and Auto Script OFFLINE.
- Read back authenticated local SML people list and frontend HTML/CSS/JS; do **not** start face scans or download models in production smoke checks.
- People naming remains opt-in and all AI matches require approval.
- Do not mark live until workflow reports healthy post-install readback; owner performs visual/functional UAT later.
