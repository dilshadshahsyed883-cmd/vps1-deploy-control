"""Fail-closed per-account ownership for uploader database entities.

One independent table per Song/Short/Copyright DB. Never infer a user from an
OAuth channel, shared Sheets row, or a legacy unassigned record; these remain
admin-only until explicitly assigned. No worker/queue mutations in this module.
"""
from __future__ import annotations

import re

KINDS = frozenset({"channel", "group", "sheet", "profile"})
USER = re.compile(r"^[a-zA-Z0-9_.@+-]{1,160}$")


class StudioTenants:
    def __init__(self, repository):
        self.db = repository

    @staticmethod
    def username(username):
        username = str(username or "").strip()
        if not USER.fullmatch(username):
            raise ValueError("Verified Studio username is required")
        return username.casefold()

    @staticmethod
    def identity(entity_kind, entity_id):
        if entity_kind not in KINDS or int(entity_id) <= 0:
            raise ValueError("Invalid Studio ownership target")
        return entity_kind, int(entity_id)

    def _initialize(self, connection):
        connection.execute("""
            CREATE TABLE IF NOT EXISTS studio_entity_owners (
                entity_kind TEXT NOT NULL,
                entity_id INTEGER NOT NULL,
                owner_username TEXT NOT NULL COLLATE NOCASE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(entity_kind, entity_id)
            )
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_studio_entity_owner
              ON studio_entity_owners (owner_username, entity_kind)
        """)

    def owner(self, kind, entity_id):
        kind, entity_id = self.identity(kind, entity_id)
        with self.db.connect() as con:
            self._initialize(con)
            row = con.execute(
                "SELECT owner_username FROM studio_entity_owners WHERE entity_kind=? AND entity_id=?",
                (kind, entity_id),
            ).fetchone()
            return str(row[0]) if row else ""

    def bind(self, kind, entity_id, username):
        """First ownership claim only; existing owner cannot be replaced here."""
        kind, entity_id = self.identity(kind, entity_id)
        owner = self.username(username)
        with self.db.connect() as con:
            self._initialize(con)
            con.execute(
                "INSERT OR IGNORE INTO studio_entity_owners"
                " (entity_kind, entity_id, owner_username) VALUES (?,?,?)",
                (kind, entity_id, owner),
            )
            actual = con.execute(
                "SELECT owner_username FROM studio_entity_owners WHERE entity_kind=? AND entity_id=?",
                (kind, entity_id),
            ).fetchone()
            if not actual or str(actual[0]).casefold() != owner:
                raise PermissionError("Studio resource belongs to a different user")
        return owner

    def require(self, kind, entity_id, user):
        self.identity(kind, entity_id)
        if not user or not str(user.get("username") or "").strip():
            raise PermissionError("Authenticated Studio account is required")
        if int(user.get("is_admin") or 0):
            return
        owner = self.owner(kind, entity_id)
        if not owner or owner != self.username(user["username"]):
            raise PermissionError("Studio resource is not assigned to this account")

    def allowed_ids(self, kind, user):
        if kind not in KINDS:
            raise ValueError("Unknown Studio resource type")
        if not user or not str(user.get("username") or "").strip():
            raise PermissionError("Authenticated Studio account is required")
        if int(user.get("is_admin") or 0):
            return None  # Admin may inspect every record including legacy.
        owner = self.username(user["username"])
        with self.db.connect() as con:
            self._initialize(con)
            return {int(row[0]) for row in con.execute(
                "SELECT entity_id FROM studio_entity_owners WHERE entity_kind=? AND owner_username=?",
                (kind, owner),
            ).fetchall()}

    def assign_by_admin(self, kind, entity_id, owner, user):
        """Admin-only explicit transfer; caller checks the entity exists and is idle."""
        if not user or not int(user.get("is_admin") or 0):
            raise PermissionError("Administrator required")
        kind, entity_id = self.identity(kind, entity_id)
        owner = self.username(owner)
        with self.db.connect() as con:
            self._initialize(con)
            con.execute(
                "INSERT INTO studio_entity_owners(entity_kind,entity_id,owner_username)"
                " VALUES(?,?,?) ON CONFLICT(entity_kind,entity_id)"
                " DO UPDATE SET owner_username=excluded.owner_username",
                (kind, entity_id, owner),
            )
        return owner
