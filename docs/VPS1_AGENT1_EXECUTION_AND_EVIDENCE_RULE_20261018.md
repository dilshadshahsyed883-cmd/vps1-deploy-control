# VPS_1 operating rule: Agent 1, small commands, and GitHub evidence (2026-10-18)

## Scope and authority

This document records the owner's revised execution preference for the current VPS_1 workstream. At the start of each new chat, establish the VPS number and Agent number once unless explicitly provided. For the chat in which this update was requested: **VPS_1 + Agent 1** are locked, and the agent may change only on explicit owner instruction. This document does not itself authorize deployment, production changes, or switching the agent in another chat.

The working objective is to **finish the requested operational task**, not stop at planning or reporting. Where feasible, do GitHub-first source changes, automated checks and actual installation through the authorized host-execution workflow. The owner performs functional/UAT testing; implementers still collect objective installation, service and route evidence and must not claim an unverified live state.

## Small-command exception

- For **one narrow, low-risk terminal command** (for example a read-only check, a service-status query, or a controlled trivial maintenance action), a new Drive package does **not** have to be uploaded each time. Supply **one complete copy/paste terminal command**, with all paths/arguments specified; no placeholders or editing.
- Do not use this exception for bulk operations, uncertain impacts, multi-step deployment, broad package upgrades, destructive cleanup, credentials, secrets, migrations, data changes, or production cutovers. Use the selected Agent and canonical Bridge package/report workflow for those.
- Treat terminal output, including screenshots, as an observation. Verify exit status and relevant subsequent state before concluding that a change succeeded. Never infer app health from an SSH login alone.
- For a small command, capture the output/error or concise redacted user-provided evidence and preserve a finding in GitHub. If a step is skipped, failed or blocked, record that fact rather than marking it PASS.

## Canonical Agent 1 Drive Bridge execution

Agent and VPS lock for this recorded workstream: **VPS_1 / Agent 1**.

- Packages: `Automation_Master_System/VPS_1/01_COMMANDS/Agent 1/PACKAGES/` **only**.
- Execution report and evidence: `Automation_Master_System/VPS_1/02_REPORTS/` **only**.
- rclone configuration: `/home/ubuntu/.config/masterctl/rclone.conf`.
- Package execution command must be one complete exact command using `rclone --config /home/ubuntu/.config/masterctl/rclone.conf copyto` from the fully named Agent 1 package into the fully named local `/tmp/` file, then `chmod +x` and `bash` that file.
- Report read command must be one complete exact `rclone --config /home/ubuntu/.config/masterctl/rclone.conf cat` command pointing to the exact expected named report.
- Workflow: inspect current report/live evidence -> prepare scoped package -> provide one exact command -> owner runs and replies DONE -> read **the exact expected report** -> verify status/errors/evidence -> record finding -> only then prepare the next production modification.
- **DONE means execution was attempted, not necessarily PASS.** Do not call a task fixed, installed, deployed, live or complete without matching evidence.

## GitHub change and finding record

Use this repository as the versioned, **PUBLIC** source-of-truth for *sanitized* process notes and code changes. Prefer an existing related issue/PR and avoid duplicate records. Keep protected `main` and its required checks intact; use a PR for changes.

For each relevant step, record:

1. **Purpose and target** — why it was required, VPS/Agent lock, scope, affected app.
2. **Prior condition / source** — issue, prior report name or redacted observation and timestamp.
3. **Action/process** — code commit or safe command/package identifier, without secrets.
4. **Finding** — actual output, failure, limitations and alternative explanations.
5. **Evidence** — report filename or CI/run URL, tested command, exit code, post-action evidence.
6. **Status and next step** — NOT STARTED, EXECUTED/UNVERIFIED, BLOCKED, FAILED or VERIFIED/PASS; explicit owner testing separately.

Do not commit private IPs where unnecessary, authentication material, `.env`, full terminal logs with secrets, user data, production media, backup archives or sensitive report content. The underlying Drive report stays in the locked Drive location; GitHub receives sanitized findings and links or report identifiers only.

## Initial 2026-10-18 observation (not an execution verification)

- Owner-supplied Ubuntu SSH terminal screenshot shows an interactive `ubuntu@` shell prompt and a successful-looking interactive login screen.
- The login banner says **"System restart required"** and mentions an available Ubuntu release upgrade. These are notifications, not evidence of a failed app, a completed update, current uptime, or production readiness.
- **Status: OBSERVED ONLY.** No reboot, upgrade, service status change, deployment or production verification performed under this documentation update.
- No OS upgrade or reboot should be triggered merely because the login banner suggests it; review impact and active workloads separately if a reboot is requested.

## Operational success rule

Record actions accurately and focus on completing the assigned app/task through permitted channels. A GitHub commit or a report alone is not proof that the VPS is deployed or the website is publicly reachable.
