# VPS_1 dependency and storage design — GitHub preparation only

Auto Script remains OFFLINE. Ten active application groups share **one OS Python
3.12 interpreter** and independent venvs, not ten separate Python installations.
FFmpeg and OS libraries are system-level dependencies installed only once.

## Exact isolation and reuse

- Manifest: `rebuild/dependency-budget.json`. Packages are installed from
  actual source requirements files; no cross-app global `pip install`.
- Installer: `python3 -m rebuild.dependency_budget install --root /tmp/<name> --python /path/to/python3.12`.
  The installer **only** accepts roots strictly below `/tmp`; production
  deployment is not enabled and requires a separately reviewed installer.
- uv: one shared cache inside the staging tree at
  `var/cache/fresh-website/uv`, `UV_LINK_MODE=hardlink` and the cache and
  venvs on the **same filesystem**. Hardlinks reuse identical package files
  without sharing writable venv configuration. A cross-device cache is rejected.
  uv-managed Python is disabled; an existing Python 3.12 interpreter is required.
- Heavy packages remain per-app isolated (Voice Studio, ACS). Shared model
  downloads should be placed under `var/lib/fresh-website/models` by a
  separately authorized model manager after model/version/license checks;
  no model download or symlink is performed during this GitHub-only step.
- Standalone standard-library services receive lightweight venvs with no
  invented third-party requirements.

## Storage policy — no fixed application cap

There is **no 30 GB (or other fixed) application footprint limit** and no
mandatory separate 30 GB investigation, modeling, or capacity-report step.
`rebuild/dependency-budget.json` does not define `application_budget_bytes`.
The builder may install beyond 30 GB when physical host space allows.

The lightweight safety check only verifies currently available host free space
against `minimum_free_bytes` (currently 5,000,000,000 decimal bytes).
It runs within the existing isolated install/check operation, not as a
separate approval or investigation workflow. CI reports unique inode-allocated
disk usage for observability without comparing it to an application cap.

Per-app planning in `rebuild/storage_admission.py` only refuses an install
when the measured host free space after its expected new files would fall
below the safety reserve. Estimates used for disk-full prevention are not a
global app budget, storage quota, or extra capacity research gate.
Actual OS/filesystem limits still apply. Existing artifact-integrity,
non-root runtime, credential, data, service-health and rollback safety
protections are unchanged.

The full ten-app dependency/model/browser stack has not been measured
on the fresh host, and GitHub CI does not prove sufficient physical disk
for a future live installation. Do not claim live space was verified.

## Pre/post cutover

OVH Cloud snapshot must be independently verified before reboot. Existing
production services/data stay intact until the user explicitly approves
rebuild/cutover. The current repo PR remains Draft and unmerged.
