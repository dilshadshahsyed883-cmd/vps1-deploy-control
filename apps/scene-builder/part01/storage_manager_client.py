from __future__ import annotations

import os
from typing import Any

import requests
from fastapi import HTTPException


def storage_manager_request(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    base_url = os.getenv("STORAGE_MANAGER_URL", "http://127.0.0.1:8799").rstrip("/")
    token = os.getenv("STORAGE_MANAGER_TOKEN", "").strip()
    if not token:
        raise HTTPException(503, "Storage Manager is not configured")
    try:
        response = requests.request(
            method,
            f"{base_url}/{path.lstrip('/')}",
            headers={"X-Storage-Token": token, "Accept": "application/json"},
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise HTTPException(503, f"Storage Manager unavailable: {exc}") from exc
    try:
        body = response.json()
    except ValueError as exc:
        raise HTTPException(502, "Storage Manager returned an invalid response") from exc
    if response.status_code >= 400:
        raise HTTPException(response.status_code, str(body.get("error") or "Storage operation failed"))
    return body
