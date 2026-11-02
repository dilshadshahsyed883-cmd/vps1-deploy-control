"""Frozen Video Merger Library V1: fail-closed, Library-only HTML overlay.

Never rewrite unrelated Merge, Queue, Proxy or website shell content.
"""
from pathlib import Path


class LibraryReleaseBlocked(RuntimeError):
    pass


def require_exact(condition, reason):
    if not condition:
        raise LibraryReleaseBlocked("VIDEO_LIBRARY_RELEASE_BLOCKED: " + reason)


def block(page, start, end):
    require_exact(page.count(start) == 1 and page.count(end) == 1,
                  "ambiguous Library panel boundary")
    a = page.index(start)
    b = page.index(end, a + len(start))
    require_exact(b > a, "Library panel order changed")
    return page[a:b]


def one_line(page, prefix):
    found = [line for line in page.splitlines() if line.startswith(prefix)]
    require_exact(len(found) == 1, "missing Library function: " + prefix)
    return found[0]


def frozen_library_overlay(live, source, baseline):
    """Apply the exact reviewed Library diff to current live HTML only."""
    require_exact(source.count('id="libraryUploadDrive" type="checkbox"') == 1,
                  "unchecked Drive control missing")
    require_exact(source.count('id="libraryPreviewDialog"') == 1,
                  "preview control missing")
    require_exact(source.count("function viewLibrary(id)") == 1,
                  "preview handler missing")
    require_exact(baseline.count('id="libraryUploadDrive"') == 0,
                  "baseline already patched")

    for start, end in (
        ('    <div id="tab-library" class="tab-panel">',
         '  <div id="tab-proxy" class="tab-panel">'),
        ('function renderLibrary(){', 'function toggleImportTrim(){'),
    ):
        old = block(baseline, start, end)
        new = block(source, start, end)
        require_exact(old != new, "expected change absent")
        if live.count(new) == 1:
            continue
        require_exact(live.count(old) == 1, "live Library panel drift")
        live = live.replace(old, new, 1)

    for prefix in (
        "function resetLibraryForm(){",
        "async function saveLibrary(){",
        "function replaceLibrary(id){",
        "async function deleteLibrary(id){",
    ):
        old = one_line(baseline, prefix)
        new = one_line(source, prefix)
        require_exact(old != new, "expected callback change absent")
        if live.count(new) == 1:
            continue
        require_exact(live.count(old) == 1, "live Library callback drift")
        live = live.replace(old, new, 1)

    require_exact(live.count('id="libraryUploadDrive"') == 1
                  and live.count('id="libraryPreviewDialog"') == 1
                  and live.count("function viewLibrary(id)") == 1
                  and live.count("upload_to_drive:libraryUploadDrive.checked") == 1,
                  "Library overlay incomplete")
    return live
