# Scene Builder duplicate-output filename release — 26 October 2026

Source: PR #320, merged into main.

## Changes
- Scene Builder project history shows _1, _2, _3 only when project names repeat.
- Google Drive output names do not collide: the first successful name is kept; new outputs select the next available _2.mp4, _3.mp4, etc.
- Previously uploaded files are not overwritten or deleted.
- Existing failed Drive uploads can use Retry Drive Upload without rerendering.
- Source code checks (Drive delivery regression, queue UI CI, offline pilot, source-of-truth contract) passed.

## Scope
Scene Builder only. No change to Auto Script, Self Media Library, other applications, or external Google Drive contents. Deployment uses the existing guarded, reversible Scene-only release path.
