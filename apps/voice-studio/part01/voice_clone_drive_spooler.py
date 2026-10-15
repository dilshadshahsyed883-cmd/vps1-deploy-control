from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("VOICE_CLONE_ROOT", "/home/ubuntu/apps/kokoro_tts_test/data/voice_clone"))
DB_PATH = ROOT / "clone.db"
AUDIO_DIR = ROOT / "audio"
TEXT_DIR = ROOT / "text"
RCLONE = os.environ.get("KOKORA_RCLONE_BIN", "/usr/bin/rclone")
RCLONE_CONFIG = os.environ.get("KOKORA_RCLONE_CONFIG", "/home/ubuntu/.config/masterctl/rclone.conf")
REMOTE = os.environ.get("KOKORA_DRIVE_REMOTE", "gdrive")
DRIVE_ROOT = os.environ.get("KOKORA_DRIVE_ROOT", "Kokora TTS Outputs").strip("/")
POLL_SECONDS = 2.0
RATE_MARKERS = ("429", "rate limit", "ratelimit", "quota", "too many requests", "user rate limit")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def now_ts() -> float:
    return time.time()


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_db() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    TEXT_DIR.mkdir(parents=True, exist_ok=True)
    if not DB_PATH.exists():
        # The web control plane owns DB creation. Do not invent a parallel schema here.
        return
    with db() as con:
        cols = {str(r["name"]) for r in con.execute("PRAGMA table_info(jobs)").fetchall()}
        required = {
            "delivery_status", "drive_remote", "drive_text_remote", "drive_error", "drive_verified_at",
            "cleaned_at", "delivery_retries", "delivery_next_retry", "drive_folder", "source_filename",
            "output_basename", "text_file", "engine", "language",
        }
        missing = sorted(required - cols)
        if missing:
            raise RuntimeError("clone DB is missing delivery columns: " + ",".join(missing))
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS clone_delivery_receipts (
              job_id TEXT PRIMARY KEY,
              audio_remote TEXT NOT NULL,
              text_remote TEXT NOT NULL,
              audio_size INTEGER NOT NULL,
              text_size INTEGER NOT NULL,
              audio_sha256 TEXT NOT NULL,
              text_sha256 TEXT NOT NULL,
              audio_md5 TEXT NOT NULL,
              text_md5 TEXT NOT NULL,
              verified_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS clone_delivery_state (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            """
        )


def state_get(key: str, default: str = "") -> str:
    if not DB_PATH.exists():
        return default
    with db() as con:
        row = con.execute("SELECT value FROM clone_delivery_state WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default


def state_set(key: str, value) -> None:
    with db() as con:
        con.execute(
            "INSERT INTO clone_delivery_state(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def rclone(args: list[str], timeout: int = 900, check: bool = True) -> subprocess.CompletedProcess:
    cmd = [RCLONE, "--config", RCLONE_CONFIG, *args]
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, check=False)
    if check and proc.returncode != 0:
        detail = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
        detail = detail[-1800:] if detail else "no rclone stderr/stdout"
        operation = args[0] if args else "command"
        raise RuntimeError(f"rclone {operation} failed rc={proc.returncode}: {detail}")
    return proc


def remote_md5(remote_path: str) -> str | None:
    p = rclone(["md5sum", f"{REMOTE}:{remote_path}"], timeout=120, check=False)
    if p.returncode != 0:
        detail = ((p.stderr or "") + "\n" + (p.stdout or "")).strip()
        low = detail.lower()
        absent_markers = ("directory not found", "file not found", "object not found", "path not found")
        if any(marker in low for marker in absent_markers):
            return None
        detail = detail[-1800:] if detail else "no rclone stderr/stdout"
        raise RuntimeError(f"rclone md5sum failed rc={p.returncode}: {detail}")
    lines = (p.stdout or "").strip().splitlines()
    if not lines:
        return None
    token = lines[0].split()[0].strip().lower()
    return token if re.fullmatch(r"[0-9a-f]{32}", token) else None


def ensure_remote_no_overwrite(local: Path, remote_path: str, local_md5: str) -> str:
    # Exact user-visible names are required, so never silently overwrite a different Drive file.
    existing = remote_md5(remote_path)
    if existing:
        if existing == local_md5:
            return "already_verified"
        raise RuntimeError(f"Drive filename conflict: {remote_path} already exists with different content")
    rclone(["copyto", "--ignore-existing", str(local), f"{REMOTE}:{remote_path}"], timeout=900, check=True)
    got = remote_md5(remote_path)
    if got != local_md5:
        if got:
            raise RuntimeError(f"Drive filename conflict or MD5 mismatch: {remote_path}")
        raise RuntimeError(f"remote MD5 verification failed: {remote_path}")
    return "uploaded_verified"


def safe_auto_component(value: str, fallback: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", (value or "").strip()).strip("-._")
    return value[:70] or fallback


def exact_one_level_folder(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if value in {".", ".."} or "/" in value or "\\" in value or any(ord(ch) < 32 for ch in value):
        raise RuntimeError("stored Drive output folder is invalid")
    return value


def exact_filename(value: str, suffix: str | None = None) -> str:
    value = (value or "").strip()
    if not value or value in {".", ".."} or "/" in value or "\\" in value or any(ord(ch) < 32 for ch in value):
        raise RuntimeError("stored output filename is invalid")
    if suffix is not None:
        value = value + suffix
    return value


def pause_seconds_for(attempt: int) -> int:
    return min(1800, 30 * (2 ** min(max(attempt - 1, 0), 6)))


def circuit_open() -> tuple[bool, float]:
    try:
        until = float(state_get("drive_pause_until", "0") or 0)
    except Exception:
        until = 0
    return until > now_ts(), until


def trip_circuit(seconds: int, reason: str) -> float:
    until = now_ts() + seconds
    state_set("drive_pause_until", until)
    state_set("drive_pause_reason", reason[:1000])
    return until


def clear_circuit() -> None:
    state_set("drive_pause_until", 0)
    state_set("drive_pause_reason", "")


def _local_text_path(rel: str) -> Path:
    p = (TEXT_DIR / rel).resolve()
    root = TEXT_DIR.resolve()
    try:
        p.relative_to(root)
    except Exception:
        raise RuntimeError("stored TXT path escapes clone text root")
    return p


def process_one() -> bool:
    if not DB_PATH.exists():
        return False
    opened, until = circuit_open()
    if opened:
        return False
    now = now_ts()
    with db() as con:
        row = con.execute(
            """
            SELECT * FROM jobs
            WHERE status='completed'
              AND audio_file IS NOT NULL AND audio_file<>''
              AND text_file IS NOT NULL AND text_file<>''
              AND delivery_status IN ('pending','upload_failed')
              AND delivery_next_retry<=?
            ORDER BY created_at LIMIT 1
            """,
            (now,),
        ).fetchone()
        if not row:
            return False
        job_id = str(row["job_id"])
        changed = con.execute(
            "UPDATE jobs SET delivery_status='uploading',drive_error='',delivery_retries=delivery_retries+1 "
            "WHERE job_id=? AND delivery_status IN ('pending','upload_failed')",
            (job_id,),
        ).rowcount
        if changed != 1:
            return False
        row = con.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()

    try:
        audio = AUDIO_DIR / str(row["audio_file"])
        text = _local_text_path(str(row["text_file"]))
        if not audio.is_file() or audio.stat().st_size <= 0:
            raise RuntimeError("local GPU audio is missing before Drive delivery")
        if not text.is_file() or text.stat().st_size <= 0:
            raise RuntimeError("local original TXT is missing before Drive delivery")

        source_filename = exact_filename(str(row["source_filename"]))
        output_basename = exact_filename(str(row["output_basename"]))
        folder = exact_one_level_folder(str(row["drive_folder"] or ""))
        if folder:
            remote_folder = f"{DRIVE_ROOT}/{folder}"
        else:
            lang = safe_auto_component(str(row["language"] or "auto"), "auto")
            engine = safe_auto_component(str(row["engine"] or "gpu"), "gpu")
            remote_folder = f"{DRIVE_ROOT}/{lang}/gpu-{engine}"

        audio_remote = f"{remote_folder}/{exact_filename(output_basename, audio.suffix.lower())}"
        text_remote = f"{remote_folder}/{source_filename}"
        am = md5(audio)
        tm = md5(text)

        ensure_remote_no_overwrite(text, text_remote, tm)
        ensure_remote_no_overwrite(audio, audio_remote, am)

        verified_at = now_ts()
        with db() as con:
            con.execute(
                """
                INSERT INTO clone_delivery_receipts(
                  job_id,audio_remote,text_remote,audio_size,text_size,
                  audio_sha256,text_sha256,audio_md5,text_md5,verified_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET
                  audio_remote=excluded.audio_remote,text_remote=excluded.text_remote,
                  audio_size=excluded.audio_size,text_size=excluded.text_size,
                  audio_sha256=excluded.audio_sha256,text_sha256=excluded.text_sha256,
                  audio_md5=excluded.audio_md5,text_md5=excluded.text_md5,verified_at=excluded.verified_at
                """,
                (
                    job_id, audio_remote, text_remote, audio.stat().st_size, text.stat().st_size,
                    sha256(audio), sha256(text), am, tm, verified_at,
                ),
            )
            con.execute(
                "UPDATE jobs SET delivery_status='drive_verified',drive_remote=?,drive_text_remote=?,"
                "drive_error='',drive_verified_at=?,delivery_next_retry=0 WHERE job_id=?",
                (f"{REMOTE}:{audio_remote}", f"{REMOTE}:{text_remote}", utc_now(), job_id),
            )
        clear_circuit()
        print(f"gpu_drive_verified={job_id} txt={text_remote} audio={audio_remote}", flush=True)
        return True
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        with db() as con:
            current = con.execute("SELECT delivery_retries FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            attempt = int(current["delivery_retries"] if current else 1)
            delay = pause_seconds_for(attempt)
            con.execute(
                "UPDATE jobs SET delivery_status='upload_failed',drive_error=?,delivery_next_retry=? WHERE job_id=?",
                (err[:2000], now_ts() + delay, job_id),
            )
        low = err.lower()
        if any(m in low for m in RATE_MARKERS):
            until = trip_circuit(max(300, delay), err)
            print(f"gpu_drive_circuit_open={job_id} until={until} error={err[-800:]}", flush=True)
        else:
            print(f"gpu_drive_failed={job_id} retry_in={delay}s error={err[-800:]}", flush=True)
        return True


def cleanup_verified(limit: int = 20) -> int:
    if not DB_PATH.exists():
        return 0
    with db() as con:
        rows = con.execute(
            """
            SELECT j.* FROM jobs j
            JOIN clone_delivery_receipts r ON r.job_id=j.job_id
            WHERE j.delivery_status='drive_verified'
            ORDER BY r.verified_at LIMIT ?
            """,
            (limit,),
        ).fetchall()
    cleaned = 0
    for row in rows:
        job_id = str(row["job_id"])
        try:
            with db() as con:
                receipt = con.execute("SELECT 1 FROM clone_delivery_receipts WHERE job_id=?", (job_id,)).fetchone()
            if not receipt:
                continue
            audio = AUDIO_DIR / str(row["audio_file"] or "")
            text = _local_text_path(str(row["text_file"] or ""))
            audio.unlink(missing_ok=True)
            text.unlink(missing_ok=True)
            try:
                text.parent.rmdir()
            except OSError:
                pass
            with db() as con:
                con.execute(
                    "UPDATE jobs SET delivery_status='cleaned',drive_error='',cleaned_at=? WHERE job_id=?",
                    (utc_now(), job_id),
                )
            cleaned += 1
            print(f"gpu_local_cleanup_ok={job_id}", flush=True)
        except Exception as exc:
            print(f"gpu_cleanup_blocked={job_id} error={type(exc).__name__}: {exc}", flush=True)
    return cleaned


def main() -> None:
    while True:
        try:
            init_db()
            processed = process_one()
            cleaned = cleanup_verified()
            if processed or cleaned:
                print(f"gpu_drive_cycle processed={int(bool(processed))} cleaned={cleaned}", flush=True)
            if not processed:
                time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            return
        except Exception as exc:
            print(f"gpu_drive_spooler_loop_error={type(exc).__name__}: {exc}", flush=True)
            time.sleep(5.0)


if __name__ == "__main__":
    main()
