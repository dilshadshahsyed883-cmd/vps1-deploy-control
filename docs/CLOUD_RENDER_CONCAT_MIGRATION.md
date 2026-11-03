# Cloud Render Concat migration record — 2026-11-04

Source: Rajudesktop/concat_original; upstream Concat v0.2.8.
Original GitHub Actions run 41718543140 succeeded; artifact Concat-v0.2.8-Windows-x64 (ID 13270812649, 86108691-byte ZIP, expires 2026-12-03).

Target: VPS1 cloud_render/concat — the packaging verifier, tests, README and a new target-owned Windows installer Actions workflow.

This is a copy-first migration: Rajudesktop original package remains intact until target-built installer SHA-256 and action results are verified. Existing binary is in Actions artifact storage, not Git.
User PC Concat projects (concat.json plus media) are NOT stored in Rajudesktop, so there are no actual PC files to transfer from that repository.
Windows EXE is not a Linux cloud executable; native Linux Concat headless rendering, API bridge, Salad GPU validation, Experiment Studio Cloud Render tab, tenant storage and Google Drive upload are separate steps.
No production deployment, no service restart, no Auto Script modification and no removal of original Google Drive files.
