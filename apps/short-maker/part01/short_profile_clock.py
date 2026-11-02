"""Dedicated daily Short Maker clock (NOT the offline Auto Script).

Activation must be explicitly gated after all source, render, queue, folder
dependency and two-user release checks. The clock plans only the current
Pakistan date, recovering already-recorded work without multi-day backfill.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone, time, timedelta
from zoneinfo import ZoneInfo

PKT = ZoneInfo("Asia/Karachi")

def local_now(when):
    if when.tzinfo is None or when.utcoffset() is None:
        raise ValueError("Clock requires timezone-aware UTC or local time")
    return when.astimezone(PKT)

LOG = logging.getLogger("short-maker.daily-profiles")


class ShortProfileDailyClock:
    def __init__(self, store, planner, bridge, interval_seconds=30,
                 now=lambda: datetime.now(timezone.utc)):
        self.store = store
        self.planner = planner
        self.bridge = bridge
        self.interval_seconds = interval_seconds
        self.now = now
        self._stop_event = threading.Event()
        self._thread = None

    def _current_day(self, when):
        return local_now(when).date().isoformat()

    def _dispatch(self, owner, profile_id, when, *, explicit=False):
        planned = self.planner.plan(owner, profile_id, when,
                                    explicit_start=explicit)
        if not planned.get("due"):
            return {"status": "not_due", "pkt_date": planned["pkt_date"]}
        # Only the current PKT date; no backlog of prior days after downtime.
        claims = self.planner.reconcile(
            owner, profile_id, self.bridge.lookup, self.bridge.enqueue,
            pkt_date=self._current_day(when))
        return {
            "status": "planned" if not planned["shortage"] else "media_shortage",
            "pkt_date": planned["pkt_date"],
            "target": planned["target"],
            "planned": planned["planned"],
            "shortage": planned["shortage"],
            "queued_or_existing": sum(c["status"] == "enqueued" for c in claims),
        }

    def start_profile(self, owner, profile_id):
        # Invoked only through the feature-gated, authenticated Start route.
        self.store.enable(owner, profile_id)
        return self._dispatch(owner, profile_id, self.now(), explicit=True)

    def tick(self, when=None):
        moment = when or self.now()
        local = local_now(moment)
        day = local.date().isoformat()
        hhmm = local.strftime("%H:%M")
        with self.store._connect() as db:
            profiles = [dict(r) for r in db.execute("""
                SELECT id,owner,schedule_hhmm FROM short_profiles
                WHERE enabled=1 AND deleted_at IS NULL
                  AND schedule_hhmm<=?
                ORDER BY owner,created_at,id
            """, (hhmm,))]
        results = []
        for item in profiles:
            try:
                value = self._dispatch(item["owner"], item["id"], moment)
                results.append({"profile_id": item["id"], "pkt_date": day, **value})
            except Exception as error:
                LOG.exception("Short Profile daily tick failed; unrelated profiles continue")
                results.append({"profile_id": item["id"], "pkt_date": day,
                                "status": "error", "reason": str(error)})
        return results

    def profile_status(self, owner, profile_id, when=None):
        """Owner-only derived progress; never expose another account's jobs."""
        profile = self.store.get(owner, profile_id)
        moment = local_now(when or self.now())
        day = moment.date().isoformat()
        claims = self.planner.list_claims(owner, profile_id, pkt_date=day)
        history = self.store.history(owner, profile_id)
        today_run = next((r for r in history if r["pkt_date"] == day), None)
        completed = running = queued = failed = 0
        for claim in claims:
            state = self.bridge.lookup(claim["job_id"])
            if not state:
                continue
            if str(state.get("owner", "")).casefold() != owner.casefold():
                continue
            status = state.get("status")
            if status == "completed":
                completed += 1
            elif status == "running":
                running += 1
            elif status in ("queued", "uploading", "pausing"):
                queued += 1
            elif status in ("failed", "cancelled"):
                failed += 1
        next_run = None
        if profile["enabled"]:
            hour, minute = map(int, profile["schedule_hhmm"].split(":"))
            due = datetime.combine(moment.date(), time(hour, minute), PKT)
            if today_run is not None:
                due += timedelta(days=1)
            next_run = due.isoformat()
        return {
            "status": "stopped" if not profile["enabled"] else (
                "media_shortage" if today_run and today_run["status"] == "media_shortage"
                else "active"),
            "today_target": today_run["target"] if today_run else profile["videos_per_day"],
            "today_planned": len(claims), "today_completed": completed,
            "today_running": running, "today_queued": queued,
            "today_failed": failed, "next_run": next_run,
            "last_run": history[0]["pkt_date"] if history else None,
            "last_run_status": history[0]["status"] if history else None,
        }

    def _loop(self):
        # The first tick occurs as soon as startup recovery has completed.
        # Event.wait supports immediate cooperative shutdown without timers
        # surviving process termination.
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception:
                LOG.exception("Short Profile scheduler tick error")
            if self._stop_event.wait(self.interval_seconds):
                break

    def launch(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="short-maker-profile-clock", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
