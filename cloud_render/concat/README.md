# Cloud Render — Concat original desktop package

Canonical package: copied from Rajudesktop/concat_original to VPS1/cloud_render/concat.

The Windows EXE is from original Concat v0.2.8, not a custom video renderer.
The EXE is intentionally NOT committed into Git. This repo owns a GitHub Actions workflow that fetches the same official upstream installer, validates its SHA-256 against the official SHA256SUMS, checks the Windows PE header and size, and publishes a new Actions artifact.

Source repository: https://github.com/dilshadshahsyed883-cmd/Rajudesktop
Original successful build: https://github.com/dilshadshahsyed883-cmd/Rajudesktop/actions/runs/41718543140
Original artifact ID: 13270812649; ZIP size: 86108691 bytes; expiration date: 2026-12-03.
Upstream: https://github.com/jub0t/Concat/releases/tag/v0.2.8 (AGPL-3.0-or-later).

Windows installer: Concat-0.2.8-windows-x86_64-setup.exe
Target build artifact: Cloud-Render-Concat-v0.2.8-Windows-x64

IMPORTANT: a Windows EXE does not run directly in Linux Salad Cloud GPU containers.
Concat's separate native Rust/Linux headless engine and API must be packaged and tested before cloud rendering is enabled. This migration does not deploy a Cloud Render app or change the VPS services.

Personal Concat projects/videos are NOT present in the Rajudesktop git tree. They must be exported from the user's PC (concat.json with referenced media) and securely transferred separately, never committed to Git.

Safeguards: no production deployment, no user data deletion, no Auto Script changes, no live Drive or Salad credentials in Git. Keep the Rajudesktop source artifact until target verification passes.

Offline test: python -m unittest discover -s cloud_render/concat/tests -v
