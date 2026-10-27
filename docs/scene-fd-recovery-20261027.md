# Scene Builder final mux stall — 2026-10-27

- Existing job: `3327eb5e931e417f9daf5bca9af526da` (`Eala_Tokyo_V25`), previously at 97% in final prepend/mux.
- Uploaded job log indicates failure to spawn FFprobe after validated chunk joining due to `OSError: [Errno 24] Too many open files`.
- Production worker uses a distinct immutable code pointer from the web UI. Worker release must pass its existing idle queue check before source refresh; no forced restart while active or queued jobs exist.
- Root cause correction merged in PR #357, commit `09d2f92045fda089e65b7b645589ca2ae168ba4b`: close each SQLite connection after progress/queue DB calls; added regression tests.
- **Do not** delete or restart the job from scratch. Preserve chunk checkpoints, original input and current Drive output state. Resume the same job only after protected production release and health/admission checks.
- This change only requests the pre-existing **read-only** Tailscale Scene backend ownership check; it is not a deployment.
