# CI and Actions storage

## Implemented scope

The scoped Concat Studio workflow runs the existing adapter/renderer tests and application self-test in a source-checks job on PR, main-push and manual events. The build-windows job depends on those checks and skips PR events. Push/manual packages retain 7 days. Other VPS1 workflows are unchanged by this focused change.

## Artifact lifecycle

These rules apply to newly uploaded artifacts after the workflow change reaches the branch used by the run. They do not delete or extend existing artifacts, GitHub Releases, branches, container images or live data. Retention is not a storage quota: frequent builds can still accumulate substantial storage within the window.

Before an active candidate expires, preserve its exact package, full source SHA, checksum and acceptance notes in a named prerelease. Keep current and previous verified releases available for rollback. Do not rely on an expiring development artifact as the only rollback copy. Stable downloads should come from a verified versioned release, never a guessed “latest successful run.”

## Verification and limits

Review the source tests separately from Windows frozen-binary or real GPU acceptance. The maintenance PR changes CI scheduling/retention only; it does not certify hardware acceptance or alter deployed services. Merging workflow changes may trigger the existing app-specific checks or publication behavior; inspect the exact event and path filters before merge.
