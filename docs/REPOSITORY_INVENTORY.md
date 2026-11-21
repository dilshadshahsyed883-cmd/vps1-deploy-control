# Repository inventory

Source review: 21 November 2026. These are source baselines for maintenance work, not deployed versions. The companion maintenance branches and PRs carry their own commit SHAs.

| Repository | Reviewed main SHA | Role |
| --- | --- | --- |
| `vps1-deploy-control` | `a3dae52409c3d7a956cd37cfaf91139bdd078e68` | Private deployment-control repository. Source and offline CI evidence are separate from verified live runtime state. |
| `concat` | `7964a9e800fef6ed4b2449dba280f8f37b85455f` | Rust/Slint desktop editor plus project-bundle and GPU-rendering components. A Windows build, image startup check, and real GPU acceptance are distinct results. |
| `concat-web` | `0a4595995308ce147eac5bac7f25ad270d7bd17a` | FastAPI dashboard, Concat Studio, uploader and administration application. Scene SSO and per-owner data boundaries are part of the runtime contract. |
| `Rajudesktop` | `727a857e365787f5a4216c0a8da790d36181f42a` | Independent Windows desktop applications share this repository. Always select the app before choosing dependencies, tests or downloads. |
| `voice_clone` | `28c6f8e4e0b98d76f36010dd15f99c64059f7c35` | main is an index and deployment-contract branch. It does not contain a self-contained inference server or publish a model image. |

## Access follow-up

`Imagen`, `heygen`, `Bilal` and `Haseeb` appeared in an earlier account listing but were absent from the latest connected repository search. Root `README.md` requests returned 404. This does not establish that those repositories were deleted or have no documentation: confirm repository visibility/access and canonical branch before writing to them.

## Rollout and completion gates

1. Correct source maps, dated status, contribution and issue/PR guidance in focused documentation PRs.
2. Review workflow retention separately, preserving artifact consumers and app tests. Existing releases and runtime data must not be deleted by housekeeping.
3. Validate changed YAML and event conditions; run the applicable source checks. Windows/frozen-binary checks require GitHub or a Windows host.
4. Keep source review, packaging, actual platform acceptance and production deployment evidence distinct.
5. Verify repository/environment rules with owner access before proposing settings changes. Preserve component licences and gather real app screenshots rather than inventing release evidence.

No repository rename, application migration or production cutover is part of these maintenance changes.
