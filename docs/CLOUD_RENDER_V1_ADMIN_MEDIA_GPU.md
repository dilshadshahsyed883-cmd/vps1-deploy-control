# VPS1 Cloud Render V1 — frozen two-tab scope (2026-11-04)

## Location and access
- Direct URL /cloud-render/; Experiment Studio -> Cloud Render.
- Exactly Media and GPU Config inner tabs; admin-only single workspace, no multi-user.
- Auto Script remains OFFLINE; no other studio services changed.

## Media
- Concat/SALAD worker sends MP4 to authenticated chunk receiver at POST /api/cloud-render/ingest/{job}/chunk.
- Header X-Cloud-Render-Token; admin can rotate token from GPU Config, not shared with OmniVoice.
- 8 MiB chunks, declared total <=20 GiB, MP4 signature and complete SHA256 verification.
- Media list: Preview, Download, Rename, Delete, individual Upload to Drive, retry failures.
- Auto Upload New Jobs checkbox default OFF; when enabled, applies ONLY to new finalized jobs.
- Existing canonical Drive Bridge rclone is required: remote gdrive, folder Cloud Render (configurable via environment).
- After rclone copyto --immutable, verify destination size and MD5 with lsjson --stat --hash.
- Only after both checks succeed, unlink VPS MP4. Never delete Drive originals or unrelated files.
- Transfer failure, MD5 mismatch or missing Drive Bridge preserves VPS original for retry.
- State/MP4 under WEB_DATA_DIR/cloud-render; active Drive transfers cannot be manually deleted.

## GPU image contract — authoritative Concat source from main #686
- Read cloud_render/concat/source.lock.json, cloud_render/concat/gpu/Dockerfile.
- Native Linux x86_64 Rust concat-cli from Concat v0.2.8 pinned commit 79116bd0f9a103c633d888603ad27890e929dbdc.
- Ubuntu 24.04, FFmpeg native libraries, ONNX native dependencies; NVIDIA driver from Salad host.
- NOT OmniVoice and NOT the previously suggested Editly/Node image; no runtime downloads.
- Dockerfile is still experimental and its entrypoint is concat-cli --help (NOT a live HTTP server).
- Source recipe is not a published/verified GPU container. Do not show it as online.
- Planned Salad group: RTX 3060 Ti 8 GB VRAM; 4 vCPU, 8 GB RAM, 30 GB disk, High priority, 1 replica.
- Planned HTTP bridge internal port 8080, readiness path /health/ready, bearer RENDERER_API_TOKEN.
- Render 1080p H.264 and concurrency=1; only planned until real GPU proof.
- SaladCloud GPU resource selection is configured in Salad, not changed by Scene form.
- Any shared desktop/GPU engine feature change must follow two-target parity/rebuild contract.

## Security / deployment
- Existing Scene admin middleware and CSRF for every management write.
- Narrow worker-token bypass only for MP4 ingest endpoint; path and filename validation.
- Reuse existing protected main, guarded Tailscale Scene release lane with idle gate, rollback and UAT.
- Tests cover auth restrictions, chunk resumability, checksum, Drive verification, cleanup and contracts.
- Deploying Media UI does not prove Salad image or end-to-end GPU readiness.

## Drive Bridge failure diagnosis (November 2026)
- Cloud Render files with Drive upload state failed remain on VPS; successful Concat rendering and Salad-to-VPS delivery are independent of the Google Drive step.
- Drive uses canonical deployment rclone remote set by CLOUD_RENDER_DRIVE_REMOTE (default gdrive), destination folder CLOUD_RENDER_DRIVE_FOLDER (default Cloud Render). Never create a new OAuth identity or guess an alternative remote.
- Before Drive upload, rclone listremotes checks configured remote under the actual application service identity. An absent remote fails safely, preserving the MP4.
- rclone lsjson --stat --hash now treats only remote object not found as missing. Authentication and permission failures must not be hidden as missing-file results.
- User-facing failure messages are classified without exposing raw stderr, credentials, local paths or OAuth URLs.
- Only after remote size and MD5 verification can Cloud Render unlink VPS original. Never delete the original merely because a copy command returned exit zero.
- If remote is not configured for the service, inspect canonical Drive Bridge service permissions/config. Retry the same MP4 after repairing. Other users and other remotes must remain untouched.
