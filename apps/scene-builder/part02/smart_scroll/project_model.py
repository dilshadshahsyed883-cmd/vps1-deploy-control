"""Project settings model."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import uuid
import json
from pathlib import Path
from typing import Any

from .persistence import atomic_write_json, load_json_with_backup


@dataclass
class ProjectSettings:
    project_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    text: str = ""
    audio_path: str = ""
    output_path: str = ""
    output_folder: str = ""

    width: int = 1280
    height: int = 720
    fps: int = 24
    # Existing saved projects without these fields retain the legacy crop layout.
    canvas_fill_mode: str = "crop"  # blur, black, solid, stretch, crop
    canvas_blur_strength: int = 16
    canvas_fill_color: str = "#101018"
    # CapCut Controls. Old saved project JSON defaults to no extra effects.
    capcut_mirror_enabled: bool = False
    capcut_mirror_percent: int = 0
    capcut_zoom_level: int = 100
    capcut_noise_enabled: bool = False
    capcut_noise_effect: str = "film"
    capcut_noise_intensity: int = 35
    capcut_noise_percent: int = 40
    capcut_noise_target: str = "both"
    duration_seconds: float = 30.0

    font_family: str = "Jameel Noori Nastaleeq Kasheeda"
    font_size: int = 48
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
    text_background_padding: int = 24  # legacy/follow-text mode
    text_background_radius: int = 18   # legacy/follow-text mode
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
    caption_style: str = "karaoke"
    caption_font_path: str = ""

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

    # One background music track is assigned to each queued video.
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

    # V17.4 permanent-library playlist and documentary audio policy.  Entries
    # are stable library references, not copied project files.
    music_playlist: list[dict[str, Any]] = field(default_factory=list)
    music_ai_select_from_library: bool = False
    music_ai_allowed_categories: list[str] = field(default_factory=list)
    music_use_track1_for_intro: bool = True
    music_allow_intro_track_in_main: bool = False
    music_playlist_loop: bool = True
    music_track_crossfade_seconds: float = 1.5

    # Keyword-driven reusable SFX policy.  Scene plan supplies editorial intent;
    # the preset controls whether/how aggressively the local library is used.
    sfx_enabled: bool = False
    environment_sfx_enabled: bool = True
    transition_sfx_library_enabled: bool = True
    sfx_amount: str = "medium"  # low, medium, high
    environment_sfx_volume_percent: int = 18
    transition_sfx_volume_percent: int = 24

    # Transient final mix path. Captions continue to use audio_path.
    render_audio_path: str = ""

    # Stable queue/job state persisted across restarts.
    job_id: str = ""
    queue_retry_count: int = 0
    queue_max_retries: int = 0
    queue_stage: str = "Pending"
    queued_at: str = ""

    # One-click automation
    auto_pipeline_enabled: bool = True
    auto_generate_voice: bool = True
    auto_prepare_backgrounds: bool = True

    # Voice Profile + optional Voice Review quality gate. These fields are frozen per
    # project/batch and do not alter the existing render path unless review is enabled.
    voice_profile_id: str = ""
    voice_profile_name: str = ""
    voice_provider: str = ""
    voice_name: str = ""
    voice_id: str = ""
    voice_language: str = ""
    voice_generation_settings: dict[str, Any] = field(default_factory=dict)
    voice_review_required: bool = False
    voice_review_status: str = "not_required"
    voice_approved: bool = False
    voice_locked: bool = False

    # Optional Visual Quality Gate. When enabled, final render requires every scene to be
    # approved+locked. Scene review state itself is stored on each scene in metadata.
    scene_review_required: bool = False

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

    # AI Media (Gemini/external generated stills). First version keeps the
    # existing Pexels/image pipeline intact and simply swaps the source folder
    # when AI Media is enabled.
    ai_media_enabled: bool = False
    ai_media_provider: str = "gemini"
    ai_media_apps_script_url: str = ""
    ai_media_prompt_template: str = "{scene_text}"
    ai_media_style_instructions: str = ""
    ai_media_output_folder: str = ""
    ai_media_use_scene_text_fallback: bool = True
    ai_media_images_per_scene: int = 1
    ai_media_retry_count: int = 2
    ai_media_timeout_seconds: int = 180
    ai_media_require_scene_id_match: bool = True
    ai_media_auto_match: bool = True
    ai_scene_limit: int = 0
    ai_scene_mode: str = "narration_auto"
    ai_scene_target_seconds: float = 4.5

    # Quality Timing / Hook Director (PC-first v1)
    quality_timing_enabled: bool = True
    max_scene_seconds: float = 4.0
    # V17.5d stage-aware media pacing. General values remain compatible with
    # max_scene_seconds/minimum_scene_seconds metadata used by older projects.
    intro_media_min_seconds: float = 1.5
    intro_media_max_seconds: float = 3.0
    hook_media_min_seconds: float = 1.5
    hook_media_max_seconds: float = 3.0
    general_media_min_seconds: float = 2.0
    general_media_max_seconds: float = 4.0
    # R6 uploaded-audio master timeline. Auto preserves the first real spoken
    # Whisper timestamp; Manual uses the explicit seconds offset from the file start.
    narration_start_mode: str = "auto"  # auto, manual
    narration_start_manual_seconds: float = 0.0
    opening_hook_enabled: bool = True
    opening_hook_seconds: float = 30.0
    opening_hook_target_seconds: float = 2.5

    # V17.5d Pexels candidate-selection policy.
    pexels_selection_mode: str = "best_match_variety"
    pexels_search_pool: int = 10
    pexels_avoid_used_media: bool = True
    pexels_avoid_same_creator: bool = True
    pexels_prefer_landscape: bool = True
    pexels_prefer_video: bool = True

    # V17.4 true pre-narration intro.  Intro queries are stored in metadata and
    # consumed in order only until this exact duration is filled.
    intro_enabled: bool = False
    intro_duration_seconds: int = 15

    # V17.5 optional retention gate.  When enabled, only the visuals intersecting
    # the first 120 seconds of the final timeline are resolved first and the job
    # pauses for a scene-by-scene human audit before full media acquisition.
    first_two_minute_visual_audit_enabled: bool = False

    # Per-project Drive delivery policy. Verified Serial may force delivery ON
    # because its release contract requires Drive verification before cleanup.
    drive_upload_enabled: bool = True

    # If enabled, Drive delivery is followed by project-scoped destructive
    # cleanup.  Permanent music/SFX libraries are never project data.
    auto_clean_project_after_drive: bool = False

    premium_treatment_enabled: bool = True
    silence_detection_enabled: bool = True
    silence_noise_db: float = -38.0
    silence_min_seconds: float = 0.35

    # Professional creative/output pipeline
    output_resolution: str = "1080p"  # 1080p, 1440p, 2160p
    video_quality_preset: str = "enhanced"  # original, enhanced, cinematic, 4k_premium
    auto_transition_sfx_enabled: bool = True
    auto_transition_sfx_volume: float = 0.22

    # Professional Transition Director
    transition_engine_enabled: bool = True
    transition_engine_mode: str = "auto_editorial"  # auto_editorial, scene_plan, cuts_only, off
    transition_preset: str = "documentary"  # restrained editorial preset catalog
    transition_intensity_percent: int = 45
    transition_base_seconds: float = 0.28
    transition_no_repeat: bool = True
    transition_hook_cut_bias: bool = True

    music_auto_download: bool = True
    music_auto_select: bool = True
    music_mood: str = "auto"

    # Professional Color Grading / LUT engine
    lut_enabled: bool = True
    lut_mode: str = "auto"  # auto, manual, custom
    lut_pack: str = "cinematic"
    lut_preset: str = "filmic"
    lut_strength_percent: int = 40
    lut_normalize_enabled: bool = True
    lut_scene_aware_enabled: bool = True
    lut_continuity_enabled: bool = True
    lut_custom_path: str = ""

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
    post_render_compress_target_mb: int = 450
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
    settings = ProjectSettings.from_dict(data)
    if not str(settings.project_id or "").strip():
        settings.project_id = uuid.uuid4().hex
    return settings
