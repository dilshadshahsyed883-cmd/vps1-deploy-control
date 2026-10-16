from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

import script_extractor_service as extractor


INTRO_ROOT = extractor.DATA_DIR / "processor_intros"
PROCESSED_FOLDER_NAME = "Processed Scripts"
INTRO_ROOT.mkdir(parents=True, exist_ok=True)
URL_RE = re.compile(r"https?://\S+", re.I)
_PROCESSOR_WORKER_STARTED = False
_PROCESSOR_WORKER_LOCK = threading.Lock()


def _safe(value: str, fallback: str = "item") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    return cleaned[:100] or fallback


def _text_from_content(value: Any) -> str:
    if isinstance(value, dict):
        if isinstance(value.get("textRun"), dict):
            return str(value["textRun"].get("content") or "")
        return "".join(_text_from_content(item) for item in value.values())
    if isinstance(value, list):
        return "".join(_text_from_content(item) for item in value)
    return ""


def _all_tabs(document: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(tabs: Any) -> None:
        for tab in tabs if isinstance(tabs, list) else []:
            if not isinstance(tab, dict):
                continue
            found.append(tab)
            walk(tab.get("childTabs"))

    walk(document.get("tabs"))
    return found


def _doc_tab_text(document: dict[str, Any], job: dict[str, Any]) -> str:
    wanted_id = str(job.get("doc_tab_id") or "")
    wanted_title = str(job.get("permanent_id") or "")
    tabs = _all_tabs(document)
    for tab in tabs:
        properties = tab.get("tabProperties") or {}
        if (
            wanted_id
            and str(properties.get("tabId") or tab.get("tabId") or "")
            == wanted_id
        ) or (
            wanted_title
            and str(properties.get("title") or tab.get("title") or "")
            == wanted_title
        ):
            return _text_from_content(tab.get("documentTab") or tab.get("body") or tab)
    return _text_from_content(document.get("body") or document) if not tabs else ""


def _job_script_text(
    owner: str,
    job: dict[str, Any],
    document_cache: dict[str, dict[str, Any] | None],
) -> str:
    document_id = str(job.get("document_id") or "")
    if not document_id:
        return ""
    if document_id not in document_cache:
        try:
            document_cache[document_id] = extractor._doc_get(owner, document_id)
        except Exception:
            document_cache[document_id] = None
    document = document_cache.get(document_id)
    return _doc_tab_text(document, job).strip() if document else ""


def _init() -> None:
    with extractor._conn() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS processor_history (
                owner TEXT NOT NULL,
                category TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                source_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(owner, category, content_hash)
            );
            CREATE TABLE IF NOT EXISTS processor_tabs (
                owner TEXT NOT NULL,
                group_name TEXT NOT NULL,
                source_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                tab_id TEXT NOT NULL,
                tab_title TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(owner, group_name, source_id)
            );
            """
        )
        connection.commit()


def _setting(owner: str, key: str, fallback: Any) -> Any:
    with extractor._conn() as connection:
        row = connection.execute(
            "SELECT value FROM settings WHERE owner=? AND key=?", (owner, key)
        ).fetchone()
    if not row:
        return fallback
    try:
        return json.loads(row["value"])
    except Exception:
        return row["value"]


def _set_setting(owner: str, key: str, value: Any) -> None:
    with extractor._conn() as connection:
        connection.execute(
            """INSERT INTO settings(owner,key,value) VALUES(?,?,?)
               ON CONFLICT(owner,key) DO UPDATE SET value=excluded.value""",
            (owner, key, json.dumps(value, ensure_ascii=False)),
        )
        connection.commit()


def _categories(owner: str) -> list[str]:
    values = _setting(owner, "processor:categories", ["general"])
    categories = sorted(
        {
            str(value).strip()[:80]
            for value in values
            if str(value).strip()
        },
        key=str.lower,
    )
    return categories or ["general"]


def _intros(owner: str) -> list[str]:
    directory = INTRO_ROOT / _safe(owner)
    directory.mkdir(parents=True, exist_ok=True)
    return sorted(
        [path.name for path in directory.glob("*.txt") if path.is_file()],
        key=str.lower,
    )


def _batch_group(owner: str, batch_id: str) -> str:
    if not batch_id:
        return ""
    with extractor._conn() as connection:
        row = connection.execute(
            "SELECT group_name FROM batches WHERE owner=? AND batch_id=?",
            (owner, batch_id),
        ).fetchone()
    return str(row["group_name"] or "") if row else ""


def _global_processor_config(owner: str) -> dict[str, Any]:
    return {
        "auto_process_extracted_batch": bool(
            _setting(owner, "processor:global:auto_process_extracted_batch", False)
        ),
        "auto_process_enabled_at": str(
            _setting(owner, "processor:global:auto_process_enabled_at", "") or ""
        ),
        "removal_words": str(
            _setting(owner, "processor:global:removal_words", "") or ""
        ),
    }


def _set_auto_process(owner: str, enabled: bool) -> None:
    current = _global_processor_config(owner)
    _set_setting(owner, "processor:global:auto_process_extracted_batch", bool(enabled))
    if enabled and not current["auto_process_extracted_batch"]:
        _set_setting(owner, "processor:global:auto_process_enabled_at", extractor._now())


def _config(owner: str, batch_id: str) -> dict[str, Any]:
    global_config = _global_processor_config(owner)
    group_name = _batch_group(owner, batch_id)
    group_intro = str(
        _setting(owner, f"processor:group_intro:{group_name}", "") or ""
    ) if group_name else ""
    result: dict[str, Any] = {
        "batch_id": batch_id,
        "category": "general",
        "intro_filename": group_intro,
        "start_serial": 1,
        "minimum_size": 0,
        "minimum_unit": "KB",
        "maximum_size": 0,
        "maximum_unit": "KB",
        "duplicate_check": True,
        "remove_words": False,
        "removal_words": global_config["removal_words"],
        "skip_failed_scripts": True,
        "auto_process_extracted_batch": global_config["auto_process_extracted_batch"],
        "processed_document_id": "",
        "processed_document_url": "",
        "processed_folder_url": "",
        "last_processed": 0,
        "last_skipped": 0,
        "processing_status": "ready",
        "processing_error": "",
        "processed_at": "",
    }
    with extractor._conn() as connection:
        rows = connection.execute(
            "SELECT key,value FROM settings WHERE owner=? AND key LIKE ?",
            (owner, f"processor:{batch_id}:%"),
        ).fetchall()
    prefix = f"processor:{batch_id}:"
    for row in rows:
        key = str(row["key"])[len(prefix) :]
        try:
            result[key] = json.loads(row["value"])
        except Exception:
            result[key] = row["value"]
    # Global Phase 2 settings always win over stale per-batch copies.
    result["removal_words"] = global_config["removal_words"]
    result["auto_process_extracted_batch"] = global_config[
        "auto_process_extracted_batch"
    ]
    if not str(result.get("intro_filename") or "") and group_intro:
        result["intro_filename"] = group_intro
    return result


def _save(owner: str, batch_id: str, values: dict[str, Any]) -> dict[str, Any]:
    if "removal_words" in values:
        _set_setting(
            owner,
            "processor:global:removal_words",
            str(values.get("removal_words") or ""),
        )
    if "auto_process_extracted_batch" in values:
        _set_auto_process(owner, bool(values.get("auto_process_extracted_batch")))
    group_name = _batch_group(owner, batch_id)
    if group_name and "intro_filename" in values:
        _set_setting(
            owner,
            f"processor:group_intro:{group_name}",
            str(values.get("intro_filename") or ""),
        )
    result = _config(owner, batch_id)
    editable = {
        "batch_id",
        "category",
        "intro_filename",
        "start_serial",
        "minimum_size",
        "minimum_unit",
        "maximum_size",
        "maximum_unit",
        "duplicate_check",
        "remove_words",
        "skip_failed_scripts",
        "processed_document_id",
        "processed_document_url",
        "processed_folder_url",
        "last_processed",
        "last_skipped",
        "processing_status",
        "processing_error",
        "processed_at",
    }
    result.update({key: values[key] for key in editable if key in values})
    result["batch_id"] = batch_id
    result["removal_words"] = _global_processor_config(owner)["removal_words"]
    result["auto_process_extracted_batch"] = _global_processor_config(owner)[
        "auto_process_extracted_batch"
    ]
    result["start_serial"] = max(1, int(result.get("start_serial") or 1))
    result["minimum_size"] = max(0, float(result.get("minimum_size") or 0))
    result["maximum_size"] = max(0, float(result.get("maximum_size") or 0))
    result["minimum_unit"] = (
        "MB" if str(result.get("minimum_unit")).upper() == "MB" else "KB"
    )
    result["maximum_unit"] = (
        "MB" if str(result.get("maximum_unit")).upper() == "MB" else "KB"
    )
    with extractor._conn() as connection:
        for key, value in result.items():
            if key in {"removal_words", "auto_process_extracted_batch"}:
                continue
            connection.execute(
                """INSERT INTO settings(owner,key,value) VALUES(?,?,?)
                   ON CONFLICT(owner,key) DO UPDATE SET value=excluded.value""",
                (
                    owner,
                    f"processor:{batch_id}:{key}",
                    json.dumps(value, ensure_ascii=False),
                ),
            )
        connection.commit()
    return result


def _state(owner: str, batch_id: str = "") -> dict[str, Any]:
    base = extractor._state(owner, "", batch_id)
    all_batches: list[dict[str, Any]] = []
    with extractor._conn() as connection:
        all_batches = [
            dict(row)
            for row in connection.execute(
                """SELECT b.*,COUNT(j.permanent_id) AS link_count
                   FROM batches b LEFT JOIN jobs j
                     ON j.owner=b.owner AND j.batch_id=b.batch_id
                   WHERE b.owner=? AND b.status='completed'
                   GROUP BY b.owner,b.batch_id
                   ORDER BY b.created_at DESC,b.batch_id DESC""",
                (owner,),
            ).fetchall()
        ]
    selected = next(
        (batch for batch in all_batches if batch["batch_id"] == batch_id),
        all_batches[0] if all_batches else None,
    ) or {}
    name = str(selected.get("batch_id") or batch_id or "")
    config = _config(owner, name) if name else {}
    document_url = str(config.get("processed_document_url") or "")
    ready = bool(name and document_url)
    return {
        "groups": all_batches,
        "batches": all_batches,
        "selected_group": selected,
        "selected_batch": selected,
        "config": config,
        "categories": _categories(owner),
        "intros": _intros(owner),
        "counts": base.get("counts", {}),
        "output": {
            "ready": ready,
            "name": name if ready else "",
            "document_id": config.get("processed_document_id") or "",
            "document_url": document_url,
            "drive_url": config.get("processed_folder_url") or "",
            "processed": int(config.get("last_processed") or 0),
            "skipped": int(config.get("last_skipped") or 0),
            "processing_status": str(config.get("processing_status") or "ready"),
            "processing_error": str(config.get("processing_error") or ""),
            "processed_at": str(config.get("processed_at") or ""),
        },
    }


async def _json(request: Request) -> dict[str, Any]:
    try:
        value = await request.json()
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _whole_phrase_remove(text: str, values: str) -> str:
    phrases = [item.strip() for item in str(values or "").split(",") if item.strip()]
    for phrase in sorted(set(phrases), key=len, reverse=True):
        text = re.sub(
            rf"(?<!\w){re.escape(phrase)}(?!\w)",
            "",
            text,
            flags=re.IGNORECASE,
        )
    return text


def _clean_extracted_script(
    text: str, remove_words: bool, removal_words: str
) -> str:
    """Return only the story body, never extractor metadata or source links."""
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    marker = re.search(r"(?im)^Extracted UTC:[^\n]*\n+", value)
    if marker:
        value = value[marker.end() :]
    else:
        transcript = re.search(r"(?im)^Transcript\b\s*[:\-]?\s*", value)
        if transcript:
            value = value[transcript.end() :]
        else:
            source = re.search(
                r"https?://(?:www\.)?(?:youtube\.com|youtu\.be)/\S+", value, re.I
            )
            if source:
                value = value[source.end() :]
    value = re.sub(r"(?is)^\s*Transcript\b\s*[:\-]?\s*", "", value, count=1)
    value = URL_RE.sub("", value)
    metadata_line = re.compile(
        r"(?im)^\s*(?:Title|Source title|Source URL|Video ID|Permanent ID|Extracted UTC)\s*:[^\n]*\n?"
    )
    value = metadata_line.sub("", value)
    if remove_words:
        value = _whole_phrase_remove(value, removal_words)
    value = "\n".join(re.sub(r"[ \t]+", " ", line).strip() for line in value.splitlines())
    return re.sub(r"\n{3,}", "\n\n", value).strip()


def _bytes(value: float, unit: str) -> int:
    return int(value * (1024 * 1024 if str(unit).upper() == "MB" else 1024))


def _tab_by_id(document: dict[str, Any], tab_id: str) -> dict[str, Any] | None:
    return next(
        (tab for tab in extractor._tab_data(document) if extractor._tab_id(tab) == tab_id),
        None,
    )


def _tab_mapping(owner: str, group: str, source_id: str) -> str:
    with extractor._conn() as connection:
        row = connection.execute(
            """SELECT tab_id FROM processor_tabs
               WHERE owner=? AND group_name=? AND source_id=?""",
            (owner, group, source_id),
        ).fetchone()
    return str(row["tab_id"] or "") if row else ""


def _save_tab_mapping(
    owner: str,
    group: str,
    source_id: str,
    document_id: str,
    tab_id: str,
    tab_title: str,
) -> None:
    with extractor._conn() as connection:
        connection.execute(
            """INSERT INTO processor_tabs(
                 owner,group_name,source_id,document_id,tab_id,tab_title,updated_at
               ) VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(owner,group_name,source_id) DO UPDATE SET
                 document_id=excluded.document_id,
                 tab_id=excluded.tab_id,
                 tab_title=excluded.tab_title,
                 updated_at=excluded.updated_at""",
            (owner, group, source_id, document_id, tab_id, tab_title, extractor._now()),
        )
        connection.commit()


def _unique_tab_title(document: dict[str, Any], requested: str) -> str:
    existing = {extractor._tab_title(tab).casefold() for tab in extractor._tab_data(document)}
    base = str(requested or "Processed Script").strip()[:90]
    candidate = base
    suffix = 2
    while candidate.casefold() in existing:
        candidate = f"{base[:84]} ({suffix})"
        suffix += 1
    return candidate


def _add_tab(document_id: str, title: str) -> str:
    response = extractor._docs_service().documents().batchUpdate(
        documentId=document_id,
        body={"requests": [{"addDocumentTab": {"tabProperties": {"title": title}}}]},
    ).execute()
    reply = (response.get("replies") or [{}])[0].get("addDocumentTab") or {}
    tab_id = str((reply.get("tabProperties") or {}).get("tabId") or reply.get("tabId") or "")
    if not tab_id:
        document = extractor._doc_get("", document_id)
        tab_id = next(
            (
                extractor._tab_id(tab)
                for tab in extractor._tab_data(document)
                if extractor._tab_title(tab) == title
            ),
            "",
        )
    if not tab_id:
        raise RuntimeError(f"Google Docs did not return a tab ID for {title}")
    return tab_id


def _ensure_owned_tab(
    owner: str,
    group: str,
    source_id: str,
    document_id: str,
    requested_title: str,
    use_default_tab: bool = False,
) -> tuple[str, str]:
    document = extractor._doc_get(owner, document_id)
    mapped = _tab_mapping(owner, group, source_id)
    if mapped and _tab_by_id(document, mapped):
        tab = _tab_by_id(document, mapped) or {}
        return mapped, extractor._tab_title(tab)
    if use_default_tab:
        default = extractor._tab_data(document)[0]
        tab_id = extractor._tab_id(default)
        title = requested_title
        if tab_id:
            try:
                extractor._docs_service().documents().batchUpdate(
                    documentId=document_id,
                    body={
                        "requests": [
                            {
                                "updateDocumentTabProperties": {
                                    "tabProperties": {"tabId": tab_id, "title": title},
                                    "fields": "title",
                                }
                            }
                        ]
                    },
                ).execute()
                _save_tab_mapping(owner, group, source_id, document_id, tab_id, title)
                return tab_id, title
            except Exception:
                pass
    title = _unique_tab_title(document, requested_title)
    tab_id = _add_tab(document_id, title)
    _save_tab_mapping(owner, group, source_id, document_id, tab_id, title)
    return tab_id, title


def _write_owned_tab(document_id: str, tab_id: str, text: str) -> None:
    document = extractor._doc_get("", document_id)
    tab = _tab_by_id(document, tab_id)
    if not tab:
        raise RuntimeError("Mapped Google Docs tab no longer exists")
    extractor._replace_tab_text(document_id, tab_id, extractor._tab_end(tab), text)


def _processed_document(
    owner: str, group_name: str, batch_id: str, config: dict[str, Any]
) -> tuple[str, str, str, bool]:
    folder_id, folder_url = extractor._group_folder(PROCESSED_FOLDER_NAME, group_name)
    extractor._ensure_drive_folder("Used", folder_id)
    document_id = str(config.get("processed_document_id") or "")
    if document_id:
        try:
            extractor._doc_get(owner, document_id)
            extractor._move_root_file(document_id, folder_id)
            return (
                document_id,
                f"https://docs.google.com/document/d/{document_id}/edit",
                folder_url,
                False,
            )
        except Exception:
            document_id = ""
    title = batch_id
    created = extractor._docs_service().documents().create(body={"title": title}).execute()
    document_id = str(created["documentId"])
    extractor._place_created_file(document_id, folder_id)
    extractor._share_editor(document_id)
    return (
        document_id,
        f"https://docs.google.com/document/d/{document_id}/edit",
        folder_url,
        True,
    )


def _move_extracted_source_to_used(group_name: str, document_id: str) -> str:
    if not document_id:
        return ""
    group_folder_id, _ = extractor._group_folder(
        extractor.EXTRACTED_FOLDER_NAME, group_name
    )
    used_folder_id, used_url = extractor._ensure_drive_folder("Used", group_folder_id)
    drive = extractor._drive_service()
    metadata = drive.files().get(fileId=document_id, fields="parents").execute()
    parents = [str(value) for value in metadata.get("parents") or []]
    if used_folder_id in parents:
        return used_url
    if group_folder_id not in parents:
        raise RuntimeError(
            "Extracted source is not in the expected Group folder; refusing an unsafe move"
        )
    drive.files().update(
        fileId=document_id,
        addParents=used_folder_id,
        removeParents=group_folder_id,
        fields="id,parents",
    ).execute()
    return used_url


def _process_batch(
    owner: str, batch_id: str, values: dict[str, Any] | None = None
) -> dict[str, Any]:
    config = _save(owner, batch_id, values or {}) if values is not None else _config(owner, batch_id)
    _save(owner, batch_id, {"processing_status": "running", "processing_error": ""})
    try:
        with extractor._conn() as connection:
            batch_row = connection.execute(
                """SELECT group_name,document_id,status FROM batches
                   WHERE owner=? AND batch_id=?""",
                (owner, batch_id),
            ).fetchone()
            jobs = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM jobs WHERE owner=? AND batch_id=? ORDER BY created_at",
                    (owner, batch_id),
                ).fetchall()
            ]
        if not batch_row:
            raise HTTPException(404, "Batch not found")
        failed_jobs = [job for job in jobs if job["status"] != "completed"]
        if failed_jobs and not config["skip_failed_scripts"]:
            raise HTTPException(
                409,
                f"{len(failed_jobs)} scripts are not complete. Enable Skip failed scripts or retry them.",
            )
        completed_jobs = [job for job in jobs if job["status"] == "completed"]

        intro_lines: list[str] = []
        intro_name = _safe(str(config.get("intro_filename") or ""), "")
        if intro_name:
            intro_path = INTRO_ROOT / _safe(owner) / intro_name
            if not intro_path.is_file():
                raise HTTPException(409, "The selected intro file no longer exists")
            intro_lines = [
                line.strip()
                for line in intro_path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
                if line.strip()
            ]
            if not intro_lines:
                raise HTTPException(409, "The selected intro file has no usable lines")

        minimum = _bytes(
            float(config.get("minimum_size") or 0),
            str(config.get("minimum_unit") or "KB"),
        )
        maximum = _bytes(
            float(config.get("maximum_size") or 0),
            str(config.get("maximum_unit") or "KB"),
        )
        category = _safe(str(config.get("category") or "general"), "general")
        current_source_ids = {str(job["permanent_id"]) for job in completed_jobs}
        with extractor._conn() as connection:
            known_hashes = {
                row["content_hash"]
                for row in connection.execute(
                    """SELECT content_hash,source_id FROM processor_history
                       WHERE owner=? AND category=?""",
                    (owner, category),
                )
                if str(row["source_id"]) not in current_source_ids
            }

        document_cache: dict[str, dict[str, Any] | None] = {}
        skipped: list[str] = [
            f"{job['permanent_id']}\t{job['status']}\t{job.get('error') or 'not completed'}"
            for job in failed_jobs
        ]
        candidates: list[tuple[int, dict[str, Any], str, str]] = []
        batch_hashes: set[str] = set()
        for serial, job in enumerate(completed_jobs, int(config["start_serial"])):
            try:
                job["document_id"] = batch_row["document_id"]
                extracted = _job_script_text(owner, job, document_cache)
                if not extracted:
                    skipped.append(
                        f"{job['permanent_id']}\tempty\tGoogle Docs tab had no readable text"
                    )
                    continue
                clean_body = _clean_extracted_script(
                    extracted,
                    bool(config["remove_words"]),
                    str(config.get("removal_words") or ""),
                )
                if not clean_body:
                    skipped.append(
                        f"{job['permanent_id']}\tempty\tNo script body remained after cleaning"
                    )
                    continue
                size = len(clean_body.encode("utf-8"))
                if minimum and size < minimum:
                    skipped.append(
                        f"{job['permanent_id']}\ttoo-small\t{size} bytes < {minimum}"
                    )
                    continue
                if maximum and size > maximum:
                    skipped.append(
                        f"{job['permanent_id']}\ttoo-large\t{size} bytes > {maximum}"
                    )
                    continue
                normalized = re.sub(r"\s+", " ", clean_body).strip().casefold()
                content_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
                if config["duplicate_check"] and (
                    content_hash in known_hashes or content_hash in batch_hashes
                ):
                    skipped.append(
                        f"{job['permanent_id']}\tduplicate\tclean script body already processed"
                    )
                    continue
                final_body = clean_body
                first_line = next(
                    (line.strip() for line in clean_body.splitlines() if line.strip()), ""
                )
                if intro_lines and first_line not in set(intro_lines):
                    final_body = f"{secrets.choice(intro_lines)}\n\n{clean_body}"
                candidates.append((serial, job, final_body.rstrip() + "\n", content_hash))
                batch_hashes.add(content_hash)
            except Exception as item_exc:
                skipped.append(
                    f"{job['permanent_id']}\tprocessing-error\t{str(item_exc).replace(chr(10), ' ')[:500]}"
                )
                continue

        extractor._sync_links_document(owner, batch_id)
        document_id, document_url, folder_url, created_new = _processed_document(
            owner, str(batch_row["group_name"]), batch_id, config
        )
        report_tab_id, _ = _ensure_owned_tab(
            owner,
            batch_id,
            "__processing_report__",
            document_id,
            "Processing Report",
            use_default_tab=created_new,
        )
        written = 0
        pending_history: list[tuple[str, str]] = []
        for serial, job, final_body, content_hash in candidates:
            try:
                tab_title = f"{serial:04d}_{_safe(job['permanent_id'])}"[:100]
                tab_id, _ = _ensure_owned_tab(
                    owner,
                    batch_id,
                    str(job["permanent_id"]),
                    document_id,
                    tab_title,
                )
                _write_owned_tab(document_id, tab_id, final_body)
                pending_history.append((content_hash, str(job["permanent_id"])))
                written += 1
            except Exception as item_exc:
                skipped.append(
                    f"{job['permanent_id']}\twrite-error\t{str(item_exc).replace(chr(10), ' ')[:500]}"
                )
                continue
        report_lines = [
            f"Group: {batch_row['group_name']}",
            f"Batch ID: {config['batch_id']}",
            f"Category: {category}",
            f"Total scripts: {len(jobs)}",
            f"Processed scripts: {written}",
            f"Skipped/failed scripts: {len(skipped)}",
            "Batch status: Completed",
            "",
            "Skipped/failed items:",
            *(skipped or ["None"]),
        ]
        _write_owned_tab(document_id, report_tab_id, "\n".join(report_lines))

        with extractor._conn() as connection:
            for content_hash, source_id in pending_history:
                connection.execute(
                    """INSERT OR IGNORE INTO processor_history(
                         owner,category,content_hash,source_id,created_at
                       ) VALUES(?,?,?,?,?)""",
                    (owner, category, content_hash, source_id, extractor._now()),
                )
            connection.commit()

        _move_extracted_source_to_used(
            str(batch_row["group_name"]), str(batch_row["document_id"] or "")
        )
        _save(
            owner,
            batch_id,
            {
                "processed_document_id": document_id,
                "processed_document_url": document_url,
                "processed_folder_url": folder_url,
                "last_processed": written,
                "last_skipped": len(skipped),
                "processing_status": "completed",
                "processing_error": "",
                "processed_at": extractor._now(),
            },
        )
        result = _state(owner, batch_id)
        result.update(
            {"processed": written, "skipped": len(skipped), "document_url": document_url}
        )
        return result
    except Exception as exc:
        _save(
            owner,
            batch_id,
            {
                "processing_status": "failed",
                "processing_error": str(exc)[:1000],
            },
        )
        raise


def _auto_processor_worker() -> None:
    while True:
        try:
            with extractor._conn() as connection:
                owners = [
                    str(row["owner"])
                    for row in connection.execute(
                        "SELECT DISTINCT owner FROM batches WHERE status='completed'"
                    ).fetchall()
                ]
            for owner in owners:
                global_config = _global_processor_config(owner)
                enabled_at = str(global_config.get("auto_process_enabled_at") or "")
                if not global_config["auto_process_extracted_batch"] or not enabled_at:
                    continue
                with extractor._conn() as connection:
                    batches = [
                        dict(row)
                        for row in connection.execute(
                            """SELECT batch_id,completed_at FROM batches
                               WHERE owner=? AND status='completed' AND completed_at>=?
                               ORDER BY completed_at""",
                            (owner, enabled_at),
                        ).fetchall()
                    ]
                for batch in batches:
                    config = _config(owner, str(batch["batch_id"]))
                    if str(config.get("processing_status") or "ready") in {
                        "running",
                        "completed",
                    }:
                        continue
                    try:
                        _process_batch(owner, str(batch["batch_id"]))
                    except Exception:
                        # Failure is recorded in per-batch processor settings. A failed
                        # batch never blocks later completed batches.
                        continue
        except Exception:
            pass
        time.sleep(2)


def _start_processor_worker() -> None:
    global _PROCESSOR_WORKER_STARTED
    with _PROCESSOR_WORKER_LOCK:
        if not _PROCESSOR_WORKER_STARTED:
            threading.Thread(
                target=_auto_processor_worker,
                name="script-processor-auto-worker",
                daemon=True,
            ).start()
            _PROCESSOR_WORKER_STARTED = True


def register_script_processor_api(app: Any) -> None:
    _init()
    if os.getenv("VPS1_RUNTIME_CANDIDATE") != "1":
        _start_processor_worker()

    @app.get("/api/script-work/processor/state")
    def processor_state(request: Request, group: str = ""):
        return _state(extractor._owner(request), group)

    @app.post("/api/script-work/processor/config")
    async def processor_config(request: Request):
        owner, data = extractor._owner(request), await _json(request)
        batch_id = str(data.get("group") or data.get("batch_id") or "").strip()
        if not batch_id:
            raise HTTPException(400, "Select an extracted batch first")
        with extractor._conn() as connection:
            exists = connection.execute(
                "SELECT 1 FROM batches WHERE owner=? AND batch_id=?", (owner, batch_id)
            ).fetchone()
        if not exists:
            raise HTTPException(404, "Batch not found")
        _save(owner, batch_id, data)
        return _state(owner, batch_id)

    @app.post("/api/script-work/processor/categories")
    async def processor_categories(request: Request):
        owner, data = extractor._owner(request), await _json(request)
        name = str(data.get("name") or "").strip()[:80]
        action = str(data.get("action") or "add")
        if not name:
            raise HTTPException(400, "Category name is required")
        categories = _categories(owner)
        if action == "delete":
            categories = [
                category for category in categories if category.lower() != name.lower()
            ]
            if not categories:
                categories = ["general"]
        elif name.lower() not in {category.lower() for category in categories}:
            categories.append(name)
        _set_setting(owner, "processor:categories", categories)
        return {"categories": _categories(owner)}

    @app.post("/api/script-work/processor/intros")
    async def processor_intro_upload(request: Request):
        owner = extractor._owner(request)
        filename = _safe(request.headers.get("x-filename") or "intro.txt")
        if not filename.lower().endswith(".txt"):
            filename += ".txt"
        body = await request.body()
        if not body or len(body) > 1024 * 1024:
            raise HTTPException(400, "Intro must be a non-empty TXT file under 1 MB")
        directory = INTRO_ROOT / _safe(owner)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / filename).write_bytes(body)
        return {"intros": _intros(owner), "filename": filename}

    @app.delete("/api/script-work/processor/intros/{filename}")
    def processor_intro_delete(request: Request, filename: str):
        owner = extractor._owner(request)
        safe_name = _safe(filename)
        (INTRO_ROOT / _safe(owner) / safe_name).unlink(missing_ok=True)
        return {"intros": _intros(owner)}

    @app.post("/api/script-work/processor/process")
    async def processor_process(request: Request):
        owner, data = extractor._owner(request), await _json(request)
        batch_id = str(data.get("group") or data.get("batch_id") or "").strip()
        if not batch_id:
            raise HTTPException(400, "Select an extracted batch first")
        return _process_batch(owner, batch_id, data)

    @app.get("/api/script-work/processor/download")
    def processor_download(request: Request, group: str = ""):
        owner = extractor._owner(request)
        url = str(_config(owner, group).get("processed_document_url") or "")
        if not url:
            raise HTTPException(404, "Process a batch before opening its Google Doc")
        return RedirectResponse(url=url, status_code=303)


_init()
