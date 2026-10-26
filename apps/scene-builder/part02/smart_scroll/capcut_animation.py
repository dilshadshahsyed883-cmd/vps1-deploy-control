"""Bounded, source-only Combo and Cam Motion effects for Scene Builder.

No proprietary CapCut assets are embedded. These are FFmpeg approximations of
the named motions, applied before grain/overlays/captions in the media graph.
"""
from __future__ import annotations

from math import isfinite

COMBO_EFFECTS = (
    "zoom_1", "bend_zoom", "distort_left", "pendulum_1", "pendulum_2",
    "spin_fall", "bouncy_rush", "zoom_2", "distort_right", "sway_in",
    "roll_in_out_2", "sway_out", "fall_right", "bounce_1", "wobble",
)
CAM_MOTION_EFFECTS = ("pan_left", "pan_right", "right_zoom", "dynamic_shake_2")
ALL_EFFECTS = frozenset((*COMBO_EFFECTS, *CAM_MOTION_EFFECTS))


def validate_animation(category: str, effect: str) -> bool:
    if effect == "none":
        return category in ("combo", "cam_motion")
    return effect in (COMBO_EFFECTS if category == "combo" else CAM_MOTION_EFFECTS if category == "cam_motion" else ())


def capcut_animation_filter(
    effect: str, duration: float, intensity: int, width: int, height: int, fps: int
) -> str:
    """Build a real per-frame filter. The media is already normalized to the canvas.

    zoompan d=1 processes one input frame per output frame (including video),
    avoiding costly still-image frame multiplication. Effects stop after the
    requested time and keep the final transform for longer scene durations.
    """
    if effect == "none":
        return "null"
    if effect not in ALL_EFFECTS:
        raise ValueError(f"Unsupported Scene Builder animation: {effect}")
    duration = float(duration)
    if not isfinite(duration) or not 0.5 <= duration <= 5.0:
        raise ValueError("Animation duration must be 0.5–5.0 seconds")
    if not 10 <= int(intensity) <= 100 or fps <= 0 or width <= 0 or height <= 0:
        raise ValueError("Invalid animation intensity or render dimensions")
    power = int(intensity) / 100.0
    frames = max(2, round(duration * fps))
    u = f"min(1,on/{frames-1})"
    wave = f"sin(PI*{u})"
    bounce = f"abs(sin(3*PI*{u}))"
    jiggle = f"sin(11*PI*{u})"
    amp = 0.055 + 0.18 * power
    travel = 0.12 + 0.24 * power
    base = 1.11 + 0.10 * power
    z = f"{base:.5f}"
    x = "(iw-iw/zoom)/2"
    y = "(ih-ih/zoom)/2"
    angle = "0"

    if effect == "zoom_1":
        z = f"1+{amp:.5f}*{u}"
    elif effect == "zoom_2":
        z = f"1+{amp*1.25:.5f}*{wave}"
    elif effect == "bend_zoom":
        z = f"1.10+{amp:.5f}*{u}"
        x = f"(iw-iw/zoom)*(0.5+0.24*{wave})"
        angle = f"{0.07*power:.5f}*sin(PI*min(1\,t/{duration:.4f}))"
    elif effect == "distort_left":
        x = f"(iw-iw/zoom)*(0.65-0.42*{u})"
        angle = f"-{0.085*power:.5f}*sin(PI*min(1\,t/{duration:.4f}))"
    elif effect == "distort_right":
        x = f"(iw-iw/zoom)*(0.35+0.42*{u})"
        angle = f"{0.085*power:.5f}*sin(PI*min(1\,t/{duration:.4f}))"
    elif effect in ("pendulum_1", "pendulum_2"):
        factor = 1.65 if effect == "pendulum_2" else 1.0
        z = f"{1.17+power*0.10:.5f}"
        angle = f"{0.085*power*factor:.5f}*sin(2*PI*min(1\,t/{duration:.4f}))"
        x = f"(iw-iw/zoom)*(0.5+{0.12*factor:.5f}*sin(2*PI*{u}))"
    elif effect == "spin_fall":
        z = f"1.12+{amp:.5f}*{u}"
        angle = f"-{0.22*power:.5f}*(1-min(1\,t/{duration:.4f}))"
        y = f"(ih-ih/zoom)*(0.18+0.32*{u})"
    elif effect == "bouncy_rush":
        z = f"1.02+{amp*1.4:.5f}*{u}+{0.08*power:.5f}*{bounce}"
    elif effect == "sway_in":
        x = f"(iw-iw/zoom)*({0.92:.4f}-{0.42:.4f}*{u})"
        angle = f"-{0.08*power:.5f}*(1-min(1\,t/{duration:.4f}))"
    elif effect == "sway_out":
        x = f"(iw-iw/zoom)*(0.5+0.43*{u})"
        angle = f"{0.08*power:.5f}*min(1\,t/{duration:.4f})"
    elif effect == "roll_in_out_2":
        z = f"1.04+{amp:.5f}*{wave}"
        angle = f"{0.16*power:.5f}*sin(2*PI*min(1\,t/{duration:.4f}))"
    elif effect == "fall_right":
        x = f"(iw-iw/zoom)*(0.18+0.65*{u})"
        y = f"(ih-ih/zoom)*(0.20+0.60*{u})"
        angle = f"{0.13*power:.5f}*min(1\,t/{duration:.4f})"
    elif effect == "bounce_1":
        z = f"1.05+{amp:.5f}*{bounce}"
        y = f"(ih-ih/zoom)*(0.50-{0.25*power:.5f}*{bounce})"
    elif effect == "wobble":
        z = f"{base+0.07:.5f}"
        angle = f"{0.055*power:.5f}*sin(8*PI*min(1\,t/{duration:.4f}))"
        x = f"(iw-iw/zoom)*(0.5+0.14*{jiggle})"
    elif effect == "pan_left":
        z = f"{base:.5f}"
        x = f"(iw-iw/zoom)*(1-{u})"
    elif effect == "pan_right":
        z = f"{base:.5f}"
        x = f"(iw-iw/zoom)*{u}"
    elif effect == "right_zoom":
        z = f"1.02+{amp*1.4:.5f}*{u}"
        x = f"(iw-iw/zoom)*(0.18+0.66*{u})"
    elif effect == "dynamic_shake_2":
        z = f"{base+0.07:.5f}"
        x = f"(iw-iw/zoom)*(0.5+{0.19*power:.5f}*{jiggle})"
        y = f"(ih-ih/zoom)*(0.5+{0.14*power:.5f}*sin(15*PI*{u}))"
        angle = f"{0.018*power:.5f}*sin(13*PI*min(1\,t/{duration:.4f}))"

    # Rotate the oversampled frame before crop; the 2x source keeps margins
    # around the central output so small camera turns do not expose corners.
    return (
        f"fps={fps},setpts=PTS-STARTPTS,"
        f"scale={width*2}:{height*2}:force_original_aspect_ratio=increase,"
        f"crop={width*2}:{height*2},setsar=1,"
        f"rotate=a='{angle}':ow=iw:oh=ih:c=black,"
        f"zoompan=z='{z}':x='{x}':y='{y}':d=1:s={width}x{height}:fps={fps}"
    )
