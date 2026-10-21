"""Real SML catalog -> worker acquisition -> three-scene V2 MP4 acceptance.

Uses only a uniquely named integration workspace and labeled usage reservations.
No existing queue, user project, original asset or Drive file is modified.
"""
from __future__ import annotations
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import ModuleType, SimpleNamespace
import uuid


def verify(part2: Path, state: Path):
    sys.path.insert(0, str(part2))
    package = ModuleType('vps_engine'); package.__path__ = [str(part2)]
    sys.modules['vps_engine'] = package
    from vps_engine import api_server as worker
    from smart_scroll.v2_renderer import render_mixed_media, _validate_final_mp4
    client = worker.SMLWorkerClient()
    catalog = client._json('GET', '/api/v1/media/search',
                           params={'media_type': 'video', 'limit': 100})['items']
    if not catalog:
        raise RuntimeError('An approved SML video is required for production acceptance')
    ident = 'SML-INTEGRATION-' + uuid.uuid4().hex
    root = state/'integration-evidence'/ident
    root.mkdir(parents=True)
    sources, clips = root/'sources', root/'clips'
    sources.mkdir(); clips.mkdir()
    used, hashes, scenes = set(), set(), []
    try:
        for candidate in catalog:
            tags = candidate.get('keywords') or []
            query = ' '.join(str(x.get('keyword', '')) if isinstance(x, dict) else str(x) for x in tags)
            query = query or candidate.get('description') or candidate.get('source_filename') or ''
            if not query.strip():
                continue
            index = len(scenes)
            scene = {'scene_code': f'SCENE_{index+1:03}', 'start': index*2.,
                     'end': (index+1)*2., 'duration': 2., '_sml_first': True,
                     'pexels_allow_video': True, 'pexels_prefer_landscape': False,
                     'transition_out': 'none', 'effect': 'none', 'filter': 'none', 'motion': 'none'}
            result = worker._try_sml_first_scene(
                client=client, job_id=ident, scene=scene, scene_code=scene['scene_code'],
                keywords=[query], assignment_root=clips, source_root=sources,
                used_sml_ids=used, used_hashes=hashes)
            if result:
                scenes.append(result)
            if len(scenes) == 3:
                break
        if not scenes:
            raise RuntimeError('SML supplied no matching playable approved video')
        # Preserve approval boundaries. Sparse approved libraries use the same
        # existing Pexels fallback as real Scene projects, never auto-approve
        # the owner's candidate clips or silently repeat one SML asset.
        providers = root/'provider-cache'; providers.mkdir()
        for query in ('forest river', 'city traffic'):
            if len(scenes) == 3:
                break
            index = len(scenes)
            scene = {'scene_code': f'SCENE_{index+1:03}', 'start': index*2.,
                     'end': (index+1)*2., 'duration': 2., '_sml_first': True,
                     'pexels_allow_video': True, 'pexels_prefer_landscape': False,
                     'transition_out': 'none', 'effect': 'none', 'filter': 'none', 'motion': 'none'}
            result = worker._try_sml_first_scene(
                client=client, job_id=ident, scene=scene, scene_code=scene['scene_code'],
                keywords=[query], assignment_root=clips, source_root=sources,
                used_sml_ids=used, used_hashes=hashes)
            if result is None:
                result, _ = worker._download_scene_candidate(
                    scene=scene, scene_code=scene['scene_code'], requested_type='video',
                    keyword=query, provider_cache=providers, assignment_root=clips,
                    used_asset_ids={str(x.get('asset_id')) for x in scenes},
                    used_hashes=hashes, target_width=640, target_height=360)
            if result:
                scenes.append(result)
        if len(scenes) != 3:
            raise RuntimeError('SML-first and existing Pexels fallback could not supply three scenes')
        audio, output = root/'narration.wav', root/'final.mp4'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                        'sine=frequency=330:sample_rate=44100:duration=6', str(audio)],
                       check=True, capture_output=True, timeout=30)
        config = SimpleNamespace(width=1280, height=720, fps=24, quality_crf=25,
                                 output_path=str(output), audio_path=str(audio), render_audio_path=str(audio),
                                 lut_enabled=False, captions_enabled=False,
                                 metadata={'scene_sync_plan': scenes, 'caption_segments': []})
        render_mixed_media(config)
        result = _validate_final_mp4(output, 'ffmpeg', expected_duration=6, expect_audio=True)
        # Full decoder pass proves readable frames/audio, beyond metadata only.
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(output), '-f', 'null', '-'],
                       check=True, capture_output=True, timeout=90)
        result.update(scene_count=3, sml_scenes=sum(x.get('provider') == 'sml' for x in scenes),
                      pexels_scenes=sum(x.get('provider') == 'pexels' for x in scenes),
                      distinct_clips=len(used), bytes=output.stat().st_size,
                      output=str(output), acquisition='REAL_AUTHENTICATED_SML',
                      renderer='EXISTING_SCENE_V2', queue_mutations='NONE')
        (root/'result.json').write_text(json.dumps(result, indent=2))
        # These sources are disposable consumer copies; SML masters remain owned by SML.
        shutil.rmtree(sources); shutil.rmtree(clips); shutil.rmtree(providers); audio.unlink()
        return result
    except BaseException:
        shutil.rmtree(root)
        raise


if __name__ == '__main__':
    with contextlib.redirect_stdout(io.StringIO()):
        result = verify(Path(sys.argv[1]), Path(sys.argv[2]))
    print(json.dumps(result))
