# Kokoro source snapshot

Sanitized source-only mirror of the existing VPS_1 Kokoro app, kept separate from the deployment pilot and every other app.

- Snapshot source SHA-256: `02140e81d9b09fddc10294a535817681305f6d331a11a61d18d01b8bd9601a09`
- Source files: `kokoro_api.py`, `kokoro_worker.py`, `voice_catalog.json`
- Excludes model weights, data, SQLite databases, generated audio, credentials, and backups.
- Kokoro retains its own `data/kokoro_jobs.sqlite3` queue and worker.
- The ONNX/Kokoro synthesis engine and job-processing implementation are preserved unchanged.
- This folder is source control only; existing pilot deployment jobs do not target it.
