"""Experiment Studio browser engine: per-account persistent Chrome profiles.

Browser contexts run inside the existing authenticated Scene service. Chrome
uses a private Playwright pipe (no exposed CDP/VNC endpoints). Profile records
and browser directories are kept under WEB_DATA_DIR, never in Git releases.
This provides isolated browser *profiles*, not a guarantee of invisibility.
"""
from __future__ import annotations

import asyncio
import os
import ipaddress
import json
import re
import shutil
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

MAX_PROFILES = 500
MAX_ACTIVE = 2
IDLE_SECONDS = 30 * 60
PROFILE_ID = re.compile(r"^[a-f0-9]{24}$")
LOCALE = re.compile(r"^[a-z]{2,3}(?:-[A-Z]{2})?$")
CHROME_PATHS = ("/usr/bin/google-chrome-stable", "/usr/bin/google-chrome",
                "/usr/bin/chromium", "/usr/bin/chromium-browser")


class BrowserError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def browser_executable() -> str | None:
    return next((name for name in CHROME_PATHS if Path(name).is_file()), None)


def _public_host(hostname: str) -> None:
    value = hostname.lower().rstrip(".")
    if not value or value in {"localhost", "metadata.google.internal"} or \
       value.endswith((".localhost", ".local", ".internal")):
        raise BrowserError("Private or local destinations are not supported")
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return
    if not ip.is_global:
        raise BrowserError("Private or local destinations are not supported")


def validate_url(value: str, *, allow_blank: bool = False) -> str:
    url = value.strip()
    if url == "about:blank" and allow_blank:
        return url
    if len(url) > 2000 or any(ord(c) < 32 for c in url):
        raise BrowserError("Invalid browser URL")
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise BrowserError("Invalid browser URL") from None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or \
       parsed.username is not None or parsed.password is not None:
        raise BrowserError("Only HTTP(S) website addresses are supported")
    _public_host(parsed.hostname)
    return url


def validate_proxy(value: str) -> str:
    if not value:
        return ""
    if len(value) > 500 or any(ord(c) < 33 for c in value):
        raise BrowserError("Invalid proxy server")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise BrowserError("Invalid proxy port or host") from None
    if parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname \
       or parsed.username is not None or parsed.password is not None \
       or not port or parsed.path not in ("", "/") \
       or parsed.query or parsed.fragment:
        raise BrowserError("Proxy must be http(s)://host:port or socks5://host:port")
    _public_host(parsed.hostname)
    return value


def validate_profile(payload: dict) -> dict:
    name = str(payload.get("name") or "").strip()
    if not name or len(name) > 64 or any(ord(c) < 32 for c in name):
        raise BrowserError("Profile name must be 1–64 characters")
    locale = str(payload.get("locale") or "en-US").strip()
    if not LOCALE.fullmatch(locale):
        raise BrowserError("Invalid locale")
    timezone = str(payload.get("timezone") or "UTC").strip()
    try:
        ZoneInfo(timezone)
    except (KeyError, ValueError):
        raise BrowserError("Unknown timezone") from None
    agent = str(payload.get("user_agent") or "").strip()
    if len(agent) > 360 or any(ord(c) < 32 for c in agent):
        raise BrowserError("Invalid user agent")
    try:
        width = int(payload.get("width") or 1280)
        height = int(payload.get("height") or 800)
    except (TypeError, ValueError):
        raise BrowserError("Viewport dimensions must be integers") from None
    if not (320 <= width <= 2560 and 320 <= height <= 1600):
        raise BrowserError("Viewport out of supported range")
    username = str(payload.get("proxy_username") or "")
    password = str(payload.get("proxy_password") or "")
    if max(len(username), len(password)) > 400 or \
       any(ord(c) < 32 for c in username + password):
        raise BrowserError("Invalid proxy credentials")
    server = validate_proxy(str(payload.get("proxy_server") or "").strip())
    if (username or password) and not server:
        raise BrowserError("Set the proxy server before its credentials")
    if server.startswith("socks5://") and (username or password):
        raise BrowserError("Authenticated SOCKS5 proxies are not supported")
    privacy = str(payload.get("privacy_mode") or "strict")
    if privacy not in {"standard", "strict"}:
        raise BrowserError("Invalid privacy mode")
    dnt_value = str(payload.get("do_not_track", "on")).lower()
    if dnt_value not in {"on", "off", "true", "false", "1", "0"}:
        raise BrowserError("Invalid Do Not Track setting")
    geo_mode = str(payload.get("geolocation_mode") or "deny")
    if geo_mode not in {"deny", "fixed"}:
        raise BrowserError("Invalid geolocation setting")
    color_scheme = str(payload.get("color_scheme") or "no-preference")
    reduced_motion = str(payload.get("reduced_motion") or "no-preference")
    if color_scheme not in {"no-preference", "light", "dark"} or \
       reduced_motion not in {"no-preference", "reduce"}:
        raise BrowserError("Invalid device appearance preference")
    try:
        scale = float(payload.get("device_scale") or 1)
        latitude = float(payload.get("geo_lat") or 0)
        longitude = float(payload.get("geo_lon") or 0)
    except (TypeError, ValueError, OverflowError):
        raise BrowserError("Invalid device scale or location") from None
    if scale not in {1.0, 1.25, 1.5, 2.0} or not (-90 <= latitude <= 90) or \
       not (-180 <= longitude <= 180):
        raise BrowserError("Device scale or geolocation out of range")
    return dict(name=name, start_url=validate_url(
        str(payload.get("start_url") or "about:blank"), allow_blank=True),
        proxy_server=server, proxy_username=username, proxy_password=password,
        locale=locale, timezone=timezone, user_agent=agent,
        width=width, height=height, privacy_mode=privacy,
        do_not_track=int(dnt_value in {"on", "true", "1"}),
        geolocation_mode=geo_mode, geo_lat=latitude, geo_lon=longitude,
        color_scheme=color_scheme, reduced_motion=reduced_motion,
        device_scale=scale)


class BrowserProfileEngine:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.lock = asyncio.Lock()
        self.running: dict[str, dict] = {}
        self.playwright = None
        self._initialized = False

    def _db(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        (self.root / "profiles").mkdir(exist_ok=True, mode=0o700)
        (self.root / "profiles").chmod(0o700)
        dbpath = self.root / "profiles.sqlite3"
        db = sqlite3.connect(dbpath, timeout=10)
        db.row_factory = sqlite3.Row
        if not self._initialized:
            db.execute("""CREATE TABLE IF NOT EXISTS profiles (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
                start_url TEXT NOT NULL, proxy_server TEXT NOT NULL,
                proxy_username TEXT NOT NULL, proxy_password TEXT NOT NULL,
                locale TEXT NOT NULL, timezone TEXT NOT NULL,
                user_agent TEXT NOT NULL, width INTEGER NOT NULL,
                height INTEGER NOT NULL, created_at TEXT NOT NULL
            )""")
            # Additive columns only; existing cookies, browsing directories, and data survive.
            columns = {row[1] for row in db.execute("PRAGMA table_info(profiles)")}
            additions = {
                "privacy_mode": "TEXT NOT NULL DEFAULT 'standard'",
                "do_not_track": "INTEGER NOT NULL DEFAULT 0",
                "geolocation_mode": "TEXT NOT NULL DEFAULT 'deny'",
                "geo_lat": "REAL NOT NULL DEFAULT 0",
                "geo_lon": "REAL NOT NULL DEFAULT 0",
                "color_scheme": "TEXT NOT NULL DEFAULT 'no-preference'",
                "reduced_motion": "TEXT NOT NULL DEFAULT 'no-preference'",
                "device_scale": "REAL NOT NULL DEFAULT 1",
            }
            for column, declaration in additions.items():
                if column not in columns:
                    db.execute("ALTER TABLE profiles ADD COLUMN " + column + " " + declaration)
            db.execute("CREATE INDEX IF NOT EXISTS profiles_owner ON profiles(owner)")
            db.commit()
            dbpath.chmod(0o600)
            self._initialized = True
        return db

    def _get(self, owner: str, profile_id: str) -> dict:
        if not PROFILE_ID.fullmatch(profile_id):
            raise BrowserError("Profile not found", 404)
        with self._db() as db:
            row = db.execute("SELECT * FROM profiles WHERE id=? AND owner=?",
                             (profile_id, owner)).fetchone()
        if row is None:
            raise BrowserError("Profile not found", 404)
        return dict(row)

    def _public(self, row: dict) -> dict:
        identity = row["id"]
        status = self.running.get(identity)
        return dict(id=identity, name=row["name"], locale=row["locale"],
                    timezone=row["timezone"], start_url=row["start_url"],
                    proxy_server=row["proxy_server"], width=row["width"],
                    height=row["height"], user_agent=row["user_agent"],
                    created_at=row["created_at"], privacy_mode=row["privacy_mode"],
                    do_not_track=bool(row["do_not_track"]),
                    geolocation_mode=row["geolocation_mode"],
                    geo_lat=row["geo_lat"] if row["geolocation_mode"] == "fixed" else None,
                    geo_lon=row["geo_lon"] if row["geolocation_mode"] == "fixed" else None,
                    color_scheme=row["color_scheme"], reduced_motion=row["reduced_motion"],
                    device_scale=row["device_scale"],
                    state="running" if status else "stopped",
                    current_url=status["page"].url if status else "")

    async def list(self, owner: str) -> list[dict]:
        async with self.lock:
            await self._sweep()
            with self._db() as db:
                rows = db.execute("SELECT * FROM profiles WHERE owner=? ORDER BY created_at",
                                  (owner,)).fetchall()
            return [self._public(dict(row)) for row in rows]

    async def create(self, owner: str, payload: dict) -> dict:
        info = validate_profile(payload)
        async with self.lock:
            with self._db() as db:
                count = db.execute("SELECT COUNT(*) FROM profiles WHERE owner=?", (owner,)).fetchone()[0]
                if count >= MAX_PROFILES:
                    raise BrowserError("Profile limit reached", 409)
                identity = uuid.uuid4().hex[:24]
                folder = self.root / "profiles" / identity
                folder.mkdir(parents=True, exist_ok=False, mode=0o700)
                columns = ("id", "owner", *info.keys(), "created_at")
                values = (identity, owner, *info.values(), str(int(time.time())))
                db.execute("INSERT INTO profiles (" + ",".join(columns) + ") VALUES (" +
                           ",".join("?" for _ in columns) + ")", values)
                db.commit()
            return self._public(self._get(owner, identity))

    async def clone(self, owner: str, profile_id: str) -> dict:
        """Clone settings only; never copy cookies, browsing history or proxy secrets."""
        row = self._get(owner, profile_id)
        fields = ("start_url", "proxy_server", "locale", "timezone", "user_agent",
                  "width", "height", "privacy_mode", "do_not_track",
                  "geolocation_mode", "geo_lat", "geo_lon", "color_scheme",
                  "reduced_motion", "device_scale")
        settings = {field: row[field] for field in fields}
        settings["name"] = ("Copy of " + row["name"])[:64]
        settings["proxy_username"] = ""
        settings["proxy_password"] = ""
        return await self.create(owner, settings)

    async def delete(self, owner: str, profile_id: str) -> None:
        async with self.lock:
            self._get(owner, profile_id)
            if profile_id in self.running:
                raise BrowserError("Stop this profile before deleting it", 409)
            with self._db() as db:
                db.execute("DELETE FROM profiles WHERE id=? AND owner=?", (profile_id, owner))
                db.commit()
            shutil.rmtree(self.root / "profiles" / profile_id, ignore_errors=True)

    async def _sweep(self) -> None:
        now = time.monotonic()
        for identity, info in list(self.running.items()):
            if now - info["used"] > IDLE_SECONDS:
                await self._close(identity)

    async def _close(self, profile_id: str) -> None:
        state = self.running.pop(profile_id, None)
        if state:
            try:
                await state["context"].close()
            except Exception:
                pass
        if not self.running and self.playwright is not None:
            try:
                await self.playwright.stop()
            finally:
                self.playwright = None

    async def start(self, owner: str, profile_id: str) -> dict:
        async with self.lock:
            await self._sweep()
            row = self._get(owner, profile_id)
            if profile_id in self.running:
                self.running[profile_id]["used"] = time.monotonic()
                return self._public(row)
            if len(self.running) >= MAX_ACTIVE:
                raise BrowserError("Two browsers are already running; stop one first", 409)
            chrome = browser_executable()
            if not chrome:
                raise BrowserError("Chrome is not installed on this VPS", 503)
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                raise BrowserError("Playwright runtime is not installed", 503) from None
            context = None
            try:
                if self.playwright is None:
                    self.playwright = await async_playwright().start()
                args = dict(headless=True, executable_path=chrome,
                            viewport={"width": row["width"], "height": row["height"]},
                            locale=row["locale"], timezone_id=row["timezone"],
                            device_scale_factor=row["device_scale"],
                            color_scheme=row["color_scheme"],
                            reduced_motion=row["reduced_motion"],
                            service_workers="block" if row["privacy_mode"] == "strict" else "allow",
                            accept_downloads=False,
                            extra_http_headers={"DNT": "1"} if row["do_not_track"] else {},
                            args=["--disable-dev-shm-usage", "--no-first-run"])
                if row["geolocation_mode"] == "fixed":
                    args["geolocation"] = {"latitude": row["geo_lat"],
                                           "longitude": row["geo_lon"]}
                    args["permissions"] = ["geolocation"]
                else:
                    args["permissions"] = []
                if row["user_agent"]:
                    args["user_agent"] = row["user_agent"]
                if row["proxy_server"]:
                    args["proxy"] = {"server": row["proxy_server"]}
                    if row["proxy_username"]:
                        args["proxy"]["username"] = row["proxy_username"]
                    if row["proxy_password"]:
                        args["proxy"]["password"] = row["proxy_password"]

                if row["privacy_mode"] == "strict" or row["proxy_server"]:
                    # Chrome policy constrains UDP leakage, not a DNS/WebRTC anonymity proof.
                    args["args"].append("--force-webrtc-ip-handling-policy=disable_non_proxied_udp")
                for folder in ("cache", "config"):
                    (self.root / folder).mkdir(exist_ok=True, mode=0o700)
                args["env"] = dict(os.environ,
                                   HOME=str(self.root),
                                   XDG_CACHE_HOME=str(self.root / "cache"),
                                   XDG_CONFIG_HOME=str(self.root / "config"))
                context = await self.playwright.chromium.launch_persistent_context(
                    str(self.root / "profiles" / profile_id), **args)
                page = context.pages[0] if context.pages else await context.new_page()
                if row["start_url"] != "about:blank":
                    try:
                        await page.goto(row["start_url"], wait_until="domcontentloaded", timeout=15000)
                    except Exception:
                        pass  # The browser remains open for manual navigation.
                self.running[profile_id] = dict(context=context, page=page,
                                                owner=owner, used=time.monotonic())
                return self._public(row)
            except Exception:
                if context is not None:
                    try:
                        await context.close()
                    except Exception:
                        pass
                if not self.running and self.playwright is not None:
                    try:
                        await self.playwright.stop()
                    finally:
                        self.playwright = None
                raise BrowserError("Chrome could not start; profile data was preserved", 502) from None

    async def stop(self, owner: str, profile_id: str) -> dict:
        async with self.lock:
            row = self._get(owner, profile_id)
            await self._close(profile_id)
            return self._public(row)

    async def _session(self, owner: str, profile_id: str) -> dict:
        row = self._get(owner, profile_id)
        state = self.running.get(profile_id)
        if not state or state["owner"] != owner:
            raise BrowserError("Start the browser profile first", 409)
        state["used"] = time.monotonic()
        return state

    async def navigate(self, owner: str, profile_id: str, url: str) -> dict:
        site = validate_url(url)
        async with self.lock:
            state = await self._session(owner, profile_id)
            try:
                await state["page"].goto(site, wait_until="domcontentloaded", timeout=18000)
            except Exception:
                raise BrowserError("Navigation failed or timed out", 502) from None
            return {"url": state["page"].url, "title": await state["page"].title()}

    async def action(self, owner: str, profile_id: str, payload: dict) -> dict:
        async with self.lock:
            state = await self._session(owner, profile_id)
            page = state["page"]
            action = payload.get("action")
            try:
                if action == "click":
                    x, y = int(payload.get("x")), int(payload.get("y"))
                    view = page.viewport_size or {"width": 1280, "height": 800}
                    if not (0 <= x < view["width"] and 0 <= y < view["height"]):
                        raise BrowserError("Click must be within the browser viewport")
                    await page.mouse.click(x, y)
                elif action == "type":
                    value = str(payload.get("text") or "")
                    if len(value) > 1500:
                        raise BrowserError("Typing limit is 1500 characters")
                    await page.keyboard.insert_text(value)
                elif action == "key":
                    key = str(payload.get("key") or "")
                    if key not in {"Enter", "Tab", "Escape", "Backspace", "ArrowUp",
                                   "ArrowDown", "ControlOrMeta+A"}:
                        raise BrowserError("Unsupported key")
                    await page.keyboard.press(key)
                elif action == "scroll":
                    dy = max(-1000, min(1000, int(payload.get("dy") or 0)))
                    await page.mouse.wheel(0, dy)
                elif action == "reload":
                    await page.reload(wait_until="domcontentloaded", timeout=18000)
                elif action == "back":
                    await page.go_back(wait_until="domcontentloaded", timeout=18000)
                else:
                    raise BrowserError("Unsupported browser action")
            except BrowserError:
                raise
            except Exception:
                raise BrowserError("Browser action failed", 502) from None
            return {"url": page.url}

    async def privacy_report(self, owner: str, profile_id: str) -> dict:
        """Report observable browser configuration; no spoofing or unverifiable claims."""
        async with self.lock:
            state = await self._session(owner, profile_id)
            row = self._get(owner, profile_id)
            try:
                observed = await state["page"].evaluate("""() => ({
                    user_agent: navigator.userAgent, language: navigator.language,
                    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                    width: innerWidth, height: innerHeight,
                    device_pixel_ratio: devicePixelRatio,
                    do_not_track: navigator.doNotTrack || null
                })""")
            except Exception:
                raise BrowserError("Browser privacy inspection failed", 502) from None
            return {"profile_id": profile_id, "privacy_mode": row["privacy_mode"],
                    "proxy_configured": bool(row["proxy_server"]),
                    "webrtc_udp_restricted": bool(
                        row["privacy_mode"] == "strict" or row["proxy_server"]),
                    "service_workers_blocked": row["privacy_mode"] == "strict",
                    "geolocation_mode": row["geolocation_mode"],
                    "dnt_header_enabled": bool(row["do_not_track"]),
                    "observed": observed,
                    "verified_dns_leak_free": False,
                    "verified_webrtc_leak_free": False}

    async def connection_check(self, owner: str, profile_id: str) -> dict:
        """Explicit user-triggered IP lookup from the selected browser context."""
        async with self.lock:
            state = await self._session(owner, profile_id)
            page = None
            try:
                page = await state["context"].new_page()
                response = await page.goto("https://api.ipify.org?format=json",
                                           wait_until="domcontentloaded", timeout=15000)
                if response is None or response.status != 200:
                    raise ValueError("IP endpoint unavailable")
                data = json.loads(await response.text())
                ip = ipaddress.ip_address(str(data["ip"]))
                if not ip.is_global:
                    raise ValueError("Non-public address")
                return {"ip": str(ip), "source": "api.ipify.org",
                        "proxy_configured": bool(self._get(owner, profile_id)["proxy_server"]),
                        "dns_leak_tested": False, "webrtc_leak_tested": False}
            except Exception:
                raise BrowserError("Exit-IP check failed; proxy or external service unavailable", 502) from None
            finally:
                if page is not None:
                    try:
                        await page.close()
                    except Exception:
                        pass

    async def screenshot(self, owner: str, profile_id: str) -> bytes:
        async with self.lock:
            state = await self._session(owner, profile_id)
            try:
                return await state["page"].screenshot(type="png", timeout=18000)
            except Exception:
                raise BrowserError("Browser preview unavailable", 502) from None

    async def close_all(self) -> None:
        async with self.lock:
            for identity in list(self.running):
                await self._close(identity)
