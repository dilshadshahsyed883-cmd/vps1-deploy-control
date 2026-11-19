# Canonical Drive intake and restricted Concat Outputs upload

This optional, independent service reuses the pinned Scene release's
`DriveIntake`, `CloudRender.execute`, and canonical worker HOME/rclone identity.
Existing Scene code, service, OAuth and uploads are unchanged. No automatic
deployment or service activation is attached to this branch.

Protocol 1 uses POST JSON over `/run/concat-drive-bridge/intake.sock`:

| Operation | Request fields | Response |
| --- | --- | --- |
| `/v1/health` | owner | configured canonical remote / protocol |
| `/v1/scan` | owner, link | existing seven-field ZIP candidate records |
| `/v1/download` | owner, row | bounded ZIP stream, byte length, SHA256, JSON filename |

Only the kernel UID of `concatweb` is accepted, in addition to socket filesystem
permissions. The owner is the validated Concat tenant SHA256, supplied only by
the trusted Concat backend; it is not an independent end-user authentication
mechanism. Concat keeps owner authentication, CSRF, isolated queues/spools and
cross-owner denial. The bridge is deliberately a read-only view of the configured
canonical account's authorized source items. It exposes no remote selector,
local path, shell command, config, token, Drive upload or delete operation.

Downloads use per-request mode-0700 temporary directories, original rclone
limits and source-resource-key handling, two bounded workers, and automatic
cleanup.

## Explicitly authorized Concat Outputs MP4 upload

The single output-write operation POST /v1/upload accepts a streamed, previously
verified MP4 only from the kernel UID of the isolated Concat service. It requires
Content-Length, X-Owner (authenticated Concat tenant SHA256), X-Job-ID,
X-File-Name, X-Content-SHA256 and X-Content-MD5. The trusted Bridge generates
the only permitted destination:

  gdrive:Concat Outputs/<owner-sha256>/<job-id>_<mp4-name>

"Concat Outputs" is the fixed Google Drive root for completed Concat MP4s.
No arbitrary remote, folder, command, local path, upload source URL, overwrite
or deletion is accepted. Streams are bounded to 20 GiB, staged privately, and
checked for MP4 ftyp signature, exact length, SHA256 and MD5 before canonical
rclone copyto --immutable. The existing destination is verified first, so a
matching retry is idempotent; conflict is rejected without overwrite. The remote
size + MD5 are checked again after transfer. Raw rclone logs and credentials
never cross the socket.

Only after this verified response does Concat Web set "delivered" and remove its
local copy. Errors preserve the original video and allow its existing Retry
Drive Upload button. This upload-only extension does not change read-only
ZIP discovery/download, Scene OAuth, user rights, YouTube, or Salad.

The bridge may be deployed as a separate approved, pinned release; the browser
does not upload the video. Verify success on the real Google Drive with an
existing retained MP4 and failure/retention with an unauthorized destination.
 Raw errors and requests are never logged. No shared output directory
is readable by Concat; bytes cross the authenticated socket.

## Separate manual activation gate

Use an immutable reviewed VPS1 source SHA; copy this standalone server to
`/opt/concat-drive-bridge/concat_drive_bridge.py` and install the pinned Scene
dependencies into its dedicated venv. The copied script, source directory and
`/etc/concat-drive-bridge/bridge.env` must be root-managed and not writable by
`concatweb`. The env file contains `SCENE_SOURCE=<pinned release>/apps/scene-builder/part01`
and the existing canonical `CLOUD_RENDER_DRIVE_REMOTE` name if nondefault.
It must not contain copied OAuth tokens. Confirm `vps1scene` can traverse the
source and use the existing canonical worker HOME; do not chmod that HOME.

Install the inert unit template with the actual existing canonical service
identity (verify `vps1scene` on the host), then start only the new bridge service.
The socket group is `concatweb`; no supplementary access to private Scene data
is granted. No Scene restart is needed. Point the Concat release at the default
socket or set `CONCAT_DRIVE_BRIDGE_SOCKET` in its private env. Install and verify
the compatible Concat integration code before trying intake.

Verify health from the actual Concat UID; allowed folder scan and queued ZIP
download, denied folder without new rows, resource-key source, interrupted and
corrupt transfers, and unauthorized UID rejection. Real Drive permission and
live render acceptance require this host check; offline tests cannot establish
them. Roll back by restoring the previous Concat release and stopping/removing
only the new bridge unit; keep Scene/OAuth/queues and credentials untouched.
