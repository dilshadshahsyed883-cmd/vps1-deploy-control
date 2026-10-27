# Scene Builder: local Media Library source modes — scoped production release

Source change merged as PR #345, commit `edbe27910e30bba3bbb1eef209f9ba9f44260638`.

## User-requested setting

**Settings → Stock Media → Scene Library priority** now displays exactly:
- Scene Media Library Only
- Scene Media Library First → Pexels Fallback

Old Pexels First is absent for newly saved settings. Legacy per-job snapshots are retained.

## Worker behavior

New jobs with local library enabled bundle matched approved local visuals, including intro shots, into their own project input archive. The worker validates intro paths against the extracted input root. Library Only refuses unmatched scenes before upload; Library First uses Pexels for unmatched scenes only. The separate Self Media Library is not read or modified.

## Protected app-scoped release

Trigger only the existing `[vps1-production-release:scene-builder]` guarded production workflow after protected main merge. Preserve independent SML, Auto Script and unrelated applications. Use installed Scene Builder-only installer, health readback and automatic rollback on failure. No database deletion or media catalog cleanup.

Regression checks passed for source changes before this release proposal (local Media Library, no-SML contract, source-of-truth and offline pilot).
