# Song Maker V1 — 29 October 2026

Status: implemented locally; protected PR/CI and live acceptance pending.
Repository: `dilshadshahsyed883-cmd/vps1-deploy-control`.
VPS_1 / Agent 1 locked. Auto Script is independently managed and is not altered by Song Maker. No Song Maker production mutation is established by this source report.

## Existing-system audit

Baseline GitHub main: `21eb613613746a789c2e52160a9e070a12d2feab`.
Inspected actual ACS renderer, compressor, output validator, stage workers,
queue store/claiming, process manager, Drive uploader, profiles, persistence,
cleanup and API auth. ACS has four separate ingress/render/compress/upload
services; compression can pass through; publishing follows validation and remote
verification; cleanup protects sibling jobs and explicit preview retention.
Song Maker reuses these proven patterns without importing ACS mutable state.

Inspected Short Maker image fit/blur/FFmpeg and rclone delivery, production
runtime/gateway/Scene SSO, independent service ownership and shared shell.
Short Maker's older in-memory/state.json approach is not reused for new persistence.

Agent 1 live read-only audit job `VPS1_AGENT1_SONG_MAKER_AUDIT_20261029_A1`:
[report](https://drive.google.com/file/d/1tglqkpZGva0SpMUGo9qZ1uyEl1-MaSuf/view).
Carrier SHA256 `ed64e11c9b66bae0d0b189f97623abe8923581b21a6ee9c2a46f9124636cfeeb`
independently downloaded/hash-checked; matching DONE manifest and report PASS.
FFmpeg 6.1.1 and rclone 1.60.1-DEV installed. Existing remote `gdrive`;
config mode 0600 owned by ubuntu. Credentials not read or copied.
ACS web + four worker units active; Short Maker and Scene active. Port 8150 free
in observed bounded listener report. Free disk 40,867,287,040 bytes.
Live source pointers: Scene `1a34ec4a898772ea99a1dd76cef3bdde9f9eaeef`, ACS
`3594ff6dc29535749809310f8e69aa9d300484e4-website-reaudit`, Short Maker
`b6525c9a1dd32c63b5a54df6c399202683eefcc6`.
These source/service observations do not assert the existing ACS/Drive workflow
passed a new functional render/upload test.

## Validation

18 focused real-media/API/release tests PASS; 31 existing runtime/navigation
regressions PASS. Chromium test PASS at 360/390/768/1024/1440 CSS px, one sidebar,
no iframe, tab/refresh persistence and actual MP4 download through running workers.
Drive failure/collision/checksum/retry tests use explicit fakes, not real upload proof.
Real Google Drive end-to-end acceptance: NOT RUN. Production browser UAT: NOT RUN.

## Deployment prerequisite

Installed helper SHA256 is
`f781743dc33ddc9f079201755ca83eef95afc4f4ca241c6c9d7d862dbd85f5d8`, exactly
matching pre-change `production/remote_deploy.py`. Its fixed allowlist excludes
Song Maker. `HOST_HELPER_UPGRADE_REQUIRED` is a real external production gate;
a scoped reviewed helper upgrade is required through an approved maintenance
transport before the protected Song Maker Tailscale release can run. Do not route
Song Maker through Scene's app identity, alter generic cutover switches or grant
an arbitrary sudo shell. No live Song Maker URL/deployment SHA is claimed.

Drive input/output folder IDs are not configured in production. Defaults are only
suggested names; no existing folders/credentials reorganized. Administrator setup
and isolated real Drive acceptance remain before completion.
