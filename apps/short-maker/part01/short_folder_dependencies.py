"""Read-only Short Maker folder dependency preflight; NO destructive operations.

Three separate owner/security domains must agree before the future destructive
endpoint can be enabled: Maker jobs/profiles/uploads, Short Uploader's original
profile/owner database and its V6 source/claim database. This module does not
modify any DB, unlink an MP4, or infer ownership from a matching folder name.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi import HTTPException

ACTIVE_MAKER = frozenset(("uploading", "queued", "running", "pausing", "cancelling"))
ACTIVE_UPLOAD = frozenset(("uploading", "paused", "publishing"))
SAFE_UPLOADER_JOB_STATES = frozenset(("COMPLETE", "CANCELLED"))
SHORT_FOLDER_PREFIX = "shortstudio-local:"


def _read_only_db(filename: str, required: dict[str, set[str]]):
    if not filename:
        raise ValueError("Short Uploader database path was not configured")
    path = Path(filename)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Verified Short Uploader database is missing or unsafe")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        for table, columns in required.items():
            found = {str(row["name"]) for row in conn.execute(
                'PRAGMA table_info("' + table + '")')}
            if not columns.issubset(found):
                raise ValueError("Short Uploader source schema is incomplete")
    except Exception:
        conn.close()
        raise
    return conn


class ShortFolderPreflight:
    def __init__(self, profile_store, short_jobs, ready_upload_root,
                 uploader_app_db="", uploader_engine_db=""):
        self.profiles = profile_store
        self.short_jobs = short_jobs
        self.ready_root = Path(ready_upload_root)
        self.uploader_app_db = uploader_app_db
        self.uploader_engine_db = uploader_engine_db

    def _uploader(self, owner: str, folder_name: str):
        """Read *all* references including disabled/paused or unassigned ones.

        Only the requesting owner's linked profile names may be returned. An
        inconsistent/foreign/unassigned source linkage is a generic blocker.
        """
        app_required = {
            "auto_engine_profiles": {"id", "category_name", "enabled", "schedule_enabled"},
            "studio_entity_owners": {"entity_kind", "entity_id", "owner_username"},
            "videos": {"id", "status", "auto_engine_profile_id"},
        }
        v6_required = {
            "profile_sources": {"profile_id", "short_folder_name"},
            "job_sources": {"video_id", "profile_id", "source_folder_id", "filename", "state"},
        }
        with _read_only_db(self.uploader_app_db, app_required) as app_db:
            with _read_only_db(self.uploader_engine_db, v6_required) as engine_db:
                profiles = {int(row["id"]): dict(row) for row in app_db.execute(
                    "SELECT id,category_name,enabled,schedule_enabled FROM auto_engine_profiles")}
                owners = {}
                for row in app_db.execute(
                        "SELECT entity_id,owner_username FROM studio_entity_owners "
                        "WHERE entity_kind='profile'"):
                    pid = int(row["entity_id"])
                    if pid in owners:
                        raise ValueError("Conflicting uploader profile ownership")
                    owners[pid] = str(row["owner_username"]).strip().casefold()
                sources = list(engine_db.execute(
                    "SELECT profile_id FROM profile_sources WHERE short_folder_name=?",
                    (folder_name,)))
                links = []
                foreign_or_ambiguous = 0
                for row in sources:
                    pid = int(row["profile_id"])
                    profile = profiles.get(pid)
                    if profile is None or owners.get(pid) != owner:
                        foreign_or_ambiguous += 1
                    else:
                        links.append({
                            "name": str(profile["category_name"]),
                            "enabled": bool(profile["enabled"]),
                            "schedule_enabled": bool(profile["schedule_enabled"]),
                            "profile_id": pid,
                        })
                source_id = SHORT_FOLDER_PREFIX + folder_name
                # If a queue claim survives deletion of its profile source,
                # it must still block deleting the actual source MP4.
                claims = list(engine_db.execute(
                    """SELECT video_id,profile_id,filename,state FROM job_sources
                       WHERE source_folder_id=?""", (source_id,)))
                active_claims = [
                    row for row in claims
                    if str(row["state"]).upper() not in SAFE_UPLOADER_JOB_STATES
                ]
                for row in claims:
                    pid = int(row["profile_id"])
                    if profiles.get(pid) is None or owners.get(pid) != owner:
                        foreign_or_ambiguous += 1
                video_ids = [int(row["video_id"]) for row in claims]
                pending_app_jobs = 0
                # Don't trust V6 alone: the separate uploader worker may have
                # already claimed a job before updating the source ledger.
                if video_ids:
                    for offset in range(0, len(video_ids), 900):
                        ids = video_ids[offset:offset+900]
                        marks = ",".join("?" for _ in ids)
                        pending_app_jobs += int(app_db.execute(
                            "SELECT COUNT(*) FROM videos WHERE id IN ("+marks+") "
                            "AND UPPER(status) NOT IN ('COMPLETED','COMPLETE',"
                            "'UPLOADED','CANCELLED','SKIPPED')", ids).fetchone()[0])
                return {
                    "linked_profiles": sorted(links, key=lambda x: x["name"].casefold()),
                    "foreign_or_unassigned_references": foreign_or_ambiguous,
                    "active_uploader_claims": len(active_claims) + pending_app_jobs,
                    "source_claims_total": len(claims),
                    "uploader_dependency_verified": True,
                }

    def inspect(self, *, owner: str, folder_name: str, folder: Path):
        owner = str(owner).strip().casefold()
        if not owner or folder.is_symlink() or not folder.is_dir():
            raise HTTPException(403, detail="Verified owned folder is required")
        marker = folder / ".studio_owner"
        if (marker.is_symlink() or not marker.is_file()
                or marker.read_text(encoding="utf-8").strip().casefold() != owner):
            raise HTTPException(403, detail="Folder owner verification failed")
        issues = []
        maker_profiles = [p for p in self.profiles.list(owner)
                          if p["output_folder"] == folder_name]
        # All own references—including stopped profiles—must be unlinked or
        # explicitly migrated, not silently deleted.
        if maker_profiles:
            issues.append("Short Profiles still link this folder")
        for state in self.short_jobs():
            if not isinstance(state, dict):
                issues.append("Unverifiable Maker job")
                continue
            config = state.get("config") or {}
            if (state.get("short_vps_folder") or config.get("short_vps_folder")) != folder_name:
                continue
            if str(state.get("owner") or "").casefold() != owner:
                issues.append("Maker job has foreign/unassigned owner")
            if state.get("status") in ACTIVE_MAKER:
                issues.append("Active Short Maker render or queue job")

        # Import manifests are owner-bound and must be readable. Incomplete
        # uploads remain blockers even if paused or their staging is recoverable.
        if self.ready_root.is_symlink():
            issues.append("Unsafe ready-made uploads root")
        elif self.ready_root.is_dir():
            for directory in self.ready_root.iterdir():
                if not directory.is_dir() or directory.is_symlink():
                    issues.append("Uninspectable ready-made upload session")
                    continue
                manifest_file = directory / "manifest.json"
                if manifest_file.is_symlink() or not manifest_file.is_file():
                    issues.append("Uninspectable ready-made upload session")
                    continue
                try:
                    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
                    if not isinstance(manifest, dict):
                        raise ValueError("Invalid manifest")
                except (ValueError, OSError):
                    issues.append("Unreadable ready-made upload session")
                    continue
                if manifest.get("folder") != folder_name:
                    continue
                if str(manifest.get("owner") or "").casefold() != owner:
                    issues.append("Unowned ready-made import references folder")
                if manifest.get("status") in ACTIVE_UPLOAD:
                    issues.append("Ready-made video upload is in progress or paused")

        # All unknown files require review. Only visible verified MP4s and
        # owner marker may later be removed by a fully authenticated operation.
        unexpected_files = []
        for entry in folder.iterdir():
            if entry.name == ".studio_owner":
                continue
            if (entry.is_symlink() or not entry.is_file() or
                    entry.suffix.lower() != ".mp4"):
                unexpected_files.append(entry.name)
        if unexpected_files:
            issues.append("Folder has unverified or non-MP4 entries")

        try:
            uploader = self._uploader(owner, folder_name)
        except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
            uploader = {
                "linked_profiles": [],
                "foreign_or_unassigned_references": 0,
                "active_uploader_claims": None,
                "source_claims_total": None,
                "uploader_dependency_verified": False,
            }
            issues.append("Short Uploader dependency verification unavailable")

        if uploader["linked_profiles"]:
            issues.append("Short Uploader profiles still link folder")
        if uploader["foreign_or_unassigned_references"]:
            issues.append("Foreign or unassigned uploader dependency")
        if uploader["active_uploader_claims"]:
            issues.append("Short Uploader has unresolved video claims")
        return {
            "folder": folder_name,
            "linked_short_profiles": [
                {"id": p["id"], "name": p["name"], "enabled": bool(p["enabled"])}
                for p in maker_profiles
            ],
            **uploader,
            "issues": sorted(set(issues)),
            # This is read-only evidence, NOT an authorization to delete:
            # user-confirmed unlink, cross-service locks, refresh-safe
            # transactional deletion and rollback are still required.
            "delete_preflight_ready": False,
            "deletion_enabled": False,
            "requires_confirmation": True,
        }
