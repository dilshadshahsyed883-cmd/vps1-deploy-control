from __future__ import annotations

import json

from .queue_store import reconcile_stale_jobs


def main() -> None:
    print(json.dumps(reconcile_stale_jobs(auto_requeue=True), indent=2))


if __name__ == "__main__":
    main()
