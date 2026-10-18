from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from .persistence import user_data_dir

APP_NAME = "Stock Media Scene Builder"
LUT_SIZE = 17

PACK_DEFINITIONS: dict[str, dict[str, Any]] = {
    "cinematic": {
        "label": "Cinematic LUTs",
        "presets": {
            "teal_orange": "Teal / Orange",
            "filmic": "Filmic",
            "blockbuster": "Blockbuster",
            "soft_cinema": "Soft Cinema",
        },
    },
    "nature": {
        "label": "Nature LUTs",
        "presets": {
            "lush_green": "Lush Green",
            "forest": "Forest",
            "tropical": "Tropical",
            "warm_landscape": "Warm Landscape",
        },
    },
    "travel": {
        "label": "Travel LUTs",
        "presets": {
            "vibrant_city": "Vibrant City",
            "beach": "Beach",
            "golden_hour": "Golden Hour",
            "drone": "Drone Footage",
        },
    },
    "dark_moody": {
        "label": "Dark / Moody LUTs",
        "presets": {
            "foggy": "Foggy",
            "desaturated_cold": "Desaturated Cold",
            "dramatic": "Dramatic",
        },
    },
    "romantic_story": {
        "label": "Romantic / Story LUTs",
        "presets": {
            "warm_skin": "Warm Skin",
            "dreamy": "Dreamy",
            "soft_contrast": "Soft Contrast",
        },
    },
    "urban_modern": {
        "label": "Urban / Modern LUTs",
        "presets": {
            "clean_contrast": "Clean Contrast",
            "cool_city": "Cool City",
            "luxury": "Luxury",
        },
    },
    "neutral_correction": {
        "label": "Neutral / Correction LUTs",
        "presets": {
            "subtle_contrast": "Subtle Contrast",
            "normalization": "Color Normalization",
        },
    },
}


def _clamp(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


def _sat(r: float, g: float, b: float, amount: float) -> tuple[float, float, float]:
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return tuple(_clamp(y + (c - y) * amount) for c in (r, g, b))


def _contrast(r: float, g: float, b: float, amount: float, pivot: float = 0.5) -> tuple[float, float, float]:
    return tuple(_clamp((c - pivot) * amount + pivot) for c in (r, g, b))


def _lift_gamma_gain(c: float, lift: float = 0.0, gamma: float = 1.0, gain: float = 1.0) -> float:
    v = _clamp(c + lift)
    if gamma > 0:
        v = pow(v, 1.0 / gamma)
    return _clamp(v * gain)


def _base_transform(
    rgb: tuple[float, float, float], *, contrast: float = 1.0, saturation: float = 1.0,
    brightness: float = 0.0, gains: tuple[float, float, float] = (1.0, 1.0, 1.0),
    biases: tuple[float, float, float] = (0.0, 0.0, 0.0), lift: float = 0.0,
    gamma: float = 1.0,
) -> tuple[float, float, float]:
    r, g, b = rgb
    r = _lift_gamma_gain(r, lift, gamma, gains[0]) + biases[0] + brightness
    g = _lift_gamma_gain(g, lift, gamma, gains[1]) + biases[1] + brightness
    b = _lift_gamma_gain(b, lift, gamma, gains[2]) + biases[2] + brightness
    r, g, b = _contrast(r, g, b, contrast)
    return _sat(r, g, b, saturation)


def _look(pack: str, preset: str, rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    r, g, b = rgb
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    shadows = 1.0 - y
    highlights = y

    if (pack, preset) == ("cinematic", "teal_orange"):
        r, g, b = _base_transform(rgb, contrast=1.10, saturation=1.06)
        return (_clamp(r + 0.045 * highlights - 0.018 * shadows), _clamp(g + 0.010 * shadows), _clamp(b + 0.045 * shadows - 0.025 * highlights))
    if (pack, preset) == ("cinematic", "filmic"):
        return _base_transform(rgb, contrast=1.06, saturation=0.94, biases=(0.012, 0.006, -0.004), lift=0.012, gamma=1.02)
    if (pack, preset) == ("cinematic", "blockbuster"):
        r, g, b = _base_transform(rgb, contrast=1.16, saturation=1.08)
        return (_clamp(r + 0.055 * highlights - 0.020 * shadows), _clamp(g + 0.008), _clamp(b + 0.060 * shadows - 0.034 * highlights))
    if (pack, preset) == ("cinematic", "soft_cinema"):
        return _base_transform(rgb, contrast=0.97, saturation=0.96, biases=(0.014, 0.006, -0.004), lift=0.018, gamma=1.03)

    if (pack, preset) == ("nature", "lush_green"):
        return _base_transform(rgb, contrast=1.07, saturation=1.12, gains=(0.995, 1.055, 1.015), biases=(-0.003, 0.008, 0.002))
    if (pack, preset) == ("nature", "forest"):
        return _base_transform(rgb, contrast=1.10, saturation=1.02, gains=(0.96, 1.045, 0.985), biases=(-0.006, 0.006, -0.004))
    if (pack, preset) == ("nature", "tropical"):
        return _base_transform(rgb, contrast=1.07, saturation=1.15, gains=(1.015, 1.035, 1.055), biases=(0.004, 0.006, 0.010))
    if (pack, preset) == ("nature", "warm_landscape"):
        return _base_transform(rgb, contrast=1.08, saturation=1.08, gains=(1.045, 1.015, 0.975), biases=(0.008, 0.002, -0.006))

    if (pack, preset) == ("travel", "vibrant_city"):
        return _base_transform(rgb, contrast=1.10, saturation=1.12, gains=(1.01, 1.01, 1.025), gamma=1.01)
    if (pack, preset) == ("travel", "beach"):
        r, g, b = _base_transform(rgb, contrast=1.05, saturation=1.15, gains=(1.025, 1.025, 1.06))
        return (_clamp(r + 0.012 * highlights), _clamp(g + 0.010), _clamp(b + 0.024))
    if (pack, preset) == ("travel", "golden_hour"):
        return _base_transform(rgb, contrast=1.06, saturation=1.10, gains=(1.06, 1.025, 0.95), biases=(0.010, 0.004, -0.010), gamma=1.01)
    if (pack, preset) == ("travel", "drone"):
        return _base_transform(rgb, contrast=1.11, saturation=1.08, gains=(1.0, 1.015, 1.025), gamma=0.995)

    if (pack, preset) == ("dark_moody", "foggy"):
        return _base_transform(rgb, contrast=0.91, saturation=0.78, gains=(0.96, 0.985, 1.045), lift=0.025, gamma=1.035)
    if (pack, preset) == ("dark_moody", "desaturated_cold"):
        return _base_transform(rgb, contrast=1.05, saturation=0.70, gains=(0.94, 0.985, 1.055), biases=(-0.008, 0.0, 0.010))
    if (pack, preset) == ("dark_moody", "dramatic"):
        return _base_transform(rgb, contrast=1.18, saturation=0.88, gains=(0.99, 1.0, 1.02), brightness=-0.010)

    if (pack, preset) == ("romantic_story", "warm_skin"):
        return _base_transform(rgb, contrast=1.02, saturation=1.03, gains=(1.045, 1.015, 0.975), biases=(0.008, 0.003, -0.004), gamma=1.02)
    if (pack, preset) == ("romantic_story", "dreamy"):
        return _base_transform(rgb, contrast=0.94, saturation=0.96, gains=(1.025, 1.01, 1.01), lift=0.025, gamma=1.045)
    if (pack, preset) == ("romantic_story", "soft_contrast"):
        return _base_transform(rgb, contrast=0.98, saturation=1.01, gains=(1.025, 1.01, 0.99), lift=0.010, gamma=1.02)

    if (pack, preset) == ("urban_modern", "clean_contrast"):
        return _base_transform(rgb, contrast=1.12, saturation=1.02, gains=(1.0, 1.0, 1.015), gamma=0.995)
    if (pack, preset) == ("urban_modern", "cool_city"):
        return _base_transform(rgb, contrast=1.11, saturation=0.98, gains=(0.975, 1.005, 1.055), biases=(-0.006, 0.0, 0.010))
    if (pack, preset) == ("urban_modern", "luxury"):
        return _base_transform(rgb, contrast=1.10, saturation=1.00, gains=(1.035, 1.015, 0.985), biases=(0.006, 0.002, -0.004), gamma=0.995)

    if (pack, preset) == ("neutral_correction", "subtle_contrast"):
        return _base_transform(rgb, contrast=1.045, saturation=1.015)
    if (pack, preset) == ("neutral_correction", "normalization"):
        return _base_transform(rgb, contrast=1.025, saturation=1.00, gamma=1.005)

    return rgb


def _write_cube(path: Path, pack: str, preset: str, size: int = LUT_SIZE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f'TITLE "SMSB {pack}/{preset}"',
        f'LUT_3D_SIZE {size}',
        'DOMAIN_MIN 0.0 0.0 0.0',
        'DOMAIN_MAX 1.0 1.0 1.0',
    ]
    # .cube convention: red changes fastest, then green, then blue.
    denom = float(size - 1)
    for b_idx in range(size):
        b = b_idx / denom
        for g_idx in range(size):
            g = g_idx / denom
            for r_idx in range(size):
                r = r_idx / denom
                rr, gg, bb = _look(pack, preset, (r, g, b))
                lines.append(f'{rr:.7f} {gg:.7f} {bb:.7f}')
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def write_manifest(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    data = {
        'schema': 1,
        'lut_size': LUT_SIZE,
        'packs': PACK_DEFINITIONS,
        'pipeline': 'normalization -> LUT -> filter/effect -> transition/overlay -> captions/final encode',
    }
    target = root / 'lut_manifest.json'
    target.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
    return target


def ensure_builtin_luts(root: Path) -> dict[str, Any]:
    root = Path(root)
    created: list[str] = []
    existing: list[str] = []
    for pack, spec in PACK_DEFINITIONS.items():
        for preset in spec['presets']:
            target = root / pack / f'{preset}.cube'
            if target.is_file() and target.stat().st_size > 1000:
                existing.append(str(target))
            else:
                _write_cube(target, pack, preset)
                created.append(str(target))
    write_manifest(root)
    return {'root': str(root), 'created': created, 'existing': existing, 'count': len(created) + len(existing)}


def bundled_lut_root() -> Path:
    return Path(__file__).resolve().parent.parent / 'assets' / 'luts'


def persistent_lut_root() -> Path:
    return user_data_dir(APP_NAME) / 'assets' / 'luts'


def prepare_lut_roots() -> list[dict[str, Any]]:
    return [ensure_builtin_luts(bundled_lut_root()), ensure_builtin_luts(persistent_lut_root())]


def pack_label(pack: str) -> str:
    return str(PACK_DEFINITIONS.get(pack, {}).get('label', pack.replace('_', ' ').title()))


def preset_label(pack: str, preset: str) -> str:
    return str(PACK_DEFINITIONS.get(pack, {}).get('presets', {}).get(preset, preset.replace('_', ' ').title()))


def presets_for_pack(pack: str) -> list[tuple[str, str]]:
    spec = PACK_DEFINITIONS.get(pack, {})
    return [(pid, str(label)) for pid, label in dict(spec.get('presets', {})).items()]


def _scene_text(scene: dict[str, Any]) -> str:
    fields = [
        scene.get('narration'), scene.get('text'), scene.get('scene_text'), scene.get('primary_keyword'),
        scene.get('backup_keywords'), scene.get('visual_direction'), scene.get('visual_mood'), scene.get('visual_prompt'),
        scene.get('query'), scene.get('search_query'), scene.get('hook_visual_family'),
    ]
    return ' '.join(str(x or '') for x in fields).lower()


def scene_aware_choice(scene: dict[str, Any], fallback_pack: str = 'cinematic', fallback_preset: str = '') -> tuple[str, str]:
    text = _scene_text(scene)
    def any_word(words: tuple[str, ...]) -> bool:
        return any(word in text for word in words)

    if any_word(('fog', 'mist', 'crime', 'danger', 'sad', 'grief', 'dark alley', 'cold night', 'storm', 'tension', 'dramatic', 'lonely')):
        if 'fog' in text or 'mist' in text: return 'dark_moody', 'foggy'
        if any_word(('cold', 'night', 'crime', 'danger')): return 'dark_moody', 'desaturated_cold'
        return 'dark_moody', 'dramatic'
    if any_word(('romance', 'romantic', 'love', 'couple', 'wedding', 'bride', 'groom', 'family', 'relationship', 'emotional', 'dreamy')):
        if any_word(('dream', 'dreamy', 'memory', 'soft')): return 'romantic_story', 'dreamy'
        if any_word(('couple', 'wedding', 'bride', 'groom', 'portrait', 'skin')): return 'romantic_story', 'warm_skin'
        return 'romantic_story', 'soft_contrast'
    if any_word(('beach', 'ocean', 'sea ', 'coast', 'coastal', 'island', 'tropical', 'resort', 'swim', 'sunset')):
        if any_word(('sunset', 'golden hour', 'golden-hour')): return 'travel', 'golden_hour'
        return 'travel', 'beach'
    if any_word(('forest', 'jungle', 'green mountain', 'mountain', 'waterfall', 'river', 'valley', 'village', 'rural', 'nature', 'lush', 'garden')):
        if any_word(('forest', 'jungle')): return 'nature', 'forest'
        if any_word(('tropical', 'waterfall')): return 'nature', 'tropical'
        if any_word(('village', 'landscape', 'mountain', 'valley')): return 'nature', 'warm_landscape'
        return 'nature', 'lush_green'
    if any_word(('dubai', 'skyscraper', 'skyline', 'urban', 'modern city', 'architecture', 'luxury', 'downtown', 'business district', 'city night', 'metropolis')):
        if any_word(('luxury', 'dubai', 'hotel', 'supercar', 'premium')): return 'urban_modern', 'luxury'
        if any_word(('night', 'technology', 'futuristic', 'cool')): return 'urban_modern', 'cool_city'
        return 'urban_modern', 'clean_contrast'
    if any_word(('travel', 'tourist', 'drone', 'aerial', 'road trip', 'landmark', 'city street')):
        if any_word(('drone', 'aerial')): return 'travel', 'drone'
        return 'travel', 'vibrant_city'
    if any_word(('documentary', 'interview', 'news', 'office', 'factory', 'technical', 'document', 'product')):
        return 'neutral_correction', 'subtle_contrast'

    fallback_pack = fallback_pack if fallback_pack in PACK_DEFINITIONS else 'cinematic'
    if fallback_preset not in dict(PACK_DEFINITIONS[fallback_pack]['presets']):
        fallback_preset = next(iter(PACK_DEFINITIONS[fallback_pack]['presets']))
    return fallback_pack, fallback_preset


def assign_scene_luts(scenes: list[dict[str, Any]], settings: Any) -> list[dict[str, str]]:
    enabled = bool(getattr(settings, 'lut_enabled', True))
    if not enabled:
        return [{'pack': 'none', 'preset': 'none', 'label': 'LUT Off'} for _ in scenes]
    mode = str(getattr(settings, 'lut_mode', 'auto') or 'auto').lower()
    base_pack = str(getattr(settings, 'lut_pack', 'cinematic') or 'cinematic')
    manual_preset = str(getattr(settings, 'lut_preset', '') or '')
    continuity = bool(getattr(settings, 'lut_continuity_enabled', True))
    custom_path = str(getattr(settings, 'lut_custom_path', '') or '').strip()

    if mode == 'custom':
        name = Path(custom_path).stem if custom_path else 'Missing Custom LUT'
        return [{'pack': 'custom', 'preset': name, 'label': f'Custom • {name}'} for _ in scenes]
    if mode == 'manual' or not bool(getattr(settings, 'lut_scene_aware_enabled', True)):
        if manual_preset not in dict(PACK_DEFINITIONS.get(base_pack, {}).get('presets', {})):
            manual_preset = next(iter(PACK_DEFINITIONS.get(base_pack, PACK_DEFINITIONS['cinematic'])['presets']))
        label = f'{pack_label(base_pack)} • {preset_label(base_pack, manual_preset)}'
        return [{'pack': base_pack, 'preset': manual_preset, 'label': label} for _ in scenes]

    choices: list[tuple[str, str]] = [scene_aware_choice(scene, base_pack, manual_preset) for scene in scenes]
    if continuity and len(choices) >= 3:
        # Remove one-scene family flicker when both neighbours use the same family.
        smoothed = list(choices)
        for i in range(1, len(choices) - 1):
            prev_pack, prev_preset = choices[i - 1]
            cur_pack, cur_preset = choices[i]
            next_pack, next_preset = choices[i + 1]
            if prev_pack == next_pack and cur_pack != prev_pack:
                smoothed[i] = (prev_pack, prev_preset if prev_preset == next_preset else next_preset)
        choices = smoothed
    return [{'pack': p, 'preset': q, 'label': f'{pack_label(p)} • {preset_label(p, q)}'} for p, q in choices]


def resolve_lut_source(choice: dict[str, str], settings: Any) -> Path | None:
    pack = str(choice.get('pack', '') or '')
    preset = str(choice.get('preset', '') or '')
    if pack == 'none': return None
    if pack == 'custom':
        candidate = Path(str(getattr(settings, 'lut_custom_path', '') or '')).expanduser()
        return candidate if candidate.is_file() and candidate.suffix.lower() == '.cube' else None
    for root in (persistent_lut_root(), bundled_lut_root()):
        candidate = root / pack / f'{preset}.cube'
        if candidate.is_file(): return candidate
    return None


def _parse_cube(path: Path) -> tuple[list[str], list[tuple[float, float, float]]]:
    headers: list[str] = []
    values: list[tuple[float, float, float]] = []
    for raw in path.read_text(encoding='utf-8-sig', errors='replace').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'): continue
        parts = line.split()
        if len(parts) == 3:
            try:
                values.append((float(parts[0]), float(parts[1]), float(parts[2])))
                continue
            except Exception:
                pass
        headers.append(line)
    if not values:
        raise ValueError(f'No LUT data found in {path.name}')
    return headers, values


def _infer_cube_size(headers: list[str], count: int) -> int:
    for line in headers:
        m = re.match(r'(?i)LUT_3D_SIZE\s+(\d+)', line)
        if m: return int(m.group(1))
    size = round(count ** (1.0 / 3.0))
    if size ** 3 != count:
        raise ValueError(f'Unsupported .cube entry count: {count}')
    return int(size)


def blended_lut(source: Path, strength_percent: float, temp_root: Path, cache: dict[str, Path] | None = None) -> Path:
    strength = max(0.0, min(1.0, float(strength_percent) / 100.0))
    if strength >= 0.999:
        return source
    key = hashlib.sha256((str(source.resolve()) + f'|{source.stat().st_mtime_ns}|{strength:.5f}').encode('utf-8')).hexdigest()[:16]
    if cache is not None and key in cache and cache[key].is_file():
        return cache[key]
    headers, values = _parse_cube(source)
    size = _infer_cube_size(headers, len(values))
    target = Path(temp_root) / f'lut_blend_{key}.cube'
    lines = [f'TITLE "SMSB blended {source.stem} {strength_percent:.1f}%"', f'LUT_3D_SIZE {size}', 'DOMAIN_MIN 0.0 0.0 0.0', 'DOMAIN_MAX 1.0 1.0 1.0']
    idx = 0
    denom = float(max(1, size - 1))
    for b_idx in range(size):
        b = b_idx / denom
        for g_idx in range(size):
            g = g_idx / denom
            for r_idx in range(size):
                r = r_idx / denom
                lr, lg, lb = values[idx]; idx += 1
                rr = _clamp(r * (1.0 - strength) + lr * strength)
                gg = _clamp(g * (1.0 - strength) + lg * strength)
                bb = _clamp(b * (1.0 - strength) + lb * strength)
                lines.append(f'{rr:.7f} {gg:.7f} {bb:.7f}')
    target.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    if cache is not None: cache[key] = target
    return target


def ffmpeg_filter_path(path: Path) -> str:
    # FFmpeg filter parser needs drive-colon escaping on Windows and forward slashes everywhere.
    text = str(path.resolve()).replace('\\', '/')
    text = text.replace(':', r'\:').replace("'", r"\'")
    return text


def lut_filter_for_scene(scene: dict[str, Any], choice: dict[str, str], settings: Any, temp_root: Path, cache: dict[str, Path] | None = None) -> tuple[str, str]:
    if not bool(getattr(settings, 'lut_enabled', True)):
        return 'null', 'LUT Off'
    source = resolve_lut_source(choice, settings)
    if source is None:
        return 'null', f"Missing LUT: {choice.get('label','unknown')}"
    raw_strength = getattr(settings, 'lut_strength_percent', 40)
    strength = float(40 if raw_strength is None else raw_strength)
    active = blended_lut(source, strength, temp_root, cache)
    chain: list[str] = []
    if bool(getattr(settings, 'lut_normalize_enabled', True)):
        # Deliberately restrained, static normalization to avoid frame-to-frame pumping.
        chain.append('eq=contrast=1.025:saturation=1.010:brightness=0.003')
    chain.append(f"lut3d=file='{ffmpeg_filter_path(active)}':interp=tetrahedral")
    return ','.join(chain), str(choice.get('label') or source.stem)


def grading_summary(scenes: list[dict[str, Any]], settings: Any) -> dict[str, Any]:
    if not bool(getattr(settings, 'lut_enabled', True)):
        return {'enabled': False, 'count': 0, 'detail': 'LUT grading disabled'}
    choices = assign_scene_luts(scenes, settings)
    missing = []
    families = Counter()
    presets = Counter()
    validated: set[str] = set()
    for choice in choices:
        families[choice['pack']] += 1
        presets[f"{choice['pack']}/{choice['preset']}"] += 1
        source = resolve_lut_source(choice, settings)
        if source is None:
            missing.append(choice['label'])
            continue
        key = str(source.resolve())
        if key not in validated:
            try:
                _parse_cube(source)
                validated.add(key)
            except Exception:
                missing.append(choice['label'])
    dominant = families.most_common(1)[0][0] if families else 'none'
    dominant_label = pack_label(dominant) if dominant not in {'none', 'custom'} else dominant.title()
    return {
        'enabled': True,
        'count': len(choices),
        'missing': sorted(set(missing)),
        'dominant_pack': dominant,
        'dominant_label': dominant_label,
        'families': dict(families),
        'presets': dict(presets),
        'strength_percent': int(40 if getattr(settings, 'lut_strength_percent', 40) is None else getattr(settings, 'lut_strength_percent', 40)),
        'mode': str(getattr(settings, 'lut_mode', 'auto') or 'auto'),
        'normalized': bool(getattr(settings, 'lut_normalize_enabled', True)),
    }
