from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import time
import wave
from pathlib import Path

import requests

from voice_clone_api import (
    AUDIO_DIR,
    SAMPLES_DIR,
    TOKEN_FILE,
    WORKER_URL,
    WORKER_AUTH_MODE,
    WORKER_GENERATION_PATH,
    WORKER_REQUEST_TIMEOUT,
    OMNIVOICE_LANGUAGES,
    ensure_omnivoice_remote_profile,
    db,
    effective_omnivoice_config,
    init_storage,
    omnivoice_auth_headers,
    omnivoice_worker_health,
    utc_now,
    worker_health,
)

POLL_SECONDS = 5.0
ASYNC_POLL_SECONDS = 2.5
ASYNC_HTTP_TIMEOUT = 20.0
ASYNC_MIN_DEADLINE_SECONDS = 1800.0
TRANSIENT_HTTP = {502, 503, 504, 524}
INDIC_LANGS = {"as", "bn", "gu", "hi", "kn", "ml", "mr", "or", "pa", "ta", "te"}


class TransientWorkerError(RuntimeError):
    pass


class PermanentWorkerError(RuntimeError):
    pass


def _token_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    mode = WORKER_AUTH_MODE().strip().lower()
    if mode not in {"bearer", "salad_api_key"}:
        return headers
    token_path = Path(TOKEN_FILE())
    if token_path.is_file():
        try:
            token = token_path.read_text("utf-8").strip()
            if token:
                if mode == "salad_api_key":
                    headers["Salad-Api-Key"] = token
                else:
                    headers["Authorization"] = f"Bearer {token}"
        except Exception:
            pass
    return headers


def _claim_job(indic_online: bool, omnivoice_online: bool):
    clauses: list[str] = []
    params: list[str] = []
    if indic_online:
        placeholders = ",".join("?" for _ in INDIC_LANGS)
        clauses.append(
            "(lower(coalesce(j.engine,''))='indicf5' OR ("
            "lower(coalesce(j.engine,'auto'))='auto' AND "
            f"lower(substr(replace(coalesce(j.language,''),'_','-'),1,2)) IN ({placeholders})))"
        )
        params.extend(sorted(INDIC_LANGS))
    if omnivoice_online:
        clauses.append("lower(coalesce(j.engine,''))='omnivoice'")
    if not clauses:
        return None
    sql = (
        "SELECT j.*,p.name profile_name,p.sample_file,p.reference_language,p.reference_text,p.engine_pref FROM jobs j "
        "JOIN profiles p ON p.profile_id=j.profile_id "
        # VOICE_STUDIO_BATCH_CONTROLS_DISPATCHER_V1
        "WHERE j.status='waiting_worker' AND COALESCE(j.queue_paused,0)=0 AND (" + " OR ".join(clauses) + ") "
        "ORDER BY COALESCE(NULLIF(j.queue_order_at,''),j.created_at) ASC, j.created_at ASC, j.rowid ASC LIMIT 1"
    )
    with db() as con:
        row = con.execute(sql, tuple(params)).fetchone()
        if not row:
            return None
        updated = con.execute(
            "UPDATE jobs SET status='processing',started_at=?,error='' "
            "WHERE job_id=? AND status='waiting_worker'",
            (utc_now(), str(row["job_id"]))
        ).rowcount
        if updated != 1:
            return None
        return dict(row)


def _set_waiting(job_id: str, error: str, increment_retry: bool = True):
    with db() as con:
        if increment_retry:
            con.execute(
                "UPDATE jobs SET status='waiting_worker',started_at=NULL,error=?,retries=retries+1,"
                "delivery_status='waiting_generation' WHERE job_id=?",
                (error[:500], job_id)
            )
        else:
            con.execute(
                "UPDATE jobs SET status='waiting_worker',started_at=NULL,error=?,delivery_status='waiting_generation' WHERE job_id=?",
                (error[:500], job_id)
            )


def _set_failed(job_id: str, error: str):
    with db() as con:
        con.execute(
            "UPDATE jobs SET status='failed',finished_at=?,error=?,delivery_status='not_applicable',drive_error='' WHERE job_id=?",
            (utc_now(), error[:500], job_id)
        )


def _probe(path: Path) -> bool:
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            text=True, stderr=subprocess.DEVNULL, timeout=20
        ).strip()
        return float(out) > 0.2
    except Exception:
        return False


def _decode_response(resp: requests.Response, job_id: str) -> tuple[bytes, str]:
    ctype = (resp.headers.get("content-type") or "").lower()
    if ctype.startswith("audio/") or ctype == "application/octet-stream":
        ext = "wav"
        if "mpeg" in ctype:
            ext = "mp3"
        elif "flac" in ctype:
            ext = "flac"
        elif "ogg" in ctype:
            ext = "ogg"
        return resp.content, ext

    data = resp.json()
    raw_b64 = data.get("audio_base64") or data.get("audio_b64")
    if not raw_b64:
        raise RuntimeError("GPU worker response did not include audio")
    fmt = str(data.get("format") or "wav").lower().strip(".")
    if fmt not in {"wav", "mp3", "flac", "ogg"}:
        fmt = "wav"
    return base64.b64decode(raw_b64), fmt


def _dispatch_engine(job: dict) -> str:
    engine = str(job.get("engine") or "auto").strip().lower()
    if engine != "auto":
        return engine
    lang = str(job.get("language") or "auto").strip().lower().replace("_", "-").split("-", 1)[0]
    return "indicf5" if lang in INDIC_LANGS else "chatterbox"


def _response_detail(resp: requests.Response) -> str:
    text = (resp.text or "").strip().replace("\x00", "")
    if not text:
        return ""
    try:
        data = resp.json()
        if isinstance(data, dict):
            detail = data.get("detail") or data.get("error") or data.get("message")
            if detail:
                return str(detail)[:350]
    except Exception:
        pass
    return text[:350]


def _submit_async(worker: str, payload: dict) -> dict:
    path = (WORKER_GENERATION_PATH() or "/v1/jobs").strip()
    if path.rstrip("/") != "/v1/jobs":
        path = "/v1/jobs"
    try:
        resp = requests.post(
            worker + path,
            headers=_token_headers(),
            json=payload,
            timeout=ASYNC_HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        raise TransientWorkerError(f"GPU submit unavailable: {exc.__class__.__name__}") from exc

    detail = _response_detail(resp)
    if resp.status_code in TRANSIENT_HTTP:
        raise TransientWorkerError(f"GPU submit HTTP {resp.status_code}" + (f": {detail}" if detail else ""))
    if resp.status_code >= 500:
        raise RuntimeError(f"GPU submit HTTP {resp.status_code}" + (f": {detail}" if detail else ""))
    if resp.status_code >= 400:
        raise PermanentWorkerError(f"GPU submit HTTP {resp.status_code}" + (f": {detail}" if detail else ""))
    try:
        data = resp.json()
    except Exception as exc:
        raise RuntimeError("GPU submit returned invalid JSON") from exc
    if str(data.get("job_id") or "") != str(payload["job_id"]):
        raise RuntimeError("GPU submit returned a mismatched job id")
    return data


def _poll_async(worker: str, job_id: str) -> requests.Response:
    deadline = time.monotonic() + max(float(WORKER_REQUEST_TIMEOUT()), ASYNC_MIN_DEADLINE_SECONDS)
    transient_count = 0
    while True:
        if time.monotonic() >= deadline:
            raise TransientWorkerError("GPU job is still running; it will resume polling")

        try:
            resp = requests.get(
                worker + f"/v1/jobs/{job_id}",
                headers=_token_headers(),
                timeout=ASYNC_HTTP_TIMEOUT,
            )
        except requests.RequestException:
            transient_count += 1
            if transient_count >= 6:
                raise TransientWorkerError("GPU status temporarily unavailable")
            time.sleep(ASYNC_POLL_SECONDS)
            continue

        detail = _response_detail(resp)
        if resp.status_code in TRANSIENT_HTTP:
            transient_count += 1
            if transient_count >= 6:
                raise TransientWorkerError(f"GPU status HTTP {resp.status_code}")
            time.sleep(ASYNC_POLL_SECONDS)
            continue
        transient_count = 0

        if resp.status_code == 404:
            raise TransientWorkerError("Remote GPU job state was lost; it will be resubmitted")
        if resp.status_code >= 500:
            raise RuntimeError(f"GPU status HTTP {resp.status_code}" + (f": {detail}" if detail else ""))
        if resp.status_code >= 400:
            raise PermanentWorkerError(f"GPU status HTTP {resp.status_code}" + (f": {detail}" if detail else ""))

        try:
            data = resp.json()
        except Exception as exc:
            raise RuntimeError("GPU status returned invalid JSON") from exc

        status = str(data.get("status") or "").strip().lower()
        if status == "completed":
            try:
                result = requests.get(
                    worker + f"/v1/jobs/{job_id}/result",
                    headers=_token_headers(),
                    timeout=60.0,
                )
            except requests.RequestException as exc:
                raise TransientWorkerError("GPU result download temporarily unavailable") from exc
            rdetail = _response_detail(result)
            if result.status_code in TRANSIENT_HTTP:
                raise TransientWorkerError(f"GPU result HTTP {result.status_code}")
            if result.status_code >= 500:
                raise RuntimeError(f"GPU result HTTP {result.status_code}" + (f": {rdetail}" if rdetail else ""))
            if result.status_code >= 400:
                raise PermanentWorkerError(f"GPU result HTTP {result.status_code}" + (f": {rdetail}" if rdetail else ""))
            return result
        if status == "failed":
            err = str(data.get("error") or "IndicF5 generation failed")
            raise PermanentWorkerError(f"IndicF5: {err[:430]}")
        if status not in {"queued", "loading_engine", "generating"}:
            raise RuntimeError(f"GPU returned unknown job status: {status or 'empty'}")
        time.sleep(ASYNC_POLL_SECONDS)



def _omnivoice_language(value: str) -> str:
    lang = str(value or "auto").strip().lower().replace("_", "-").split("-", 1)[0]
    return "arb" if lang == "ar" else lang


OMNIVOICE_MAX_TEXT = 12000
OMNIVOICE_CHUNK_TARGET = 11500
OMNIVOICE_SENTENCE_BREAKS = {".", "!", "?", "।", "؟", "۔"}
OMNIVOICE_COMMA_BREAKS = {",", "،"}


def _omnivoice_text_chunks(text: str, limit: int = OMNIVOICE_CHUNK_TARGET) -> list[str]:
    """Split long text at natural punctuation, then at a complete-word boundary.

    Sentence punctuation is preferred, comma is the second choice, and text with
    no punctuation is split at the last whitespace inside the safe chunk window.
    """
    raw = str(text or "").strip()
    if not raw:
        raise PermanentWorkerError("OmniVoice text is empty")
    if limit <= 0 or limit > OMNIVOICE_MAX_TEXT:
        limit = OMNIVOICE_CHUNK_TARGET

    chunks: list[str] = []
    remaining = raw

    while len(remaining) > OMNIVOICE_MAX_TEXT:
        window = remaining[:limit]

        # Prefer the last full-stop / sentence boundary inside the safe window.
        cut = -1
        for i, ch in enumerate(window):
            if ch in OMNIVOICE_SENTENCE_BREAKS:
                cut = i + 1

        # If no sentence end is available, use the last comma.
        if cut <= 0:
            for i, ch in enumerate(window):
                if ch in OMNIVOICE_COMMA_BREAKS:
                    cut = i + 1

        # Normal fallback for punctuation-free text: cut at the last
        # complete-word boundary inside the safe window instead of failing.
        if cut <= 0:
            for i in range(len(window) - 1, 0, -1):
                if window[i].isspace():
                    cut = i
                    break

        # Only an abnormally huge single token has no complete-word boundary.
        if cut <= 0:
            raise PermanentWorkerError(
                "OmniVoice long text contains a single word over the safe chunk limit"
            )

        piece = remaining[:cut].strip()
        if not piece:
            raise PermanentWorkerError("OmniVoice could not create a safe punctuation chunk")
        if len(piece) > OMNIVOICE_MAX_TEXT:
            raise PermanentWorkerError("OmniVoice safe chunk still exceeds 12000 characters")
        chunks.append(piece)
        remaining = remaining[cut:].lstrip()

    if remaining:
        if len(remaining) > OMNIVOICE_MAX_TEXT:
            raise PermanentWorkerError("OmniVoice final chunk exceeds 12000 characters")
        chunks.append(remaining)

    return chunks


def _merge_omnivoice_wavs(parts: list[bytes]) -> bytes:
    if not parts:
        raise RuntimeError("OmniVoice returned no audio chunks")
    if len(parts) == 1:
        return parts[0]

    expected = None
    frames: list[bytes] = []
    for idx, blob in enumerate(parts, start=1):
        try:
            with wave.open(io.BytesIO(blob), "rb") as w:
                fmt = (
                    w.getnchannels(),
                    w.getsampwidth(),
                    w.getframerate(),
                    w.getcomptype(),
                    w.getcompname(),
                )
                if expected is None:
                    expected = fmt
                elif fmt != expected:
                    raise RuntimeError(f"OmniVoice WAV format mismatch at chunk {idx}")
                frames.append(w.readframes(w.getnframes()))
        except wave.Error as exc:
            raise RuntimeError(f"OmniVoice chunk {idx} is not a valid WAV") from exc

    nchannels, sampwidth, framerate, comptype, compname = expected
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(nchannels)
        w.setsampwidth(sampwidth)
        w.setframerate(framerate)
        w.setcomptype(comptype, compname)
        for frame_blob in frames:
            w.writeframes(frame_blob)
    return out.getvalue()


def _omnivoice_audio_one(
    worker: str,
    payload: dict,
    timeout: int,
    headers: dict[str, str],
    chunk_no: int,
    chunk_total: int,
) -> bytes:
    try:
        submit = requests.post(worker + "/v1/jobs", headers=headers, json=payload, timeout=ASYNC_HTTP_TIMEOUT)
    except requests.RequestException as exc:
        raise TransientWorkerError(
            f"OmniVoice chunk {chunk_no}/{chunk_total} submit unavailable: {exc.__class__.__name__}"
        ) from exc
    detail = _response_detail(submit)
    if submit.status_code in TRANSIENT_HTTP:
        raise TransientWorkerError(
            f"OmniVoice chunk {chunk_no}/{chunk_total} submit HTTP {submit.status_code}"
            + (f": {detail}" if detail else "")
        )
    if submit.status_code >= 500:
        raise RuntimeError(
            f"OmniVoice chunk {chunk_no}/{chunk_total} submit HTTP {submit.status_code}"
            + (f": {detail}" if detail else "")
        )
    if submit.status_code >= 400:
        raise PermanentWorkerError(
            f"OmniVoice chunk {chunk_no}/{chunk_total} submit HTTP {submit.status_code}"
            + (f": {detail}" if detail else "")
        )
    try:
        remote_job_id = str(submit.json().get("job_id") or "")
    except Exception as exc:
        raise RuntimeError(f"OmniVoice chunk {chunk_no}/{chunk_total} submit returned invalid JSON") from exc
    if not remote_job_id:
        raise RuntimeError(f"OmniVoice chunk {chunk_no}/{chunk_total} submit did not return job_id")

    deadline = time.monotonic() + max(float(timeout), ASYNC_MIN_DEADLINE_SECONDS)
    transient_count = 0
    while True:
        if time.monotonic() >= deadline:
            raise TransientWorkerError(f"OmniVoice chunk {chunk_no}/{chunk_total} is still running; it will be retried")
        try:
            status_resp = requests.get(
                worker + f"/v1/jobs/{remote_job_id}",
                headers=headers,
                timeout=ASYNC_HTTP_TIMEOUT,
            )
        except requests.RequestException:
            transient_count += 1
            if transient_count >= 6:
                raise TransientWorkerError(f"OmniVoice chunk {chunk_no}/{chunk_total} status temporarily unavailable")
            time.sleep(ASYNC_POLL_SECONDS)
            continue

        detail = _response_detail(status_resp)
        if status_resp.status_code in TRANSIENT_HTTP:
            transient_count += 1
            if transient_count >= 6:
                raise TransientWorkerError(
                    f"OmniVoice chunk {chunk_no}/{chunk_total} status HTTP {status_resp.status_code}"
                )
            time.sleep(ASYNC_POLL_SECONDS)
            continue
        transient_count = 0

        if status_resp.status_code == 404:
            raise TransientWorkerError(f"OmniVoice chunk {chunk_no}/{chunk_total} remote job state was lost")
        if status_resp.status_code >= 500:
            raise RuntimeError(
                f"OmniVoice chunk {chunk_no}/{chunk_total} status HTTP {status_resp.status_code}"
                + (f": {detail}" if detail else "")
            )
        if status_resp.status_code >= 400:
            raise PermanentWorkerError(
                f"OmniVoice chunk {chunk_no}/{chunk_total} status HTTP {status_resp.status_code}"
                + (f": {detail}" if detail else "")
            )
        try:
            data = status_resp.json()
        except Exception as exc:
            raise RuntimeError(f"OmniVoice chunk {chunk_no}/{chunk_total} status returned invalid JSON") from exc

        state = str(data.get("state") or "").strip().lower()
        if state == "completed":
            try:
                audio_resp = requests.get(
                    worker + f"/v1/jobs/{remote_job_id}/audio",
                    headers=headers,
                    timeout=60.0,
                )
            except requests.RequestException as exc:
                raise TransientWorkerError(
                    f"OmniVoice chunk {chunk_no}/{chunk_total} audio download temporarily unavailable"
                ) from exc
            adetail = _response_detail(audio_resp)
            if audio_resp.status_code in TRANSIENT_HTTP:
                raise TransientWorkerError(
                    f"OmniVoice chunk {chunk_no}/{chunk_total} audio HTTP {audio_resp.status_code}"
                )
            if audio_resp.status_code >= 500:
                raise RuntimeError(
                    f"OmniVoice chunk {chunk_no}/{chunk_total} audio HTTP {audio_resp.status_code}"
                    + (f": {adetail}" if adetail else "")
                )
            if audio_resp.status_code >= 400:
                raise PermanentWorkerError(
                    f"OmniVoice chunk {chunk_no}/{chunk_total} audio HTTP {audio_resp.status_code}"
                    + (f": {adetail}" if adetail else "")
                )
            return audio_resp.content

        if state == "failed":
            err = str(data.get("error") or "OmniVoice generation failed")
            raise PermanentWorkerError(f"OmniVoice chunk {chunk_no}/{chunk_total}: {err[:400]}")
        if state not in {"queued", "running"}:
            raise RuntimeError(
                f"OmniVoice chunk {chunk_no}/{chunk_total} returned unknown job state: {state or 'empty'}"
            )
        time.sleep(ASYNC_POLL_SECONDS)


def _omnivoice_audio(worker: str, job: dict, sample: Path) -> tuple[bytes, str]:
    cfg = effective_omnivoice_config()
    timeout = int(cfg.get("request_timeout") or 300)
    headers = {"Content-Type": "application/json", **omnivoice_auth_headers()}
    settings = json.loads(str(job.get("settings_json") or "{}"))
    chunks = _omnivoice_text_chunks(str(job["text"]))
    # VPS3 owns the reference. Create/reuse one stable remote prompt and then
    # send only voice_id for every long-form chunk.
    ensure_omnivoice_remote_profile(job, sample, worker)
    common = {
        "language": _omnivoice_language(str(job["language"] or "auto")),
        "mode": "clone",
        "voice_id": str(job["profile_id"]),
        "num_step": max(8, min(64, int(settings.get("num_step", 32)))),
        "speed": float(job.get("speed") or 1.0),
    }

    audio_parts: list[bytes] = []
    total = len(chunks)
    for idx, chunk in enumerate(chunks, start=1):
        payload = {"text": chunk, **common}
        audio_parts.append(_omnivoice_audio_one(worker, payload, timeout, headers, idx, total))

    return _merge_omnivoice_wavs(audio_parts), "wav"

def process_one():
    indic_worker = WORKER_URL()
    indic_online = False
    if indic_worker:
        indic_online, _ = worker_health(timeout_seconds=8.0)

    omni_cfg = effective_omnivoice_config()
    omni_worker = str(omni_cfg.get("endpoint_url") or "")
    omni_online = False
    if omni_worker:
        omni_online, _ = omnivoice_worker_health(timeout_seconds=8.0)

    job = _claim_job(indic_online=indic_online, omnivoice_online=omni_online)
    if not job:
        return False

    job_id = str(job["job_id"])
    sample = SAMPLES_DIR / str(job["sample_file"])
    if not sample.is_file():
        _set_failed(job_id, "Reference sample is missing")
        return True

    try:
        engine = _dispatch_engine(job)
        if engine == "omnivoice":
            audio, ext = _omnivoice_audio(omni_worker, job, sample)
        elif engine == "indicf5":
            payload = {
                "job_id": job_id,
                "profile_id": str(job["profile_id"]),
                "profile_name": str(job["profile_name"]),
                "text": str(job["text"]),
                "language": str(job["language"] or "auto"),
                "engine": "indicf5",
                "preset": str(job.get("preset") or "narration"),
                "speed": float(job.get("speed") or 1.0),
                "settings": json.loads(str(job.get("settings_json") or "{}")),
                "reference_language": str(job.get("reference_language") or "auto"),
                "reference_text": str(job.get("reference_text") or ""),
                "sample_wav_base64": base64.b64encode(sample.read_bytes()).decode("ascii"),
                "output_format": "wav",
            }
            _submit_async(indic_worker, payload)
            resp = _poll_async(indic_worker, job_id)
            audio, ext = _decode_response(resp, job_id)
        else:
            raise PermanentWorkerError(f"Unsupported GPU engine for this worker: {engine}")

        if len(audio) < 1000 or len(audio) > 200 * 1024 * 1024:
            raise RuntimeError("GPU worker returned invalid audio size")

        final = AUDIO_DIR / f"clone-{job_id}.{ext}"
        partial = final.with_name(final.name + ".partial")
        partial.write_bytes(audio)
        if not _probe(partial):
            partial.unlink(missing_ok=True)
            raise RuntimeError("GPU worker returned unreadable audio")
        os.replace(partial, final)

        with db() as con:
            current = con.execute("SELECT text_file FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            delivery = "pending" if current and str(current["text_file"] or "") else "not_applicable"
            con.execute(
                "UPDATE jobs SET status='completed',finished_at=?,audio_file=?,error='',delivery_status=?,"
                "drive_remote='',drive_text_remote='',drive_error='',drive_verified_at=NULL,cleaned_at=NULL,"
                "delivery_retries=0,delivery_next_retry=0 WHERE job_id=?",
                (utc_now(), final.name, delivery, job_id)
            )
        return True
    except TransientWorkerError as exc:
        _set_waiting(job_id, str(exc), increment_retry=False)
        return True
    except PermanentWorkerError as exc:
        _set_failed(job_id, str(exc))
        return True
    except Exception as exc:
        # Generic processing failures are terminal: do not regenerate the same job.
        _set_failed(job_id, str(exc))
        return True


def main():
    init_storage()
    while True:
        try:
            worked = process_one()
            time.sleep(0.5 if worked else POLL_SECONDS)
        except KeyboardInterrupt:
            break
        except Exception:
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()


# VPS3_OMNIVOICE_REUSABLE_PROFILES_DESIGN_V1
