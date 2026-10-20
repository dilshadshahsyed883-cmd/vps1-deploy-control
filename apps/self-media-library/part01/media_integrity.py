"""Bounded, dependency-light Self Media Library integrity and video quality checks.

All results are advisory except obviously empty (near-all black/white) sample
frames. A decode failure never auto-rejects an otherwise valid video.
"""
from __future__ import annotations

import hashlib
import math
import subprocess
from pathlib import Path

FRAME_WIDTH = 64
FRAME_HEIGHT = 36
FRAME_BYTES = FRAME_WIDTH * FRAME_HEIGHT
MAX_HASH_CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(MAX_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def quality_pixels(frame: bytes) -> tuple[int, bool]:
    """Return explainable 0-100 visual score, blank-only automatic reject flag."""
    if len(frame) != FRAME_BYTES:
        raise ValueError("Expected exactly one scaled grayscale video frame")
    dark = sum(value <= 16 for value in frame)
    bright = sum(value >= 240 for value in frame)
    if dark >= int(FRAME_BYTES * 0.98) or bright >= int(FRAME_BYTES * 0.98):
        return 5, True
    mean = sum(frame) / FRAME_BYTES
    contrast = math.sqrt(sum((value - mean) ** 2 for value in frame) / FRAME_BYTES)
    # Adjacent-pixel differences indicate structure, not a definitive blur verdict.
    gradients = 0
    for y in range(FRAME_HEIGHT):
        offset = y * FRAME_WIDTH
        for x in range(FRAME_WIDTH - 1):
            gradients += abs(frame[offset + x] - frame[offset + x + 1])
    spatial_detail = gradients / (FRAME_HEIGHT * (FRAME_WIDTH - 1))
    score = int(min(95, max(40, 45 + min(29, contrast * 0.7)
                           + min(21, spatial_detail * 1.2))))
    return score, False


def _sample_gray_frame(path: Path, at: float) -> bytes:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-ss", f"{at:.3f}", "-i", str(path), "-frames:v", "1",
         "-vf", f"scale={FRAME_WIDTH}:{FRAME_HEIGHT}:flags=fast_bilinear,format=gray",
         "-f", "rawvideo", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=35,
    )
    return result.stdout


def score_clip(path: Path, duration: float) -> tuple[int, bool]:
    """Sample two points; reject as blank only if BOTH decode as blank.

    Fail open on unavailable/invalid frames to avoid destructive false positives.
    Final approval remains human-controlled.
    """
    if duration <= 0:
        return 65, False
    scores: list[int] = []
    blank_flags: list[bool] = []
    for fraction in (0.33, 0.67):
        try:
            frame = _sample_gray_frame(path, max(0.01, duration * fraction))
            value, blank = quality_pixels(frame)
        except (ValueError, OSError, subprocess.SubprocessError):
            return 65, False
        scores.append(value)
        blank_flags.append(blank)
    return max(scores), all(blank_flags)
