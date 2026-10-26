# Scene Builder — cancelled-project delete production release

This is a protected-main Scene-only release trigger for already-reviewed PR #323
and the guarded idle-queue retry in PR #326.

* Show **Delete Permanently** only for jobs whose status is `cancelled`.
* Require an irreversible action confirmation and project ownership authorization.
* Remove project-owned local files, job history and settings.
* Delete an associated Drive MP4 only when uniquely and cryptographically verified.
* Preserve all other projects and shared media.
* Do not bypass live render safety: the existing deploy guard defers while
  jobs are queued or in flight, and returns without interrupting them.

The previous Scene-only attempt was deferred by live rendering and later became
stale when main advanced. This marker starts a guarded release at the latest main.
