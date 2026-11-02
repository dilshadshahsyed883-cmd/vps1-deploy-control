# Song Audio 8152 failed-start diagnostic — read-only release 2026-11-02

## Evidence and reason
Existing protected cutover run 41475387692 merged from PR #652 failed at immediate 127.0.0.1:8152/healthz with ConnectionRefusedError. Preflight reported one dormant historical Song Maker orphan preserving the original queue; no ineligible live handoff was reported. Worker crash versus startup readiness race, rollback and current Voice/Scene status remain unverified.

## Read-only authorization
After PR #653 (read-only classifier) CI PASS, protected-main merge **only** with exact release selectors:
[vps1-song-studio-release]
[vps1-song-audio-voice-readonly-diagnose]

Existing serialized Tailscale production helper must run the read-only audit from production.song_audio_voice_diagnostic.report using the exact Voice installer lane. No source overlay deployment; no systemctl start/stop/restart, no worker, no route or queue changes. Print only reviewed allowlisted unit state, owned pointer class, Voice/Song health class, latest cutover rollback status, safe error-class booleans and validated missing Python module name; never raw journal text, filenames, secrets, database rows or tokens.

## Decision gate
If Voice is not healthy, require Voice recovery before any new cutover. If rollback is ROLLBACK_FAILED or Song worker remains active, stop and reconcile before retry. If 8152 has a definite missing module or permission error, fix that exact issue with tests; if no crash evidence and startup just races immediate HTTP check, apply bounded health wait and fail-closed liveness to existing service. Full CI, guarded release and final two-user live acceptance still required.
