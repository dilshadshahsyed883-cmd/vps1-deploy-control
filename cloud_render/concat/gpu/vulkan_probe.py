"""Read-only Vulkan/NVIDIA diagnostics for a Salad GPU container.

Never reports that a GPU render succeeded: Vulkan enumeration and actual
Concat frame export are separate acceptance gates. No secrets or raw host
environment are returned by this endpoint.
"""
from __future__ import annotations

import re
import subprocess

_NAME = re.compile(r"^\s*deviceName\s*=\s*(.+?)\s*$", re.MULTILINE)
_TYPE = re.compile(r"^\s*deviceType\s*=\s*(.+?)\s*$", re.MULTILINE)
_DRIVER = re.compile(r"^\s*driverName\s*=\s*(.+?)\s*$", re.MULTILINE)


def _run(command: list[str]) -> tuple[int | None, str]:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, errors="replace",
            timeout=12, check=False,
        )
        return result.returncode, result.stdout[:65536]
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None, ""


def probe() -> dict:
    """Report detected Vulkan adapters; never conflate CUDA with Vulkan."""
    status, output = _run(["vulkaninfo", "--summary"])
    names = list(dict.fromkeys(_NAME.findall(output)))[:16]
    types = _TYPE.findall(output)[:16]
    drivers = _DRIVER.findall(output)[:16]
    # Vulkaninfo may enumerate Lavapipe even when NVIDIA GPU utilities work.
    # Only classify NVIDIA Vulkan when the Vulkan adapter is actually listed.
    nvidia_vulkan = any("nvidia" in name.lower() for name in names)
    software = any(
        marker in name.lower()
        for name in names
        for marker in ("llvmpipe", "lavapipe", "software rasterizer", "softpipe")
    )
    smi_status, smi_out = _run(
        ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"]
    )
    nvidia_compute_visible = smi_status == 0 and bool(smi_out.strip())
    return {
        "vulkaninfo_exit_code": status,
        "vulkan_adapters": names,
        "vulkan_device_types": types,
        "vulkan_drivers": drivers,
        "nvidia_vulkan_visible": nvidia_vulkan,
        "nvidia_compute_visible": nvidia_compute_visible,
        "software_vulkan_visible": software,
        "renderer_hint": (
            "nvidia_vulkan_detected_render_unverified" if nvidia_vulkan
            else "software_only_render_unverified" if software
            else "no_vulkan_adapter_detected"
        ),
    }
