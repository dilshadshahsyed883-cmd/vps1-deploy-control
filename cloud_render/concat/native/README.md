# Concat Studio Native — Rust + Slint (Z Builder → Concat → Salad)

Native Rust 2024/Slint 1.18 Windows desktop; no Python, Tkinter, PyInstaller or Node runtime. The official Concat v0.2.8 Rust editor remains the native editor and handles editing/preview/export via its documented MCP API. This project builds a separate Rust control centre, not a replacement rendering engine.

## Five native tabs (Auto Editing is second)

1. **AI Builder:** Scene Plan TXT, optional narration/SRT, Z Builder media import and matching, native Concat project generation.
2. **Auto Editing:** Z Builder-style native Concat **Transitions / Animations / Scale**. Load real image/video clips, preview temporary effects (unsaved + undo), apply all/selected as an atomic native edit and save concat.json. Supported transition: cross-fade; picture motion: Zoom In/Out, Pan Left/Right, Combo Zoom+Pan; framing: Fit, Fill, 105%, 110%. Reset Presets resets selections only (never silently deletes saved edits). Unsupported original CapCut combos are not claimed.
3. **Editor:** launch original Concat and inspect its real project timeline.
4. **Render Queue:** persistent SQLite queue; Add PC/GPU, Start, Pause, Resume, Skip, Cancel, Retry, Delete, Refresh. Only one queue job starts at once.
5. **GPU Config:** HTTPS Salad Gateway, separate Salad and renderer API credentials stored in Windows Credential Manager, NVIDIA Vulkan diagnostics and worker job status.

## Media Library and Used Media

- Recursive import approved images/videos; original files remain untouched.
- Tags, image/video filter, Used/Unused/Missing filter, Media Summary TXT.
- Import old Z Builder SQLite catalog read-only with its CapCut draft export history in a separate legacy_used field — never present as final Concat MP4 renders.
- Used count increments only when a native PC export creates MP4 and it passes format and SHA256 verification; once per distinct asset per verified render. Repeated record of the same project/output/checksum does not double count.
- A GPU-delivered job may be marked done in the queue, but media usage is not incremented without a verifiable final output receipt on the PC. Queue submission or scene matching is never usage.
- Clear Library currently clears catalog records only (with confirmation), never originals. Original Z Builder database is never written.
- Timed and legacy SCENE_PLAN_VERSION=2 inputs supported. Unsupported CapCut-only animations/transitions are not fabricated. Provisional legacy timestamps are warned before building.

## Queue control behavior

| Action | Queued | Running PC | Running Salad |
|---|---|---|---|
| Start / Resume | Unpause scheduler | Current job continues | Current job continues |
| Pause Queue | Prevent new jobs | Current export continues | Current remote job continues |
| Skip | Skipped | Request native export cancellation | Stop local upload; detach if submitted |
| Cancel | Cancelled | Request native export cancellation | Stop local upload; detach if submitted |
| Retry | Requeue safe terminal states | Blocked while running | Blocked if remote ID exists |
| Delete | Remove queue record, retain files | Blocked | Blocked |
| Restart | Queue persisted | Mark interrupted for review | Mark interrupted; never silently double-submit |

Current Salad worker has no cancel-render endpoint. Studio cannot stop an already submitted remote GPU render, so it detaches honestly and preserves remote/VPS outputs. Server-side remote cancellation would need a separately tested new worker feature.

## Build

GitHub Actions workflow: .github/workflows/vps1-concat-studio-native-windows.yml

- Rust unit tests, release Windows EXE compilation, packaged self-test, live GUI launch smoke, SHA256SUMS artifact.
- **Combined portable ZIP:** `Concat-Studio-Portable-Windows-x64.zip` has one `Concat Studio/` folder containing `ConcatStudio.exe`, the *unchanged checksum-verified upstream Concat v0.2.8* `Concat.exe`, and its bundled FFmpeg/DLL support files. Studio detects the adjacent Concat.exe automatically. The original standalone native EXE remains available as a separate artifact.
- The GitHub Windows runner verifies upstream's SHA256SUMS, stages the official Inno installer in an isolated temporary directory, checks both EXEs and auto-discovery, launches the Slint GUI, and uploads ZIP plus checksums. Nothing modifies VPS production or the user's existing Windows installation.
- No VPS production deployment; official Concat binary, GPU image and source.lock.json version lock unchanged; Auto Script stays OFFLINE.

## Acceptance boundaries

Actual PC editing/preview/export and Salad GPU end-to-end tests with real media are required for this new integration. Auto Editing uses official Concat MCP commands and produces saved native concat.json, not a separate GPU-only effects format. Editor look presets, karaoke and advanced effects remain in Concat's own editor; do not present unimplemented features as part of the control centre.
