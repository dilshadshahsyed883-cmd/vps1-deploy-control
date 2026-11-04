"""Windows entry point for Concat Studio, suitable for PyInstaller one-file."""
from __future__ import annotations

import sys


def main():
    if "--self-test" in sys.argv:
        from cloud_render.concat.studio.core import parse_plan, parse_srt
        from cloud_render.concat.studio.gpu import origin_only
        sample = ("name=Smoke Test\naspect=16:9\nresolution=1080p\nfps=30\n"
                  "00:00.000 --> 00:02.000 | image | smoke.jpg\n")
        plan = parse_plan(sample)
        assert plan.duration == 2.0 and len(plan.shots) == 1
        assert parse_srt("1\n00:00:00,000 --> 00:00:01,000\nTest\n", 2.0)
        assert origin_only("https://sample.example/") == "https://sample.example"
        return
    from cloud_render.concat.studio.app import main as app_main
    app_main()


if __name__ == "__main__":
    main()
