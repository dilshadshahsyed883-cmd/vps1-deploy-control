# Song Audio post-cutover read-only production proof — 2026-11-03

## Why this readback is required

Protected Song Audio release #41564374157 executed the canonical atomic cutover and emitted a success receipt: `song_audio_cutover=PASS`, `worker=ONE`, `route=SONG_8152`, `original_database=PRESERVED`. The **separate legacy generic gateway reconciliation** then failed an authenticated UI request with HTTP 401 and restored only its prior Nginx gateway file. The workflow is therefore FAILED, despite the earlier Song transaction PASS. We must not presume the current live state, restart a working worker, or rerun the cutover against its own installed unit.

## Approved action

Run only the existing protected-main root read-only diagnostic `[vps1-song-studio-release] [vps1-song-audio-voice-readonly-diagnose]` through pinned Tailscale and serialized host lock.

Inspect and report sanitized, credential-free:
- Song service 8152 active/substate and owned release pointer
- Voice synthesis 8143 status and split switch
- Scene Song upstream (legacy or 8152) and owned Scene pointer
- latest Song transaction rollback/active receipt and permitted startup error classes
- database/queue untouched, AUTO_SCRIPT OFFLINE

No systemctl start/stop, no gateway changes, no queue claims, no file cleanup, no database or user data mutation. Use the observed result to choose targeted follow-up. A successful Song cutover receipt is not independent proof of end-to-end live service until this readback and real auth acceptance pass.

If the legacy generic gateway 401 is a Scene SSO mismatch, repair the **verification mechanism** without loosening authentication or bypassing route checks, and use a separate scoped release for any required follow-up.
