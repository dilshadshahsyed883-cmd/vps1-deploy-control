from __future__ import annotations

import os
import hashlib
import json
import traceback
from pathlib import Path
from typing import Any


def render_entry(settings_payload: dict[str, Any], messages, cancel_event) -> None:
    if os.name == "posix":
        try:
            os.setsid()
        except OSError:
            pass
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtGui import QFontDatabase, QGuiApplication

        qt_app = QGuiApplication.instance() or QGuiApplication(["auto-caption-render"])
        from smart_scroll.project_model import ProjectSettings
        from smart_scroll.renderer import render_video

        code_root = Path(os.environ.get("AUTO_CAPTION_CODE_ROOT") or Path(__file__).resolve().parents[1])
        fonts_dir = code_root / "fonts"
        manifest_path = fonts_dir / "SHA256SUMS.json"
        if not manifest_path.is_file():
            raise RuntimeError("Bundled font checksum manifest is missing.")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for filename, expected in manifest.items():
            bundled = fonts_dir / filename
            if not bundled.is_file():
                raise RuntimeError(f"Required bundled font is missing: {filename}")
            actual = hashlib.sha256(bundled.read_bytes()).hexdigest()
            if actual != expected:
                raise RuntimeError(f"Bundled font checksum mismatch: {filename}")
            if QFontDatabase.addApplicationFont(str(bundled)) < 0:
                raise RuntimeError(f"Bundled font could not be loaded: {filename}")

        metadata = settings_payload.get("metadata", {})
        metadata = metadata if isinstance(metadata, dict) else {}
        font_path = str(metadata.get("custom_font_vps_path") or "").strip()
        if font_path:
            font_id = QFontDatabase.addApplicationFont(font_path)
            if font_id < 0:
                raise RuntimeError(f"Uploaded custom font could not be loaded: {font_path}")
            families = QFontDatabase.applicationFontFamilies(font_id)
            requested = str(
                metadata.get("custom_font_family")
                or settings_payload.get("font_family")
                or ""
            ).strip()
            if requested and requested in families:
                settings_payload["font_family"] = requested
            elif families:
                settings_payload["font_family"] = families[0]
            metadata["custom_font_loaded_family"] = settings_payload.get("font_family", "")
            settings_payload["metadata"] = metadata
        else:
            from .font_fallback import choose_font_family

            families = list(QFontDatabase.families())
            lookup = {str(name).casefold(): str(name) for name in families}
            is_web_flow = bool(metadata.get("web_flow_v1"))
            font_mode = str(metadata.get("web_font_mode") or "auto").strip().lower() if is_web_flow else "auto"
            if font_mode == "manual":
                requested = str(metadata.get("web_manual_font_family") or settings_payload.get("font_family") or "").strip()
                actual = lookup.get(requested.casefold()) if requested else ""
                if not actual:
                    raise RuntimeError(f"Selected manual font is not available to the VPS renderer: {requested or '[empty]'}")
                settings_payload["font_family"] = actual
                metadata["manual_font_family"] = actual
                metadata.pop("automatic_font_family", None)
                metadata.pop("automatic_font_script", None)
                settings_payload["metadata"] = metadata
            else:
                selected, script = choose_font_family(
                    str(settings_payload.get("text") or ""),
                    str(settings_payload.get("caption_language") or "auto"),
                    families,
                )
                if script and not selected:
                    raise RuntimeError(
                        f"Required Unicode font is missing for {script}. "
                        "Install a compatible VPS font before rendering."
                    )
                if selected:
                    settings_payload["font_family"] = selected
                    metadata["automatic_font_family"] = selected
                    metadata["automatic_font_script"] = script
                    settings_payload["metadata"] = metadata

        settings = ProjectSettings(**settings_payload)

        def progress(percent: int, message: str) -> None:
            messages.put(
                {
                    "type": "progress",
                    "progress": int(percent),
                    "message": str(message),
                }
            )

        render_video(
            settings,
            progress_callback=progress,
            cancel_callback=cancel_event.is_set,
        )
        _ = qt_app
        messages.put({"type": "result", "ok": True})
    except BaseException as exc:
        messages.put(
            {
                "type": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }
        )
        raise
