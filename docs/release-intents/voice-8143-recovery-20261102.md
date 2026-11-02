# Voice Studio 8143 — guarded recovery release intent (2 Nov 2026)

## Confirmed production evidence
Protected read-only run 41442830710 found Voice 8143 FAILED, `voice_missing_python_module=voice_tenant`, Voice/Scene pointers LEGACY_OWNED, Scene active, independent Song 8152 inactive, and last cutover ROLLBACK_FAILED. This is not proof that the recovery already succeeded.

## Exact authorized release
Use the protected-main GitHub actor `dilshadshahsyed883-cmd`, the existing serialized Tailscale voice-studio deployment lane, and **only** the exact commit markers `[vps1-song-studio-release]` and `[vps1-song-audio-voice-recovery]` on the protected merge commit. This does NOT authorize `[vps1-song-audio-separate]` in the same commit.

The protected `production.song_audio_voice_recovery.recover` helper must re-check all owners' jobs, original Voice unit and immutable release pointer, voice_tenant module evidence, Scene/Voice split flags, 8152 idle port and no Song worker. Stage an immutable reviewed Voice companion, import-test with existing Voice runtime, reset only the failed Voice unit and start original Voice 8143. Verify localhost health and real authenticated Scene SSO, deny anonymous session. Never overwrite original Voice release/SQLite/media/Drive data or change public repository visibility. Keep Auto Script OFFLINE.

## Follow-up
Only after Voice recovery is independently confirmed, create a **separate** guarded Song Audio cutover release using exact `[vps1-song-studio-release]` and `[vps1-song-audio-separate]` markers. The cutover's own all-owner idle gate, Song Maker handoff, single 8152 worker, Scene route-last, rollback journal, 2-user isolation and media/Drive preservation are mandatory. Stop on any failure; report exact host evidence.
