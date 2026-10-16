"""Qt/FFmpeg based renderer for Auto Caption Video Studio."""
from __future__ import annotations

from dataclasses import dataclass
import html
import math
import hashlib
import os
import random
import re
import threading
import time
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QImage,
    QLinearGradient,
    QPainter,
    QPen,
    QTextDocument,
    QTextOption,
)

from .ffmpeg_utils import FFmpegError, get_audio_duration, start_ffmpeg_rawvideo
from .local_ai_tools import (
    app_root,
    build_story_queries,
    explain_story_queries,
    extract_scene_plan,
    download_pexels_backgrounds,
    estimate_pexels_needed,
    list_background_images,
    list_background_videos,
    prepare_pexels_video_background_frames,
    pexels_library_dir,
    resolve_pexels_library_dir,
)
from .lang_utils import looks_rtl
from .caption_sync import CaptionSegment, build_caption_segments, caption_generation_signature, is_rtl_text, language_code, write_srt
from .project_model import ProjectSettings
from .output_validator import validate_video_file
from .motion_engine import build_motion_plan, deterministic_visual_order, interpolate_plan, stable_seed, transition_name
from .image_timing_engine import build_visual_durations
from .scene_sync_service import apply_scene_media_tags, build_scene_sync_plan, summarize_scene_sync
from .text_cleaner import split_paragraphs, to_single_paragraph

ProgressCallback = Callable[[int, str], None]
CancelCallback = Callable[[], bool]


class RenderCancelled(RuntimeError):
    pass


_ACTIVE_RENDER_PROCS: dict[int, object] = {}
_ACTIVE_RENDER_LOCK = threading.Lock()


def _register_running_render_process(proc: object | None) -> int | None:
    thread_id = threading.get_ident()
    if proc is None:
        return None
    with _ACTIVE_RENDER_LOCK:
        _ACTIVE_RENDER_PROCS[thread_id] = proc
    return thread_id


def _unregister_running_render_process(thread_id: int | None = None) -> None:
    if thread_id is None:
        thread_id = threading.get_ident()
    with _ACTIVE_RENDER_LOCK:
        _ACTIVE_RENDER_PROCS.pop(int(thread_id), None)


def cancel_running_render_process(thread_id: int | None) -> None:
    if thread_id is None:
        return
    proc = None
    with _ACTIVE_RENDER_LOCK:
        proc = _ACTIVE_RENDER_PROCS.get(int(thread_id))
    if proc is None:
        return
    try:
        stdin = getattr(proc, "stdin", None)
        if stdin is not None and not stdin.closed:
            stdin.close()
    except Exception:
        pass
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


ALIGN_MAP = {
    "left": Qt.AlignmentFlag.AlignLeft,
    "center": Qt.AlignmentFlag.AlignHCenter,
    "right": Qt.AlignmentFlag.AlignRight,
    "justify": Qt.AlignmentFlag.AlignJustify,
}


@dataclass
class RenderContext:
    settings: ProjectSettings
    background: QImage
    main_doc: QTextDocument
    shadow_doc: QTextDocument | None
    outline_doc: QTextDocument | None
    doc_width: int
    text_height: float
    duration: float
    scroll_duration: float
    start_y: float
    end_y: float
    speed: float
    text_layer: QImage | None = None
    text_layer_pad_x: int = 0
    text_layer_pad_y: int = 0
    text_layer_width: int = 0
    text_layer_height: int = 0
    text_layer_tile_height: int = 2048
    text_layer_tile_cache: dict[int, QImage] | None = None
    text_layer_tile_cache_order: list[int] | None = None
    background_sequence: list[QImage] | None = None
    background_sequence_paths: list[str] | None = None
    background_sequence_interval: float = 8.0
    background_sequence_durations: list[float] | None = None
    background_sequence_cache: dict[int, QImage] | None = None
    background_sequence_cache_order: list[int] | None = None
    caption_segments: list[CaptionSegment] | None = None
    caption_doc_cache: dict[str, QTextDocument] | None = None
    foreground_sequence_paths: list[str] | None = None
    foreground_image_cache: dict[str, QImage] | None = None


def _hex_to_qcolor(value: str, alpha: float = 1.0) -> QColor:
    color = QColor(value or "#FFFFFF")
    if not color.isValid():
        color = QColor("#FFFFFF")
    color.setAlphaF(max(0.0, min(1.0, alpha)))
    return color


def _cover_image(image: QImage, width: int, height: int) -> QImage:
    if image.isNull():
        return QImage(width, height, QImage.Format.Format_RGB888)
    scaled = image.scaled(QSize(width, height), Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
    x = max(0, (scaled.width() - width) // 2)
    y = max(0, (scaled.height() - height) // 2)
    return scaled.copy(QRect(x, y, width, height))


def _cheap_blur(image: QImage, blur_amount: int) -> QImage:
    """Fast blur approximation by scaling down/up. Good enough for video backgrounds."""
    blur_amount = int(max(0, min(30, blur_amount)))
    if blur_amount <= 0:
        return image
    factor = max(2, min(20, blur_amount))
    small_w = max(24, image.width() // factor)
    small_h = max(24, image.height() // factor)
    return image.scaled(small_w, small_h, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation).scaled(
        image.width(), image.height(), Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
    )


def _apply_background_adjustments(image: QImage, settings: ProjectSettings) -> QImage:
    """Apply brightness and dark overlay settings to any background frame."""
    adjusted = image.convertToFormat(QImage.Format.Format_RGB888)
    painter = QPainter(adjusted)
    if settings.background_brightness < 100:
        alpha = int((100 - settings.background_brightness) / 100 * 220)
        painter.fillRect(0, 0, settings.width, settings.height, QColor(0, 0, 0, alpha))
    elif settings.background_brightness > 100:
        alpha = int(min(100, settings.background_brightness - 100) / 100 * 160)
        painter.fillRect(0, 0, settings.width, settings.height, QColor(255, 255, 255, alpha))

    overlay_alpha = int(max(0, min(100, settings.dark_overlay_percent)) / 100 * 255)
    if overlay_alpha > 0:
        painter.fillRect(0, 0, settings.width, settings.height, QColor(0, 0, 0, overlay_alpha))
    painter.end()
    return adjusted


def make_background(settings: ProjectSettings) -> QImage:
    width, height = settings.width, settings.height
    image = QImage(width, height, QImage.Format.Format_RGB888)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    custom_background_on = bool(getattr(settings, "custom_background_enabled", True))

    if custom_background_on and settings.background_mode == "image" and settings.background_image_path and Path(settings.background_image_path).exists():
        bg = QImage(settings.background_image_path)
        bg = _cover_image(bg, width, height)
        bg = _cheap_blur(bg, settings.background_blur)
        painter.drawImage(0, 0, bg)
    elif custom_background_on and settings.background_mode == "solid":
        painter.fillRect(0, 0, width, height, _hex_to_qcolor(settings.background_color))
    else:
        gradient = QLinearGradient(0, 0, width, height)
        gradient.setColorAt(0.0, _hex_to_qcolor(settings.gradient_color_1))
        gradient.setColorAt(1.0, _hex_to_qcolor(settings.gradient_color_2))
        painter.fillRect(0, 0, width, height, QBrush(gradient))

    painter.end()
    return _apply_background_adjustments(image, settings)


def _background_paths_for_settings(settings: ProjectSettings, max_paths: int = 20000) -> list[Path]:
    explicit = [Path(str(p)) for p in (getattr(settings, "pexels_cache_paths", []) or []) if Path(str(p)).exists()]
    if explicit:
        source_paths = explicit[:max_paths]
    elif getattr(settings, "pexels_enabled", False) and getattr(settings, "pexels_use_library", True) and bool(getattr(settings, "use_image_backgrounds", True)):
        library_folder = str(getattr(settings, "pexels_library_folder", "") or "").strip()
        source_paths = list_background_images(resolve_pexels_library_dir(library_folder))[:max_paths]
    else:
        return []

    configured_seed = int(getattr(settings, "visual_motion_seed", 0) or 0)
    seed = configured_seed or stable_seed(
        getattr(settings, "job_id", ""),
        getattr(settings, "project_name", "Untitled"),
        getattr(settings, "pexels_library_folder", ""),
    )
    randomize = bool(getattr(settings, "pexels_randomize_library", True)) and bool(getattr(settings, "visual_smart_rotation", True))
    return deterministic_visual_order(source_paths, seed, randomize)[:max_paths]



def _duration_for_background_path(settings: ProjectSettings, path: Path) -> float:
    text = str(path).replace("\\", "/").lower()
    if "/pexels_video_frames/" in text or "\\pexels_video_frames\\" in text or "/video_frames/" in text or "\\video_frames\\" in text:
        motion_fps = max(1.0, min(12.0, float(getattr(settings, "pexels_video_frame_fps", 6.0) or 6.0)))
        return 1.0 / motion_fps
    return max(1.0, float(getattr(settings, "pexels_interval_seconds", 8.0) or 8.0))


def _load_background_sequence(settings: ProjectSettings) -> tuple[list[str], list[float]]:
    paths: list[str] = []
    if not getattr(settings, "pexels_enabled", False):
        return paths, []
    for path in _background_paths_for_settings(settings):
        if path.exists():
            paths.append(str(path))
    fixed = max(1.0, float(getattr(settings, "pexels_interval_seconds", 8.0) or 8.0))
    if bool(getattr(settings, "single_background_enabled", False)) and paths:
        paths = paths[:1]
    durations = build_visual_durations(settings, len(paths), fixed)
    # Prepared video frames keep their true motion-frame duration.
    for index, path in enumerate(paths):
        detected = _duration_for_background_path(settings, Path(path))
        if detected < 0.5:
            durations[index] = detected
    return paths, durations



def _load_sequence_image(ctx: RenderContext, index: int) -> QImage | None:
    paths = ctx.background_sequence_paths or []
    if index < 0 or index >= len(paths):
        return None
    if ctx.background_sequence_cache is None:
        ctx.background_sequence_cache = {}
    if ctx.background_sequence_cache_order is None:
        ctx.background_sequence_cache_order = []
    cache = ctx.background_sequence_cache
    order = ctx.background_sequence_cache_order
    if index in cache:
        try:
            order.remove(index)
        except ValueError:
            pass
        order.append(index)
        return cache[index].copy()

    img = QImage(str(paths[index]))
    if img.isNull():
        return None
    # Cache the original source so each frame can render its own pan/zoom crop.
    img = img.convertToFormat(QImage.Format.Format_RGB888)
    cache[index] = img
    order.append(index)
    while len(order) > 6:
        old = order.pop(0)
        cache.pop(old, None)
    return img.copy()


def _background_motion_cycles(settings: ProjectSettings) -> int:
    speed_pct = max(10, min(300, int(getattr(settings, "pexels_motion_speed_percent", 100) or 100)))
    if speed_pct >= 230:
        return 4
    if speed_pct >= 160:
        return 3
    if speed_pct >= 110:
        return 2
    return 1


def _render_background_source_frame(
    source: QImage,
    settings: ProjectSettings,
    progress: float = 0.0,
    animate: bool = False,
    *,
    sequence_index: int = 0,
    segment_duration: float = 8.0,
) -> QImage:
    width, height = int(settings.width), int(settings.height)
    if source.isNull():
        return make_background(settings)

    canvas = QImage(width, height, QImage.Format.Format_RGB888)
    canvas.fill(QColor("#000000"))
    iw, ih = max(1, source.width()), max(1, source.height())
    base_scale = max(float(width) / float(iw), float(height) / float(ih))
    progress = max(0.0, min(1.0, float(progress)))

    if animate and bool(getattr(settings, "motion_enabled", True)) and int(getattr(settings, "pexels_motion_intensity_percent", 42) or 42) > 0:
        configured_seed = int(getattr(settings, "visual_motion_seed", 0) or 0)
        seed = configured_seed or stable_seed(getattr(settings, "job_id", ""), getattr(settings, "project_name", "Untitled"))
        style = str(getattr(settings, "visual_motion_style", "cinematic") or "cinematic")
        plan = build_motion_plan(
            seed=seed,
            index=sequence_index,
            style=style,
            intensity_percent=int(getattr(settings, "pexels_motion_intensity_percent", 42) or 42),
            source_size=(iw, ih),
            target_size=(width, height),
            segment_duration=segment_duration,
            quality_protection=bool(getattr(settings, "visual_quality_protection", True)),
            avoid_repeats=bool(getattr(settings, "visual_avoid_repeated_motion", True)),
        )
        speed_ratio = max(0.35, min(1.75, float(getattr(settings, "pexels_motion_speed_percent", 100) or 100) / 100.0))
        motion_progress = min(1.0, progress * speed_ratio)
        zoom, norm_x, norm_y = interpolate_plan(plan, motion_progress, style)
    else:
        zoom, norm_x, norm_y = 1.0, 0.0, 0.0

    scale = max(base_scale, base_scale * max(1.0, zoom))
    scaled_w = max(float(width), float(iw) * scale)
    scaled_h = max(float(height), float(ih) * scale)
    max_pan_x = max(0.0, (scaled_w - float(width)) / 2.0)
    max_pan_y = max(0.0, (scaled_h - float(height)) / 2.0)
    pan_x = max(-1.0, min(1.0, norm_x)) * max_pan_x
    pan_y = max(-1.0, min(1.0, norm_y)) * max_pan_y
    draw_x = -((scaled_w - float(width)) / 2.0 + pan_x)
    draw_y = -((scaled_h - float(height)) / 2.0 + pan_y)

    painter = QPainter(canvas)
    painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform, True)
    painter.drawImage(QRectF(draw_x, draw_y, scaled_w, scaled_h), source, QRectF(0, 0, iw, ih))
    painter.end()
    if int(getattr(settings, "background_blur", 0) or 0) > 0:
        canvas = _cheap_blur(canvas, int(getattr(settings, "background_blur", 0) or 0))
    return _apply_background_adjustments(canvas, settings).convertToFormat(QImage.Format.Format_RGB888)



def _sequence_background_frame(ctx: RenderContext, time_seconds: float) -> QImage:
    paths = ctx.background_sequence_paths or []
    if not paths:
        images = ctx.background_sequence or []
        if not images:
            return ctx.background.copy()
        interval = max(1.0, float(ctx.background_sequence_interval or 8.0))
        idx = min(len(images) - 1, int(max(0.0, time_seconds) // interval))
        return images[idx].copy()

    if bool(getattr(ctx.settings, "single_background_enabled", False)) and paths:
        current_source = _load_sequence_image(ctx, 0)
        if current_source is None:
            return ctx.background.copy()
        full_duration = max(0.01, float(getattr(ctx, "duration", 0.0) or 0.0))
        progress = min(1.0, max(0.0, float(time_seconds)) / full_duration)
        return _render_background_source_frame(
            current_source,
            ctx.settings,
            progress,
            animate=bool(getattr(ctx.settings, "motion_enabled", True)),
            sequence_index=0,
            segment_duration=full_duration,
        )

    durations = list(ctx.background_sequence_durations or [])
    if len(durations) != len(paths):
        durations = [max(1.0, float(ctx.background_sequence_interval or 8.0)) for _ in paths]
    t = max(0.0, float(time_seconds))
    total_duration = sum(max(0.01, float(value or 0.01)) for value in durations)
    if total_duration > 0 and t >= total_duration:
        t %= total_duration

    idx, elapsed = 0, 0.0
    for i, duration in enumerate(durations):
        duration = max(0.01, float(duration or 0.01))
        if t < elapsed + duration:
            idx = i
            break
        elapsed += duration
        idx = i
    idx = min(len(paths) - 1, idx)
    interval = max(0.01, float(durations[idx] or ctx.background_sequence_interval or 8.0))
    current_source = _load_sequence_image(ctx, idx)
    if current_source is None:
        return ctx.background.copy()

    is_motion_frame = interval < 0.5
    segment_pos = max(0.0, t - elapsed)
    segment_progress = min(1.0, segment_pos / max(0.01, interval))
    current = _render_background_source_frame(
        current_source, ctx.settings, segment_progress, animate=(not is_motion_frame and bool(getattr(ctx.settings, "motion_enabled", True))),
        sequence_index=idx, segment_duration=interval,
    )
    if is_motion_frame or len(paths) < 2:
        return current

    transition_seconds = max(0.0, min(interval * 0.45, float(getattr(ctx.settings, "image_transition_seconds", 0.55) or 0.0)))
    if transition_seconds <= 0.001 or segment_pos < interval - transition_seconds:
        return current
    next_idx = (idx + 1) % len(paths)
    nxt_source = _load_sequence_image(ctx, next_idx)
    if nxt_source is None:
        return current
    alpha = min(1.0, max(0.0, (segment_pos - (interval - transition_seconds)) / transition_seconds))
    configured_seed = int(getattr(ctx.settings, "visual_motion_seed", 0) or 0)
    seed = configured_seed or stable_seed(getattr(ctx.settings, "job_id", ""), getattr(ctx.settings, "project_name", "Untitled"))
    mode = transition_name(str(getattr(ctx.settings, "image_transition_mode", "crossfade") or "crossfade"), seed, idx)
    nxt = _render_background_source_frame(
        nxt_source, ctx.settings, 0.0, animate=bool(getattr(ctx.settings, "motion_enabled", True)),
        sequence_index=next_idx, segment_duration=max(0.01, float(durations[next_idx] or interval)),
    )
    if mode == "cut":
        return nxt if alpha >= 0.5 else current
    if mode == "fade_black":
        painter = QPainter(current)
        if alpha < 0.5:
            painter.setOpacity(alpha * 2.0)
            painter.fillRect(0, 0, current.width(), current.height(), QColor("#000000"))
        else:
            painter.fillRect(0, 0, current.width(), current.height(), QColor("#000000"))
            painter.setOpacity((alpha - 0.5) * 2.0)
            painter.drawImage(0, 0, nxt)
        painter.end()
        return current
    if mode == "push":
        result = QImage(current.size(), current.format())
        result.fill(QColor("#000000"))
        painter = QPainter(result)
        offset = int(round(current.width() * alpha))
        painter.drawImage(-offset, 0, current)
        painter.drawImage(current.width() - offset, 0, nxt)
        painter.end()
        return result
    if mode == "dissolve":
        alpha = alpha * alpha * (3.0 - 2.0 * alpha)
    painter = QPainter(current)
    painter.setOpacity(alpha)
    painter.drawImage(0, 0, nxt)
    painter.end()
    return current




def _apply_text_case(text: str, case_mode: str) -> str:
    """Apply UI text case safely before building QTextDocument HTML."""
    value = text or ""
    mode = (case_mode or "normal").strip().lower()
    if mode == "upper":
        return value.upper()
    if mode == "lower":
        return value.lower()
    if mode == "title":
        # Python title() works reasonably for English while leaving Urdu/Hindi
        # and other scripts safe enough because uncased characters remain unchanged.
        return value.title()
    return value


def _html_text(settings: ProjectSettings, color: str) -> str:
    direction = settings.direction
    if direction == "auto":
        direction = "rtl" if looks_rtl(settings.text) else "ltr"

    align = settings.alignment if settings.alignment in {"left", "center", "right", "justify"} else "center"
    source_text = to_single_paragraph(settings.text) if getattr(settings, "single_paragraph", False) else settings.text
    source_text = _apply_text_case(source_text, str(getattr(settings, "text_case", "normal") or "normal"))
    escaped_paragraphs = split_paragraphs(source_text) or [source_text]
    paragraphs = []
    total_paragraphs = len(escaped_paragraphs)
    for index, paragraph in enumerate(escaped_paragraphs):
        safe = html.escape(paragraph).replace("\n", "<br>")
        bottom_margin = settings.paragraph_spacing if index < total_paragraphs - 1 else 0
        paragraphs.append(f"<p style='margin:0 0 {bottom_margin}px 0; padding:0;'>{safe}</p>")

    return f"""
    <html>
      <body style="margin:0; padding:0; background:transparent;">
        <div dir="{direction}" style="
            color:{color};
            text-align:{align};
            line-height:{max(50, min(350, int(settings.line_height_percent) + int(getattr(settings, "text_line_spacing", 0) or 0)))}%;
            white-space:normal;
        ">
          {''.join(paragraphs)}
        </div>
      </body>
    </html>
    """


def _text_document_width(settings: ProjectSettings) -> int:
    """Return the row/text-box width used for wrapping text.

    text_width_percent lets the editor create narrow or wide rows without
    forcing users to calculate left/right margins. It is applied inside the
    existing left/right safe margins.
    """
    usable_width = max(100, int(settings.width) - int(settings.margin_left) - int(settings.margin_right))
    width_percent = max(20, min(250, int(getattr(settings, "text_width_percent", 100))))
    scale_width = max(10, min(300, int(getattr(settings, "text_scale_width_percent", 100) or 100)))
    return max(100, int(round(usable_width * width_percent / 100.0 * scale_width / 100.0)))


def _text_document_x(settings: ProjectSettings, doc_width: int) -> float:
    """Return X position for the text block using 0%-100% left/right control."""
    min_x = float(max(0, int(settings.margin_left)))
    max_x = float(min(int(settings.width) - doc_width - int(settings.margin_right), int(settings.width) - doc_width))
    if max_x < min_x:
        return max(0.0, (float(settings.width) - float(doc_width)) / 2.0)
    x_percent = max(0, min(100, int(getattr(settings, "text_x_percent", 50))))
    base_x = min_x + ((max_x - min_x) * x_percent / 100.0)
    offset_percent = max(-100, min(100, int(getattr(settings, "text_offset_x_percent", 0) or 0)))
    offset_px = float(settings.width) * offset_percent / 100.0
    return max(-float(settings.width), min(float(settings.width), base_x + offset_px))


def make_text_document(settings: ProjectSettings, color: str | None = None) -> QTextDocument:
    font = QFont(settings.font_family)
    font.setPointSize(max(6, int(settings.font_size)))
    font.setBold(bool(settings.bold))
    font.setUnderline(bool(getattr(settings, "underline", False)))
    font.setItalic(bool(getattr(settings, "italic", False)))
    char_spacing = int(getattr(settings, "character_spacing", 0) or 0)
    if char_spacing:
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, float(char_spacing))

    doc_width = _text_document_width(settings)
    doc = QTextDocument()
    doc.setDocumentMargin(0)
    doc.setDefaultFont(font)
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WordWrap)
    option.setAlignment(ALIGN_MAP.get(settings.alignment, Qt.AlignmentFlag.AlignHCenter))
    if settings.direction == "rtl" or (settings.direction == "auto" and looks_rtl(settings.text)):
        option.setTextDirection(Qt.LayoutDirection.RightToLeft)
    else:
        option.setTextDirection(Qt.LayoutDirection.LeftToRight)
    doc.setDefaultTextOption(option)
    doc.setTextWidth(doc_width)
    doc.setHtml(_html_text(settings, color or settings.text_color))
    doc.setTextWidth(doc_width)
    return doc





def _caption_plain_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def _caption_direction(settings: ProjectSettings, text: str) -> str:
    lang = str(getattr(settings, "caption_language", "auto") or "auto")
    return "rtl" if is_rtl_text(text or getattr(settings, "text", ""), lang) else "ltr"


def _caption_markup(text: str, active_word_index: int | None = None, active_color: str | None = None) -> str:
    clean = _caption_plain_text(text)
    if active_word_index is None or active_word_index < 0 or not active_color:
        return html.escape(clean).replace("\n", "<br>")
    # Keep punctuation attached to each word. For scripts without spaces, fall
    # back to normal caption text instead of forcing unnatural character-level karaoke.
    parts = re.findall(r"\S+", clean)
    if len(parts) <= 1:
        return html.escape(clean).replace("\n", "<br>")
    active_word_index = max(0, min(len(parts) - 1, active_word_index))
    out: list[str] = []
    for idx, word in enumerate(parts):
        safe_word = html.escape(word)
        if idx == active_word_index:
            out.append(f'<span style="color:{active_color}; font-weight:800;">{safe_word}</span>')
        else:
            out.append(safe_word)
    return " ".join(out)


def _caption_html(settings: ProjectSettings, text: str, color: str, active_word_index: int | None = None, active_color: str | None = None) -> str:
    safe = _caption_markup(text, active_word_index, active_color)
    direction = _caption_direction(settings, text)
    align = settings.alignment if settings.alignment in {"left", "center", "right", "justify"} else "center"
    if direction == "rtl" and align == "left":
        align = "right"
    line_height = max(60, min(250, int(settings.line_height_percent) + int(getattr(settings, "text_line_spacing", 0) or 0)))
    return (
        '<html><body style="margin:0; padding:0; background:transparent;">'
        f'<div dir="{direction}" style="color:{color}; text-align:{align}; line-height:{line_height}%; white-space:normal;">'
        f'{safe}</div></body></html>'
    )


def _make_caption_document(settings: ProjectSettings, text: str, color: str | None = None, active_word_index: int | None = None) -> QTextDocument:
    font = QFont(settings.font_family)
    font.setPointSize(max(6, int(settings.font_size)))
    font.setBold(bool(settings.bold))
    font.setUnderline(bool(getattr(settings, "underline", False)))
    font.setItalic(bool(getattr(settings, "italic", False)))
    char_spacing = int(getattr(settings, "character_spacing", 0) or 0)
    if char_spacing:
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, float(char_spacing))
    doc_width = _text_document_width(settings)
    doc = QTextDocument()
    doc.setDocumentMargin(0)
    doc.setDefaultFont(font)
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WordWrap)
    option.setAlignment(ALIGN_MAP.get(settings.alignment, Qt.AlignmentFlag.AlignHCenter))
    option.setTextDirection(Qt.LayoutDirection.RightToLeft if _caption_direction(settings, text) == "rtl" else Qt.LayoutDirection.LeftToRight)
    doc.setDefaultTextOption(option)
    doc.setTextWidth(doc_width)
    active_color = getattr(settings, "caption_highlight_color", "#FFD54A") if active_word_index is not None else None
    doc.setHtml(_caption_html(settings, text, color or settings.text_color, active_word_index, active_color))
    doc.setTextWidth(doc_width)
    return doc


def _active_caption_segment(ctx: RenderContext, time_seconds: float) -> CaptionSegment | None:
    for seg in ctx.caption_segments or []:
        if float(seg.start) <= time_seconds <= float(seg.end):
            return seg
    return None


def _caption_doc_from_cache(ctx: RenderContext, text: str, color: str, active_word_index: int | None = None) -> QTextDocument:
    key = f"{color}|{active_word_index if active_word_index is not None else '-'}|{text}"
    if ctx.caption_doc_cache is None:
        ctx.caption_doc_cache = {}
    doc = ctx.caption_doc_cache.get(key)
    if doc is None:
        doc = _make_caption_document(ctx.settings, text, color, active_word_index=active_word_index)
        ctx.caption_doc_cache[key] = doc
        if len(ctx.caption_doc_cache) > 500:
            for old in list(ctx.caption_doc_cache.keys())[:100]:
                ctx.caption_doc_cache.pop(old, None)
    return doc


def _caption_segment_progress(seg: CaptionSegment, time_seconds: float) -> float:
    span = max(0.05, float(seg.end) - float(seg.start))
    return max(0.0, min(1.0, (float(time_seconds) - float(seg.start)) / span))


def _caption_active_word_index(text: str, progress: float) -> int | None:
    words = re.findall(r"\S+", _caption_plain_text(text))
    if len(words) <= 1:
        return None
    return max(0, min(len(words) - 1, int(progress * len(words))))


def _caption_pop_scale(settings: ProjectSettings, progress: float) -> float:
    if not bool(getattr(settings, "caption_heavy_enabled", False)):
        return 1.0
    pop = max(0.0, min(0.40, float(getattr(settings, "caption_pop_percent", 0) or 0) / 100.0))
    if pop <= 0.0:
        return 1.0
    # Strong at entry, settles quickly. This gives a heavy CapCut-style punch
    # without changing segment timing.
    entry = max(0.0, 1.0 - min(1.0, progress / 0.22))
    pulse = math.sin(min(1.0, progress) * math.pi) * 0.22
    return 1.0 + pop * (entry + pulse)


def _caption_fade_opacity(settings: ProjectSettings, progress: float) -> float:
    base = max(0.05, min(1.0, float(getattr(settings, "text_opacity", 1.0) or 1.0)))
    if not bool(getattr(settings, "caption_heavy_enabled", False)):
        return base
    fade_in = min(1.0, progress / 0.08)
    fade_out = min(1.0, (1.0 - progress) / 0.08)
    return base * max(0.0, min(1.0, fade_in, fade_out))


def _caption_y(settings: ProjectSettings, doc_height: float) -> float:
    position = str(getattr(settings, "caption_position", "bottom") or "bottom").lower()
    margin_top = float(getattr(settings, "margin_top", 80) or 80)
    margin_bottom = float(getattr(settings, "margin_bottom", 80) or 80)
    offset_y_percent = max(-100, min(100, int(getattr(settings, "text_offset_y_percent", 0) or 0)))
    offset_y = float(settings.height) * offset_y_percent / 100.0
    if position == "top":
        return max(0.0, margin_top + offset_y)
    if position == "center":
        return max(0.0, (float(settings.height) - doc_height) / 2.0 + offset_y)
    return max(0.0, float(settings.height) - margin_bottom - doc_height + offset_y)


def _draw_caption_background_box(painter: QPainter, settings: ProjectSettings, x: float, y: float, w: float, h: float) -> None:
    if not bool(getattr(settings, "text_background_enabled", True)):
        return
    color = _text_background_qcolor(settings)
    if color is None:
        return
    pad = max(8, int(getattr(settings, "text_background_padding", 24) or 24))
    rect = QRectF(x - pad, y - pad * 0.6, w + pad * 2, h + pad * 1.2)
    rect = rect.intersected(QRectF(0, 0, settings.width, settings.height))
    radius = max(0.0, min(rect.width(), rect.height()) * max(0, min(100, int(getattr(settings, "text_background_radius_percent", 40) or 40))) / 200.0)
    painter.save()
    painter.setBrush(QBrush(color))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(rect, radius, radius)
    painter.restore()


def _draw_caption(ctx: RenderContext, painter: QPainter, time_seconds: float) -> None:
    settings = ctx.settings
    seg = _active_caption_segment(ctx, time_seconds)
    if seg is None or not (seg.text or "").strip():
        return
    text = _caption_plain_text(seg.text)
    progress = _caption_segment_progress(seg, time_seconds)
    active_word = _caption_active_word_index(text, progress) if bool(getattr(settings, "caption_karaoke_enabled", False)) else None
    main_doc = _caption_doc_from_cache(ctx, text, settings.text_color, active_word_index=active_word)
    shadow_doc = _caption_doc_from_cache(ctx, text, settings.shadow_color) if settings.shadow_enabled else None
    outline_doc = _caption_doc_from_cache(ctx, text, settings.outline_color) if settings.outline_enabled and settings.outline_width > 0 else None
    doc_width = _text_document_width(settings)
    doc_height = max(1.0, float(main_doc.size().height()))
    x = _text_document_x(settings, doc_width)
    y = _caption_y(settings, doc_height)

    scale = _caption_pop_scale(settings, progress)
    opacity = _caption_fade_opacity(settings, progress)
    center_x = x + doc_width / 2.0
    center_y = y + doc_height / 2.0

    def _map_x(value: float) -> float:
        return center_x + (value - center_x) * scale

    def _map_y(value: float) -> float:
        return center_y + (value - center_y) * scale

    if bool(getattr(settings, "caption_heavy_enabled", False)) and scale != 1.0:
        bg_x = _map_x(x)
        bg_y = _map_y(y)
        bg_w = doc_width * scale
        bg_h = doc_height * scale
        _draw_caption_background_box(painter, settings, bg_x, bg_y, bg_w, bg_h)
    else:
        _draw_caption_background_box(painter, settings, x, y, doc_width, doc_height)

    painter.save()
    if scale != 1.0:
        painter.translate(center_x, center_y)
        painter.scale(scale, scale)
        painter.translate(-center_x, -center_y)

    if settings.shadow_enabled and shadow_doc is not None:
        painter.save()
        painter.setOpacity(min(0.85, opacity))
        painter.translate(x + settings.shadow_offset_x, y + settings.shadow_offset_y)
        shadow_doc.drawContents(painter, QRectF(0, 0, doc_width, shadow_doc.size().height()))
        painter.restore()

    if outline_doc is not None:
        width = max(1, int(settings.outline_width))
        offsets: list[tuple[int, int]] = []
        for ox in range(-width, width + 1, width):
            for oy in range(-width, width + 1, width):
                if ox or oy:
                    offsets.append((ox, oy))
        painter.save()
        painter.setOpacity(min(0.95, opacity))
        for ox, oy in offsets:
            painter.save()
            painter.translate(x + ox, y + oy)
            outline_doc.drawContents(painter, QRectF(0, 0, doc_width, outline_doc.size().height()))
            painter.restore()
        painter.restore()

    painter.save()
    painter.setOpacity(opacity)
    painter.translate(x, y)
    main_doc.drawContents(painter, QRectF(0, 0, doc_width, main_doc.size().height()))
    painter.restore()
    painter.restore()

def _safe_layer_dimension(value: float) -> int:
    """Return a safe integer image dimension for cached text layers."""
    return max(1, int(math.ceil(value)))


def _text_background_qcolor(settings: ProjectSettings) -> QColor | None:
    opacity_percent = max(0, min(100, int(getattr(settings, "text_background_opacity_percent", 45) or 0)))
    if opacity_percent <= 0:
        return None
    color = QColor(str(getattr(settings, "text_background_color", "#000000") or "#000000"))
    if not color.isValid():
        color = QColor("#000000")
    color.setAlpha(int(opacity_percent / 100.0 * 255))
    return color


def _scroll_text_background_mode(settings: ProjectSettings) -> str:
    if not bool(getattr(settings, "text_background_enabled", False)):
        return "off"
    explicit = str(getattr(settings, "text_background_mode", "") or "").strip().lower()
    if explicit in {"off", "whole", "follow"}:
        return explicit
    # Backward compatibility: old follow_text=True meant one box around the full text document;
    # old False meant the fixed frame-relative plate.
    return "whole" if bool(getattr(settings, "text_background_follow_text", False)) else "fixed"


def _text_background_padding_xy(settings: ProjectSettings) -> tuple[int, int]:
    fallback = max(0, int(getattr(settings, "text_background_padding", 24) or 0))
    px = max(0, int(getattr(settings, "text_background_padding_x", fallback) or 0))
    py = max(0, int(getattr(settings, "text_background_padding_y", fallback) or 0))
    return px, py


def _draw_follow_text_line_backgrounds(
    painter: QPainter,
    settings: ProjectSettings,
    doc: QTextDocument,
    origin_x: float,
    origin_y: float,
) -> None:
    """Draw one rounded background behind each wrapped visual text line."""
    if _scroll_text_background_mode(settings) != "follow":
        return
    color = _text_background_qcolor(settings)
    if color is None:
        return
    pad_x, pad_y = _text_background_padding_xy(settings)
    radius = max(0, int(getattr(settings, "text_background_radius", 18) or 0))
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(color))
    block = doc.begin()
    while block.isValid():
        layout = block.layout()
        if layout is not None:
            base = layout.position()
            for index in range(layout.lineCount()):
                line = layout.lineAt(index)
                rect = line.naturalTextRect().translated(base)
                if rect.width() <= 0 or rect.height() <= 0:
                    continue
                box = QRectF(
                    float(origin_x) + rect.x() - pad_x,
                    float(origin_y) + rect.y() - pad_y,
                    max(1.0, rect.width() + pad_x * 2),
                    max(1.0, rect.height() + pad_y * 2),
                )
                painter.drawRoundedRect(box, radius, radius)
        block = block.next()
    painter.restore()


def _draw_scrolling_text_background(
    painter: QPainter,
    settings: ProjectSettings,
    doc: QTextDocument,
    x: float,
    y: float,
    doc_width: float,
    text_height: float,
) -> None:
    mode = _scroll_text_background_mode(settings)
    if mode == "whole":
        _draw_text_background_box(painter, settings, x, y, doc_width, text_height)
    elif mode == "follow":
        _draw_follow_text_line_backgrounds(painter, settings, doc, x, y)


def _draw_fixed_text_background_plate(painter: QPainter, settings: ProjectSettings) -> None:
    """Draw a CapCut/Canva-style fixed rounded plate behind the text area.

    This is intentionally frame-relative, so it stays smooth in live preview and
    during long scrolling renders.  X/Y values are treated as center offsets
    from 0% to 100%; width/height are percentages of the video frame.
    """
    if _scroll_text_background_mode(settings) != "fixed":
        return
    color = _text_background_qcolor(settings)
    if color is None:
        return

    width_percent = max(5, min(100, int(getattr(settings, "text_background_width_percent", 92) or 92)))
    height_percent = max(5, min(100, int(getattr(settings, "text_background_height_percent", 32) or 32)))
    x_percent = max(0, min(100, int(getattr(settings, "text_background_x_percent", 50) or 50)))
    y_percent = max(0, min(100, int(getattr(settings, "text_background_y_percent", 50) or 50)))
    w = max(12.0, float(settings.width) * width_percent / 100.0)
    h = max(12.0, float(settings.height) * height_percent / 100.0)
    cx = float(settings.width) * x_percent / 100.0
    cy = float(settings.height) * y_percent / 100.0
    x = max(0.0, min(float(settings.width) - w, cx - w / 2.0))
    y = max(0.0, min(float(settings.height) - h, cy - h / 2.0))
    radius_percent = max(0, min(100, int(getattr(settings, "text_background_radius_percent", 40) or 0)))
    radius = min(w, h) * radius_percent / 200.0

    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(color))
    painter.drawRoundedRect(QRectF(x, y, w, h), radius, radius)
    painter.restore()


def _draw_text_background_box(
    painter: QPainter,
    settings: ProjectSettings,
    x: float,
    y: float,
    doc_width: float,
    text_height: float,
) -> None:
    """Draw the legacy/follow-text rounded box behind the scrolling text layer."""
    if _scroll_text_background_mode(settings) != "whole":
        return

    padding_x, padding_y = _text_background_padding_xy(settings)
    radius = max(0, int(getattr(settings, "text_background_radius", 18) or 0))
    color = _text_background_qcolor(settings)
    if color is None:
        return

    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(color))
    rect = QRectF(
        float(x) - padding_x,
        float(y) - padding_y,
        max(1.0, float(doc_width) + padding_x * 2),
        max(1.0, float(text_height) + padding_y * 2),
    )
    painter.drawRoundedRect(rect, radius, radius)
    painter.restore()


def _make_cached_text_layer(
    settings: ProjectSettings,
    main_doc: QTextDocument,
    shadow_doc: QTextDocument | None,
    outline_doc: QTextDocument | None,
    doc_width: int,
    text_height: float,
) -> tuple[QImage | None, int, int, int, int]:
    """Pre-render the complete text/effects layer once.

    The old renderer drew QTextDocument contents again for every video frame.
    Long videos could require tens of thousands of expensive document redraws.
    This cached layer lets each frame only crop/move a ready transparent image.
    """
    outline_pad = max(0, int(getattr(settings, "outline_width", 0))) if getattr(settings, "outline_enabled", False) else 0
    shadow_x = int(getattr(settings, "shadow_offset_x", 0)) if getattr(settings, "shadow_enabled", False) else 0
    shadow_y = int(getattr(settings, "shadow_offset_y", 0)) if getattr(settings, "shadow_enabled", False) else 0
    bg_px, bg_py = _text_background_padding_xy(settings) if _scroll_text_background_mode(settings) in {"whole", "follow"} else (0, 0)
    pad_x = max(4, outline_pad + 3, abs(shadow_x) + 4, bg_px + 4)
    pad_y = max(4, outline_pad + 3, abs(shadow_y) + 4, bg_py + 4)
    layer_width = _safe_layer_dimension(doc_width + (pad_x * 2))
    layer_height = _safe_layer_dimension(text_height + (pad_y * 2))

    # Guard against accidental giant allocations. If a project is extremely
    # large, fall back to the compatible document-per-frame path instead of
    # crashing the app.
    estimated_bytes = layer_width * layer_height * 4
    max_layer_bytes = 220 * 1024 * 1024
    if estimated_bytes > max_layer_bytes:
        # Avoid hard crashes on very long scripts/high resolutions. The renderer
        # will use the compatible direct-draw path instead of allocating a huge image.
        return None, pad_x, pad_y, layer_width, layer_height

    layer = QImage(layer_width, layer_height, QImage.Format.Format_ARGB32_Premultiplied)
    if layer.isNull():
        return None, pad_x, pad_y, layer_width, layer_height
    layer.fill(Qt.GlobalColor.transparent)

    painter = QPainter(layer)
    painter.setRenderHints(
        QPainter.RenderHint.Antialiasing
        | QPainter.RenderHint.TextAntialiasing
        | QPainter.RenderHint.SmoothPixmapTransform,
        True,
    )

    _draw_scrolling_text_background(painter, settings, main_doc, pad_x, pad_y, doc_width, text_height)

    if getattr(settings, "shadow_enabled", False) and shadow_doc is not None:
        painter.save()
        painter.setOpacity(min(0.8, float(getattr(settings, "text_opacity", 1.0))))
        painter.translate(pad_x + shadow_x, pad_y + shadow_y)
        shadow_doc.drawContents(painter, QRectF(0, 0, doc_width, shadow_doc.size().height()))
        painter.restore()

    if getattr(settings, "outline_enabled", False) and int(getattr(settings, "outline_width", 0)) > 0 and outline_doc is not None:
        w = max(1, int(getattr(settings, "outline_width", 1)))
        offsets: list[tuple[int, int]] = []
        for ox in range(-w, w + 1, w):
            for oy in range(-w, w + 1, w):
                if ox or oy:
                    offsets.append((ox, oy))
        painter.save()
        painter.setOpacity(min(0.9, float(getattr(settings, "text_opacity", 1.0))))
        for ox, oy in offsets:
            painter.save()
            painter.translate(pad_x + ox, pad_y + oy)
            outline_doc.drawContents(painter, QRectF(0, 0, doc_width, outline_doc.size().height()))
            painter.restore()
        painter.restore()

    painter.save()
    painter.setOpacity(max(0.05, min(1.0, float(getattr(settings, "text_opacity", 1.0)))))
    painter.translate(pad_x, pad_y)
    main_doc.drawContents(painter, QRectF(0, 0, doc_width, main_doc.size().height()))
    painter.restore()
    painter.end()

    scale_height = max(10, min(300, int(getattr(settings, "text_scale_height_percent", 100) or 100))) / 100.0
    if abs(scale_height - 1.0) > 0.01:
        new_height = max(1, int(round(layer.height() * scale_height)))
        layer = layer.scaled(layer.width(), new_height, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        pad_y = max(0, int(round(pad_y * scale_height)))

    return layer, pad_x, pad_y, layer.width(), layer.height()


def _draw_document_region(
    painter: QPainter,
    doc: QTextDocument,
    doc_width: int,
    clip_top_layer: float,
    clip_bottom_layer: float,
    doc_origin_y: float,
) -> None:
    """Draw only the document area that can affect the current text-layer tile."""
    doc_top = max(0.0, clip_top_layer - doc_origin_y)
    doc_bottom = min(float(doc.size().height()), clip_bottom_layer - doc_origin_y)
    if doc_bottom <= doc_top:
        return
    doc.drawContents(painter, QRectF(0, doc_top, doc_width, doc_bottom - doc_top))


def _paint_text_effects_to_layer(
    painter: QPainter,
    settings: ProjectSettings,
    main_doc: QTextDocument,
    shadow_doc: QTextDocument | None,
    outline_doc: QTextDocument | None,
    doc_width: int,
    text_height: float,
    pad_x: int,
    pad_y: int,
    clip_top_layer: float,
    clip_bottom_layer: float,
) -> None:
    """Paint text, shadow, outline and follow-text plate in text-layer coordinates."""
    _draw_scrolling_text_background(painter, settings, main_doc, pad_x, pad_y, doc_width, text_height)

    if getattr(settings, "shadow_enabled", False) and shadow_doc is not None:
        shadow_x = int(getattr(settings, "shadow_offset_x", 0))
        shadow_y = int(getattr(settings, "shadow_offset_y", 0))
        painter.save()
        painter.setOpacity(min(0.8, float(getattr(settings, "text_opacity", 1.0))))
        painter.translate(pad_x + shadow_x, pad_y + shadow_y)
        _draw_document_region(
            painter,
            shadow_doc,
            doc_width,
            clip_top_layer,
            clip_bottom_layer,
            pad_y + shadow_y,
        )
        painter.restore()

    if getattr(settings, "outline_enabled", False) and int(getattr(settings, "outline_width", 0)) > 0 and outline_doc is not None:
        w = max(1, int(getattr(settings, "outline_width", 1)))
        offsets: list[tuple[int, int]] = []
        for ox in range(-w, w + 1, w):
            for oy in range(-w, w + 1, w):
                if ox or oy:
                    offsets.append((ox, oy))
        painter.save()
        painter.setOpacity(min(0.9, float(getattr(settings, "text_opacity", 1.0))))
        for ox, oy in offsets:
            painter.save()
            painter.translate(pad_x + ox, pad_y + oy)
            _draw_document_region(
                painter,
                outline_doc,
                doc_width,
                clip_top_layer,
                clip_bottom_layer,
                pad_y + oy,
            )
            painter.restore()
        painter.restore()

    painter.save()
    painter.setOpacity(max(0.05, min(1.0, float(getattr(settings, "text_opacity", 1.0)))))
    painter.translate(pad_x, pad_y)
    _draw_document_region(
        painter,
        main_doc,
        doc_width,
        clip_top_layer,
        clip_bottom_layer,
        pad_y,
    )
    painter.restore()


def _render_text_layer_tile(ctx: RenderContext, tile_index: int) -> QImage:
    """Render one vertical chunk of a huge scrolling text layer on demand."""
    tile_height = max(512, int(ctx.text_layer_tile_height or 2048))
    tile_y = tile_index * tile_height
    remaining = max(0, int(ctx.text_layer_height) - tile_y)
    height = max(1, min(tile_height, remaining))
    width = max(1, int(ctx.text_layer_width))
    tile = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    tile.fill(Qt.GlobalColor.transparent)

    painter = QPainter(tile)
    painter.setRenderHints(
        QPainter.RenderHint.Antialiasing
        | QPainter.RenderHint.TextAntialiasing
        | QPainter.RenderHint.SmoothPixmapTransform,
        True,
    )
    painter.translate(0, -tile_y)
    painter.setClipRect(QRectF(0, tile_y, width, height))
    _paint_text_effects_to_layer(
        painter,
        ctx.settings,
        ctx.main_doc,
        ctx.shadow_doc,
        ctx.outline_doc,
        ctx.doc_width,
        ctx.text_height,
        ctx.text_layer_pad_x,
        ctx.text_layer_pad_y,
        float(tile_y),
        float(tile_y + height),
    )
    painter.end()
    return tile


def _get_text_layer_tile(ctx: RenderContext, tile_index: int) -> QImage:
    """Small LRU tile cache. Keeps RAM low while avoiding text redraw per frame."""
    if ctx.text_layer_tile_cache is None:
        ctx.text_layer_tile_cache = {}
    if ctx.text_layer_tile_cache_order is None:
        ctx.text_layer_tile_cache_order = []

    cache = ctx.text_layer_tile_cache
    order = ctx.text_layer_tile_cache_order
    if tile_index in cache:
        try:
            order.remove(tile_index)
        except ValueError:
            pass
        order.append(tile_index)
        return cache[tile_index]

    tile = _render_text_layer_tile(ctx, tile_index)
    cache[tile_index] = tile
    order.append(tile_index)

    max_cached_tiles = 5
    while len(order) > max_cached_tiles:
        old = order.pop(0)
        cache.pop(old, None)
    return tile


def prepare_context(settings: ProjectSettings) -> RenderContext:
    """Prepare cached rendering assets. Call once per preview/render."""
    background = make_background(settings)
    background_sequence_paths, background_sequence_durations = _load_background_sequence(settings)
    main_doc = make_text_document(settings, settings.text_color)
    shadow_doc = make_text_document(settings, settings.shadow_color) if settings.shadow_enabled else None
    outline_doc = make_text_document(settings, settings.outline_color) if settings.outline_enabled else None

    doc_width = _text_document_width(settings)
    text_h_raw = max(1.0, float(main_doc.size().height()))
    scale_height = max(10, min(300, int(getattr(settings, "text_scale_height_percent", 100) or 100))) / 100.0
    text_h = max(1.0, text_h_raw * scale_height)
    h = float(settings.height)

    audio_duration = 0.0
    if settings.audio_path:
        audio_duration = get_audio_duration(settings.audio_path)
    voice_speed = max(0.25, min(4.0, getattr(settings, "voice_speed_percent", 100) / 100.0))
    effective_audio_duration = audio_duration / voice_speed if audio_duration > 0 else 0.0

    if not bool(getattr(settings, "captions_enabled", True)):
        clean_duration = effective_audio_duration if effective_audio_duration > 0 else max(1.0, float(getattr(settings, "duration_seconds", 30.0) or 30.0))
        clean_duration += max(0.0, float(getattr(settings, "extra_end_seconds", 0.0) or 0.0))
        return RenderContext(
            settings=settings,
            background=background,
            main_doc=main_doc,
            shadow_doc=shadow_doc,
            outline_doc=outline_doc,
            doc_width=doc_width,
            text_height=text_h,
            duration=clean_duration,
            scroll_duration=clean_duration,
            start_y=0.0,
            end_y=0.0,
            speed=0.0,
            text_layer=None,
            text_layer_pad_x=0,
            text_layer_pad_y=0,
            text_layer_width=0,
            text_layer_height=0,
            text_layer_tile_height=2048,
            text_layer_tile_cache=None,
            text_layer_tile_cache_order=None,
            background_sequence=None,
            background_sequence_paths=background_sequence_paths,
            background_sequence_interval=float(getattr(settings, "pexels_interval_seconds", 8.0) or 8.0),
            background_sequence_durations=background_sequence_durations,
            caption_segments=[],
            foreground_sequence_paths=_load_foreground_sequence(settings),
            foreground_image_cache={},
        )

    if bool(getattr(settings, "caption_mode", False)):
        caption_duration = effective_audio_duration if effective_audio_duration > 0 else max(1.0, float(getattr(settings, "duration_seconds", 30.0) or 30.0))
        raw_segments = []
        try:
            raw_segments = list((getattr(settings, "metadata", {}) or {}).get("caption_segments", []) or [])
        except Exception:
            raw_segments = []
        caption_segments: list[CaptionSegment] = []
        for item in raw_segments:
            try:
                caption_segments.append(CaptionSegment(start=float(item.get("start", 0.0)), end=float(item.get("end", 0.0)), text=str(item.get("text", ""))))
            except Exception:
                continue
        caption_no_fallback = bool((getattr(settings, "metadata", {}) or {}).get("caption_no_fallback", False))
        caption_source = str(getattr(settings, "caption_text_source", "whisper") or "whisper").lower().strip()
        if not caption_segments and not caption_no_fallback and caption_source != "whisper" and str(getattr(settings, "text", "") or "").strip():
            caption_segments, _detected, _engine = build_caption_segments(
                text=settings.text,
                audio_path=settings.audio_path if settings.audio_path else None,
                duration=caption_duration,
                sync_mode="fast",
                language=str(getattr(settings, "caption_language", "auto") or "auto"),
                max_words=int(getattr(settings, "caption_max_words", 8) or 8),
                offset=float(getattr(settings, "caption_timing_offset", 0.0) or 0.0),
                text_source="script",
                prefer_script_language=bool(getattr(settings, "caption_prefer_script_language", True)),
                model_size=str(getattr(settings, "caption_whisper_model", "base") or "base"),
                min_duration=float(getattr(settings, "caption_min_duration", 0.0) or 0.0) if bool(getattr(settings, "caption_heavy_enabled", False)) else 0.0,
                tighten_gaps=bool(getattr(settings, "caption_gap_tighten", False)) if bool(getattr(settings, "caption_heavy_enabled", False)) else False,
            )
        if caption_segments:
            caption_duration = max(caption_duration, max(float(seg.end) for seg in caption_segments))
        caption_duration += max(0.0, float(getattr(settings, "extra_end_seconds", 0.0) or 0.0))
        return RenderContext(
            settings=settings,
            background=background,
            main_doc=main_doc,
            shadow_doc=shadow_doc,
            outline_doc=outline_doc,
            doc_width=doc_width,
            text_height=text_h,
            duration=caption_duration,
            scroll_duration=caption_duration,
            start_y=0.0,
            end_y=0.0,
            speed=0.0,
            text_layer=None,
            text_layer_pad_x=0,
            text_layer_pad_y=0,
            text_layer_width=0,
            text_layer_height=0,
            text_layer_tile_height=2048,
            text_layer_tile_cache=None,
            text_layer_tile_cache_order=None,
            background_sequence=None,
            background_sequence_paths=background_sequence_paths,
            background_sequence_interval=float(getattr(settings, "pexels_interval_seconds", 8.0) or 8.0),
            background_sequence_durations=background_sequence_durations,
            background_sequence_cache={},
            background_sequence_cache_order=[],
            caption_segments=caption_segments,
            caption_doc_cache={},
            foreground_sequence_paths=_load_foreground_sequence(settings),
            foreground_image_cache={},
        )

    line_h = max(1.0, settings.font_size * max(50, min(350, int(settings.line_height_percent) + int(getattr(settings, "text_line_spacing", 0) or 0))) / 100.0)
    center_y = h * 0.50

    if settings.start_mode == "voice_centered":
        # CapCut-style voice sync: first text line starts in the vertical middle,
        # and the last line reaches the same middle guide at the voice end.
        start_y = center_y - line_h / 2.0
        end_y = center_y - text_h + line_h / 2.0
    elif settings.start_mode == "immediate":
        start_y = h - settings.margin_bottom - max(40, settings.font_size * 1.3)
        end_y = -text_h - settings.margin_top
    elif settings.start_mode == "centered":
        start_y = h * 0.35
        end_y = -text_h - settings.margin_top
    else:
        start_y = h + settings.extra_start_seconds * max(1.0, settings.manual_speed_px_per_sec)
        end_y = -text_h - settings.margin_top

    offset_y_percent = max(-100, min(100, int(getattr(settings, "text_offset_y_percent", 0) or 0)))
    offset_y = float(settings.height) * offset_y_percent / 100.0
    start_y += offset_y
    end_y += offset_y

    distance = max(1.0, start_y - end_y)

    if settings.auto_sync_audio and effective_audio_duration > 0:
        scroll_duration = max(0.1, effective_audio_duration)
        duration = scroll_duration + max(0.0, settings.extra_end_seconds)
        speed = distance / scroll_duration
    elif not settings.auto_sync_audio:
        speed = max(1.0, float(settings.manual_speed_px_per_sec))
        scroll_duration = distance / speed
        duration = scroll_duration + max(0.0, settings.extra_end_seconds)
    else:
        duration = max(1.0, float(settings.duration_seconds))
        scroll_duration = duration
        speed = distance / duration

    text_layer, layer_pad_x, layer_pad_y, layer_width, layer_height = _make_cached_text_layer(
        settings, main_doc, shadow_doc, outline_doc, doc_width, text_h_raw
    )

    # When the text is too tall for one safe cached image, the renderer now uses
    # a lazy vertical tile cache. This fixes the 20-minute-video case where the
    # old fallback redrew the QTextDocument on every single frame.
    tile_height = 2048

    return RenderContext(
        settings=settings,
        background=background,
        main_doc=main_doc,
        shadow_doc=shadow_doc,
        outline_doc=outline_doc,
        doc_width=doc_width,
        text_height=text_h,
        duration=duration,
        scroll_duration=scroll_duration,
        start_y=start_y,
        end_y=end_y,
        speed=speed,
        text_layer=text_layer,
        text_layer_pad_x=layer_pad_x,
        text_layer_pad_y=layer_pad_y,
        text_layer_width=layer_width,
        text_layer_height=layer_height,
        text_layer_tile_height=tile_height,
        text_layer_tile_cache={} if text_layer is None and layer_width > 0 and layer_height > 0 else None,
        text_layer_tile_cache_order=[] if text_layer is None and layer_width > 0 and layer_height > 0 else None,
        background_sequence=None,
        background_sequence_paths=background_sequence_paths,
        background_sequence_interval=float(getattr(settings, "pexels_interval_seconds", 8.0) or 8.0),
        background_sequence_durations=background_sequence_durations,
        background_sequence_cache={},
        background_sequence_cache_order=[],
        foreground_sequence_paths=_load_foreground_sequence(settings),
        foreground_image_cache={},
    )


def calculate_scroll(settings: ProjectSettings) -> tuple[float, float, float, float]:
    ctx = prepare_context(settings)
    return ctx.duration, ctx.start_y, ctx.end_y, ctx.speed


def _draw_text_documents_compatible(ctx: RenderContext, painter: QPainter, x: float, y: float) -> None:
    """Compatibility fallback for unusually huge projects where cache allocation is skipped."""
    settings = ctx.settings
    _draw_scrolling_text_background(painter, settings, ctx.main_doc, x, y, ctx.doc_width, ctx.text_height)
    if settings.shadow_enabled and ctx.shadow_doc is not None:
        painter.save()
        painter.setOpacity(min(0.8, settings.text_opacity))
        painter.translate(x + settings.shadow_offset_x, y + settings.shadow_offset_y)
        ctx.shadow_doc.drawContents(painter, QRectF(0, 0, ctx.doc_width, ctx.shadow_doc.size().height()))
        painter.restore()

    if settings.outline_enabled and settings.outline_width > 0 and ctx.outline_doc is not None:
        w = max(1, int(settings.outline_width))
        offsets: list[tuple[int, int]] = []
        for ox in range(-w, w + 1, w):
            for oy in range(-w, w + 1, w):
                if ox or oy:
                    offsets.append((ox, oy))
        painter.save()
        painter.setOpacity(min(0.9, settings.text_opacity))
        for ox, oy in offsets:
            painter.save()
            painter.translate(x + ox, y + oy)
            ctx.outline_doc.drawContents(painter, QRectF(0, 0, ctx.doc_width, ctx.outline_doc.size().height()))
            painter.restore()
        painter.restore()

    painter.save()
    painter.setOpacity(max(0.05, min(1.0, settings.text_opacity)))
    painter.translate(x, y)
    ctx.main_doc.drawContents(painter, QRectF(0, 0, ctx.doc_width, ctx.main_doc.size().height()))
    painter.restore()


def _draw_tiled_text_layer(ctx: RenderContext, painter: QPainter, x: float, y: float) -> None:
    """Draw the visible crop from lazily-rendered text tiles for huge projects."""
    settings = ctx.settings
    layer_width = int(ctx.text_layer_width or 0)
    layer_height = int(ctx.text_layer_height or 0)
    if layer_width <= 0 or layer_height <= 0:
        _draw_text_documents_compatible(ctx, painter, x, y)
        return

    rotation = float(getattr(settings, "text_rotation_degrees", 0.0) or 0.0)
    if abs(rotation) > 0.001:
        # Rotation requires a complete layer. Keep the compatible path for this
        # uncommon case rather than allocating a giant image.
        _draw_text_documents_compatible(ctx, painter, x, y)
        return

    layer_x = x - ctx.text_layer_pad_x
    layer_y = y - ctx.text_layer_pad_y

    src_left = max(0.0, -layer_x)
    src_top = max(0.0, -layer_y)
    src_right = min(float(layer_width), float(settings.width) - layer_x)
    src_bottom = min(float(layer_height), float(settings.height) - layer_y)

    if src_right <= src_left or src_bottom <= src_top:
        return

    tile_height = max(512, int(ctx.text_layer_tile_height or 2048))
    first_tile = int(src_top // tile_height)
    last_tile = int((src_bottom - 0.001) // tile_height)

    src_w = src_right - src_left
    for tile_index in range(first_tile, last_tile + 1):
        tile_y = tile_index * tile_height
        tile = _get_text_layer_tile(ctx, tile_index)
        tile_src_top = max(src_top, float(tile_y)) - float(tile_y)
        tile_src_bottom = min(src_bottom, float(tile_y + tile.height())) - float(tile_y)
        tile_h = tile_src_bottom - tile_src_top
        if tile_h <= 0:
            continue

        dst_x = layer_x + src_left
        dst_y = layer_y + float(tile_y) + tile_src_top
        painter.drawImage(
            QRectF(dst_x, dst_y, src_w, tile_h),
            tile,
            QRectF(src_left, tile_src_top, src_w, tile_h),
        )


def _draw_cached_text_layer(ctx: RenderContext, painter: QPainter, x: float, y: float) -> None:
    """Draw only the visible crop of the cached text layer."""
    settings = ctx.settings
    layer = ctx.text_layer
    if layer is None or layer.isNull():
        if int(ctx.text_layer_width or 0) > 0 and int(ctx.text_layer_height or 0) > 0:
            _draw_tiled_text_layer(ctx, painter, x, y)
        else:
            _draw_text_documents_compatible(ctx, painter, x, y)
        return

    layer_x = x - ctx.text_layer_pad_x
    layer_y = y - ctx.text_layer_pad_y

    rotation = float(getattr(settings, "text_rotation_degrees", 0.0) or 0.0)
    if abs(rotation) > 0.001:
        painter.save()
        cx = layer_x + (layer.width() / 2.0)
        cy = layer_y + (layer.height() / 2.0)
        painter.translate(cx, cy)
        painter.rotate(rotation)
        painter.drawImage(
            QRectF(-layer.width() / 2.0, -layer.height() / 2.0, layer.width(), layer.height()),
            layer,
            QRectF(0, 0, layer.width(), layer.height()),
        )
        painter.restore()
        return

    src_x = 0.0
    src_y = 0.0
    dst_x = layer_x
    dst_y = layer_y
    visible_w = float(layer.width())
    visible_h = float(layer.height())

    if dst_x < 0:
        src_x = -dst_x
        visible_w -= src_x
        dst_x = 0.0
    if dst_y < 0:
        src_y = -dst_y
        visible_h -= src_y
        dst_y = 0.0
    if dst_x + visible_w > settings.width:
        visible_w = float(settings.width) - dst_x
    if dst_y + visible_h > settings.height:
        visible_h = float(settings.height) - dst_y

    if visible_w <= 0 or visible_h <= 0:
        return

    painter.drawImage(
        QRectF(dst_x, dst_y, visible_w, visible_h),
        layer,
        QRectF(src_x, src_y, visible_w, visible_h),
    )




def _story_pct(settings: ProjectSettings, x_percent: float, y_percent: float) -> tuple[float, float]:
    return float(settings.width) * float(x_percent) / 100.0, float(settings.height) * float(y_percent) / 100.0


def _story_title_source(settings: ProjectSettings) -> str:
    title = str(getattr(settings, "title_text", "") or "").strip()
    if title:
        return title
    for line in str(getattr(settings, "text", "") or "").splitlines():
        line = line.strip()
        if line:
            return line[:90]
    return ""


def _load_foreground_sequence(settings: ProjectSettings) -> list[str]:
    # Kept for backward-compatible saved projects. Foreground folders are not
    # rotated inside one video; bulk import assigns one image to each queue item.
    return []


def _current_story_foreground_path(ctx: RenderContext, time_seconds: float) -> str:
    # Foreground folder selection is a bulk-import assignment helper only.
    # During preview/render, each project uses its own selected foreground_image_path.
    settings = ctx.settings
    return str(getattr(settings, "foreground_image_path", "") or "").strip()


def _draw_story_foreground(ctx: RenderContext, painter: QPainter, time_seconds: float) -> None:
    settings = ctx.settings
    if not bool(getattr(settings, "foreground_enabled", False)):
        return
    path = _current_story_foreground_path(ctx, time_seconds)
    if not path or not Path(path).exists():
        return
    cache = ctx.foreground_image_cache
    fg = cache.get(path) if isinstance(cache, dict) else None
    if fg is None:
        fg = QImage(path)
        if fg.isNull():
            return
        if isinstance(cache, dict):
            cache[path] = fg
    if fg.isNull():
        return
    target_w = max(20.0, float(settings.width) * max(1, min(100, int(getattr(settings, "foreground_width_percent", 39) or 39))) / 100.0)
    target_h = target_w * fg.height() / max(1, fg.width())
    cx, cy = _story_pct(settings, int(getattr(settings, "foreground_x_percent", 17) or 17), int(getattr(settings, "foreground_y_percent", 58) or 58))
    rect = QRectF(cx - target_w / 2.0, cy - target_h / 2.0, target_w, target_h)
    opacity = max(0.05, min(1.0, float(getattr(settings, "foreground_opacity_percent", 100) or 100) / 100.0))
    painter.save()
    painter.setOpacity(opacity)
    painter.drawImage(rect, fg, QRectF(0, 0, fg.width(), fg.height()))
    painter.restore()


def _highlight_title_html(settings: ProjectSettings, title: str) -> str:
    safe = html.escape(title)
    accent = str(getattr(settings, "title_accent_words", "") or "").strip()
    accent_color = str(getattr(settings, "title_accent_color", "#00D9E8") or "#00D9E8")
    if accent:
        for part in [p.strip() for p in re.split(r"[,|]+", accent) if p.strip()]:
            safe_part = html.escape(part)
            safe = safe.replace(safe_part, f"<span style='color:{accent_color};'>{safe_part}</span>")
    else:
        pieces = safe.split(" ", 1)
        if len(pieces) == 2:
            safe = f"<span style='color:{accent_color};'>{pieces[0]}</span> {pieces[1]}"
    direction = "rtl" if is_rtl_text(title, str(getattr(settings, "caption_language", "auto") or "auto")) else "ltr"
    return (
        '<html><body style="margin:0; padding:0; background:transparent;">'
        f'<div dir="{direction}" style="text-align:center; color:{getattr(settings, "title_color", "#FFFFFF")}; line-height:122%; white-space:normal;">'
        f'{safe}</div></body></html>'
    )


def _draw_story_title(painter: QPainter, settings: ProjectSettings) -> None:
    if not bool(getattr(settings, "title_enabled", False)):
        return
    title = _story_title_source(settings)
    if not title:
        return
    width = max(80.0, float(settings.width) * max(10, min(95, int(getattr(settings, "title_width_percent", 46) or 46))) / 100.0)
    font_size = max(8, int(getattr(settings, "title_font_size", 56) or 56))
    font_family = str(getattr(settings, "title_font_family", "Noto Naskh Arabic") or "Noto Naskh Arabic")
    doc = QTextDocument()
    doc.setDocumentMargin(0)
    font = QFont(font_family)
    font.setPointSize(font_size)
    font.setBold(True)
    doc.setDefaultFont(font)
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WordWrap)
    option.setAlignment(Qt.AlignmentFlag.AlignHCenter)
    option.setTextDirection(Qt.LayoutDirection.RightToLeft if is_rtl_text(title, str(getattr(settings, "caption_language", "auto") or "auto")) else Qt.LayoutDirection.LeftToRight)
    doc.setDefaultTextOption(option)
    doc.setTextWidth(width)
    doc.setHtml(_highlight_title_html(settings, title))
    doc.setTextWidth(width)
    h = max(1.0, float(doc.size().height()))
    cx, cy = _story_pct(settings, int(getattr(settings, "title_x_percent", 67) or 67), int(getattr(settings, "title_y_percent", 38) or 38))
    pad_x = max(12.0, width * 0.04)
    pad_y = max(8.0, h * 0.18)
    rect = QRectF(cx - width / 2.0 - pad_x, cy - h / 2.0 - pad_y, width + pad_x * 2.0, h + pad_y * 2.0)
    rect = rect.intersected(QRectF(0, 0, settings.width, settings.height))
    box_color = QColor(str(getattr(settings, "title_box_color", "#000000") or "#000000"))
    if not box_color.isValid():
        box_color = QColor("#000000")
    box_color.setAlpha(int(max(0, min(100, int(getattr(settings, "title_box_opacity_percent", 88) or 88))) / 100.0 * 255))
    radius = min(rect.width(), rect.height()) * max(0, min(100, int(getattr(settings, "title_box_radius_percent", 35) or 35))) / 200.0
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(box_color))
    painter.drawRoundedRect(rect, radius, radius)
    painter.translate(cx - width / 2.0, cy - h / 2.0)
    doc.drawContents(painter, QRectF(0, 0, width, h))
    painter.restore()


def _draw_story_waveform(painter: QPainter, settings: ProjectSettings, time_seconds: float) -> None:
    if not bool(getattr(settings, "waveform_enabled", False)):
        return
    cx, cy = _story_pct(settings, int(getattr(settings, "waveform_x_percent", 72) or 72), int(getattr(settings, "waveform_y_percent", 60) or 60))
    width = max(80.0, float(settings.width) * max(8, min(95, int(getattr(settings, "waveform_width_percent", 30) or 30))) / 100.0)
    height = max(18.0, float(settings.height) * max(3, min(50, int(getattr(settings, "waveform_height_percent", 14) or 14))) / 100.0)
    bars = max(8, min(96, int(getattr(settings, "waveform_bars", 38) or 38)))
    color = QColor(str(getattr(settings, "waveform_color", "#FFFFFF") or "#FFFFFF"))
    if not color.isValid():
        color = QColor("#FFFFFF")
    color.setAlpha(235)
    bar_w = max(2.0, width / (bars * 1.9))
    gap = (width - (bar_w * bars)) / max(1, bars - 1)
    left = cx - width / 2.0
    mid = cy
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(color))
    for i in range(bars):
        phase = (i * 0.55) + (float(time_seconds) * 4.2)
        amp = 0.28 + 0.72 * abs(math.sin(phase) * math.cos(phase * 0.37 + i * 0.11))
        bar_h = max(8.0, height * amp)
        x = left + i * (bar_w + gap)
        painter.drawRoundedRect(QRectF(x, mid - bar_h / 2.0, bar_w, bar_h), bar_w / 2.0, bar_w / 2.0)
    painter.restore()


def _draw_story_player_controls(painter: QPainter, settings: ProjectSettings, time_seconds: float, duration: float) -> None:
    if not bool(getattr(settings, "player_controls_enabled", False)):
        return
    cx, cy = _story_pct(settings, int(getattr(settings, "waveform_x_percent", 72) or 72), int(getattr(settings, "waveform_y_percent", 60) or 60) + 13)
    width = max(120.0, float(settings.width) * max(10, min(90, int(getattr(settings, "waveform_width_percent", 30) or 30))) / 100.0)
    line_y = cy - float(settings.height) * 0.035
    progress = 0.0 if duration <= 0 else max(0.0, min(1.0, float(time_seconds) / max(0.01, float(duration))))
    painter.save()
    line_color = QColor("#FFFFFF")
    line_color.setAlpha(230)
    painter.setPen(QPen(line_color, max(3.0, settings.height * 0.004), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.drawLine(QRectF(cx - width/2.0, line_y, width, 1).topLeft(), QRectF(cx + width/2.0, line_y, 1, 1).topLeft())
    prog_color = QColor("#FFFFFF")
    prog_color.setAlpha(255)
    painter.setPen(QPen(prog_color, max(4.0, settings.height * 0.005), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.drawLine(QRectF(cx - width/2.0, line_y, width * progress, 1).topLeft(), QRectF(cx - width/2.0 + width * progress, line_y, 1, 1).topLeft())

    icon_color = QColor("#FFFFFF")
    icon_color.setAlpha(235)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(icon_color))
    button_r = max(18.0, settings.height * 0.036)
    painter.drawEllipse(QRectF(cx - button_r, cy - button_r, button_r * 2.0, button_r * 2.0))
    # pause glyph in the central white circle
    dark = QColor("#111111")
    painter.setBrush(QBrush(dark))
    bar_w = button_r * 0.18
    bar_h = button_r * 0.75
    painter.drawRoundedRect(QRectF(cx - bar_w*1.7, cy - bar_h/2.0, bar_w, bar_h), bar_w/2.0, bar_w/2.0)
    painter.drawRoundedRect(QRectF(cx + bar_w*0.7, cy - bar_h/2.0, bar_w, bar_h), bar_w/2.0, bar_w/2.0)
    painter.restore()


def _draw_subscribe_badge(painter: QPainter, settings: ProjectSettings) -> None:
    if not bool(getattr(settings, "subscribe_badge_enabled", False)):
        return
    w = max(190.0, settings.width * 0.16)
    h = max(34.0, settings.height * 0.055)
    x = settings.width * 0.73
    y = settings.height * 0.055
    red = QColor("#FF0000")
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(red))
    painter.drawRoundedRect(QRectF(x, y, w, h), h * 0.18, h * 0.18)
    font = QFont("Arial")
    font.setPointSize(max(10, int(h * 0.38)))
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QColor("#FFFFFF"))
    painter.drawText(QRectF(x, y, w, h), Qt.AlignmentFlag.AlignCenter, "SUBSCRIBE")
    # left like circle and right bell circle
    circle = h * 0.78
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor("#FF0000")))
    painter.drawEllipse(QRectF(x - circle * 1.35, y + (h - circle) / 2, circle, circle))
    painter.setBrush(QBrush(QColor("#E5E7EB")))
    painter.drawEllipse(QRectF(x + w + circle * 0.25, y + (h - circle) / 2, circle, circle))
    icon_font = QFont("Arial")
    icon_font.setPointSize(max(9, int(h * 0.42)))
    icon_font.setBold(True)
    painter.setFont(icon_font)
    painter.setPen(QColor("#FFFFFF"))
    painter.drawText(QRectF(x - circle * 1.35, y + (h - circle) / 2, circle, circle), Qt.AlignmentFlag.AlignCenter, "👍")
    painter.setPen(QColor("#6B7280"))
    painter.drawText(QRectF(x + w + circle * 0.25, y + (h - circle) / 2, circle, circle), Qt.AlignmentFlag.AlignCenter, "🔔")
    painter.restore()


def _draw_story_visualizer_overlay(ctx: RenderContext, painter: QPainter, time_seconds: float) -> None:
    settings = ctx.settings
    if not bool(getattr(settings, "story_visualizer_enabled", False)):
        return
    _draw_story_foreground(ctx, painter, time_seconds)
    _draw_story_title(painter, settings)
    _draw_story_waveform(painter, settings, time_seconds)
    _draw_story_player_controls(painter, settings, time_seconds, ctx.duration)
    _draw_subscribe_badge(painter, settings)

def draw_frame_from_context(ctx: RenderContext, time_seconds: float) -> QImage:
    settings = ctx.settings
    image = _sequence_background_frame(ctx, time_seconds)
    painter = QPainter(image)
    painter.setRenderHints(
        QPainter.RenderHint.Antialiasing
        | QPainter.RenderHint.TextAntialiasing
        | QPainter.RenderHint.SmoothPixmapTransform,
        True,
    )

    painter.setClipRect(0, 0, settings.width, settings.height)
    _draw_story_visualizer_overlay(ctx, painter, time_seconds)
    if bool(getattr(settings, "captions_enabled", True)):
        if bool(getattr(settings, "caption_mode", False)):
            _draw_caption(ctx, painter, time_seconds)
        else:
            scroll_t = min(max(0.0, time_seconds), max(0.0, ctx.scroll_duration))
            y = ctx.start_y - ctx.speed * scroll_t
            x = _text_document_x(settings, ctx.doc_width)
            _draw_fixed_text_background_plate(painter, settings)
            _draw_cached_text_layer(ctx, painter, x, y)

    painter.end()
    return image

def draw_frame(settings: ProjectSettings, background: QImage, time_seconds: float) -> QImage:
    """Compatibility helper for external callers."""
    ctx = prepare_context(settings)
    ctx.background = background
    return draw_frame_from_context(ctx, time_seconds)


def qimage_to_rgb_bytes(image: QImage) -> bytes:
    img = image.convertToFormat(QImage.Format.Format_RGB888)
    width = img.width()
    expected_bpl = width * 3
    bpl = img.bytesPerLine()
    size = img.sizeInBytes()
    ptr = img.bits()
    try:
        ptr.setsize(size)
        data = bytes(ptr)
    except AttributeError:
        data = bytes(ptr)

    if bpl == expected_bpl:
        return data

    rows = []
    for y in range(img.height()):
        start = y * bpl
        rows.append(data[start : start + expected_bpl])
    return b"".join(rows)


def _format_eta(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _read_process_stderr(proc) -> str:
    """Best-effort FFmpeg stderr reader used when the rawvideo pipe closes.

    The previous renderer wrote directly to stdin and Windows sometimes surfaced
    FFmpeg startup/encoder failures as a plain OSError(22) at proc.stdin.write().
    Reading stderr here gives the user the real reason instead of a crash popup.
    """
    try:
        if proc.stderr is None:
            return ""
        return proc.stderr.read().decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _write_frame_to_ffmpeg(proc, data: bytes) -> None:
    """Write one RGB frame safely to FFmpeg.

    Windows pipes can be fragile with multi-megabyte writes, especially when a
    GPU encoder exits early. Writing in small chunks prevents Errno 22 and lets
    us show a clean FFmpegError when the encoder/process stops.
    """
    if proc.stdin is None:
        raise FFmpegError("FFmpeg pipe is not available.")

    view = memoryview(data)
    offset = 0
    # 2 MiB keeps Windows pipe writes safe while reducing write-call overhead
    # versus the old 1 MiB chunks on 1080p frames.
    chunk_size = 2 * 1024 * 1024
    while offset < len(view):
        if proc.poll() is not None:
            stderr = _read_process_stderr(proc)
            raise FFmpegError(stderr or f"FFmpeg stopped early with code {proc.returncode}.")
        end = min(offset + chunk_size, len(view))
        try:
            written = proc.stdin.write(view[offset:end])
        except (BrokenPipeError, OSError) as exc:
            stderr = ""
            if proc.poll() is not None:
                stderr = _read_process_stderr(proc)
            detail = stderr or str(exc) or exc.__class__.__name__
            raise FFmpegError(f"FFmpeg pipe write failed: {detail}") from exc
        if written is None:
            written = end - offset
        if written <= 0:
            raise FFmpegError("FFmpeg pipe stopped accepting video frames.")
        offset += int(written)



def _estimate_render_duration_fast(settings: ProjectSettings) -> float:
    """Cheap duration estimate used before background preparation.

    The old path called prepare_context() here, which builds fonts/text layers and can
    make queue items sit on "Starting" for a long time before any render progress.
    This estimate is good enough for deciding how many reusable background items to
    pick; the real duration is still calculated later during normal context setup.
    """
    try:
        audio_path = str(getattr(settings, "audio_path", "") or "").strip()
        if audio_path and os.path.exists(audio_path):
            voice_speed = max(0.25, min(4.0, float(getattr(settings, "voice_speed_percent", 100) or 100) / 100.0))
            duration = float(get_audio_duration(audio_path) or 0.0) / voice_speed
            if duration > 0:
                return max(1.0, duration + max(0.0, float(getattr(settings, "extra_end_seconds", 0.0) or 0.0)))
    except Exception:
        pass
    try:
        return max(1.0, float(getattr(settings, "duration_seconds", 30.0) or 30.0) + max(0.0, float(getattr(settings, "extra_end_seconds", 0.0) or 0.0)))
    except Exception:
        return 30.0


def _cycle_image_paths_for_backgrounds(images: list[Path], needed: int, randomize: bool) -> list[str]:
    if not images or needed <= 0:
        return []
    items = list(images)
    if randomize:
        random.shuffle(items)
    out: list[str] = []
    index = 0
    while len(out) < needed:
        out.append(str(items[index % len(items)]))
        index += 1
    return out


def _usable_existing_image_backgrounds(cache_dir: Path, needed: int, randomize: bool) -> list[str]:
    try:
        images = list_background_images(cache_dir)
    except Exception:
        images = []
    if not images:
        return []
    # For large queues, existing library images should be reused immediately.
    # Fill Library is the intentional download step; render should not block each
    # queue item by searching/downloading more Pexels images when the library exists.
    if randomize:
        try:
            from smart_scroll.pexels_rotation_v1 import select_images
            return select_images(images, max(1, needed), randomize=True, library=cache_dir)
        except Exception:
            # Preserve the last known-good renderer path if metadata is unavailable.
            pass
    return _cycle_image_paths_for_backgrounds(images, max(1, needed), randomize=randomize)


def render_video(
    settings: ProjectSettings,
    progress_callback: ProgressCallback | None = None,
    cancel_callback: CancelCallback | None = None,
) -> None:
    """Render an MP4 video using FFmpeg.

    Caption flow rule:
    - Default captions come from the selected voice/audio using Whisper voice-to-text.
    - If Whisper is unavailable or fails, captions are skipped; no script-duration
      fallback captions are generated.
    """
    captions_requested = bool(getattr(settings, "captions_enabled", True)) and bool(getattr(settings, "caption_mode", False))
    # Whisper is only valid for timed caption-video mode. Legacy scrolling-text
    # mode renders directly from settings.text and must never transcribe audio.
    if not bool(getattr(settings, "caption_mode", False)) and isinstance(getattr(settings, "metadata", None), dict):
        for key in (
            "caption_segments",
            "caption_generation_signature",
            "caption_detected_language",
            "caption_sync_engine",
            "caption_no_fallback",
            "caption_manual_ready",
        ):
            settings.metadata.pop(key, None)
    has_audio_for_captions = bool(getattr(settings, "audio_path", "") and os.path.exists(str(getattr(settings, "audio_path", ""))))
    if not settings.text.strip() and not (captions_requested and has_audio_for_captions):
        raise ValueError("Text is empty. Paste/import script text, or select a voice/audio file for voice-to-text captions.")
    if not settings.output_path:
        raise ValueError("Output path is empty.")

    final_output_path = str(Path(settings.output_path).with_suffix(".mp4"))
    final_output = Path(final_output_path)
    final_output.parent.mkdir(parents=True, exist_ok=True)
    settings.output_path = final_output_path
    safe_job_id = re.sub(r"[^A-Za-z0-9_-]+", "_", str(getattr(settings, "job_id", "") or final_output.stem))[:64] or "job"
    render_temp = final_output.with_name(f".{final_output.stem}.{safe_job_id}.__rendering__.mp4")
    try:
        render_temp.unlink(missing_ok=True)
    except TypeError:
        if render_temp.exists():
            render_temp.unlink()
    output_path = str(render_temp)
    pending_srt_segments: list[CaptionSegment] = []

    if bool(getattr(settings, "captions_enabled", True)) and bool(getattr(settings, "caption_mode", False)):
        try:
            audio_duration = get_audio_duration(settings.audio_path) if settings.audio_path and os.path.exists(settings.audio_path) else 0.0
        except Exception:
            audio_duration = 0.0
        voice_speed = max(0.25, min(4.0, getattr(settings, "voice_speed_percent", 100) / 100.0))
        caption_duration = (audio_duration / voice_speed) if audio_duration > 0 else max(1.0, float(getattr(settings, "duration_seconds", 30.0) or 30.0))
        sync_mode = str(getattr(settings, "caption_sync_mode", "better") or "better")
        caption_lang = str(getattr(settings, "caption_language", "auto") or "auto")
        caption_source = str(getattr(settings, "caption_text_source", "whisper") or "whisper").lower().strip()
        if caption_source not in {"whisper", "script"}:
            caption_source = "whisper"
        prefer_script_language = bool(getattr(settings, "caption_prefer_script_language", True)) and caption_source != "whisper"
        if not isinstance(getattr(settings, "metadata", None), dict):
            settings.metadata = {}

        current_caption_signature = caption_generation_signature(
            text=settings.text,
            audio_path=settings.audio_path,
            language=caption_lang,
            model_size=str(getattr(settings, "caption_whisper_model", "base") or "base"),
            max_words=int(getattr(settings, "caption_max_words", 8) or 8),
            offset=float(getattr(settings, "caption_timing_offset", 0.0) or 0.0),
            min_duration=float(getattr(settings, "caption_min_duration", 0.0) or 0.0) if bool(getattr(settings, "caption_heavy_enabled", False)) else 0.0,
            tighten_gaps=bool(getattr(settings, "caption_gap_tighten", False)) if bool(getattr(settings, "caption_heavy_enabled", False)) else False,
            voice_speed_percent=int(getattr(settings, "voice_speed_percent", 100) or 100),
            text_source=caption_source,
        )
        cached_signature = str((settings.metadata or {}).get("caption_generation_signature", "") or "")
        cached_segments: list[CaptionSegment] = []
        if cached_signature == current_caption_signature:
            for item in list((settings.metadata or {}).get("caption_segments", []) or []):
                try:
                    text = str(item.get("text", "") or "").strip()
                    if not text:
                        continue
                    cached_segments.append(
                        CaptionSegment(
                            start=float(item.get("start", 0.0) or 0.0),
                            end=float(item.get("end", 0.0) or 0.0),
                            text=text,
                        )
                    )
                except Exception:
                    continue
        elif (settings.metadata or {}).get("caption_segments") and progress_callback:
            progress_callback(0, "Saved caption cache is stale; captions will be regenerated.")

        if cached_segments:
            segments = cached_segments
            detected_language = str((settings.metadata or {}).get("caption_detected_language", caption_lang) or caption_lang)
            sync_engine = str((settings.metadata or {}).get("caption_sync_engine", "manual-whisper-cache") or "manual-whisper-cache")
            if progress_callback:
                progress_callback(0, f"Using existing generated captions: {len(segments)} segments | engine={sync_engine}")
        else:
            if progress_callback:
                if caption_source == "whisper":
                    progress_callback(0, f"Preparing captions from default voice-to-text transcript (language={caption_lang})...")
                elif sync_mode == "better" and settings.audio_path and os.path.exists(settings.audio_path):
                    progress_callback(0, f"Preparing captions from script words + selected voice timing (language={caption_lang})...")
                else:
                    progress_callback(0, f"Preparing captions from script timing (language={caption_lang})...")
            try:
                segments, detected_language, sync_engine = build_caption_segments(
                    text=settings.text,
                    audio_path=settings.audio_path if settings.audio_path else None,
                    duration=caption_duration,
                    sync_mode=sync_mode,
                    language=caption_lang,
                    max_words=int(getattr(settings, "caption_max_words", 8) or 8),
                    offset=float(getattr(settings, "caption_timing_offset", 0.0) or 0.0),
                    text_source=caption_source,
                    prefer_script_language=prefer_script_language,
                    model_size=str(getattr(settings, "caption_whisper_model", "base") or "base"),
                    min_duration=float(getattr(settings, "caption_min_duration", 0.0) or 0.0) if bool(getattr(settings, "caption_heavy_enabled", False)) else 0.0,
                    tighten_gaps=bool(getattr(settings, "caption_gap_tighten", False)) if bool(getattr(settings, "caption_heavy_enabled", False)) else False,
                    progress_callback=progress_callback,
                    cancel_callback=cancel_callback,
                )
            except Exception as exc:
                raise FFmpegError(f"Caption generation failed; render was stopped instead of exporting without captions: {exc}") from exc
        if cancel_callback and cancel_callback():
            raise RenderCancelled("Render cancelled by user.")
        if not segments:
            raise FFmpegError("Caption generation returned no usable segments; render was stopped to prevent a captionless export.")
        settings.metadata["caption_no_fallback"] = False
        settings.metadata["caption_generation_signature"] = current_caption_signature
        settings.metadata["caption_segments"] = [seg.to_dict() for seg in segments]
        settings.metadata["caption_detected_language"] = detected_language
        settings.metadata["caption_sync_engine"] = sync_engine
        if bool(getattr(settings, "caption_export_srt", True)):
            # Write the sidecar only after the video itself passes structural
            # validation, so failed renders cannot leave a fresh misleading SRT.
            pending_srt_segments = list(segments)
        if progress_callback:
            progress_callback(0, f"Captions ready: {len(segments)} segments | engine={sync_engine} | detected={detected_language}")

    if getattr(settings, "pexels_enabled", False) and (
        getattr(settings, "auto_prepare_backgrounds", True) or not (getattr(settings, "pexels_cache_paths", []) or [])
    ):
        use_images = bool(getattr(settings, "use_image_backgrounds", True))
        use_videos = bool(getattr(settings, "use_video_backgrounds", False))
        # Live mode always downloads fresh media. Library mode may download only to refill shortages when a saved key exists.
        api_key_available = bool(str(getattr(settings, "pexels_api_key", "") or "").strip())
        allow_pexels_download = bool(getattr(settings, "pexels_download_enabled", True)) or (bool(getattr(settings, "pexels_use_library", True)) and api_key_available)
        if not use_images and not use_videos:
            raise ValueError("Backgrounds are enabled, but both Image Backgrounds and Video Backgrounds are off.")

        if progress_callback:
            progress_callback(0, "Preparing background media list...")

        duration_estimate = _estimate_render_duration_fast(settings)
        image_interval = max(1.0, float(getattr(settings, "pexels_interval_seconds", 8.0) or 8.0))
        video_interval = max(1.0, float(getattr(settings, "pexels_video_interval_seconds", 4.0) or 4.0))
        video_frame_fps = max(1.0, min(12.0, float(getattr(settings, "pexels_video_frame_fps", 6.0) or 6.0)))
        narration_timing = str(getattr(settings, "image_timing_mode", "fixed") or "fixed").strip().lower() == "narration"
        settings.metadata = dict(getattr(settings, "metadata", {}) or {})
        scene_plan = build_scene_sync_plan(
            str(getattr(settings, "text", "") or ""),
            caption_segments=list((settings.metadata or {}).get("caption_segments", []) or []),
            total_duration=duration_estimate,
            requested_limit=int(getattr(settings, "scene_sync_limit", 0) or 0),
        ) if narration_timing and use_images else []
        if scene_plan:
            settings.metadata["scene_sync_plan"] = scene_plan
            image_needed = len(scene_plan)
            # Exact scene-tag matching uses one still image per scene. Video mixing
            # is intentionally skipped in narration mode because frame expansion
            # breaks the one-scene/one-media timing contract.
            video_needed = 0
            if progress_callback:
                summary = summarize_scene_sync(scene_plan)
                progress_callback(0, f"Narration-based scene sync: {summary.get('label', image_needed)}")
                if use_videos:
                    progress_callback(0, "Narration scene sync: video mix skipped to preserve exact sentence/tag/image order.")
        else:
            extra_buffer = 1
            if use_images and use_videos:
                cycle = image_interval + video_interval
                image_needed = max(1, int(math.ceil(duration_estimate / cycle))) + extra_buffer
                video_needed = max(1, int(math.ceil(duration_estimate / cycle))) + extra_buffer
            else:
                image_needed = estimate_pexels_needed(duration_estimate, image_interval) + extra_buffer if use_images else 0
                video_needed = estimate_pexels_needed(duration_estimate, video_interval) + extra_buffer if use_videos else 0

        if getattr(settings, "pexels_use_library", True):
            library_folder = str(getattr(settings, "pexels_library_folder", "") or "").strip()
            cache_dir = resolve_pexels_library_dir(library_folder)
        else:
            # Live mode must not silently fall back to the user's reusable storage
            # folder. Keep only a job-scoped temporary cache so retries are stable,
            # while each new queued video receives a fresh live Pexels selection.
            safe_project = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(getattr(settings, "project_name", "project") or "project"))[:60]
            identity = str(getattr(settings, "job_id", "") or getattr(settings, "output_path", "") or safe_project)
            live_token = hashlib.sha256(identity.encode("utf-8", errors="replace")).hexdigest()[:12]
            cache_dir = app_root() / "temp" / "pexels_live" / f"{safe_project}_{live_token}"

        image_paths: list[str] = []
        video_frames: list[str] = []

        if use_images and image_needed > 0:
            randomize_images = bool(getattr(settings, "pexels_randomize_library", True))
            existing_unique = list_background_images(cache_dir)
            existing_images = _usable_existing_image_backgrounds(cache_dir, image_needed, randomize=randomize_images)
            library_short = len(existing_unique) < image_needed
            should_refill = bool(getattr(settings, "pexels_use_library", True)) and library_short and allow_pexels_download
            fresh_live = not bool(getattr(settings, "pexels_use_library", True))
            if existing_images and not should_refill and not fresh_live:
                image_paths = existing_images
                if progress_callback:
                    progress_callback(0, f"Using {len(set(image_paths))} reusable image background(s) from library.")
            else:
                if not allow_pexels_download:
                    if existing_images:
                        image_paths = existing_images
                    else:
                        raise ValueError("No local background images found. Add images to the library or save a Pexels API key for automatic refill.")
                else:
                    if progress_callback:
                        action = "Refilling library" if should_refill else "Downloading fresh image backgrounds"
                        progress_callback(0, f"{action}: need {image_needed}, found {len(existing_unique)} unique image(s).")
                    if scene_plan:
                        details = {"manual_keywords": [], "script_keywords": [str(item.get("query", "")) for item in scene_plan]}
                        queries = [str(item.get("query", "")).strip() for item in scene_plan if str(item.get("query", "")).strip()]
                    else:
                        details = explain_story_queries(
                            settings.text,
                            str(getattr(settings, "pexels_query", "") or ""),
                            not bool(getattr(settings, "pexels_auto_keywords", True)),
                            target_count=max(image_needed, 1),
                        )
                        queries = list(details.get("final_queries") or [])
                    if progress_callback:
                        manual = details.get("manual_keywords") or []
                        script_keywords = details.get("script_keywords") or []
                        if manual:
                            progress_callback(0, "Pexels manual keywords: " + ", ".join(manual))
                        elif script_keywords:
                            progress_callback(0, "Pexels detected script keywords: " + ", ".join(script_keywords))
                        else:
                            progress_callback(0, "Pexels detected script keywords: none")
                        progress_callback(0, "Pexels final search queries: " + (", ".join(queries) if queries else "none"))
                    downloaded_images = download_pexels_backgrounds(
                        api_key=str(getattr(settings, "pexels_api_key", "") or "").strip(),
                        queries=queries,
                        needed=image_needed,
                        cache_dir=cache_dir,
                        progress_callback=progress_callback,
                        cancel_callback=cancel_callback,
                        use_existing=bool(getattr(settings, "pexels_use_library", True)),
                        randomize=randomize_images,
                        min_library_size=image_needed,
                    )
                    image_paths = [str(p) for p in downloaded_images]

        if scene_plan and image_paths:
            tagged_root = cache_dir / "_scene_tagged_current"
            image_paths, scene_plan = apply_scene_media_tags(scene_plan, image_paths, tagged_root)
            settings.metadata["scene_sync_plan"] = scene_plan
            if progress_callback:
                progress_callback(0, f"Scene tags matched to image media: {len(image_paths)} item(s)")

        if use_videos and video_needed > 0:
            if progress_callback:
                progress_callback(0, f"Video backgrounds needed: {video_needed} | clip interval {video_interval:g}s | motion fps {video_frame_fps:g}")
            local_video_paths = [str(p) for p in (getattr(settings, "background_video_paths", []) or []) if Path(str(p)).exists()]
            details = explain_story_queries(
                settings.text,
                str(getattr(settings, "pexels_query", "") or ""),
                not bool(getattr(settings, "pexels_auto_keywords", True)),
                target_count=max(video_needed, 1),
            )
            queries = list(details.get("final_queries") or [])
            downloaded_video_frames = prepare_pexels_video_background_frames(
                api_key=(str(getattr(settings, "pexels_api_key", "") or "").strip() if allow_pexels_download else ""),
                queries=queries,
                needed=video_needed,
                cache_dir=cache_dir,
                interval_seconds=video_interval,
                frame_fps=video_frame_fps,
                target_width=int(getattr(settings, "width", 1280) or 1280),
                target_height=int(getattr(settings, "height", 720) or 720),
                progress_callback=progress_callback,
                cancel_callback=cancel_callback,
                randomize=bool(getattr(settings, "pexels_randomize_library", True)),
                local_video_paths=local_video_paths,
            )
            video_frames = [str(p) for p in downloaded_video_frames]

        prepared_paths: list[str] = []
        if use_images and use_videos and video_frames:
            frames_per_segment = max(1, int(math.ceil(video_interval * video_frame_fps)))
            video_segments = [video_frames[i:i + frames_per_segment] for i in range(0, len(video_frames), frames_per_segment)]
            mix_mode = str(getattr(settings, "pexels_mix_mode", "alternate") or "alternate")
            image_index = 0
            video_index = 0
            pattern = {
                "alternate": ("image", "video"),
                "image_heavy": ("image", "image", "video"),
                "video_heavy": ("image", "video", "video"),
            }.get(mix_mode, ("image", "video"))
            total_slots = max(len(image_paths) + len(video_segments), 1)
            if mix_mode == "random":
                import random as _random
                seed = int(getattr(settings, "visual_motion_seed", 0) or 0)
                rng = _random.Random(seed or 8675309)
                slots = ["image"] * len(image_paths) + ["video"] * len(video_segments)
                rng.shuffle(slots)
            else:
                slots = [pattern[i % len(pattern)] for i in range(total_slots)]
            for kind in slots:
                if kind == "image" and image_paths:
                    prepared_paths.append(image_paths[image_index % len(image_paths)])
                    image_index += 1
                elif kind == "video" and video_segments:
                    prepared_paths.extend(video_segments[video_index % len(video_segments)])
                    video_index += 1
            while image_index < len(image_paths):
                prepared_paths.append(image_paths[image_index]); image_index += 1
            while video_index < len(video_segments):
                prepared_paths.extend(video_segments[video_index]); video_index += 1
        else:
            prepared_paths.extend(image_paths)
            prepared_paths.extend(video_frames)

        settings.pexels_cache_paths = prepared_paths
        if progress_callback:
            if scene_plan:
                progress_callback(0, f"Scene-matched background media ready: {len(prepared_paths)} item(s) for {len(scene_plan)} scene(s)")
            else:
                progress_callback(0, f"Background media ready: {len(prepared_paths)} item(s)")




    if progress_callback:
        progress_callback(0, "Preparing render context/text cache...")
    ctx = prepare_context(settings)
    total_frames = max(1, int(math.ceil(ctx.duration * settings.fps)))
    total_video_time = _format_eta(ctx.duration)

    if progress_callback:
        if ctx.text_layer is not None and not ctx.text_layer.isNull():
            cache_note = "cached text layer ready"
        elif int(ctx.text_layer_width or 0) > 0 and int(ctx.text_layer_height or 0) > 0:
            cache_note = "large text tile cache ready"
        else:
            cache_note = "compatible fallback mode"
        progress_callback(0, f"Prepared {total_frames} frames | Video {total_video_time} | {cache_note}")

    requested_video_encoder = str(getattr(settings, "video_encoder", "cpu_x264") or "cpu_x264")
    prepared_audio_path = str(getattr(settings, "render_audio_path", "") or "")
    has_prepared_audio = bool(prepared_audio_path and os.path.exists(prepared_audio_path))
    selected_audio_path = prepared_audio_path if has_prepared_audio else (
        settings.audio_path if settings.audio_path and os.path.exists(settings.audio_path) else None
    )
    proc = start_ffmpeg_rawvideo(
        width=settings.width,
        height=settings.height,
        fps=settings.fps,
        output_path=output_path,
        audio_path=selected_audio_path,
        crf=settings.quality_crf,
        preset=settings.ffmpeg_preset,
        audio_volume=(1.0 if has_prepared_audio else getattr(settings, "voice_volume_percent", 100) / 100.0),
        audio_speed=(1.0 if has_prepared_audio else getattr(settings, "voice_speed_percent", 100) / 100.0),
        video_encoder=requested_video_encoder,
    )
    registered_thread_id = _register_running_render_process(proc)

    render_started_at = time.perf_counter()
    last_progress_emit_at = 0.0
    warmup_frames = max(30, int(settings.fps * 2))

    try:
        assert proc.stdin is not None
        for frame_index in range(total_frames):
            if cancel_callback and cancel_callback():
                cancel_running_render_process(registered_thread_id)
                raise RenderCancelled("Render cancelled by user.")
            t = frame_index / float(settings.fps)
            frame = draw_frame_from_context(ctx, t)
            _write_frame_to_ffmpeg(proc, qimage_to_rgb_bytes(frame))
            if progress_callback:
                now = time.perf_counter()
                if frame_index % max(1, settings.fps) == 0 or frame_index == total_frames - 1 or (now - last_progress_emit_at) >= 0.75:
                    done_frames = frame_index + 1
                    progress_value = done_frames / total_frames * 100.0
                    progress = max(0, min(100, int(progress_value)))
                    elapsed = max(0.0, now - render_started_at)
                    fps_done = done_frames / elapsed if elapsed > 0.1 else 0.0
                    if done_frames < warmup_frames or elapsed < 2.0 or fps_done <= 0:
                        eta_text = "calculating..."
                    else:
                        remaining = (total_frames - done_frames) / fps_done
                        eta_text = _format_eta(remaining)
                    progress_callback(
                        progress,
                        f"Frame {done_frames}/{total_frames} | {progress_value:.1f}% | Speed {fps_done:.1f} FPS | Elapsed {_format_eta(elapsed)} | ETA {eta_text}",
                    )
                    last_progress_emit_at = now
        proc.stdin.close()
        stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
        return_code = proc.wait()
        if return_code != 0:
            raise FFmpegError(stderr.strip() or f"FFmpeg failed with code {return_code}.")
    except Exception as exc:
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass
        _unregister_running_render_process(registered_thread_id)
        try:
            render_temp.unlink(missing_ok=True)
        except Exception:
            pass

        # Real-world fix for the user's Errno 22 crash: many FFmpeg builds list
        # GPU encoders even when the current PC/driver cannot actually start
        # them. In Auto mode, retry once with stable CPU x264 instead of showing
        # a pipe-write crash.
        if isinstance(exc, FFmpegError) and requested_video_encoder.strip().lower() == "auto":
            if progress_callback:
                progress_callback(0, "Auto GPU encoder failed; retrying safely with CPU x264...")
            previous_encoder = getattr(settings, "video_encoder", "auto")
            try:
                settings.video_encoder = "cpu_x264"
                render_video(settings, progress_callback=progress_callback, cancel_callback=cancel_callback)
                return
            finally:
                settings.video_encoder = previous_encoder
        raise

    _unregister_running_render_process(registered_thread_id)
    try:
        # Validate the temporary file before replacing any previous good output.
        # The worker performs the stricter duration check after optional compression.
        validate_video_file(
            render_temp,
            require_audio=bool(selected_audio_path),
            min_size_bytes=4096,
        )
    except Exception as exc:
        try:
            render_temp.unlink(missing_ok=True)
        except Exception:
            pass
        raise FFmpegError(f"Temporary render verification failed; existing output was preserved: {exc}") from exc
    os.replace(render_temp, final_output)
    settings.output_path = final_output_path
    if pending_srt_segments:
        srt_path = str(final_output.with_suffix(".srt"))
        write_srt(pending_srt_segments, srt_path)
        settings.metadata["caption_srt_path"] = srt_path
    if progress_callback:
        progress_callback(100, f"Done: {final_output_path}")

def preview_frame(settings: ProjectSettings, time_seconds: float = 0.0) -> QImage:
    """Render one preview frame at an exact timeline time in seconds."""
    ctx = prepare_context(settings)
    t = max(0.0, min(float(time_seconds), ctx.duration))
    return draw_frame_from_context(ctx, t)
