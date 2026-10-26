"""Manual person collections for SML. Additive: no face models, file moves or legacy tag edits.

Person/name/action associations are virtual search keywords. Unassigning never
removes user-entered tags, face-review decisions or original media.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from pathlib import Path

from fastapi import HTTPException, Query
from pydantic import BaseModel, Field


class PersonInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    aliases: list[str] = Field(default_factory=list, max_length=12)


class PersonPatch(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    aliases: list[str] = Field(default_factory=list, max_length=12)


class AssignAssets(BaseModel):
    media_type: str
    ids: list[str] = Field(min_length=1, max_length=500)
    action_tags: list[str] = Field(default_factory=list, max_length=20)


class RemoveAssets(BaseModel):
    media_type: str
    ids: list[str] = Field(min_length=1, max_length=500)


def clean_name(value: str) -> str:
    name = " ".join(str(value).split()).strip()
    if not name or len(name) > 100 or any(ord(ch) < 32 for ch in name):
        raise HTTPException(422, "Person name must be 1–100 characters")
    return name


def clean_aliases(values: list[str], name: str) -> list[str]:
    aliases = []
    for value in values:
        alias = clean_name(value)
        if alias.casefold() not in {x.casefold() for x in [name, *aliases]}:
            aliases.append(alias)
    return aliases[:12]


def clean_tags(values: list[str]) -> list[str]:
    result = []
    for value in values:
        tag = " ".join(str(value).split()).casefold().strip()
        if not tag or len(tag) > 60 or any(ord(ch) < 32 for ch in tag):
            raise HTTPException(422, "Action tags must be 1–60 characters")
        if tag not in result:
            result.append(tag)
    return result[:20]


def _kind(media_type: str) -> tuple[str, str]:
    if media_type == "image":
        return "image_assets", "image"
    if media_type == "video":
        return "clips", "video"
    raise HTTPException(422, "Select images or video clips")


def matching_collections(conn: sqlite3.Connection, query: str) -> set[str]:
    """Only recognize complete, user-named person/alias phrases."""
    text = " ".join(str(query).casefold().split())
    if not text:
        return set()
    hits = set()
    for row in conn.execute("SELECT id,name,aliases_json FROM sml_person_collections"):
        phrases = [row["name"], *json.loads(row["aliases_json"])]
        if any(re.search(r"(?<!\w)" + re.escape(p.casefold()) + r"(?!\w)", text) for p in phrases):
            hits.add(row["id"])
    return hits


def associated_ids(conn: sqlite3.Connection, media_type: str, person_ids: set[str], limit: int = 500) -> list[str]:
    """Expose older person-tagged media outside the generic recent-media window."""
    _kind(media_type)
    if not person_ids:
        return []
    keys = sorted(person_ids)
    marks = ",".join("?" for _ in keys)
    return [r["asset_id"] for r in conn.execute(
        f"SELECT DISTINCT asset_id FROM sml_person_assets WHERE media_type=? AND person_id IN ({marks}) LIMIT ?",
        [media_type, *keys, limit],
    )]


def virtual_tags(conn: sqlite3.Connection, media_type: str, ids: list[str]) -> tuple[dict[str, list[str]], dict[str, set[str]]]:
    """Batch-load collection keywords and memberships without editing media rows."""
    _kind(media_type)
    if not ids:
        return {}, {}
    tags: dict[str, list[str]] = {}
    memberships: dict[str, set[str]] = {}
    for start in range(0, len(ids), 400):
        batch = ids[start:start + 400]
        marks = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"""SELECT a.asset_id,a.person_id,a.action_tags_json,p.name,p.aliases_json
                FROM sml_person_assets a JOIN sml_person_collections p ON p.id=a.person_id
                WHERE a.media_type=? AND a.asset_id IN ({marks})""",
            [media_type, *batch],
        )
        for row in rows:
            ident = row["asset_id"]
            memberships.setdefault(ident, set()).add(row["person_id"])
            collected = tags.setdefault(ident, [])
            for value in [row["name"], *json.loads(row["aliases_json"]), *json.loads(row["action_tags_json"])]:
                if value.casefold() not in {t.casefold() for t in collected}:
                    collected.append(value)
    return tags, memberships


def register_manual_people(app, *, db, now_iso):
    @app.on_event("startup")
    def initialize_manual_people():
        with db() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sml_person_collections (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    aliases_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sml_person_assets (
                    person_id TEXT NOT NULL REFERENCES sml_person_collections(id),
                    media_type TEXT NOT NULL CHECK(media_type IN ('image','video')),
                    asset_id TEXT NOT NULL,
                    action_tags_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(person_id,media_type,asset_id)
                );
                CREATE INDEX IF NOT EXISTS idx_sml_person_assets_media
                    ON sml_person_assets(media_type,asset_id);
                CREATE INDEX IF NOT EXISTS idx_sml_person_assets_person
                    ON sml_person_assets(person_id,media_type);
            """)

    def person_row(conn, ident):
        row = conn.execute("SELECT * FROM sml_person_collections WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise HTTPException(404, "Person collection not found")
        return row

    @app.get("/api/v1/manual-people")
    def list_manual_people():
        with db() as conn:
            rows = conn.execute("SELECT * FROM sml_person_collections ORDER BY name COLLATE NOCASE").fetchall()
            items = []
            for row in rows:
                counts = {}
                for kind, table in (("image", "image_assets"), ("video", "clips")):
                    counts[kind] = conn.execute(
                        f"""SELECT COUNT(*) FROM sml_person_assets a
                            JOIN {table} m ON m.id=a.asset_id
                            WHERE a.person_id=? AND a.media_type=? AND m.deleted_at IS NULL""",
                        (row["id"], kind),
                    ).fetchone()[0]
                items.append({"id": row["id"], "name": row["name"],
                              "aliases": json.loads(row["aliases_json"]), "counts": counts})
        return {"items": items}

    @app.post("/api/v1/manual-people")
    def create_manual_person(body: PersonInput):
        name = clean_name(body.name)
        aliases = clean_aliases(body.aliases, name)
        ident = "PERSON-" + uuid.uuid4().hex
        with db() as conn:
            try:
                conn.execute(
                    "INSERT INTO sml_person_collections(id,name,aliases_json,created_at,updated_at) VALUES (?,?,?,?,?)",
                    (ident, name, json.dumps(aliases, ensure_ascii=False), now_iso(), now_iso()),
                )
            except sqlite3.IntegrityError as exc:
                raise HTTPException(409, "This person already has a collection") from exc
        return {"id": ident, "name": name, "aliases": aliases}

    @app.patch("/api/v1/manual-people/{person_id}")
    def update_manual_person(person_id: str, body: PersonPatch):
        name = clean_name(body.name)
        aliases = clean_aliases(body.aliases, name)
        with db() as conn:
            person_row(conn, person_id)
            try:
                conn.execute(
                    "UPDATE sml_person_collections SET name=?,aliases_json=?,updated_at=? WHERE id=?",
                    (name, json.dumps(aliases, ensure_ascii=False), now_iso(), person_id),
                )
            except sqlite3.IntegrityError as exc:
                raise HTTPException(409, "This person already has a collection") from exc
        return {"id": person_id, "name": name, "aliases": aliases}

    @app.post("/api/v1/manual-people/{person_id}/assign")
    def assign_manual_assets(person_id: str, body: AssignAssets):
        table, kind = _kind(body.media_type)
        ids = list(dict.fromkeys(body.ids))
        actions = clean_tags(body.action_tags)
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            person_row(conn, person_id)
            # Atomic batch validation; no half-assigned collections.
            for ident in ids:
                if not conn.execute(
                    f"SELECT 1 FROM {table} WHERE id=? AND deleted_at IS NULL", (ident,)
                ).fetchone():
                    raise HTTPException(404, "Media asset missing or in Trash: " + ident)
            for ident in ids:
                row = conn.execute(
                    "SELECT action_tags_json FROM sml_person_assets "
                    "WHERE person_id=? AND media_type=? AND asset_id=?",
                    (person_id, kind, ident),
                ).fetchone()
                combined = clean_tags([*(json.loads(row["action_tags_json"]) if row else []), *actions])
                conn.execute(
                    """INSERT INTO sml_person_assets
                       (person_id,media_type,asset_id,action_tags_json,created_at,updated_at)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT(person_id,media_type,asset_id) DO UPDATE SET
                         action_tags_json=excluded.action_tags_json,updated_at=excluded.updated_at""",
                    (person_id, kind, ident, json.dumps(combined, ensure_ascii=False), now_iso(), now_iso()),
                )
        return {"ok": True, "assigned": len(ids), "action_tags": actions}

    @app.post("/api/v1/manual-people/{person_id}/unassign")
    def unassign_manual_assets(person_id: str, body: RemoveAssets):
        _kind(body.media_type)
        ids = list(dict.fromkeys(body.ids))
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            person_row(conn, person_id)
            count = 0
            for ident in ids:
                count += conn.execute(
                    "DELETE FROM sml_person_assets WHERE person_id=? AND media_type=? AND asset_id=?",
                    (person_id, body.media_type, ident),
                ).rowcount
        return {"ok": True, "unassigned": count}

    @app.get("/api/v1/manual-people/{person_id}/assets")
    def list_person_assets(person_id: str, media_type: str = "all",
                           limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0)):
        if media_type not in {"all", "image", "video"}:
            raise HTTPException(422, "Invalid media filter")
        with db() as conn:
            person_row(conn, person_id)
            clauses = ["a.person_id=?"]
            params = [person_id]
            if media_type != "all":
                clauses.append("a.media_type=?")
                params.append(media_type)
            where = " AND ".join(clauses)
            rows = conn.execute(
                f"""SELECT a.media_type,a.asset_id,a.action_tags_json,a.created_at,
                    i.original_name AS image_name,i.status AS image_status,i.deleted_at AS image_deleted,
                    c.filename AS clip_name,c.status AS clip_status,c.deleted_at AS clip_deleted,
                    c.thumb_filename AS clip_thumb
                    FROM sml_person_assets a
                    LEFT JOIN image_assets i ON a.media_type='image' AND i.id=a.asset_id
                    LEFT JOIN clips c ON a.media_type='video' AND c.id=a.asset_id
                    WHERE {where}
                    ORDER BY a.created_at DESC LIMIT ? OFFSET ?""",
                [*params, limit, offset],
            ).fetchall()
            items = []
            for row in rows:
                kind = row["media_type"]
                if kind == "image" and (row["image_name"] is None or row["image_deleted"]):
                    continue
                if kind == "video" and (row["clip_name"] is None or row["clip_deleted"]):
                    continue
                ident = row["asset_id"]
                items.append({
                    "id": ident, "type": kind,
                    "name": row["image_name"] if kind == "image" else row["clip_name"],
                    "status": row["image_status"] if kind == "image" else row["clip_status"],
                    "action_tags": json.loads(row["action_tags_json"]),
                    "thumb_url": f"/api/v1/images/{ident}/preview" if kind == "image"
                        else f"/media/thumbs/{row['clip_thumb']}" if row["clip_thumb"] else "",
                })
        return {"items": items, "limit": limit, "offset": offset}
