"""Real FFmpeg CapCut Controls validation; all test media is synthetic."""
from __future__ import annotations
from pathlib import Path
from types import SimpleNamespace
import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"apps/scene-builder/part02"))


def frames(filter_complex: str, width=128, height=72, count=4) -> bytes:
    proc=subprocess.run([
        "ffmpeg","-y","-nostdin","-hide_banner","-loglevel","error",
        "-filter_complex_threads","1","-f","lavfi",
        "-i","testsrc2=s=72x128:r=8:d=1.5",
        "-filter_complex",filter_complex,"-map","[done]",
        "-frames:v",str(count),"-pix_fmt","rgb24","-f","rawvideo","-",
    ],capture_output=True,timeout=65)
    if proc.returncode:
        raise AssertionError(proc.stderr.decode(errors="replace")[-2500:])
    assert len(proc.stdout)==width*height*3*count, (len(proc.stdout),width,height,count)
    return proc.stdout


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),"FFmpeg required")
class CapCutControlsTest(unittest.TestCase):
    def test_selection_is_deterministic_and_applies_only_to_requested_media(self):
        from smart_scroll.v2_renderer import _capcut_selected_scene_indices
        scenes=[{"scene_code":f"SCENE_{i:03d}","media_path":f"/tmp/media_{i}.mp4" if i%2 else f"/tmp/image_{i}.jpg"} for i in range(20)]
        clips=_capcut_selected_scene_indices(scenes,30,target="videos")
        self.assertEqual(clips,_capcut_selected_scene_indices(scenes,30,target="videos"))
        self.assertEqual(len(clips),3)
        self.assertTrue(all(i%2 for i in clips))
        images=_capcut_selected_scene_indices(scenes,40,target="images")
        self.assertEqual(len(images),4)
        self.assertTrue(all(not i%2 for i in images))
        self.assertEqual(len(_capcut_selected_scene_indices(scenes,100,target="both")),20)
        self.assertEqual(len(_capcut_selected_scene_indices(scenes,0,target="both")),0)

    def test_zoom_50_100_300_and_blur_background_real_ffmpeg(self):
        from smart_scroll.v2_renderer import _canvas_fill_chains,_capcut_zoom_chains,_motion
        first=None
        for w,h in [(128,72),(72,128),(96,96)]:
            for zoom in (50,100,300):
                with self.subTest(w=w,h=h,zoom=zoom):
                    canvas,source=_canvas_fill_chains(0,0,w,h,"blur",12,"#101018")
                    base=f"[capcutbase0]"
                    z,output=_capcut_zoom_chains(base,0,w,h,zoom,"blur","#101018",12)
                    graph=";".join(canvas+[f"{source}{_motion({'motion':'none'},w,h,8,1,False)}{base}"]+z+
                                   [f"{output}format=rgb24[done]"])
                    data=frames(graph,w,h)
                    self.assertTrue(data)
                    if w==128 and h==72 and zoom==100:
                        first=data
                    if w==128 and h==72 and zoom==300:
                        self.assertNotEqual(data,first)

    def test_zoom_out_uses_solid_canvas_fill(self):
        from smart_scroll.v2_renderer import _canvas_fill_chains,_capcut_zoom_chains
        canvas,source=_canvas_fill_chains(0,0,128,72,"solid",12,"#0000ff")
        base="[capcutbase0]"
        zs,out=_capcut_zoom_chains(base,0,128,72,50,"solid","#0000ff",12)
        graph=";".join(canvas+[f"{source}null{base}"]+zs+[f"{out}format=rgb24[done]"])
        data=frames(graph)
        x,y=2,2
        pixel=data[(y*128+x)*3:(y*128+x)*3+3]
        self.assertGreater(pixel[2],220)
        self.assertLess(pixel[0],20)

    def test_five_noise_effects_generate_distinct_frames(self):
        from smart_scroll.v2_renderer import _capcut_grain_filter,_canvas_fill_chains
        original=None
        digests=[]
        for effect in ("white","black","film","analog","dust"):
            with self.subTest(effect=effect):
                chains,src=_canvas_fill_chains(0,0,128,72,"blur",12,"#101018")
                graph=";".join(chains+[f"{src}{_capcut_grain_filter(effect,75)},format=rgb24[done]"])
                data=frames(graph)
                digests.append(hashlib.sha256(data).hexdigest())
        self.assertEqual(len(set(digests)),5)
        self.assertEqual(_capcut_grain_filter("film",0),"null")
        with self.assertRaises(ValueError):
            _capcut_grain_filter("unknown",75)

    def test_combo_cam_motion_real_ffmpeg_matrix(self):
        from smart_scroll.capcut_animation import (
            ALL_EFFECTS, COMBO_EFFECTS, CAM_MOTION_EFFECTS,
            capcut_animation_filter, validate_animation,
        )
        from smart_scroll.v2_renderer import _canvas_fill_chains
        self.assertEqual(len(ALL_EFFECTS), 19)
        self.assertTrue(validate_animation("combo", "zoom_1"))
        self.assertTrue(validate_animation("cam_motion", "pan_left"))
        self.assertFalse(validate_animation("combo", "pan_left"))
        self.assertFalse(validate_animation("wrong", "none"))
        self.assertEqual(capcut_animation_filter("none", 2.5, 65, 128, 72, 8), "null")
        signatures = {}
        for effect in (*COMBO_EFFECTS, *CAM_MOTION_EFFECTS):
            with self.subTest(effect=effect):
                chain, source = _canvas_fill_chains(0, 0, 128, 72, "blur", 12, "#101018")
                flt = capcut_animation_filter(effect, 1.0, 65, 128, 72, 8)
                graph = ";".join(chain + [
                    f"{source}fps=8[media]",
                    f"[media]{flt},format=rgb24[done]",
                ])
                output = frames(graph)
                signatures[effect] = hashlib.sha256(output).hexdigest()
        self.assertNotEqual(signatures["pan_left"], signatures["pan_right"])
        self.assertNotEqual(signatures["zoom_1"], signatures["zoom_2"])
        for bad in ("unknown", "capcut_secret"):
            with self.assertRaises(ValueError):
                capcut_animation_filter(bad, 1, 65, 128, 72, 8)
        with self.assertRaises(ValueError):
            capcut_animation_filter("wobble", 0.1, 65, 128, 72, 8)
        with self.assertRaises(ValueError):
            capcut_animation_filter("wobble", 1.0, 101, 128, 72, 8)

    def test_real_mp4_render_with_flip_zoom_and_grain_preserves_job_compat(self):
        from smart_scroll.v2_renderer import render_mixed_media,_validate_final_mp4
        with tempfile.TemporaryDirectory(prefix="scene-capcut-ci-") as tmp:
            root=Path(tmp)
            video=root/"video.mp4"
            proc=subprocess.run([
                "ffmpeg","-y","-nostdin","-hide_banner","-loglevel","error",
                "-f","lavfi","-i","testsrc2=s=72x128:r=8:d=1.4",
                "-c:v","libx264","-pix_fmt","yuv420p","-preset","ultrafast",str(video),
            ],capture_output=True,timeout=35)
            self.assertEqual(proc.returncode,0,proc.stderr.decode(errors="replace")[-900:])
            for zoom,effect in [(50,"film"),(140,"analog")]:
                with self.subTest(zoom=zoom,effect=effect):
                    folder=root/str(zoom)
                    folder.mkdir()
                    output=folder/"complete.mp4"
                    settings=SimpleNamespace(
                        width=128,height=72,fps=8,output_path=str(output),
                        audio_path="",render_audio_path="",quality_crf=24,
                        intermediate_render_preset="ultrafast",render_chunk_scenes=4,
                        video_quality_preset="original",lut_enabled=False,
                        captions_enabled=False,
                        canvas_fill_mode="blur",canvas_blur_strength=12,
                        canvas_fill_color="#101018",
                        capcut_mirror_enabled=True,capcut_mirror_percent=100,
                        capcut_zoom_level=zoom,
                        capcut_noise_enabled=True,capcut_noise_effect=effect,
                        capcut_noise_intensity=60,capcut_noise_percent=100,
                        capcut_noise_target="videos",
                        capcut_animation_category="cam_motion",
                        capcut_animation_effect="pan_left",
                        capcut_animation_duration=0.9,
                        capcut_animation_intensity=70,
                        capcut_animation_target="videos",
                        metadata={"scene_sync_plan":[dict(scene_code="VIDEO_1",start=0,end=1.2,
                          media_type="video",media_path=str(video),motion="none",
                          transition_out="none")],"caption_segments":[]},
                    )
                    render_mixed_media(settings)
                    info=_validate_final_mp4(output,"ffmpeg",expected_duration=1.2,expect_audio=False)
                    self.assertEqual((info["width"],info["height"]),(128,72))

if __name__=="__main__":
    unittest.main()
