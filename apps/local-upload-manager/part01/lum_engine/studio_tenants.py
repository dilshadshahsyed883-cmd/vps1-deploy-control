"""Fail-closed per-account ownership for uploader database entities.

One independent table per Song/Short/Copyright DB. Never infer a user from an
OAuth channel, shared Sheets row, or a legacy unassigned record; these remain
admin-only until explicitly assigned. No worker/queue mutations in this module.
"""
from __future__ import annotations

import os
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
        if int(user.get("is_admin") or 0) and os.getenv("LUM_ENGINE_INSTANCE") != "shortstudio":
            return
        owner = self.owner(kind, entity_id)
        if not owner or owner != self.username(user["username"]):
            raise PermissionError("Studio resource is not assigned to this account")

    def allowed_ids(self, kind, user):
        if kind not in KINDS:
            raise ValueError("Unknown Studio resource type")
        if not user or not str(user.get("username") or "").strip():
            raise PermissionError("Authenticated Studio account is required")
        # In Short Uploader the administrator is a normal, owner-scoped
        # operator. Never expose another tenant or unassigned legacy records.
        if int(user.get("is_admin") or 0) and os.getenv("LUM_ENGINE_INSTANCE") != "shortstudio":
            return None  # Other services retain their existing admin semantics.
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

    def sync_owned_sheet_rows(self, username, sheet_id, rows):
        """One-way atomic, owner-scoped Sheet -> Short/Song configuration mirror.

        The Sheet controls its explicitly supplied configuration columns.
        A row's DB identity, OAuth token, queue history and foreign resources
        never change as a side effect of a Sheet refresh. No external writes.
        """
        owner = self.username(username)
        source_id = int(sheet_id)
        if source_id <= 0 or len(rows) > 2000:
            raise ValueError("Invalid Sheet source or row limit")
        prepared = []
        seen_keys = set()
        seen_uc = set()
        group_names = {}
        unmapped_status_keys = set()
        for raw in rows:
            key = str(getattr(raw, "key", "") or "").strip().lower()
            name = str(getattr(raw, "channel_name", "") or "").strip()
            ucid = str(getattr(raw, "channel_id", "") or "").strip()
            email = str(getattr(raw, "gmail", "") or "").strip()
            if not key or len(key) > 180 or not name or len(name) > 180:
                raise ValueError("Sheet row has invalid channel key/name")
            if key in seen_keys:
                raise ValueError("Duplicate Sheet row key")
            seen_keys.add(key)
            if ucid.lower().startswith("sheet:"):
                ucid = f"sheet:{source_id}:{key}"
            elif not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", ucid):
                raise ValueError("Sheet channel ID must be a verified UC ID or a provisional row")
            if ucid in seen_uc:
                raise ValueError("Duplicate YouTube Channel ID in this Sheet")
            seen_uc.add(ucid)
            supplied = set(getattr(raw, "present_fields", ()) or ())
            fields = {}
            if "proxy" in supplied:
                from desktop_core.modules.secret_store import protect_secret
                host = str(getattr(raw, "proxy_host", "") or "").strip()
                port = str(getattr(raw, "proxy_port", "") or "").strip()
                scheme = str(getattr(raw, "proxy_scheme", "") or "").strip().lower()
                proxy_user = str(getattr(raw, "proxy_username", "") or "").strip()
                proxy_password = str(getattr(raw, "proxy_password", "") or "")
                if (host and (not re.fullmatch(r"[A-Za-z0-9_.:-]{1,253}", host)
                             or not port.isascii() or not port.isdecimal()
                             or not 1 <= int(port) <= 65535
                             or scheme not in {"http", "https"})):
                    raise ValueError(f"Invalid proxy host/port in Sheet row {key}")
                if not host and (port or proxy_user or proxy_password):
                    raise ValueError(f"Incomplete proxy in Sheet row {key}")
                fields.update(proxy_scheme=scheme if host else "", proxy_host=host,
                              proxy_port=port if host else "", proxy_username=proxy_user if host else "",
                              proxy_password=protect_secret(proxy_password if host else ""),
                              sheet_ip_raw=str(getattr(raw, "ip", "") or "").strip())
            if "browser_profile" in supplied:
                fields["browser_profile_name"] = str(getattr(raw, "browser_profile", "") or "").strip()[:180]
            if "channel_link" in supplied:
                fields["channel_url"] = str(getattr(raw, "channel_link", "") or "").strip()[:1500]
            if "sr" in supplied:
                fields["sheet_sr"] = str(getattr(raw, "sr", "") or "").strip()[:80]
            if "status" in supplied:
                status = str(getattr(raw, "sheet_status", "") or "").strip().upper()[:120]
                if status:
                    if status in {"ACTIVE", "ENABLED", "YES", "TRUE", "ON", "1"}:
                        fields["status"] = "ACTIVE"
                    elif status in {"INACTIVE", "NO", "FALSE", "OFF", "0", "REMOVE", "REMOVED"}:
                        fields["status"] = "INACTIVE"
                    elif status in {"DISABLED", "PAUSED"}:
                        fields["status"] = "DISABLED"
                    else:
                        # Real-world Sheets may use Status as free-text notes.
                        # Preserve it verbatim in sheet_status and do not infer
                        # that a channel is ACTIVE/DISABLED from an unknown label.
                        # Unlike ID/proxy conflicts, this cannot invalidate all
                        # unrelated Sheet proxy and channel configuration.
                        unmapped_status_keys.add(key)
                fields["sheet_status"] = status
            if "group" in supplied:
                names = []
                for candidate in re.split(r"[,;|]+", str(getattr(raw, "group", "") or "")):
                    group_name = re.sub(r"\s+", " ", candidate).strip()
                    if not group_name or group_name.lower() in {"none", "no", "na", "n/a", "-"}:
                        continue
                    if (len(group_name) > 80 or any(ord(c) < 32 for c in group_name)):
                        raise ValueError(f"Invalid group name in Sheet row {key}")
                    names.append(group_name)
                    group_names[group_name.casefold()] = group_name
                fields["_groups"] = tuple(dict.fromkeys(names))
            notes = str(getattr(raw, "notes", "") or "").strip()[:500]
            prepared.append((key, ucid, name, email, notes, fields))
        if not prepared:
            raise ValueError("Google Sheet returned zero usable rows; no changes applied")

        with self.db.connect() as con:
            self._initialize(con)
            con.execute("BEGIN IMMEDIATE")
            source = con.execute("SELECT id FROM sheet_sources WHERE id=?", (source_id,)).fetchone()
            assigned = con.execute("""SELECT owner_username FROM studio_entity_owners
                WHERE entity_kind='sheet' AND entity_id=?""", (source_id,)).fetchone()
            if not source or not assigned or str(assigned[0]).casefold() != owner:
                raise PermissionError("Sheet source is not owned by this account")
            columns = {str(col[1]) for col in con.execute("PRAGMA table_info(channels)")}
            managed_groups = any("_groups" in entry[-1] for entry in prepared)
            group_ids = {}
            if managed_groups:
                # A globally named group owned by someone else (or legacy
                # unassigned) cannot be silently adopted by the Sheet.
                for group_name in group_names.values():
                    group = con.execute(
                        "SELECT id FROM channel_groups WHERE group_name=? COLLATE NOCASE",
                        (group_name,)).fetchone()
                    if group:
                        gid = int(group[0])
                        linked = con.execute("""SELECT owner_username FROM studio_entity_owners
                            WHERE entity_kind='group' AND entity_id=?""", (gid,)).fetchone()
                        if not linked or str(linked[0]).casefold() != owner:
                            raise PermissionError("Sheet group name belongs to an unassigned or different workspace")
                    else:
                        gid = int(con.execute(
                            "INSERT INTO channel_groups(group_name,notes) VALUES (?,?)",
                            (group_name, "Owned Sheet-managed group"),
                        ).lastrowid)
                        con.execute("""INSERT INTO studio_entity_owners
                            (entity_kind,entity_id,owner_username) VALUES ('group',?,?)""", (gid, owner))
                    group_ids[group_name.casefold()] = gid
            updates = 0
            memberships = {}
            for key, ucid, name, email, notes, fields in prepared:
                by_row = con.execute(
                    "SELECT id,channel_id FROM channels WHERE sheet_source_id=? AND sheet_row_key=?",
                    (source_id, key)).fetchone()
                by_id = con.execute("SELECT id FROM channels WHERE channel_id=?", (ucid,)).fetchone()
                if by_id and (not by_row or int(by_row[0]) != int(by_id[0])):
                    existing = con.execute(
                        "SELECT id,channel_id,sheet_source_id FROM channels WHERE id=?",
                        (int(by_id[0]),)).fetchone()
                    linked = con.execute("""SELECT owner_username FROM studio_entity_owners
                        WHERE entity_kind='channel' AND entity_id=?""", (int(by_id[0]),)).fetchone()
                    if (by_row or not existing or
                        (existing[2] is not None and int(existing[2]) != source_id) or
                        not linked or str(linked[0]).casefold() != owner):
                        raise PermissionError("Sheet channel ID exists outside this owned Sheet row")
                    # A verified UC ID may safely identify this same owner's
                    # row after Sr changes without resetting history/token.
                    by_row = (int(existing[0]), str(existing[1]))
                values = {"channel_id": ucid, "channel_name": name,
                          "google_account_email": email, "sheet_notes": notes,
                          "sheet_source_id": source_id, "sheet_row_key": key, "sheet_present": 1}
                values.update({k: v for k, v in fields.items() if k != "_groups"})
                if by_row:
                    cid = int(by_row[0])
                    assigned_channel = con.execute("""SELECT owner_username FROM studio_entity_owners
                        WHERE entity_kind='channel' AND entity_id=?""", (cid,)).fetchone()
                    if not assigned_channel or str(assigned_channel[0]).casefold() != owner:
                        raise PermissionError("Existing Sheet channel is not owned by this account")
                    previous = str(by_row[1] or "")
                    if previous.startswith("UC") and ucid.startswith("sheet:"):
                        values["channel_id"] = previous  # OAuth's verified UC ID always wins over a provisional Sheet label.
                    elif previous.startswith("UC") and previous != ucid:
                        raise PermissionError("Existing authorized UC Channel ID cannot change via Sheet")
                    assignments = [f"{k}=?" for k in values if k in columns]
                    parameters = [v for k, v in values.items() if k in columns]
                    con.execute("UPDATE channels SET " + ",".join(assignments) +
                                ",sheet_last_seen_at=CURRENT_TIMESTAMP,last_used_at=CURRENT_TIMESTAMP WHERE id=?",
                                (*parameters, cid))
                else:
                    # Unknown explicit status must never activate a new channel.
                    values.update(token_file_path="", status=values.get(
                        "status", "INACTIVE" if key in unmapped_status_keys else "ACTIVE"))
                    names = [k for k in values if k in columns]
                    cid = int(con.execute(
                        "INSERT INTO channels (" + ",".join(names) + ",sheet_last_seen_at) VALUES (" +
                        ",".join("?" for _ in names) + ",CURRENT_TIMESTAMP)",
                        tuple(values[k] for k in names)).lastrowid)
                    con.execute("""INSERT INTO studio_entity_owners
                        (entity_kind,entity_id,owner_username) VALUES ('channel',?,?)""",
                        (cid, owner))
                if "_groups" in fields:
                    memberships[cid] = [group_ids[n.casefold()] for n in fields["_groups"]]
                updates += 1
            # Only replace memberships on proven owned rows explicitly covered
            # by the Sheet's Group column. Never delete foreign channel/group records.
            if managed_groups:
                for cid, target_groups in memberships.items():
                    con.execute("DELETE FROM channel_group_members WHERE channel_db_id=?", (cid,))
                    for gid in target_groups:
                        con.execute("""INSERT OR IGNORE INTO channel_group_members
                            (group_id,channel_db_id) VALUES (?,?)""", (gid, cid))
            keys = [entry[0] for entry in prepared]
            placeholders = ",".join("?" for _ in keys)
            removed = con.execute("""UPDATE channels SET sheet_present=0
                WHERE sheet_source_id=? AND COALESCE(sheet_row_key,'') NOT IN (""" +
                placeholders + """) AND COALESCE(sheet_present,1)<>0""",
                (source_id, *keys)).rowcount
            con.execute("""UPDATE sheet_sources SET last_sync_at=CURRENT_TIMESTAMP,
                last_sync_status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (f"OWNED_SYNC: rows={updates} hidden={removed} groups={len(group_ids)} "
                 f"master=1 status_unmapped={len(unmapped_status_keys)}", source_id))
            return {"ok": True, "rows": updates, "updated": updates, "hidden_removed": removed,
                    "groups_touched": len(group_ids),
                    "status_unmapped": len(unmapped_status_keys), "source_id": source_id}

    def delete_owned_sheet(self, username, sheet_id):
        """Detach only owned channels; do not delete media, groups or tokens."""
        owner=self.username(username)
        source_id=int(sheet_id)
        with self.db.connect() as con:
            self._initialize(con)
            con.execute("BEGIN IMMEDIATE")
            assigned=con.execute("""SELECT owner_username FROM studio_entity_owners
                WHERE entity_kind='sheet' AND entity_id=?""",(source_id,)).fetchone()
            if not assigned or str(assigned[0]).casefold()!=owner:
                raise PermissionError("Sheet source belongs to a different user")
            channels=[int(row[0]) for row in con.execute(
                "SELECT id FROM channels WHERE sheet_source_id=?",(source_id,))]
            for cid in channels:
                linked=con.execute("""SELECT owner_username FROM studio_entity_owners
                    WHERE entity_kind='channel' AND entity_id=?""",(cid,)).fetchone()
                if not linked or str(linked[0]).casefold()!=owner:
                    raise PermissionError("Sheet contains an unowned or foreign channel")
            con.execute("""UPDATE channels SET sheet_present=0,sheet_source_id=NULL,
                sheet_row_key=NULL WHERE sheet_source_id=?""",(source_id,))
            con.execute("DELETE FROM sheet_sources WHERE id=?",(source_id,))
            con.execute("""DELETE FROM studio_entity_owners
                WHERE entity_kind='sheet' AND entity_id=?""",(source_id,))
            return len(channels)

    def assign_by_admin(self, kind, entity_id, owner, user):
        """First assignment of a legacy entity; never silently transfer history."""
        if os.getenv("LUM_ENGINE_INSTANCE") == "shortstudio":
            raise PermissionError("Short Uploader workspaces cannot assign another user\'s resources")
        if not user or not int(user.get("is_admin") or 0):
            raise PermissionError("Administrator required")
        return self.bind(kind, entity_id, owner)
