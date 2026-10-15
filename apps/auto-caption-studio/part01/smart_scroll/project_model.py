"""Project settings model."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
from typing import Any

from .persistence import atomic_write_json, load_json_with_backup


@dataclass
class ProjectSettings:
    text: str = ""
    audio_path: str = ""
    output_path: str = ""
    output_folder: str = ""

    width: int = 1280
    height: int = 720
    fps: int = 24
    duration_seconds: float = 30.0

    font_family: str = "Jameel Noori Nastaleeq Kasheeda"
    font_size: int = 54
    bold: bool = False
    underline: bool = False
    italic: bool = False
    text_case: str = "normal"  # normal, upper, lower, title
    character_spacing: int = 0
    text_line_spacing: int = 0
    text_color: str = "#FFFFFF"
    text_opacity: float = 1.0

    # Readability plate / box behind scrolling text
    text_background_enabled: bool = True
    text_background_color: str = "#000000"
    text_background_opacity_percent: int = 45
    # Explicit web scrolling background mode. Empty keeps legacy behavior for old projects.
    # off = none, whole = one box around the complete text document, follow = one box per wrapped visual line.
    text_background_mode: str = ""
    text_background_padding: int = 24  # legacy compatibility / fallback
    text_background_padding_x: int = 24
    text_background_padding_y: int = 24
    text_background_radius: int = 18
    text_background_radius_percent: int = 40
    text_background_width_percent: int = 92
    text_background_height_percent: int = 32
    text_background_x_percent: int = 50
    text_background_y_percent: int = 50
    text_background_follow_text: bool = False
    text_background_style_name: str = "none"

    alignment: str = "center"  # left, center, right, justify
    direction: str = "auto"  # legacy/backward compatible; UI auto-detects direction

    margin_left: int = 100
    margin_right: int = 100
    margin_top: int = 80
    margin_bottom: int = 80
    line_height_percent: int = 135
    paragraph_spacing: int = 26
    text_width_percent: int = 85
    text_x_percent: int = 50

    # CapCut/Canva-style transform controls for the text layer
    text_scale_width_percent: int = 100
    text_scale_height_percent: int = 100
    text_uniform_scale: bool = True
    text_offset_x_percent: int = 0
    text_offset_y_percent: int = 0
    text_rotation_degrees: float = 0.0

    shadow_enabled: bool = True
    shadow_color: str = "#000000"
    shadow_offset_x: int = 3
    shadow_offset_y: int = 3
    outline_enabled: bool = False
    outline_color: str = "#000000"
    outline_width: int = 2

    # Master switch for manually selected solid/gradient/image background controls.
    # When False, renderer ignores custom background fields and uses rotating local/Pexels media or a safe default gradient.
    custom_background_enabled: bool = True
    background_mode: str = "gradient"  # solid, gradient, image
    background_color: str = "#101018"
    gradient_color_1: str = "#101018"
    gradient_color_2: str = "#283048"
    background_image_path: str = ""
    background_blur: int = 0
    background_brightness: int = 100
    dark_overlay_percent: int = 35

    auto_sync_audio: bool = True
    manual_speed_px_per_sec: float = 80.0

    # Auto Caption Video Studio mode
    # captions_enabled is a master visibility switch for caption/text overlay.
    captions_enabled: bool = True
    # caption_mode turns the old scrolling text renderer into a caption/subtitle renderer.
    caption_mode: bool = True
    caption_sync_mode: str = "better"  # better, fast
    caption_language: str = "auto"     # auto or Whisper language code
    caption_text_source: str = "whisper" # whisper, script; default uses voice-to-text transcript
    caption_prefer_script_language: bool = True
    caption_position: str = "bottom"   # top, center, bottom
    caption_max_words: int = 8
    caption_timing_offset: float = 0.0
    caption_export_srt: bool = True

    # Heavy Caption Lab: stronger Shorts/Reels/TikTok style captions.
    # These are optional and remain backward-compatible with old projects.
    caption_heavy_enabled: bool = False
    caption_heavy_preset: str = "story_pro"  # story_pro, viral_pop, glass, documentary, minimal
    caption_whisper_model: str = "base"       # tiny, base, small, medium, large-v3, turbo
    caption_karaoke_enabled: bool = False
    caption_highlight_color: str = "#FFD54A"
    caption_pop_percent: int = 16
    caption_min_duration: float = 0.65
    caption_gap_tighten: bool = True

    # Story Visualizer / YouTube narration layout (CapCut-style layered editor)
    story_visualizer_enabled: bool = False
    story_visualizer_template: str = "none"  # none, arabic_story, custom
    foreground_enabled: bool = False
    foreground_image_path: str = ""
    foreground_use_folder_rotation: bool = False
    foreground_folder_path: str = ""
    foreground_x_percent: int = 17
    foreground_y_percent: int = 58
    foreground_width_percent: int = 39
    foreground_opacity_percent: int = 100
    title_enabled: bool = False
    title_text: str = ""
    title_accent_words: str = ""
    title_x_percent: int = 67
    title_y_percent: int = 38
    title_width_percent: int = 46
    title_font_family: str = "Noto Naskh Arabic"
    title_font_size: int = 56
    title_color: str = "#FFFFFF"
    title_accent_color: str = "#00D9E8"
    title_box_color: str = "#000000"
    title_box_opacity_percent: int = 88
    title_box_radius_percent: int = 35
    waveform_enabled: bool = False
    waveform_x_percent: int = 72
    waveform_y_percent: int = 60
    waveform_width_percent: int = 30
    waveform_height_percent: int = 14
    waveform_bars: int = 38
    waveform_color: str = "#FFFFFF"
    player_controls_enabled: bool = False
    subscribe_badge_enabled: bool = False

    start_mode: str = "voice_centered"  # legacy scrolling mode: classic, immediate, centered, voice_centered
    extra_start_seconds: float = 0.0
    extra_end_seconds: float = 0.0

    voice_volume_percent: int = 100
    voice_speed_percent: int = 100
    voice_processing_preset: str = "natural"  # off, natural, clear, strong

    # Legacy background-music fields retained only for loading old project files.
    # The UI and render pipeline permanently force this feature off.
    music_enabled: bool = False
    music_folder_path: str = ""
    music_paths: list[str] = field(default_factory=list)
    music_rotation_mode: str = "sequential"  # sequential, random, shuffle
    music_remember_position: bool = True
    music_assigned_path: str = ""
    music_volume_percent: int = 12
    music_ducking_enabled: bool = True
    music_ducking_strength: str = "medium"  # light, medium, strong
    music_normalize: bool = True
    music_loop_if_short: bool = True
    music_trim_if_long: bool = True
    music_loop_crossfade_seconds: float = 0.5
    music_fade_in_seconds: float = 1.5
    music_fade_out_seconds: float = 2.0

    # Transient final mix path. Captions continue to use audio_path.
    render_audio_path: str = ""

    # Stable queue/job state persisted across restarts.
    job_id: str = ""
    queue_retry_count: int = 0
    queue_max_retries: int = 1
    queue_stage: str = "Pending"
    queued_at: str = ""

    # One-click automation
    auto_pipeline_enabled: bool = True
    auto_generate_voice: bool = True
    auto_prepare_backgrounds: bool = True

    # Local PiperTTS voice generation
    piper_model_path: str = ""
    piper_speaker: str = ""
    piper_length_scale: str = ""
    piper_output_name: str = "Untitled"

    # Pexels story backgrounds
    pexels_enabled: bool = False
    # True = app may call the Pexels API when local folder/library is short.
    # False = strictly use local folder/library/background videos only.
    pexels_download_enabled: bool = True
    pexels_api_key: str = ""
    pexels_query: str = ""
    pexels_auto_keywords: bool = True
    use_image_backgrounds: bool = True
    use_video_backgrounds: bool = False
    pexels_interval_seconds: float = 8.0
    # Independent background behavior controls.
    single_background_enabled: bool = False
    motion_enabled: bool = True
    # Professional Ken Burns style motion for still image backgrounds.
    # Speed controls loop pace/style, intensity controls zoom/pan strength.
    pexels_motion_speed_percent: int = 100
    pexels_motion_intensity_percent: int = 42

    # Batch 2 visual engine. Existing speed/intensity controls remain supported.
    visual_motion_style: str = "cinematic"  # very_slow, cinematic, dynamic
    visual_motion_seed: int = 0
    visual_avoid_repeated_motion: bool = True
    visual_quality_protection: bool = True
    visual_smart_rotation: bool = True
    image_transition_mode: str = "crossfade"  # cut, crossfade, dissolve, push, fade_black, varied
    image_transition_seconds: float = 0.55
    image_timing_mode: str = "fixed"  # fixed, narration
    image_timing_min_seconds: float = 4.0
    image_timing_max_seconds: float = 10.0
    # 0 = Auto; otherwise cap narration scene detection.
    scene_sync_limit: int = 0

    pexels_video_interval_seconds: float = 4.0
    pexels_mix_mode: str = "alternate"  # alternate, random, image_heavy, video_heavy
    pexels_video_frame_fps: float = 6.0  # internal motion frame rate for Pexels video backgrounds
    background_video_paths: list[str] = field(default_factory=list)
    pexels_cache_paths: list[str] = field(default_factory=list)
    pexels_use_library: bool = True
    pexels_library_folder: str = ""
    pexels_randomize_library: bool = True
    pexels_min_library_images: int = 1

    quality_crf: int = 23
    ffmpeg_preset: str = "ultrafast"
    # Export encoding is fixed to CPU x264/H.264 for maximum compatibility on older PCs.
    video_encoder: str = "cpu_x264"
    single_paragraph: bool = True

    # Post-render compression. When enabled, the final MP4 is compressed in-place
    # after each render before the next queue item starts.
    post_render_compress_enabled: bool = False
    post_render_compress_quality: str = "Balanced Fast"
    post_render_compress_encoder: str = "CPU Fast (Reliable)"
    post_render_compress_overwrite_only_smaller: bool = True
    post_render_compress_keep_failed_temp: bool = False

    project_name: str = "Untitled"
    queue_status: str = "Pending"
    queue_note: str = ""

    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectSettings":
        allowed = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in allowed}
        return cls(**filtered)


def save_project(settings: ProjectSettings, path: str) -> None:
    target = Path(path)
    backup = target.with_name(target.name + ".backup")
    atomic_write_json(target, settings.to_dict(), backup_path=backup, indent=2)


def load_project(path: str) -> ProjectSettings:
    target = Path(path)
    backup = target.with_name(target.name + ".backup")
    data, _loaded_from = load_json_with_backup(target, backup)
    if not isinstance(data, dict):
        raise ValueError("Project file does not contain a valid settings object.")
    return ProjectSettings.from_dict(data)
