# Short Maker Desktop (Windows x64)

Local-only standalone Windows EXE based on the VPS 1 Short Maker concept.
No profiles, scheduling, Google Drive login, automatic upload, VPS access or YouTube publishing.

## Quick start

1. Download the ShortMaker-Windows-x64 Actions artifact and extract its ZIP.
2. Run ShortMaker.exe on Windows 10/11 (64-bit). The GitHub CI build is
   currently unsigned; check the repository and build origin before executing.
3. In Maker, choose exactly one Input folder (mandatory). Supported
   photos are scanned recursively. Any audio files in that folder and its
   subfolders are detected automatically.
4. Optionally choose additional audio files; these join the detected audio
   pool. There is no mandatory separate audio input. If no audio exists, MP4s
   will be silent.
5. Choose an existing Output folder (mandatory and separate from input).
6. Set video duration, image duration and the number of videos, then choose
   Generate Videos -> Add to Queue. Queue execution starts automatically.
7. Use Queue to Pause/Resume/Cancel/Retry/Delete a batch and Open Output.
   Completed MP4s are never deleted when deleting queue entries.
8. Upload finished MP4s to Google Drive yourself. Copy that Drive folder link
   into a VPS 1 Short Uploader profile. No automatic Drive upload occurs.

Outputs use the pattern Short_<batch-id>_001.mp4 with 1080x1920 H.264 video,
30 FPS, CRF 23, randomized imagery, randomized audio selection (one per
video), randomized motion and randomized transitions.

## Recovery and storage

Queue JSON persists in %LOCALAPPDATA%\ShortMakerDesktop\queue.json.
After a crash or restart, interrupted renders are marked paused.
Press Resume to restart the current video (previous completed MP4s stay).
Partial MP4s are disposable; original images, audio, finished output files and
Google Drive content are never automatically deleted.

If the source files are moved or removed, restore them before Retry. Choose
different input/output folders; nesting is blocked. When only one audio track
exists, it can be reused on subsequent videos.

## Concat motion & transition adaptation

This build reuses the 20 named Concat procedural transition styles as a
catalogue and their randomized use policy. Windows CPU FFmpeg xfade filters
approximate the appearance. These are NOT the original CUDA shader effects
or frame-identical render output. Animations likewise use FFmpeg zoom/pan
approximations of Concat's six physics patterns plus basic zoom and pan. There
are no manual animation or transition controls. Full Concat CUDA parity would
require a separate port and compatible GPU.

FFmpeg/FFprobe (including libx264) are bundled with the Windows distribution.
See https://ffmpeg.org/legal.html and https://ffmpeg.org/download.html for
FFmpeg licensing and corresponding sources. FFmpeg itself is independent of
this application's Python source.

## Development

Python 3.12 standard library (Tkinter) + FFmpeg / FFprobe. No external Python
runtime is required for the packaged Windows EXE.

Run tests:

    python -m unittest discover -s desktop/short-maker/tests -v

The GitHub workflow runs Linux FFmpeg smoke tests, then packages Windows
ShortMaker.exe using PyInstaller and uploads an Actions artifact. It does not
deploy or change VPS 1 production.
