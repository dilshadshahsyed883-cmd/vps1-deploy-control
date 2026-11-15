# Guarded HeyGen Studio GPU Config HTTP 403 release

Date: 2026-11-15
Validated change: PR #746 / main commit `0b35ba5b0c5f03cbe58851320dcd85bc50ac4180`.

## Scope
Update Scene Builder's owner-scoped HeyGen Studio to permit authenticated
configuration saving and exact CSRF-protected queue/job actions through the
candidate mutation guard. Fix VPS-side HTTP 403 and expose errors clearly.
Retain per-user credential and job isolation, login/CSRF, Host HTTPS restrictions
and independent admin-only Cloud Render and Experiment Studio policy.

The user-supplied Salad gateway hostname has exact-host permission only.
Secrets must be entered privately into HeyGen Studio GPU Config; none are
committed. No change to GPU image, SaladCloud deployment or other Studios.

## Release and rollback
Use the existing main-branch marker `[vps1-heygen-studio-release]` in
the merge commit to activate the protected Tailscale Scene Builder-only
production deployment. It pins source SHA and verifies the public
assets plus anonymous access restrictions. Failure triggers the existing
guarded Scene Builder rollback.

## Acceptance
PR #746 completed focused HeyGen integration, pre-cutover candidate policy,
role regression and full-history secret checks successfully. Production
deployment verifies site assets and anonymous session restrictions.
The owner must still save both correct credentials and test the live
authenticated SaladCloud GPU endpoint; actual MP4 rendering remains unverified.
