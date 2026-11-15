from __future__ import annotations

import re
from typing import Any, Dict, List

from desktop_core.modules.google_sheet_sync import (
    auto_sheet_source_name,
    extract_sheet_id,
    fetch_sheet_csv,
    rows_to_sheet_rows,
    smart_group_name,
)
from desktop_core.modules.path_utils import TOKEN_DIR


def split_sheet_group_names(group_value: str) -> List[str]:
    names: List[str] = []
    for part in re.split(r"[,;|]+", str(group_value or "")):
        clean = re.sub(r"\s+", " ", part).strip()
        if clean and clean.lower() not in {"none", "no", "na", "n/a", "-"}:
            names.append(clean)
    return names


def save_sheet_source(
    db: Any,
    *,
    source_id: int | None,
    sheet_name: str,
    sheet_url_or_id: str,
    enabled: bool,
) -> int:
    sheet_url = str(sheet_url_or_id or "").strip()
    extract_sheet_id(sheet_url)
    final_name = str(sheet_name or "").strip() or auto_sheet_source_name(sheet_url)
    return int(db.save_sheet_source(
        source_id=source_id,
        sheet_name=final_name,
        sheet_url_or_id=sheet_url,
        gid="FIRST_TAB",
        json_folder_path=str(TOKEN_DIR),
        enabled=bool(enabled),
    ))


def delete_sheet_source(db: Any, source_id: int) -> None:
    db.delete_sheet_source(int(source_id))


def sync_owned_sheet_source(
    db: Any, tenants: Any, username: str, source_id: int, *,
    fetch_rows=None, parse_rows=None,
) -> Dict[str, Any]:
    """Canonical first-tab Sheet pull for one verified owner.

    One failed CSV/validation pass changes only the Sheet's failure receipt.
    Channels and OAuth remain unchanged, and new uploads cannot use a stale
    success receipt from before the error.
    """
    source = db.get_sheet_source(int(source_id))
    if not source:
        raise ValueError("Sheet source not found")
    if tenants.owner("sheet", int(source_id)) != tenants.username(username):
        raise PermissionError("Sheet source is not owned by this account")
    try:
        rows = (fetch_rows or fetch_sheet_csv)(source["sheet_url_or_id"], None)
        if not rows:
            raise ValueError("Sheet returned zero rows; no configuration changed")
        parsed = (parse_rows or rows_to_sheet_rows)(rows, str(TOKEN_DIR))
        return tenants.sync_owned_sheet_rows(username, int(source_id), parsed)
    except Exception:
        with db.connect() as conn:
            conn.execute("""UPDATE sheet_sources
                SET last_sync_status='SHEET_MASTER_FAILED: check source and retry',
                    updated_at=CURRENT_TIMESTAMP WHERE id=?""", (int(source_id),))
        raise


def owned_sheet_fresh(db: Any, owner_username: str, *, minutes: int = 17) -> bool:
    """Gate NEW Short Uploader leases/queue admissions on recent clean Sheet data.

    No data is cleared on a network error; queued or in-flight uploads remain
    stored, and the scheduler retries later. The grace covers one scheduler tick.
    """
    from datetime import datetime, timedelta, timezone

    owner = str(owner_username or "").strip().casefold()
    if not owner:
        return False
    with db.connect() as conn:
        sources = conn.execute(
            """SELECT s.last_sync_at,s.last_sync_status FROM sheet_sources s
               JOIN studio_entity_owners o ON o.entity_kind='sheet' AND o.entity_id=s.id
               WHERE s.enabled=1 AND o.owner_username=? COLLATE NOCASE""",
            (owner,),
        ).fetchall()
    if not sources:
        return True  # Manual-only installs remain functional.
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    for src in sources:
        if not (str(src["last_sync_status"] or "").startswith("OWNED_SYNC:") and
                "master=1" in str(src["last_sync_status"] or "")):
            return False
        stamp = str(src["last_sync_at"] or "").strip()
        try:
            last = datetime.fromisoformat(stamp.replace(" ", "T"))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
        except ValueError:
            return False
        if last < cutoff:
            return False
    return True


def sync_sheet_source_by_id(db: Any, source_id: int) -> Dict[str, Any]:
    source = db.get_sheet_source(int(source_id))
    if not source:
        raise ValueError("Sheet source not found.")
    try:
        if str(source.get("json_folder_path") or "") != str(TOKEN_DIR):
            db.update_sheet_source_json_folder(int(source_id), str(TOKEN_DIR))

        raw_rows = fetch_sheet_csv(source["sheet_url_or_id"], None)
        if not raw_rows:
            raise ValueError(
                "Google Sheet returned zero data rows. No existing channels were changed. "
                "Check sharing, first-tab selection, and sheet contents, then retry."
            )

        parsed_rows = rows_to_sheet_rows(raw_rows, str(TOKEN_DIR))
        added_or_updated = 0
        active_keys: List[str] = []
        token_missing = 0
        invalid_tokens = 0
        groups_touched = 0
        group_normalizations: List[str] = []
        known_groups = [g.get("group_name") for g in db.list_channel_groups()]
        sheet_group_ids: Dict[str, set[int]] = {}

        for row in parsed_rows:
            active_keys.append(row.key)
            if row.status == "TOKEN_MISSING":
                token_missing += 1
            if row.status == "INVALID_TOKEN":
                invalid_tokens += 1

            channel_db_id = db.upsert_sheet_channel(int(source_id), {
                "channel_id": row.channel_id,
                "channel_name": row.channel_name,
                "gmail": row.gmail,
                "token_file_path": row.token_file_path,
                "channel_url": row.channel_link,
                "browser_profile_name": row.browser_profile,
                "proxy_scheme": row.proxy_scheme,
                "proxy_host": row.proxy_host,
                "proxy_port": row.proxy_port,
                "proxy_username": row.proxy_username,
                "proxy_password": row.proxy_password,
                "status": row.status,
                "sheet_row_key": row.key,
                "sheet_sr": row.sr,
                "sheet_ip_raw": row.ip,
                "sheet_password": row.password,
                "sheet_backup_code": row.backup_code,
                "sheet_backup_key": row.backup_key,
                "sheet_status": row.sheet_status,
                "sheet_notes": row.notes,
            })
            added_or_updated += 1

            for raw_group in split_sheet_group_names(row.group):
                final_group, group_reason = smart_group_name(raw_group, known_groups)
                if final_group:
                    if final_group not in known_groups:
                        known_groups.append(final_group)
                    if final_group != raw_group:
                        group_normalizations.append(
                            f"{raw_group} → {final_group} ({group_reason})"
                        )
                    sheet_group_ids.setdefault(final_group, set()).add(int(channel_db_id))

        cleared_memberships = db.clear_sheet_channel_group_memberships(int(source_id))
        for final_group, row_ids in sheet_group_ids.items():
            try:
                group_id = db.ensure_channel_group(
                    final_group, "Created/updated from Google Sheet sync"
                )
                db.add_channels_to_group(group_id, sorted(row_ids))
                groups_touched += 1
            except Exception as group_exc:
                group_normalizations.append(
                    f"Group save failed for {final_group}: {group_exc}"
                )

        removed_from_inventory = db.mark_missing_sheet_channels_inactive(
            int(source_id), active_keys
        )
        removed_empty_groups = db.delete_empty_sheet_sync_groups()

        status = (
            f"OK: rows={len(parsed_rows)}, updated={added_or_updated}, "
            f"hidden_removed={removed_from_inventory}, token_missing={token_missing}, "
            f"invalid_tokens={invalid_tokens}"
        )
        db.update_sheet_sync_status(int(source_id), status)
        db.add_log(
            None, "INFO",
            f"Sheet sync {source.get('sheet_name')}: {status}",
            None,
        )

        result = (
            f"Sheet: {source.get('sheet_name')}\n"
            f"Tab used: FIRST TAB only\n"
            f"Rows read: {len(raw_rows)}\n"
            f"Channels added/updated: {added_or_updated}\n"
            f"Channels removed from current inventory (history/tokens kept): {removed_from_inventory}\n"
            f"Old group memberships cleared for this sheet: {cleared_memberships}\n"
            f"Empty sheet-sync groups removed: {removed_empty_groups}\n"
            f"Missing Channel-ID token files: {token_missing}\n"
            f"Invalid token files: {invalid_tokens}\n"
            f"Groups touched: {groups_touched}\n"
            f"Group normalizations: {len(group_normalizations)}"
            + ("\n" + "\n".join(group_normalizations[:20]) if group_normalizations else "")
            + f"\nFixed token folder: {TOKEN_DIR}"
        )
        return {
            "ok": True,
            "source_id": int(source_id),
            "sheet_name": str(source.get("sheet_name") or ""),
            "status": status,
            "result": result,
            "rows": len(raw_rows),
            "updated": added_or_updated,
            "hidden_removed": int(removed_from_inventory or 0),
            "token_missing": token_missing,
            "invalid_tokens": invalid_tokens,
            "groups_touched": groups_touched,
        }
    except Exception as exc:
        msg = f"FAILED: {exc}"
        db.update_sheet_sync_status(int(source_id), msg[:500])
        db.add_log(
            None, "ERROR",
            f"Sheet sync {source.get('sheet_name')}: {msg}",
            None,
        )
        return {
            "ok": False,
            "source_id": int(source_id),
            "sheet_name": str(source.get("sheet_name") or ""),
            "status": msg,
            "result": f"Sheet: {source.get('sheet_name')}\n{msg}",
            "error": str(exc),
        }


def sync_all_sheet_sources(db: Any) -> Dict[str, Any]:
    sources = [s for s in db.list_sheet_sources() if int(s.get("enabled") or 0) == 1]
    if not sources:
        raise ValueError("Add/enable at least one sheet source first.")
    results = [sync_sheet_source_by_id(db, int(source["id"])) for source in sources]
    failed = [r for r in results if not r.get("ok")]
    return {
        "ok": not failed,
        "count": len(sources),
        "failed": len(failed),
        "rows": sum(int(r.get("rows") or 0) for r in results),
        "results": results,
    }
