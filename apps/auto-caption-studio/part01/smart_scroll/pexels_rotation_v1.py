"""Fair, restart-safe rotation for cached Pexels still-image backgrounds.

The ledger is private metadata inside the selected reusable library; media files,
Pexels search, downloads, and video clip selection are never changed.
"""
from __future__ import annotations

import fcntl
import json
import os
import random
import tempfile
from pathlib import Path
from typing import Iterable


STATE_NAME = '.acs_pexels_image_rotation_v1.json'
LOCK_NAME = '.acs_pexels_image_rotation_v1.lock'


def select_images(images: Iterable[Path], needed: int, randomize: bool, library: Path) -> list[str]:
    """Draw a no-repeat shuffled bag across renders; repeat only after exhaustion."""
    pool = list(dict.fromkeys(str(Path(p).resolve()) for p in images))
    count = max(0, int(needed))
    if not pool or not count:
        return []
    if not randomize:
        return [pool[i % len(pool)] for i in range(count)]
    base = Path(library).resolve()
    # The existing media directory is reused. Hidden metadata is not an image.
    if not base.is_dir():
        raise FileNotFoundError('Pexels image library is missing')
    with (base / LOCK_NAME).open('a+b') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state_path = base / STATE_NAME
        try:
            state = json.loads(state_path.read_text(encoding='utf-8'))
            if not isinstance(state, dict) or state.get('version') != 1:
                state = {}
        except (OSError, ValueError, TypeError):
            state = {}
        eligible = set(pool)
        previous = set(str(x) for x in state.get('known', []) if isinstance(x, str))
        remaining = list(dict.fromkeys(str(x) for x in state.get('remaining', []) if isinstance(x, str) and x in eligible))
        additions = [p for p in pool if p not in previous and p not in remaining]
        random.shuffle(additions)
        remaining.extend(additions)
        last = str(state.get('last') or '')
        selected: list[str] = []
        while len(selected) < count:
            if not remaining:
                remaining = list(pool)
                random.shuffle(remaining)
            # Avoid adjacent duplicates, including across separate video jobs.
            if len(pool) > 1 and remaining[0] == last:
                ix = next((i for i, p in enumerate(remaining) if p != last), -1)
                if ix > 0:
                    remaining[0], remaining[ix] = remaining[ix], remaining[0]
                elif ix < 0:
                    # The bag contains only the last item. Complete it now and
                    # start a new cycle without a duplicate boundary.
                    # This case is unavoidable if there is only one pool image.
                    remaining = list(pool)
                    random.shuffle(remaining)
                    ix = next((i for i, p in enumerate(remaining) if p != last), -1)
                    if ix > 0:
                        remaining[0], remaining[ix] = remaining[ix], remaining[0]
            choice = remaining.pop(0)
            selected.append(choice)
            last = choice
        updated = {'version': 1, 'known': pool, 'remaining': remaining, 'last': last}
        fd, temp_name = tempfile.mkstemp(prefix='.acs_pexels_rotation.', suffix='.tmp', dir=str(base))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as output:
                os.fchmod(output.fileno(), 0o600)
                json.dump(updated, output, ensure_ascii=False, separators=(',', ':'))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_name, state_path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return selected

