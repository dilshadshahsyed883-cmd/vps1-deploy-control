# Scene Builder Settings — banner-only UI release

Source PR: https://github.com/dilshadshahsyed883-cmd/vps1-deploy-control/pull/255

Only user-visible modification: remove the redundant **Project Settings** banner from `/settings`. The **Selected Preset** bar, seven sub-tabs, **Presets** heading, **+ New Preset** button and all cards/behaviors remain unchanged.

Source status: PR #255 merged as `338f7854f2f6c36054a2a60bf926b4c7c797bf1b`; protected checks 22 PASS and one intended skip. First production release run `39600248055` was cancelled by GitHub concurrency before starting any job. No production claim made for that run.

Release routing: use the existing `[vps1-ui-shell-phase-b-release]` guarded native-studio workflow. Its dedicated concurrency lane executes only `vps1-production-deploy install <merge-sha> scene-builder` and preserves all independent app workers. No multi-app UI work is authorized by this record.
