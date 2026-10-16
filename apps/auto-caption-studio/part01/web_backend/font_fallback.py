from __future__ import annotations


SCRIPT_FONTS: dict[str, tuple[str, ...]] = {
    "urdu": ("Jameel Noori Nastaleeq", "Noto Nastaliq Urdu", "Noto Naskh Arabic", "Noto Sans Arabic"),
    "arabic": ("Noto Naskh Arabic", "Noto Sans Arabic", "Amiri", "DejaVu Sans"),
    "devanagari": ("Noto Sans Devanagari", "Noto Serif Devanagari", "Mangal"),
    "gujarati": ("Noto Sans Gujarati", "Noto Serif Gujarati", "Shruti"),
    "bengali": ("Noto Sans Bengali", "Noto Serif Bengali", "Vrinda"),
    "gurmukhi": ("Noto Sans Gurmukhi", "Noto Serif Gurmukhi", "Raavi"),
    "tamil": ("Noto Sans Tamil", "Noto Serif Tamil", "Latha"),
    "telugu": ("Noto Sans Telugu", "Noto Serif Telugu", "Gautami"),
    "kannada": ("Noto Sans Kannada", "Noto Serif Kannada", "Tunga"),
    "malayalam": ("Noto Sans Malayalam", "Noto Serif Malayalam", "Kartika"),
    "oriya": ("Noto Sans Oriya", "Noto Sans Odia", "Noto Serif Oriya", "Kalinga"),
    "universal": ("Noto Sans", "DejaVu Sans", "Arial", "Liberation Sans"),
}



def detect_script(text: str, language: str = "auto") -> str:
    language_key = (language or "auto").strip().casefold().replace("_", "-")
    language_map = {
        "ur": "urdu", "urdu": "urdu", "ar": "arabic", "arabic": "arabic",
        "fa": "arabic", "persian": "arabic", "ps": "arabic", "pashto": "arabic", "sd": "arabic", "sindhi": "arabic",
        "mr": "devanagari", "marathi": "devanagari", "hi": "devanagari", "hindi": "devanagari",
        "gu": "gujarati", "gujarati": "gujarati",
        "bn": "bengali", "bangla": "bengali", "bengali": "bengali", "as": "bengali", "assamese": "bengali",
        "pa": "gurmukhi", "punjabi": "gurmukhi", "ta": "tamil", "tamil": "tamil",
        "te": "telugu", "telugu": "telugu", "kn": "kannada", "kannada": "kannada",
        "ml": "malayalam", "malayalam": "malayalam", "or": "oriya", "odia": "oriya", "oriya": "oriya",
    }
    if language_key in language_map:
        return language_map[language_key]
    counts = {name: 0 for name in SCRIPT_FONTS}
    for char in text or "":
        code = ord(char)
        if 0x0600 <= code <= 0x06FF or 0x0750 <= code <= 0x077F or 0x08A0 <= code <= 0x08FF:
            counts["urdu" if char in "ٹڈڑںھہےکگچژپے" else "arabic"] += 1
        elif 0x0900 <= code <= 0x097F:
            counts["devanagari"] += 1
        elif 0x0A80 <= code <= 0x0AFF:
            counts["gujarati"] += 1
        elif 0x0980 <= code <= 0x09FF:
            counts["bengali"] += 1
        elif 0x0A00 <= code <= 0x0A7F:
            counts["gurmukhi"] += 1
        elif 0x0B80 <= code <= 0x0BFF:
            counts["tamil"] += 1
        elif 0x0C00 <= code <= 0x0C7F:
            counts["telugu"] += 1
        elif 0x0C80 <= code <= 0x0CFF:
            counts["kannada"] += 1
        elif 0x0D00 <= code <= 0x0D7F:
            counts["malayalam"] += 1
        elif 0x0B00 <= code <= 0x0B7F:
            counts["oriya"] += 1
        elif char.isalpha():
            counts["universal"] += 1
    if counts["urdu"]:
        return "urdu"
    script, count = max(counts.items(), key=lambda item: item[1])
    return script if count else ""


def choose_font_family(text: str, language: str, installed_families: list[str]) -> tuple[str, str]:
    language_key = (language or "auto").strip().casefold().replace("_", "-")
    script = language_key if language_key in SCRIPT_FONTS else detect_script(text, language)
    if not script:
        return "", ""
    lookup = {family.casefold(): family for family in installed_families}
    for candidate in SCRIPT_FONTS[script]:
        actual = lookup.get(candidate.casefold())
        if actual:
            return actual, script
    return "", script
