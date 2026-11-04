# Concat Studio V1 — Windows control centre

This is a **real desktop Windows companion EXE**, not a patch of Concat's Rust
window. It adds four tabs: AI Builder, Editor, Render Queue, GPU Config.

- AI Builder: Z Builder V6.3 **simple timed** scene-plan TXT, local media
  filename/keyword matching, narration MP3/WAV, SRT captions and preflight.
  It creates a *native editable Concat project* using the v0.2.8 MCP API.
  Does not yet support all legacy SCENE_PLAN_VERSION=2 INI fields, CapCut-native
  effects, or timed animation extensions. Unsupported fields fail explicitly.
- Editor: launches the unchanged official Concat video editor; you open the
  generated project from Concat Home. Its real timeline/preview and manual
  editing remain native. The Studio's read-only native timeline inspector
  works when Concat does not have the project locked. There is no embedded
  replica of the Rust timeline in this Python window.
- Render Queue: PC MP4 export via native MCP (one job), Salad upload via existing
  authenticated chunk worker, job status and checksum-verified GPU download.
  If the worker delivers to VPS, the MP4 is managed at /cloud-render/ on VPS1.
- GPU Config: stored Salad Gateway origin and **two separate** keys, saved in
  Windows Credential Manager (explicit WinVault; no insecure fallback),
  NVIDIA Vulkan diagnostics, no CUDA-only false success.

## Setup and compatibility

Install the official SHA256-verified Concat v0.2.8 installer bundled as a
separate file with this Studio build; then launch ConcatStudio.exe. Set
Concat.exe path if it is not detected. The Studio does not replace the official
Concat editor installation. This integration uses the existing MCP API,
retains native concat.json, and requires no changes to the Linux GPU engine
or source lock. Media and projects always stay on the local disk until the
user explicitly requests a GPU upload.

- Concat Studio source: cloud_render/concat/studio/
- Windows launcher: run_concat_studio.py
- Build workflow: .github/workflows/vps1-concat-studio-windows.yml
- GPU auth: Salad-Api-Key plus Authorization: Bearer RENDERER_API_TOKEN
- Source bundle: existing checksum-verified project_bundle.pack()
- Worker endpoints: /api/diagnostics/gpu, /api/uploads/{job}, /api/jobs/{job}

**Limits:** Concat 0.2.8 MCP must be present and allowed to open the project.
If the GUI already locks it, close the GUI project before building/rendering.
The Studio is a non-production desktop client. No VPS deploy, new Salad image
or automatic Drive upload is performed by its build workflow.
Local PC and Salad parity still needs an external real-media acceptance test
for this new *integration*, distinct from the previously approved GPU worker.

Security: never put tokens in Git, JSON settings, logs, or stdout; no original
media deletion; new project names refuse overwrite; GPU blocks if the NVIDIA
Vulkan adapter is not visible.
