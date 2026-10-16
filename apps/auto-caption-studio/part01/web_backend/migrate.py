from __future__ import annotations

import json

from .database import migration_status, run_migrations


def main() -> None:
    applied = run_migrations()
    print(
        json.dumps(
            {"applied_now": applied, "migrations": migration_status()},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
