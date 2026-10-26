"""Windows CI smoke test of the real packaged SML source."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

repo = Path(__file__).resolve().parents[2]
stage = repo / "build" / "sml_desktop" / "stage"
sys.path.insert(0, str(stage))
os.environ["SML_DATA_ROOT"] = tempfile.mkdtemp(prefix="sml-desktop-smoke-")
os.environ["SML_AI_WORKER"] = "0"
os.environ["SML_DESKTOP_MODE"] = "1"

from fastapi.testclient import TestClient
from app import app

with TestClient(app) as client:
    health = client.get("/api/health")
    assert health.status_code == 200, health.text
    assert health.json()["ok"] is True
    assert client.get("/").status_code == 200
    assert client.get("/api/stats").status_code == 200
    assert client.get("/api/sources").status_code == 200
    clip = Path(os.environ["SML_DATA_ROOT"]) / "sample.mp4"
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
           "color=c=blue:s=320x240:r=25:d=2", "-c:v", "mpeg4", str(clip)]
    subprocess.run(cmd, check=True, timeout=30)
    with clip.open("rb") as stream:
        response = client.post("/api/clips/processed/upload", files={"file": ("smoke.mp4", stream, "video/mp4")})
    assert response.status_code in (200, 201), (response.status_code, response.text)
    assert client.get("/api/clips").status_code == 200
print("PASS: offline SML desktop startup, routes, and processed clip intake")
