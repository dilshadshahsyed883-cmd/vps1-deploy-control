from __future__ import annotations
import hashlib
import json

_SUSPICIOUS_KEYS = (
    "audio_path", "output_path", "font_family", "font_size",
    "pexels_enabled", "queue_status", "settings_json", "metadata",
    "background_image_path", "foreground_image_path",
)

def normalize_clean_script(text: str) -> str:
    return (
        (text or "")
        .replace("\ufeff", "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
    )

def validate_clean_script(text: str) -> None:
    value = normalize_clean_script(text)
    if not value:
        raise ValueError("Clean script is empty.")

    lowered = value.lower()
    suspicious = [
        key for key in _SUSPICIOUS_KEYS
        if f"{key}=" in lowered or f'"{key}"' in lowered
    ]
    if suspicious:
        raise ValueError(
            "Invalid clean script: project settings were detected inside text "
            f"({', '.join(suspicious[:6])})."
        )

    if value[:1] in {"{", "["}:
        try:
            parsed = json.loads(value)
        except Exception:
            parsed = None
        if isinstance(parsed, (dict, list)):
            raise ValueError(
                "Invalid clean script: serialized JSON payload was supplied as text."
            )

def script_sha256(text: str) -> str:
    return hashlib.sha256(normalize_clean_script(text).encode("utf-8")).hexdigest()
