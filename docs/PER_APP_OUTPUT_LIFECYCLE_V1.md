# VPS_1 — Per-Application Drive-Verified Output Lifecycle (20 Oct 2026)

**Owner decision:** Each app independently owns output upload, verification, cleanup and retries. The Storage Manager provides a cross-app dashboard only; no host-wide media deletion worker or shared output processor is authorized.

## Master rule
1. At project/job completion, deliver every required output to its configured Google Drive destination. Verify actual remote content (at least remote size match; checksum where supported), store durable Drive receipt/location and complete downstream handoffs.
2. Only after the complete project's jobs are terminal/successful, uploaded/verified, and no active/retry/consumer dependency remains, delete its own disposable job-scoped original input copies, staging, intermediate renders, temp/cache and locally duplicated output.
3. Keep failed/pending uploads, in-progress jobs, sole surviving originals, unverified remote objects, unrelated projects, shared media libraries/assets, models, databases, OAuth secrets, user credentials, backups and rollback releases. Do not delete file paths supplied by users outside the app's safe managed workspace.
4. Cleanup immediately after verified delivery; for cache with no delivery requirement, 12-hour eligibility applies only to safely identified disposable and inactive material. Log the result, bytes recovered, blocked reasons and retained paths. Retain tiny job state/receipts for status/history.
5. Existing users must not be prompted each time. In new apps make this default at implementation time. Each app owns its controls/cleanup worker; Storage Manager only aggregates status and safe per-app control links.
6. Never call a job "cleaned" until disk removal succeeded. Treat permissions/remote ambiguity as retain-and-retry, not as implicit permission to delete. Keep Auto Script OFFLINE.

## Verified source audit (not production claims)
- **Short Maker:** per-job Drive copy + remote MD5 and size; verified-clean now enforced when Drive delivery selected and job temp directories included.
- **Auto Caption / Video Studio:** web-flow cleanup already existed. Hardened to require *all sibling jobs* complete, uploaded and verified before deleting any shared project directory. Legacy explicit retention and preview references stay protected.
- **Video Merger:** already deletes its per-job workspace on successful Drive delivery; strengthened remote verification to match local byte count.
- **Self Media Library — owner exception, 22 Oct 2026:** VPS-only reusable clips/images. No Drive upload/retry/deletion. Original and rejected clips are removed only after complete manual consecutive-pair review, local hash/decode verification and no active job/editor dependency. Other applications retain the Drive rule.
- **Voice Studio (clone spooler):** already has per-job Drive-verified receipt-gated audio/text cleanup; keep protected library/media and other voice engines distinct.
- **Scene Builder:** existing project-scoped cleanup in Storage Manager scanner is *not* app-owned; migrate delivery/verification hook into Scene runtime before enabling deletion.
- **Local Upload Manager / Auto Uploader:** YouTube uploads and Google Drive source staging are not equivalent to uploading the output to Google Drive; retain active publish sources and queue data until genuine receipt + downstream completion. App-specific integration still required.
- **Short Stream / Auto Stream:** active streams and any reused media must stay until stream ends and all consumers release the files. App-specific integration required.
- **Kokoro, Script Work:** support engines; preserve reusable model/data and only remove truly job-scoped verified disposable outputs after a lifecycle audit.
- **VPS Storage Manager:** monitoring/visibility only for app outputs; existing deployment-artifact GC remains a distinct 12h safety-controlled process.
- **Auto Script:** remains OFFLINE; no activation.

## Completion evidence and rollout
- Source changes + regression contract: this PR. Source/CI passing is **not** production rollout.
- App-specific deployments and authenticated readback required before reporting any app as deployed/live.
- Follow-up application runtime adapters required for Scene Builder, Auto Uploader/Stream, Short Stream and other engines without complete proven verification hooks. No risky blanket rmtree scan.
- Application-wise Master Google Doc records: issue → fix → verification → PR/SHA → deployment proof, with honest status.
