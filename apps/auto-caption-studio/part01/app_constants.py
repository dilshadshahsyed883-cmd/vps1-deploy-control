from __future__ import annotations

APP_NAME = "auto_caption_studio"
ORG_NAME = "SmartScroll"
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".wmv", ".flv"}

# Curated professional font list. Keep the interface clean instead of exposing
# hundreds of system fonts. Two strong choices are listed for each requested
# language group. Font files are not bundled; users can load licensed fonts
# from smart_scroll/fonts or with the Load Custom Font button.
CURATED_FONT_FAMILIES = [
    # Urdu / Nastaleeq
    "Jameel Noori Nastaleeq Kasheeda",
    "Noto Nastaliq Urdu",
    # Arabic
    "Noto Naskh Arabic",
    "Noto Kufi Arabic",
    # Hindi / Devanagari
    "Noto Sans Devanagari",
    "Noto Serif Devanagari",
    # Gujarati
    "Noto Sans Gujarati",
    "Noto Serif Gujarati",
    # Marathi
    "Mukta",
    "Baloo 2",
    # Sanskrit
    "Siddhanta",
    "Sanskrit 2003",
    # Indian Punjabi / Gurmukhi
    "Noto Sans Gurmukhi",
    "Noto Serif Gurmukhi",
    # English / universal fallbacks
    "Arial",
    "Georgia",
    "Times New Roman",
]

