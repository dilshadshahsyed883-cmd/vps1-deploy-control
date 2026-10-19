# Shared reusable modules — compatibility-first extraction

This directory is a versioned home for **actual stateless utilities extracted**
from existing VPS_1 application source code. It is NOT a new daemon, OAuth
vault, background agent, production package or central mutable state manager.

## Extracted candidates

| Module | Existing source | Reuse candidate |
| --- | --- | --- |
| time_utils | Auto Stream V1 scheduled_utc / utc_now | Timezone-aware schedules |
| media_files | Auto Stream V1 StreamEngine._source | Read-only media path validation |
| youtube_ids | Auto Stream V1/V2 CHANNEL_RE | Channel-ID syntax only |

See registry.json for precise source references, versions, adoption state
and deferred stateful candidates.

## Safe adoption sequence

1. Compare exact input/output/error contracts; verify source parity and edge cases.
2. Confirm app-specific authorization, storage and privilege boundaries.
3. Explicitly bundle an immutable version in the independent app release.
4. Add an app-owned adapter if the app has particular error messages/paths.
5. Run both shared-module CI and the consuming application's own regression suite.
6. Deploy only the affected app through its guarded rollback-enabled release.
7. Record the consumer in registry.json AFTER integration is verified.

Current application imports have intentionally NOT been changed. Their
deployment packages might not include the repository root. A shared import
without packaging verification could break an otherwise healthy application.
These files constitute an extracted reusable library, but existing app runtimes
continue using their proven local implementations until migration.

No live services, uploads, media, credentials, queues or databases are changed.
Auto Script remains OFFLINE.

Test: python3 -m unittest discover -s tests -p test_shared_modules.py -v
