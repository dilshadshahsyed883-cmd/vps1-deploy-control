# AI Mystery Maker — isolated one-video prototype

**Scope:** one original English fictional mystery Short, for **manual review**, without modifying VPS_1 apps or deploying to production. This prototype is intentionally not a Sheets scheduler or an auto-publisher.

Pipeline: `topic → Ollama/Qwen JSON script + 8 scene prompts → existing Kokoro job API WAV → local ComfyUI workflow for FLUX.1-schnell → FFmpeg 9:16 MP4 + approximate SRT captions → optional MD5-verified rclone handoff`.

## Prerequisites

- Python 3.10+; `ffmpeg` with `subtitles`/libass and `ffprobe` in PATH.
- Ollama with `qwen3:4b` installed and available on localhost.
- Kokoro worker running with an accessible **authorized** `/tts/api/kokoro` route, with voice `am_michael` in its catalog. Do not bypass Voice Studio login or access other users' jobs; use a dedicated authorized local integration. If authorization is unavailable, synthesis must fail closed.
- ComfyUI running on localhost with a **working FLUX.1-schnell API-format workflow**. Export it from ComfyUI (API format) after installing required weights yourself. Set `comfy_prompt_node` to the positive-prompt node exposing `inputs.text`. This prototype does not install models or change the Concat CUDA image; those are distinct workloads.
- Optional existing, approved Drive Bridge `rclone` configuration. No service account JSON is committed or required for the one-video prototype; Google Sheets input will be a later phase.

## Example local-only configuration (`config.json` outside Git)

```json
{
  "ollama_url": "http://127.0.0.1:11434",
  "ollama_model": "qwen3:4b",
  "kokoro_url": "http://127.0.0.1:8143",
  "voice": "am_michael",
  "comfy_url": "http://127.0.0.1:8188",
  "comfy_workflow": "/secure/local/path/flux_schnell_api.json",
  "comfy_prompt_node": "6"
}
```

Run from the repository root, using your *actual authorized* local ports:

```bash
python3 pilot/ai_mystery_maker.py \
  --topic 'A Man Receives a Call From Himself Tomorrow' \
  --config /secure/local/config.json \
  --output /secure/local/mystery-output
```

Optional Drive handoff (only after canonical Drive Bridge rules and remote ownership are validated): add `--drive-remote 'gdrive:AI Mystery Videos'` to place a unique MP4 in that folder. Files are **not deleted** after upload. The result JSON includes a verified remote rclone path, not a fabricated shareable Drive URL. On missing, failed, or mismatched MD5 verification, retain the local MP4 and fail the job.

Output per job: `story.json`, `narration.wav`, `scenes/01.png` … `08.png`, `captions.srt`, `timeline.ffconcat`, `video.mp4`, `result.json`. Every run receives a unique folder. No YouTube upload, scheduled polling, auto-delete, or background mutation is enabled.

## Verify

```bash
python3 -m unittest discover -s tests -p 'test_ai_mystery_maker.py' -v
```

These tests mock external services and Drive; they do **not** prove that an Ollama model, Kokoro authentication/worker, ComfyUI FLUX workflow, FFmpeg codec filters, or approved rclone remote exists on the live VPS. Production integration and any Sheet JSON/service-account handling require a separately authorized, isolated release.

Quality control: confirm original script, correct scene continuity, intelligible voice, image rights and synthetic-content disclosures before manually publishing. Captions are approximate scene-based timing, not forced word alignment. Maintain original story variation; mass-produced repetitive templates risk YouTube inauthentic-content restrictions.
