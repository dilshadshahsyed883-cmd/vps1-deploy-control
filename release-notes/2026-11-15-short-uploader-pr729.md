# Short Uploader queue layout release — 2026-11-15

- Source PR: #729 (merged commit `16c425d33e1ea6c3ca497e42e90bb42f8c1c8b54`).
- Change: remove retired Short Uploader per-job review UI; show `Error / Post State` immediately after `Status`.
- Scope: existing guarded Short Studio workflow, **local-upload-manager only** (`shortstudio` instance); do not deploy Short Maker or Song Uploader.
- Invariants: preserve all job records, media, OAuth, remote upload IDs and duplicate-upload ledger; Auto Script remains offline.
- Gate: run existing protected-main release only with the documented `vps1-shortstudio-final-ui` intent; fail closed on host idle/health guards, and preserve rollback.
- This document records release intent; no application or deployment script source changes are introduced.
