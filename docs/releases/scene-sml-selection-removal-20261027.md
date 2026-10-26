# Scene Builder SML selector removal: scoped production release

Change-set merged by PR #343: remove SML selection from Scene Builder frontend, web backend, and independent worker. Preserve standalone Self Media Library and Scene Builder-local media library. Only `scene-builder` is in scope for this rollout.

The existing protected-main, app-scoped production workflow performs guarded installation, health readback and rollback; no changes to Auto Script or independent SML service are requested.

Source commit: `04cd42fb9381c98e061da1051f2985590a8971fe`.
