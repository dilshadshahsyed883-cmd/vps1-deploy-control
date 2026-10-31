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

    def create_channel(self, username, channel_id, channel_name, email=""):
        """Atomically create a NEW UC channel, without upserting legacy OAuth."""
        owner=self.username(username)
        channel_id=str(channel_id or "").strip()
        channel_name=str(channel_name or "").strip()
        email=str(email or "").strip()
        if not re.fullmatch(r"UC[A-Za-z0-9_-]{22}",channel_id):
            raise ValueError("Enter the exact 24-character YouTube UC Channel ID")
        if not (1 <= len(channel_name) <= 120) or any(ord(c)<32 for c in channel_name):
            raise ValueError("Channel name must be 1–120 printable characters")
        if len(email)>160 or any(c in email for c in "\r\n"):
            raise ValueError("Invalid account email")
        with self.db.connect() as con:
            self._initialize(con)
            con.execute("BEGIN IMMEDIATE")
            # An existing channel ID is globally unique. Never overwrite it,
            # its token path, historical queue, or another tenant's credentials.
            if con.execute("SELECT 1 FROM channels WHERE channel_id=?",
                           (channel_id,)).fetchone():
                raise PermissionError("This YouTube channel is already registered")
            cur=con.execute("""
                INSERT INTO channels(channel_id,channel_name,google_account_email,
                                     token_file_path,channel_url,status)
                VALUES (?,?,?,'',?,'ACTIVE')
                """,(channel_id,channel_name,email or None,
                     "https://www.youtube.com/channel/"+channel_id))
            cid=int(cur.lastrowid)
            con.execute("""INSERT INTO studio_entity_owners
                (entity_kind,entity_id,owner_username) VALUES ('channel',?,?)""",
                (cid,owner))
            return cid

    def create_group(self, username, group_name, channel_ids):
        """New group with ONLY owner's channels, in one SQLite transaction."""
        owner=self.username(username)
        name=str(group_name or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]{1,79}",name):
            raise ValueError("Group name must be 2–80 safe characters")
        ids=sorted({int(value) for value in channel_ids})
        if not ids or len(ids)>500 or any(cid<=0 for cid in ids):
            raise ValueError("Select 1–500 owned channels")
        with self.db.connect() as con:
            self._initialize(con)
            con.execute("BEGIN IMMEDIATE")
            if con.execute("SELECT 1 FROM channel_groups WHERE group_name=? COLLATE NOCASE",
                           (name,)).fetchone():
                raise PermissionError("This channel-group name is already in use")
            marks=",".join("?" for _ in ids)
            owned=con.execute(
                "SELECT c.id FROM channels c JOIN studio_entity_owners o "
                "ON o.entity_kind='channel' AND o.entity_id=c.id "
                "WHERE o.owner_username=? AND c.id IN ("+marks+")",
                (owner,*ids)).fetchall()
            if len(owned)!=len(ids):
                raise PermissionError("Group contains a channel not owned by this user")
            cur=con.execute("INSERT INTO channel_groups(group_name,notes) VALUES (?,?)",
                            (name,"Created by authorized Studio account"))
            gid=int(cur.lastrowid)
            con.executemany(
                "INSERT INTO channel_group_members(group_id,channel_db_id) VALUES (?,?)",
                [(gid,cid) for cid in ids])
            con.execute("""INSERT INTO studio_entity_owners
                (entity_kind,entity_id,owner_username) VALUES ('group',?,?)""",
                (gid,owner))
            return gid

    def assign_by_admin(self, kind, entity_id, owner, user):
        """First assignment of a legacy entity; never silently transfer history."""
        if not user or not int(user.get("is_admin") or 0):
            raise PermissionError("Administrator required")
        return self.bind(kind, entity_id, owner)
