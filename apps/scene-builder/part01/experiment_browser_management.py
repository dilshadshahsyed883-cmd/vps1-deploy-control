"""Owner-scoped management for existing persistent Experiment Studio browser profiles.

Additive SQLite migration only.  Profile directories, cookies and credentials
remain in the existing WEB_DATA_DIR; templates never retain proxy credentials.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
import uuid

from experiment_browser import (BrowserError, BrowserProfileEngine, MAX_PROFILES,
                                PROFILE_ID, validate_profile, validate_proxy)


def _name(value: object, label: str = "Name") -> str:
    text = str(value or "").strip()
    if not 1 <= len(text) <= 64 or any(ord(c) < 32 for c in text):
        raise BrowserError(label + " must be 1–64 characters")
    return text


def _tags(value: object) -> str:
    parts = value if isinstance(value, list) else str(value or "").split(",")
    if not isinstance(parts, list) or len(parts) > 12:
        raise BrowserError("Maximum 12 tags")
    names = []
    for part in parts:
        tag = str(part).strip()
        if tag and (len(tag) > 30 or any(ord(c) < 32 for c in tag)):
            raise BrowserError("Invalid tag")
        if tag and tag.casefold() not in {s.casefold() for s in names}:
            names.append(tag)
    return ",".join(names)


class ManagedBrowserProfileEngine(BrowserProfileEngine):
    """Existing Playwright runtime with isolated, backward-compatible metadata."""

    def __init__(self, root):
        super().__init__(root)
        self._management_initialized = False

    def _db(self):
        db = super()._db()
        if not self._management_initialized:
            try:
                # Online SQLite backup before the first additive management migration.
                # Browsing directories are never altered by schema upgrades.
                existing = {r[1] for r in db.execute("PRAGMA table_info(profiles)")}
                if "group_id" not in existing and db.execute(
                        "SELECT COUNT(*) FROM profiles").fetchone()[0]:
                    backup_dir = self.root / "profile-backups"
                    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                    backup_dir.chmod(0o700)
                    backup_path = backup_dir / ("profiles-pre-management-" +
                                                str(time.time_ns()) + ".sqlite3")
                    try:
                        with sqlite3.connect(backup_path) as safe:
                            db.backup(safe)
                        backup_path.chmod(0o600)
                    except Exception:
                        backup_path.unlink(missing_ok=True)
                        raise
                db.execute("""CREATE TABLE IF NOT EXISTS profile_groups (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
                    proxy_server TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                    UNIQUE(owner, name COLLATE NOCASE))""")
                db.execute("""CREATE TABLE IF NOT EXISTS profile_templates (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
                    settings TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(owner, name COLLATE NOCASE))""")
                cols = {r[1] for r in db.execute("PRAGMA table_info(profiles)")}
                for column, declaration in (
                    ("group_id", "TEXT DEFAULT NULL"),
                    ("tags", "TEXT NOT NULL DEFAULT ''"),
                    ("last_used", "TEXT NOT NULL DEFAULT ''"),
                ):
                    if column not in cols:
                        db.execute("ALTER TABLE profiles ADD COLUMN " + column + " " + declaration)
                db.execute("CREATE INDEX IF NOT EXISTS profiles_owner_group ON profiles(owner,group_id)")
                db.execute("CREATE INDEX IF NOT EXISTS groups_owner ON profile_groups(owner)")
                db.execute("CREATE INDEX IF NOT EXISTS templates_owner ON profile_templates(owner)")
                db.commit()
                self._management_initialized = True
            except Exception:
                db.close()
                raise
        return db

    def _public(self, row):
        data = super()._public(row)
        data["group_id"] = row.get("group_id") or ""
        data["tags"] = row.get("tags") or ""
        data["last_used"] = row.get("last_used") or ""
        return data

    @staticmethod
    def _unique(db, owner, name, skip=""):
        match = db.execute(
            "SELECT id FROM profiles WHERE owner=? AND name=? COLLATE NOCASE AND id<>?",
            (owner, name, skip)).fetchone()
        if match:
            raise BrowserError("A profile with this name already exists", 409)

    @staticmethod
    def _group(db, owner, identity):
        if not identity:
            return ""
        if not PROFILE_ID.fullmatch(str(identity)):
            raise BrowserError("Unknown profile group", 404)
        row = db.execute("SELECT id FROM profile_groups WHERE owner=? AND id=?",
                         (owner, identity)).fetchone()
        if not row:
            raise BrowserError("Unknown profile group", 404)
        return identity

    @staticmethod
    def _insert(db, folder_root, owner, info, group_id="", tags=""):
        identity = uuid.uuid4().hex[:24]
        folder = folder_root / identity
        folder.mkdir(parents=True, exist_ok=False, mode=0o700)
        try:
            columns = ("id", "owner", *info.keys(), "created_at", "group_id", "tags", "last_used")
            values = (identity, owner, *info.values(), str(int(time.time())), group_id or None, tags, "")
            db.execute("INSERT INTO profiles (" + ",".join(columns) + ") VALUES (" +
                       ",".join("?" for _ in columns) + ")", values)
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return identity

    async def create(self, owner, payload):
        info = validate_profile(payload)
        tags = _tags(payload.get("tags"))
        async with self.lock:
            with self._db() as db:
                self._unique(db, owner, info["name"])
                group = self._group(db, owner, payload.get("group_id"))
                if not info["proxy_server"] and group:
                    row = db.execute("SELECT proxy_server FROM profile_groups WHERE id=? AND owner=?",
                                     (group, owner)).fetchone()
                    info["proxy_server"] = row["proxy_server"] if row else ""
                count = db.execute("SELECT COUNT(*) FROM profiles WHERE owner=?", (owner,)).fetchone()[0]
                if count >= MAX_PROFILES:
                    raise BrowserError("Profile limit reached", 409)
                identity = self._insert(db, self.root / "profiles", owner, info, group, tags)
                db.commit()
                row = db.execute("SELECT * FROM profiles WHERE id=?", (identity,)).fetchone()
                return self._public(dict(row))

    async def clone(self, owner, profile_id):
        original = self._get(owner, profile_id)
        keys = ("start_url", "proxy_server", "locale", "timezone", "user_agent",
                "width", "height", "privacy_mode", "do_not_track",
                "geolocation_mode", "geo_lat", "geo_lon", "color_scheme",
                "reduced_motion", "device_scale", "group_id", "tags")
        data = {key: original[key] for key in keys}
        base = ("Copy of " + original["name"])[:55]
        for suffix in range(1, 1002):
            data["name"] = (base if suffix == 1 else base + " " + str(suffix))[:64]
            try:
                # Only settings are cloned. Cookies and proxy passwords never copied.
                return await self.create(owner, data)
            except BrowserError as exc:
                if exc.status != 409 or "name already exists" not in str(exc):
                    raise
        raise BrowserError("No available copy name", 409)

    async def update(self, owner, profile_id, payload):
        async with self.lock:
            row = self._get(owner, profile_id)
            if profile_id in self.running:
                raise BrowserError("Stop this browser before editing its configuration", 409)
            allowed = ("name", "start_url", "proxy_server", "proxy_username",
                       "proxy_password", "locale", "timezone", "user_agent",
                       "width", "height", "privacy_mode", "do_not_track",
                       "geolocation_mode", "geo_lat", "geo_lon", "color_scheme",
                       "reduced_motion", "device_scale")
            fields = {key: payload.get(key, row[key]) for key in allowed}
            info = validate_profile(fields)
            with self._db() as db:
                self._unique(db, owner, info["name"], profile_id)
                group = self._group(db, owner, payload.get("group_id", row["group_id"]))
                tags = _tags(payload.get("tags", row["tags"]))
                updates = dict(info, group_id=group or None, tags=tags)
                db.execute("UPDATE profiles SET " + ",".join(k + "=?" for k in updates) +
                           " WHERE id=? AND owner=?", (*updates.values(), profile_id, owner))
                db.commit()
                changed = db.execute("SELECT * FROM profiles WHERE id=? AND owner=?",
                                     (profile_id, owner)).fetchone()
                return self._public(dict(changed))

    async def start(self, owner, profile_id):
        started = await super().start(owner, profile_id)
        with self._db() as db:
            db.execute("UPDATE profiles SET last_used=? WHERE id=? AND owner=?",
                       (str(int(time.time())), profile_id, owner))
            db.commit()
        return self._public(self._get(owner, profile_id))

    async def groups(self, owner):
        async with self.lock:
            with self._db() as db:
                rows = db.execute("SELECT g.*, (SELECT COUNT(*) FROM profiles p "
                                  "WHERE p.owner=g.owner AND p.group_id=g.id) AS profile_count "
                                  "FROM profile_groups g WHERE owner=? ORDER BY name COLLATE NOCASE",
                                  (owner,)).fetchall()
                return [dict(row) for row in rows]

    async def save_group(self, owner, payload, group_id=""):
        name = _name(payload.get("name"), "Group name")
        proxy = validate_proxy(str(payload.get("proxy_server") or "").strip())
        async with self.lock:
            with self._db() as db:
                try:
                    if group_id:
                        self._group(db, owner, group_id)
                        db.execute("UPDATE profile_groups SET name=?,proxy_server=? "
                                   "WHERE owner=? AND id=?", (name, proxy, owner, group_id))
                        identity = group_id
                    else:
                        identity = uuid.uuid4().hex[:24]
                        db.execute("INSERT INTO profile_groups VALUES (?,?,?,?,?)",
                                   (identity, owner, name, proxy, str(int(time.time()))))
                    db.commit()
                except sqlite3.IntegrityError:
                    raise BrowserError("Group name already exists", 409) from None
                return dict(db.execute("SELECT * FROM profile_groups WHERE owner=? AND id=?",
                                       (owner, identity)).fetchone())

    async def delete_group(self, owner, group_id):
        async with self.lock:
            with self._db() as db:
                self._group(db, owner, group_id)
                # No cascade: existing profile storage and credentials remain intact.
                db.execute("UPDATE profiles SET group_id=NULL WHERE owner=? AND group_id=?",
                           (owner, group_id))
                db.execute("DELETE FROM profile_groups WHERE owner=? AND id=?", (owner, group_id))
                db.commit()

    async def templates(self, owner):
        async with self.lock:
            with self._db() as db:
                return [dict(row, settings=json.loads(row["settings"])) for row in
                        db.execute("SELECT * FROM profile_templates WHERE owner=? "
                                   "ORDER BY name COLLATE NOCASE", (owner,)).fetchall()]

    async def save_template(self, owner, payload, template_id=""):
        name = _name(payload.get("name"), "Template name")
        config = payload.get("settings") or {}
        if not isinstance(config, dict):
            raise BrowserError("Template settings must be an object")
        info = validate_profile(dict(config, name="Template"))
        # Never persist passwords, usernames, or template owner secrets.
        info.pop("proxy_username", None)
        info.pop("proxy_password", None)
        info.pop("name", None)
        async with self.lock:
            with self._db() as db:
                try:
                    if template_id:
                        existing = db.execute("SELECT id FROM profile_templates WHERE id=? AND owner=?",
                                              (template_id, owner)).fetchone()
                        if not existing:
                            raise BrowserError("Template not found", 404)
                        db.execute("UPDATE profile_templates SET name=?, settings=? "
                                   "WHERE owner=? AND id=?", (name, json.dumps(info), owner, template_id))
                        identity = template_id
                    else:
                        identity = uuid.uuid4().hex[:24]
                        db.execute("INSERT INTO profile_templates VALUES (?,?,?,?,?)",
                                   (identity, owner, name, json.dumps(info), str(int(time.time()))))
                    db.commit()
                except sqlite3.IntegrityError:
                    raise BrowserError("Template name already exists", 409) from None
                result = db.execute("SELECT * FROM profile_templates WHERE owner=? AND id=?",
                                    (owner, identity)).fetchone()
                return dict(result, settings=info)

    async def delete_template(self, owner, template_id):
        async with self.lock:
            with self._db() as db:
                if db.execute("DELETE FROM profile_templates WHERE owner=? AND id=?",
                              (owner, template_id)).rowcount == 0:
                    raise BrowserError("Template not found", 404)
                db.commit()

    async def batch_create(self, owner, payload):
        try:
            count = int(payload.get("count", 1))
            start = int(payload.get("start_index", 1))
        except (ValueError, TypeError):
            raise BrowserError("Invalid batch count or starting index") from None
        if not 1 <= count <= 100 or not 1 <= start <= 999999:
            raise BrowserError("Batch supports 1–100 profiles and positive start index")
        prefix = _name(payload.get("prefix", "Profile"), "Batch prefix")
        rows = payload.get("csv_rows") or []
        proxies = payload.get("proxies") or []
        if not isinstance(rows, list) or len(rows) > 100 or not isinstance(proxies, list) or len(proxies) > 100:
            raise BrowserError("Invalid CSV rows or proxy assignments")
        if rows and len(rows) != count:
            raise BrowserError("CSV row count must match batch count")
        group_id = str(payload.get("group_id") or "")
        template_id = str(payload.get("template_id") or "")
        # Results are per row: invalid names/proxies never create a partial profile.
        async with self.lock:
            with self._db() as db:
                group_id = self._group(db, owner, group_id)
                group = db.execute("SELECT proxy_server FROM profile_groups WHERE id=? AND owner=?",
                                   (group_id, owner)).fetchone() if group_id else None
                config = {}
                if template_id:
                    template = db.execute("SELECT settings FROM profile_templates "
                                          "WHERE id=? AND owner=?", (template_id, owner)).fetchone()
                    if not template:
                        raise BrowserError("Template not found", 404)
                    config = json.loads(template["settings"])
                existing = {r[0].casefold() for r in db.execute(
                    "SELECT name FROM profiles WHERE owner=?", (owner,))}
                capacity = MAX_PROFILES - len(existing)
                pending = []
                errors = []
                for idx in range(count):
                    try:
                        row = rows[idx] if rows else {}
                        if not isinstance(row, dict):
                            raise BrowserError("Invalid CSV row")
                        name = _name(row.get("name") or (prefix + " " + str(start + idx)))
                        if name.casefold() in existing:
                            raise BrowserError("Duplicate profile name")
                        if len(pending) >= capacity:
                            raise BrowserError("Profile limit reached")
                        values = {**config, **row, "name": name}
                        if idx < len(proxies):
                            values["proxy_server"] = proxies[idx]
                        elif payload.get("proxy_server"):
                            values["proxy_server"] = payload["proxy_server"]
                        if not values.get("proxy_server") and group:
                            values["proxy_server"] = group["proxy_server"]
                        info = validate_profile(values)
                        tags = _tags(values.get("tags") or payload.get("tags"))
                        pending.append((idx + 1, info, tags))
                        existing.add(name.casefold())
                    except (BrowserError, ValueError, TypeError) as exc:
                        errors.append({"row": idx + 1, "error": str(exc)})
                created = []
                try:
                    for idx, info, tags in pending:
                        identity = self._insert(db, self.root / "profiles", owner, info,
                                                group_id, tags)
                        created.append({"row": idx, "id": identity, "name": info["name"]})
                    db.commit()
                except Exception:
                    db.rollback()
                    for item in created:
                        shutil.rmtree(self.root / "profiles" / item["id"], ignore_errors=True)
                    raise BrowserError("Batch storage failed; all new rows rolled back", 500) from None
                return {"requested": count, "created": created, "errors": errors,
                        "created_count": len(created), "failed_count": len(errors)}

    async def bulk(self, owner, payload):
        ids = payload.get("profile_ids") or []
        if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or len(ids) != len(set(ids)):
            raise BrowserError("Select 1–100 distinct profiles")
        action = str(payload.get("action") or "")
        if action not in {"move", "delete"}:
            raise BrowserError("Unsupported bulk action")
        async with self.lock:
            with self._db() as db:
                group = self._group(db, owner, payload.get("group_id")) if action == "move" else ""
                for identity in ids:
                    if not isinstance(identity, str) or not PROFILE_ID.fullmatch(identity):
                        raise BrowserError("Invalid profile selection")
                    if not db.execute("SELECT 1 FROM profiles WHERE id=? AND owner=?",
                                      (identity, owner)).fetchone():
                        raise BrowserError("Profile not found", 404)
                    if action == "delete" and identity in self.running:
                        raise BrowserError("Stop every selected profile before bulk deletion", 409)
                if action == "move":
                    db.executemany("UPDATE profiles SET group_id=? WHERE owner=? AND id=?",
                                   ((group or None, owner, identity) for identity in ids))
                else:
                    db.executemany("DELETE FROM profiles WHERE owner=? AND id=?",
                                   ((owner, identity) for identity in ids))
                db.commit()
            if action == "delete":
                for identity in ids:
                    shutil.rmtree(self.root / "profiles" / identity, ignore_errors=True)
            return {"ok": True, "updated": len(ids), "action": action}
