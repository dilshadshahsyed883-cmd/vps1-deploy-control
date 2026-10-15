"""Pexels Manual Fill V2: keyword-complete, image-unique, resumable batch downloader.

Keeps legacy render download behavior unchanged. All network calls require explicit Fill.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from typing import Any
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import uuid

import requests

BATCH_LOCK = threading.RLock()
VALID_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_KEYWORDS = 500
MAX_PER_KEYWORD = 20
MAX_RESULTS_PER_PAGE = 40
MAX_PAGES_PER_KEYWORD = 8
MAX_IMAGE_BYTES = 30 * 1024 * 1024


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_keywords(text: str) -> list[str]:
    parts = re.split(r"[,;|\n\r]+", text or "")
    seen: set[str] = set()
    out: list[str] = []
    for raw in parts:
        word = " ".join(raw.strip().split())
        if not word:
            continue
        if len(word) > 180:
            raise ValueError("One or more keywords exceed the 180-character limit")
        if word.casefold() in seen:
            continue
        seen.add(word.casefold())
        out.append(word)
        if len(out) > MAX_KEYWORDS:
            raise ValueError(f"Up to {MAX_KEYWORDS} unique keywords allowed per batch; split larger lists")
    if not out:
        raise ValueError("At least one keyword is required")
    return out


def state_path(config_path: Path) -> Path:
    return Path(config_path).with_name("pexels_smart_fill_state.json")


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = now()
    fd, tmp = tempfile.mkstemp(prefix=".pexels-smart-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def get_batch_status(config_path: Path) -> dict[str, Any]:
    data = _load(state_path(config_path))
    if not data:
        return {"status": "idle", "message": "Ready", "processed": 0, "total_keywords": 0}
    return data


def new_batch(
    *, config_path: Path, keywords: str, images_per_keyword: int,
    mode: str, actor: str, retry_incomplete: bool = False
) -> dict[str, Any]:
    if not 1 <= images_per_keyword <= MAX_PER_KEYWORD:
        raise ValueError("Images per keyword must be between 1 and 20")
    if mode not in ("new_only", "new_or_reuse"):
        raise ValueError("Invalid download mode")
    path = state_path(config_path)
    with BATCH_LOCK:
        old = get_batch_status(config_path)
        if old.get("status") in ("queued", "running"):
            raise RuntimeError("Another Pexels batch is already queued or running")
        pairs: list[tuple[str,int]]
        if retry_incomplete:
            if not old.get("rows"):
                raise ValueError("No previous batch exists to retry")
            pairs = []
            for row in old.get("rows", []):
                pending = max(0, int(row.get("requested", 0)) - int(row.get("downloaded", 0)) - int(row.get("reused", 0)))
                if pending:
                    pairs.append((str(row.get("keyword", "")), pending))
            if not pairs:
                raise ValueError("Previous batch has no incomplete keywords")
            mode = str(old.get("mode") or mode)
        else:
            pairs = [(keyword, images_per_keyword) for keyword in parse_keywords(keywords)]
        state = {
            "job_id": uuid.uuid4().hex,
            "status": "queued", "mode": mode,
            "images_per_keyword": images_per_keyword,
            "total_keywords": len(pairs), "requested_total": sum(n for _,n in pairs),
            "processed": 0, "downloaded": 0, "reused": 0, "failed": 0,
            "current_keyword": "", "progress": 0,
            "message": "Pexels keyword batch queued",
            "actor": actor, "created_at": now(),
            "retry_of": str(old.get("job_id")) if retry_incomplete else None,
            "rows": [{"keyword":k, "requested":n, "downloaded":0, "reused":0,"missing":n, "status":"pending", "reason":""} for k,n in pairs],
        }
        _save(path, state)
        return state


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:78] or "keyword"


def _saved_media(library: Path) -> tuple[set[str],set[str]]:
    # Older ACS image filenames are <keyword_slug>_<pexels-photo-id>.jpeg.
    ids: set[str] = set()
    digests: set[str] = set()
    for p in library.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in VALID_EXTS:
            continue
        match = re.search(r"_(\d{3,})$", p.stem)
        if match:
            ids.add(match.group(1))
        try:
            if p.stat().st_size >= 10_000:
                digests.add(_digest_file(p))
        except OSError:
            pass
    return ids,digests


def _digest_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(1024*1024), b""):
            h.update(part)
    return h.hexdigest()


def _safe_image_url(photo: dict) -> str:
    src = photo.get("src") if isinstance(photo,dict) else None
    if not isinstance(src,dict):
        return ""
    for size in ("large2x", "large", "landscape", "original", "medium"):
        url = str(src.get(size) or "")
        parsed = urlparse(url)
        if parsed.scheme == "https" and parsed.hostname == "images.pexels.com":
            return url
    return ""


def _media_sig(data: bytes) -> bool:
    return (data.startswith(b"\xff\xd8\xff") or data.startswith(b"\x89PNG\r\n\x1a\n") or (data.startswith(b"RIFF") and data[8:12] == b"WEBP"))


def _download_image(session: requests.Session, url: str, target: Path, digests: set[str]) -> tuple[bool,str]:
    if target.is_file():
        return False,"already_saved"
    fd,tmp = tempfile.mkstemp(prefix=".pexels-new-", suffix=".part",dir=str(target.parent))
    digest = hashlib.sha256()
    total=0
    head=bytearray()
    try:
        with os.fdopen(fd,"wb") as output:
            with session.get(url,timeout=(10,45),stream=True) as resp:
                resp.raise_for_status()
                kind = resp.headers.get("Content-Type","").split(";",1)[0].strip().lower()
                if kind and not kind.startswith("image/"):
                    return False,"non_image_response"
                for chunk in resp.iter_content(64*1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_IMAGE_BYTES:
                        return False,"image_too_large"
                    if len(head) < 16:
                        head.extend(chunk[:16-len(head)])
                    digest.update(chunk)
                    output.write(chunk)
        if total < 10_000 or not _media_sig(bytes(head)):
            return False,"invalid_or_too_small_image"
        sha = digest.hexdigest()
        if sha in digests:
            return False,"duplicate_content"
        os.replace(tmp,target)
        digests.add(sha)
        return True,"downloaded"
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _reusable_count(keyword: str, library: Path, limit: int, preexisting: set[Path]) -> int:
    folder = library / _slug(keyword)
    if not folder.is_dir():
        return 0
    return min(limit,sum(1 for p in preexisting if p.parent == folder and p.is_file() and p.suffix.lower() in VALID_EXTS and p.stat().st_size>=10_000))


def run_batch(*, config_path: Path, library: Path, keys: list[str], job_id: str, session_factory=requests.Session) -> None:
    path=state_path(config_path)
    library=Path(library)
    with BATCH_LOCK:
        state=_load(path)
        if state.get("job_id")!=job_id or state.get("status")!="queued":
            return
        state.update(status="running",started_at=now(),message="Scanning existing Pexels library")
        _save(path,state)
        session=session_factory()
        try:
            library.mkdir(parents=True, exist_ok=True)
            ids,digests=_saved_media(library)
            rows=state.get("rows",[])
            key_index=0
            for n,row in enumerate(rows):
                query=str(row["keyword"])
                needed=int(row["requested"])
                downloaded=0
                reason=""
                no_more=False
                state.update(current_keyword=query, message=f"Searching {n+1}/{len(rows)}: {query}")
                _save(path,state)
                query_dir=library/_slug(query)
                query_dir.mkdir(parents=True,exist_ok=True)
                preexisting_paths=set(p for p in query_dir.iterdir() if p.is_file())
                for page in range(1,MAX_PAGES_PER_KEYWORD+1):
                    if downloaded>=needed:
                        break
                    key_errors=0
                    result=None
                    for attempt in range(max(1,len(keys))*2):
                        key=keys[key_index%len(keys)] if keys else ""
                        key_index+=1
                        if not key:
                            reason="No server Pexels key configured"
                            break
                        try:
                            result=session.get("https://api.pexels.com/v1/search",params={"query":query,"per_page":MAX_RESULTS_PER_PAGE,"page":page,"orientation":"landscape"},headers={"Authorization":key},timeout=(10,30))
                            if result.status_code==429:
                                key_errors+=1
                                reason="Pexels API rate limited"
                                continue
                            if 500<=result.status_code<600:
                                reason=f"Pexels server error {result.status_code}"
                                if attempt==0:
                                    time.sleep(1)
                                    continue
                                break
                            result.raise_for_status()
                            break
                        except requests.RequestException as exc:
                            reason=f"Pexels search request failed: {type(exc).__name__}"
                            result=None
                            if attempt==0:
                                time.sleep(1)
                                continue
                            break
                    if result is None or result.status_code!=200:
                        if key_errors>=max(1,len(keys))*2:
                            state["status"]="rate_limited"
                            break
                        no_more=True
                        break
                    try:
                        photo_rows=result.json().get("photos") or []
                    except (TypeError, ValueError,AttributeError):
                        photo_rows=[]
                        reason="Invalid Pexels search response"
                    if not photo_rows:
                        if not reason:reason="No more Pexels search results"
                        no_more=True
                        break
                    for photo in photo_rows:
                        if downloaded>=needed:
                            break
                        if not isinstance(photo,dict):
                            continue
                        pid=str(photo.get("id") or "")
                        if not pid or pid in ids:
                            continue
                        url=_safe_image_url(photo)
                        if not url:
                            continue
                        dest=query_dir/f"{_slug(query)}_{pid}.jpeg"
                        try:
                            ok,msg=_download_image(session,url,dest,digests)
                        except (requests.RequestException,OSError) as exc:
                            ok=False
                            msg=f"Download failed: {type(exc).__name__}"
                        if ok:
                            ids.add(pid)
                            downloaded+=1
                            state["downloaded"]+=1
                            row["downloaded"]=downloaded
                            row["missing"]=needed-downloaded
                            state["message"]=f"Saved {downloaded}/{needed} for {query}"
                            _save(path,state)
                        elif not reason:
                            reason=msg
                    if downloaded>=needed or len(photo_rows)<MAX_RESULTS_PER_PAGE:
                        no_more=True
                        break
                row["status"]="complete" if downloaded>=needed else "partial"
                if state.get("status")=="rate_limited" and downloaded<needed:
                    row["status"]="rate_limited"
                remaining=needed-downloaded
                if remaining and state.get("mode")=="new_or_reuse":
                    reused=_reusable_count(query,library,remaining,preexisting_paths)
                    row["reused"]=reused
                    state["reused"]+=reused
                    remaining-=reused
                row["missing"]=remaining
                row["reason"] = "" if not remaining else (reason or "No additional unique results within 8 search pages")
                row["status"] = ("complete" if not remaining else row["status"])
                if remaining:
                    state["failed"]+=remaining
                state["processed"]+=1
                state["progress"]=round(100*state["processed"]/max(1,len(rows)))
                _save(path,state)
                if state["status"]=="rate_limited":
                    for pending in rows[n+1:]:
                        pending["status"]="pending_rate_limit"
                        pending["reason"]="Waiting for Pexels quota; use Retry Incomplete later"
                    break
            if state["status"]!="rate_limited":
                state["status"]="completed" if state["failed"]==0 else "completed_with_errors"
            state["finished_at"]=now()
            state["message"]=f"{state['processed']}/{state['total_keywords']} keywords processed; {state['downloaded']} new; {state['reused']} reused; {state['failed']} missing"
            _save(path,state)
        except BaseException as exc:
            state.update(status="failed",finished_at=now(),message=f"Batch stopped: {type(exc).__name__}")
            _save(path,state)
        finally:
            session.close()
