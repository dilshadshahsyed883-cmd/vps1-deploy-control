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

## Storage budget

`30,000,000,000` decimal bytes for the staged application tree (source
releases, venvs, uv cache, app DBs/queues/uploads/work and shared models).
`5,000,000,000` bytes of host disk remain free at admission and after each
installation. The scanner counts allocated disk blocks once per unique
device/inode to avoid double-counting hardlinked files. It is not an
estimate based on package download size.

**Important:** The budget check is a fail-closed deployment *admission*
gate, **not** a persistent OS/filesystem quota on future uploads/jobs. A
true 30 GB hard cap for application growth requires a filesystem quota
or per-service limits and recurring usage checks after snapshot/cutover.
Python system packages, OS logs and FFmpeg installed outside the app
tree are not counted in the 30 GB application footprint.

The full ten-app dependency/model/Chrome stack has not yet been installed
and measured on the target VPS. CI currently runs an **actual uv install**
for two lightweight independent apps; heavy workers and models remain
explicitly unverified. Never claim the whole site fits in 30 GB without
real post-snapshot VPS measurements.

## Pre/post cutover

OVH Cloud snapshot must be independently verified before reboot. Existing
production services/data stay intact until the user explicitly approves
rebuild/cutover. The current repo PR remains Draft and unmerged.
