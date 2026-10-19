"""Auto Stream V3 YouTube broadcast control.

Creates private YouTube broadcast + RTMPS stream + binding only by explicit,
gated user request. No automatic public publishing, no shared Uploader tokens.
Remote YouTube state is always authoritative; local encoder != YouTube LIVE.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Body, HTTPException

import auto_stream_integrations as google
from auto_stream_engine import StreamEngine, service, utc_now

router = APIRouter(prefix="/engine", tags=["Auto Stream YouTube Live Controller"])
_ID = re.compile(r"^[A-Za-z0-9_-]{6,120}$")
_CHANNEL = google.CHANNEL_RE


def _allowed(flag: str):
    if os.environ.get(flag) != "1":
        raise PermissionError(flag + " is OFF")


def _yt_url(endpoint: str, **query):
    return google.YT_BASE + "/" + endpoint + "?" + urlencode(query)


def _rtmps(value: str):
    """Only Google's verified RTMPS ingest host is accepted."""
    p = urlparse(value)
    if (p.scheme != "rtmps" or p.username or p.password or p.port or
            p.hostname not in ("a.rtmps.youtube.com", "b.rtmps.youtube.com") or
            p.path != "/live2" or p.query or p.fragment):
        raise ValueError("YouTube returned unsupported RTMPS ingest address")
    return value.rstrip("/")


class BroadcastController:
    def __init__(self, *, engine: StreamEngine | None = None,
                 acct: google.AutoStreamAccounts | None = None):
        self.engine = engine or service()
        self.acct = acct or google.accounts()
        self.lock = threading.RLock()
        self._init()

    def _init(self):
        # Always use Auto Stream's own integration DB; no uploader DB/files.
        with self.acct.connect() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS live_control (
              job_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL,
              broadcast_id TEXT, stream_id TEXT,
              secret BLOB, state TEXT NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            con.execute("""CREATE INDEX IF NOT EXISTS live_channel_state
                ON live_control(channel_id, state)""")

    @staticmethod
    def _public(row):
        if not row:
            return None
        return {"job_id": row["job_id"], "channel_id": row["channel_id"],
                "broadcast_id": row["broadcast_id"], "stream_id": row["stream_id"],
                "state": row["state"], "updated_at": row["updated_at"],
                "secret_available": bool(row["secret"])}

    def _row(self, job_id):
        with self.acct.connect() as con:
            return con.execute("SELECT * FROM live_control WHERE job_id=?",
                               (job_id,)).fetchone()

    def get(self, job_id):
        self.engine.get(job_id)  # 404 for nonexisting job
        return self._public(self._row(job_id))

    def _set(self, job_id, **updates):
        if not updates:
            return
        fields = ", ".join(k+"=?" for k in updates)
        with self.acct.connect() as con:
            con.execute("UPDATE live_control SET " + fields + ", updated_at=? WHERE job_id=?",
                        (*updates.values(), utc_now(), job_id))

    def _access_verified(self, channel_id: str):
        access = self.acct._access(channel_id, google.YOUTUBE_SCOPE)
        # Important: a revoked or switched Google grant cannot control
        # another channel even if a stale local channel record exists.
        data = google._http_json(_yt_url("channels", part="id", mine="true",
                                        maxResults="10"), access_token=access)
        ids = [c.get("id") for c in data.get("items", [])]
        if channel_id not in ids or len(ids) != 1:
            raise ValueError("Authorized Google identity does not match stream channel")
        return access

    def _record_reservation(self, job_id, channel_id):
        with self.acct.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute("SELECT * FROM live_control WHERE job_id=?",
                                   (job_id,)).fetchone()
            if existing:
                raise ValueError("YouTube provision already attempted: inspect/reconcile before retry")
            con.execute("""INSERT INTO live_control
                (job_id, channel_id, state, created_at, updated_at)
                VALUES (?,?, 'PROVISIONING', ?, ?)""",
                (job_id, channel_id, utc_now(), utc_now()))

    def provision(self, job_id):
        """Explicit API creates private broadcast, RTMPS stream and binds them.

        An interrupted/partial request never blindly recreates resources.
        YouTube may have committed a request before an HTTP timeout.
        """
        _allowed("AUTO_STREAM_YOUTUBE_MUTATIONS_ENABLED")
        with self.lock:
            job = self.engine.get(job_id)
            if job["status"] not in ("READY", "SCHEDULED"):
                raise ValueError("Prepare a job before creating YouTube resources")
            start = job["scheduled_at"]
            if start is None:
                start = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(
                    timespec="seconds")
            if datetime.fromisoformat(start) <= datetime.now(timezone.utc) + timedelta(seconds=30):
                raise ValueError("YouTube requires a future scheduled start time")
            access = self._access_verified(job["channel_id"])
            self._record_reservation(job_id, job["channel_id"])
            try:
                b = google._http_json(
                    _yt_url("liveBroadcasts", part="snippet,status,contentDetails"),
                    access_token=access, method="POST",
                    payload={"snippet": {"title": job["title"], "scheduledStartTime": start},
                             "status": {"privacyStatus": "private"},
                             "contentDetails": {"enableAutoStart": False,
                                                "enableAutoStop": False,
                                                "monitorStream": {"enableMonitorStream": True}}})
                bid = b.get("id")
                if not isinstance(bid, str) or not _ID.fullmatch(bid):
                    raise ValueError("Google returned an invalid broadcast identifier")
                self._set(job_id, broadcast_id=bid, state="BROADCAST_CREATED")
                s = google._http_json(_yt_url("liveStreams", part="snippet,cdn,status"),
                                      access_token=access, method="POST",
                                      payload={"snippet": {"title": job["title"][:100]},
                                               "cdn": {"frameRate": "30fps",
                                                       "resolution": "720p",
                                                       "ingestionType": "rtmp"}})
                sid = s.get("id")
                ingest = (s.get("cdn") or {}).get("ingestionInfo") or {}
                key = ingest.get("streamName")
                url = ingest.get("rtmpsIngestionAddress")
                if not isinstance(sid, str) or not _ID.fullmatch(sid):
                    raise ValueError("Google returned invalid stream identifier")
                if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,200}", key):
                    raise ValueError("Google RTMPS key is missing or invalid")
                url = _rtmps(str(url or ""))
                encrypted = google._cipher().encrypt(
                    json.dumps({"url":url,"key":key}).encode())
                self._set(job_id, stream_id=sid, secret=encrypted, state="STREAM_CREATED")
                bound = google._http_json(
                    _yt_url("liveBroadcasts/bind", id=bid, streamId=sid,
                            part="id,contentDetails,status"),
                    access_token=access, method="POST")
                if (bound.get("contentDetails") or {}).get("boundStreamId") != sid:
                    raise ValueError("YouTube stream binding cannot be confirmed")
                self._set(job_id, state="BOUND")
            except Exception:
                self._set(job_id, state="NEEDS_RECONCILIATION")
                raise
            return self.get(job_id)

    def _google_state(self, row, access):
        bid, sid = row["broadcast_id"], row["stream_id"]
        if not bid or not sid:
            raise ValueError("YouTube resources are not fully provisioned")
        b = google._http_json(_yt_url("liveBroadcasts", id=bid,
                             part="id,status,contentDetails"), access_token=access)
        items = b.get("items") or []
        if len(items) != 1 or items[0].get("id") != bid:
            raise ValueError("YouTube broadcast not found or access revoked")
        broadcast = items[0]
        if (broadcast.get("contentDetails") or {}).get("boundStreamId") != sid:
            raise ValueError("YouTube broadcast stream binding changed")
        s = google._http_json(_yt_url("liveStreams", id=sid,
                              part="id,status,cdn"), access_token=access)
        streams = s.get("items") or []
        if len(streams) != 1 or streams[0].get("id") != sid:
            raise ValueError("YouTube stream not found or access revoked")
        status = (broadcast.get("status") or {}).get("lifeCycleStatus", "unknown")
        ingest_status = (streams[0].get("status") or {}).get("streamStatus", "unknown")
        return {"job_id": row["job_id"], "channel_id": row["channel_id"],
                "broadcast_id": bid, "stream_id": sid,
                "youtube_broadcast_status": status, "youtube_ingest_status": ingest_status,
                "youtube_verified_live": status == "live",
                "privacy": (broadcast.get("status") or {}).get("privacyStatus", "unknown"),
                "local_state": row["state"]}

    def inspect_remote(self, job_id):
        row = self._row(job_id)
        if not row:
            raise ValueError("No YouTube resources provisioned for this job")
        return self._google_state(row, self._access_verified(row["channel_id"]))

    def start_ingest(self, job_id):
        """Starts FFmpeg only, not the public YouTube broadcast."""
        _allowed("AUTO_STREAM_PUBLISH_ENABLED")
        _allowed("AUTO_STREAM_YOUTUBE_MUTATIONS_ENABLED")
        with self.lock:
            row = self._row(job_id)
            if not row or row["state"] != "BOUND" or not row["secret"]:
                raise ValueError("YouTube resources must be bound before ingest starts")
            access = self._access_verified(row["channel_id"])
            current = self._google_state(row, access)
            if current["youtube_broadcast_status"] in ("complete", "live"):
                raise ValueError("Broadcast already completed/live: explicit review required")
            try:
                packed = json.loads(google._cipher().decrypt(row["secret"]).decode())
                result = self.engine.start(job_id, internal_ingest=(
                    _rtmps(packed["url"]), packed["key"]))
            except (ValueError, RuntimeError, OSError):
                raise
            self._set(job_id, state="ENCODER_STARTED")
            return {"job": result, "youtube_verified_live": False,
                    "next": "Verify active YouTube ingestion before transition"}

    def transition(self, job_id, target: str):
        _allowed("AUTO_STREAM_YOUTUBE_MUTATIONS_ENABLED")
        _allowed("AUTO_STREAM_LIVE_TRANSITIONS_ENABLED")
        if target not in ("testing", "live", "complete"):
            raise ValueError("Unsupported transition")
        if target == "live":
            _allowed("AUTO_STREAM_GO_LIVE_ENABLED")
        with self.lock:
            row = self._row(job_id)
            if not row or row["state"] not in ("BOUND", "ENCODER_STARTED", "TESTING", "LIVE"):
                raise ValueError("Broadcast is not in a transitionable state")
            access = self._access_verified(row["channel_id"])
            previous = self._google_state(row, access)
            if target in ("testing","live"):
                if previous["youtube_ingest_status"] != "active":
                    raise ValueError("YouTube ingest is not active; LIVE transition blocked")
                if target == "live" and self.engine.get(job_id)["status"] not in ("INGESTING",):
                    raise ValueError("Local streaming worker is not active")
                if target == "testing" and previous["youtube_broadcast_status"] not in ("ready", "testStarting", "testing"):
                    raise ValueError("Broadcast cannot enter testing from current state")
                if target == "live" and previous["youtube_broadcast_status"] not in ("ready", "testing", "liveStarting"):
                    raise ValueError("Broadcast cannot enter live from current state")
            response = google._http_json(_yt_url("liveBroadcasts/transition",
                                    id=row["broadcast_id"], broadcastStatus=target,
                                    part="id,status,contentDetails"),
                                    access_token=access, method="POST")
            new_state = (response.get("status") or {}).get("lifeCycleStatus")
            self._set(job_id, state={"testing":"TESTING","live":"LIVE",
                                     "complete":"COMPLETED"}[target])
            return {"requested_transition": target,
                    "youtube_reported_status": new_state,
                    "youtube_verified_live": new_state == "live",
                    "job_id": job_id}

    def integration_health(self):
        return {"ok": True, "youtube_mutations_enabled":
                os.environ.get("AUTO_STREAM_YOUTUBE_MUTATIONS_ENABLED") == "1",
                "transitions_enabled":
                os.environ.get("AUTO_STREAM_LIVE_TRANSITIONS_ENABLED") == "1",
                "go_live_enabled": os.environ.get("AUTO_STREAM_GO_LIVE_ENABLED") == "1",
                "publishing_enabled": os.environ.get("AUTO_STREAM_PUBLISH_ENABLED") == "1"}


_controller: BroadcastController | None = None
_controller_lock = threading.Lock()


def controller():
    global _controller
    with _controller_lock:
        if _controller is None:
            _controller = BroadcastController()
        return _controller


def _guard(fn):
    try:
        return fn()
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/youtube/health")
def youtube_health():
    return controller().integration_health()


@router.get("/jobs/{job_id}/broadcast")
def broadcast_record(job_id: str):
    return _guard(lambda: {"broadcast": controller().get(job_id)})


@router.post("/jobs/{job_id}/broadcast/provision")
def broadcast_provision(job_id: str):
    return _guard(lambda: controller().provision(job_id))


@router.get("/jobs/{job_id}/broadcast/verify")
def broadcast_verify(job_id: str):
    return _guard(lambda: controller().inspect_remote(job_id))


@router.post("/jobs/{job_id}/broadcast/ingest")
def broadcast_ingest(job_id: str):
    return _guard(lambda: controller().start_ingest(job_id))


@router.post("/jobs/{job_id}/broadcast/transition")
def broadcast_transition(job_id: str, payload: dict = Body(...)):
    return _guard(lambda: controller().transition(job_id, str(payload.get("status", ""))))
