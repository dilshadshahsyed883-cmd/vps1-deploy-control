#!/usr/bin/env python3
from __future__ import annotations
import getpass, hashlib, hmac, os, sqlite3, sys
from datetime import datetime, timezone
from pathlib import Path

DB = Path(os.getenv("WEB_DATA_DIR", "/opt/scene-builder-web/data")) / "web.sqlite3"

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def hash_secret(secret, salt_hex=None):
    salt = bytes.fromhex(salt_hex) if salt_hex else os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode(), salt, 200_000)
    return f"{salt.hex()}:{digest.hex()}"

def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS users(
      username TEXT PRIMARY KEY COLLATE NOCASE,
      secret_hash TEXT NOT NULL,
      is_active INTEGER NOT NULL DEFAULT 1,
      is_admin INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL)""")
    return c

def list_users():
    with conn() as c:
        rows = c.execute("SELECT username,is_active,is_admin,created_at FROM users ORDER BY username").fetchall()
    print("\nUSERNAME                 ACTIVE  ADMIN  CREATED")
    print("-"*72)
    for r in rows:
        print(f"{r['username'][:24]:24} {'YES' if r['is_active'] else 'NO ':6} {'YES' if r['is_admin'] else 'NO ':5} {r['created_at'][:19]}")
    if not rows: print("(no users)")

def add_user():
    username=input("Username: ").strip()
    if not username: raise SystemExit("Username is required.")
    secret=getpass.getpass("Secret key: ").strip()
    confirm=getpass.getpass("Confirm secret key: ").strip()
    if len(secret)<8: raise SystemExit("Secret key must be at least 8 characters.")
    if secret!=confirm: raise SystemExit("Secret keys do not match.")
    admin=input("Admin user? [y/N]: ").strip().lower()=="y"
    with conn() as c:
        c.execute("INSERT INTO users(username,secret_hash,is_active,is_admin,created_at) VALUES(?,?,?,?,?)",
                  (username,hash_secret(secret),1,1 if admin else 0,now_iso()))
    print("User added.")

def reset_key():
    username=input("Username: ").strip()
    secret=getpass.getpass("New secret key: ").strip()
    confirm=getpass.getpass("Confirm secret key: ").strip()
    if len(secret)<8: raise SystemExit("Secret key must be at least 8 characters.")
    if secret!=confirm: raise SystemExit("Secret keys do not match.")
    with conn() as c:
        cur=c.execute("UPDATE users SET secret_hash=? WHERE username=? COLLATE NOCASE",(hash_secret(secret),username))
    if not cur.rowcount: raise SystemExit("User not found.")
    print("Secret key updated.")

def toggle(active):
    username=input("Username: ").strip()
    with conn() as c:
        cur=c.execute("UPDATE users SET is_active=? WHERE username=? COLLATE NOCASE",(1 if active else 0,username))
    if not cur.rowcount: raise SystemExit("User not found.")
    print("User enabled." if active else "User disabled.")

def delete_user():
    username=input("Username to delete: ").strip()
    confirm=input(f"Type DELETE to remove {username}: ").strip()
    if confirm!="DELETE": raise SystemExit("Cancelled.")
    with conn() as c:
        cur=c.execute("DELETE FROM users WHERE username=? COLLATE NOCASE",(username,))
    if not cur.rowcount: raise SystemExit("User not found.")
    print("User deleted.")

actions={"1":list_users,"2":add_user,"3":reset_key,"4":lambda:toggle(True),"5":lambda:toggle(False),"6":delete_user}
while True:
    print("\nScene Builder User Control")
    print("1. List users\n2. Add user\n3. Reset secret key\n4. Enable user\n5. Disable user\n6. Delete user\n0. Exit")
    choice=input("Choose: ").strip()
    if choice=="0": break
    try:
        actions.get(choice, lambda: print("Invalid choice."))()
    except sqlite3.IntegrityError:
        print("That username already exists.")
    except Exception as e:
        print(f"ERROR: {e}")
