import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time
import unicodedata
from PIL import Image, ImageFilter, ImageOps

IMAGES = {'.jpg', '.jpeg', '.png', '.webp'}
AUDIO = {'.mp3', '.wav', '.m4a'}
FORMATS = {'1080p': (1920, 1080), '720p': (1280, 720), '9:16': (1080, 1920)}


class Cancelled(Exception):
    pass


def safe_name(name):
    # Treat both Windows and POSIX separators as unsafe, preserving Unicode.
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', unicodedata.normalize('NFC', name))
    value = value.strip(' .')
    while len(value.encode('utf-8')) > 220:
        value = value[:-1]
    return value or 'audio'


def output_name(audio):
    return safe_name(Path(audio).stem) + '.mp4'


def match_key(name):
    return unicodedata.normalize('NFC', Path(name).stem).casefold()


def fingerprint(item):
    return {k: item.get(k) for k in ('ID', 'Name', 'Size', 'ModTime', 'Hashes', 'Version')}


def pairs(images, audio):
    groups = {}
    for kind, items, extensions in [('images', images, IMAGES), ('audio', audio, AUDIO)]:
        for item in items:
            if item.get('IsDir') or Path(item['Name']).suffix.lower() not in extensions:
                continue
            group = groups.setdefault(match_key(item['Name']), {'images': [], 'audio': []})
            group[kind].append(item)
    result = []
    for key, group in sorted(groups.items()):
        ni, na = len(group['images']), len(group['audio'])
        stage = ('Conflict / Needs Selection' if ni > 1 or na > 1 else
                 'Waiting for Audio' if not na else 'Waiting for Image' if not ni else 'Matched')
        result.append(dict(key=key, stage=stage, **group))
    return result


def digest(path, algorithm='sha256'):
    h = hashlib.new(algorithm)
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()


def run(command, cancelled=lambda: False, progress=None, timeout=7200, telemetry=None):
    # Temporary files avoid pipe deadlocks and unbounded in-memory FFmpeg logs.
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(command, stdout=out, stderr=err, start_new_session=True)
        start = time.monotonic()
        try:
            while proc.poll() is None:
                if cancelled():
                    raise Cancelled('Cancelled by user or worker shutdown')
                if time.monotonic()-start > timeout:
                    raise RuntimeError('External process timed out')
                if progress:
                    pos = os.fstat(out.fileno()).st_size
                    chunk = os.pread(out.fileno(), 4096, max(0, pos-4096)).decode('utf-8', errors='replace')
                    times = re.findall(r'out_time_us=(\d+)', chunk)
                    if times:
                        progress(int(times[-1])/1_000_000)
                    if telemetry:
                        fps = re.findall(r'(?:^|\n)fps=\s*([0-9.]+)', chunk)
                        speeds = re.findall(r'(?:^|\n)speed=\s*([0-9.]+)x', chunk)
                        if fps or speeds:
                            telemetry({'fps': float(fps[-1]) if fps else None,
                                       'speed': float(speeds[-1]) if speeds else None,
                                       'seconds': int(times[-1])/1_000_000 if times else None})
                time.sleep(.15)
            out.seek(0)
            stdout = out.read().decode('utf-8', errors='replace')
            if proc.returncode:
                err.seek(0, 2)
                length = err.tell()
                err.seek(max(0, length-3000))
                raise RuntimeError(err.read().decode('utf-8', errors='replace'))
            if cancelled():
                raise Cancelled('Cancelled')
            return stdout
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()


def probe(path):
    return json.loads(run(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)], timeout=30))


def audio_duration(path):
    data = probe(path)
    if not any(s.get('codec_type') == 'audio' for s in data['streams']):
        raise ValueError('Audio stream required')
    value = float(data['format']['duration'])
    if not math.isfinite(value) or value <= 0 or value > 24*3600:
        raise ValueError('Invalid audio duration; maximum 24 hours')
    return value


def validate(path, duration, cancelled=lambda: False):
    data = probe(path)
    streams = {s['codec_type']: s for s in data['streams']}
    if streams.get('video', {}).get('codec_name') != 'h264' or streams.get('audio', {}).get('codec_name') != 'aac':
        raise ValueError('H.264 video and AAC audio required')
    for kind in ('video', 'audio'):
        if abs(float(streams[kind].get('duration', data['format']['duration']))-duration) > .15:
            raise ValueError('Output duration does not match complete audio')
    # Decode all packets, not merely the container header.
    run(['ffmpeg', '-v', 'error', '-xerror', '-i', str(path), '-f', 'null', '-'], cancelled, timeout=max(120, duration*3))
    return {'duration': duration, 'size': Path(path).stat().st_size, 'md5': digest(path, 'md5')}


def render(image, audio, output, temporary, resolution='1080p', fit='fit', cancelled=lambda: False, progress=None, telemetry=None):
    if resolution not in FORMATS or fit not in {'fit', 'blur'}:
        raise ValueError('Unsupported output options')
    duration = audio_duration(audio)
    width, height = FORMATS[resolution]
    with Image.open(image) as source:
        source = ImageOps.exif_transpose(source).convert('RGB')
        foreground = ImageOps.contain(source, (width, height), Image.Resampling.LANCZOS)
        frame = (ImageOps.fit(source, (width, height), Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(28))
                 if fit == 'blur' else Image.new('RGB', (width, height), 'black'))
        frame.paste(foreground, ((width-foreground.width)//2, (height-foreground.height)//2))
        frame.save(temporary / 'frame.png')
    partial = temporary / 'partial.mp4'
    run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', '-loop', '1',
         '-framerate', '25', '-i', str(temporary / 'frame.png'), '-i', str(audio),
         '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'libx264', '-preset', 'veryfast',
         '-tune', 'stillimage', '-crf', '20', '-threads', '2', '-pix_fmt', 'yuv420p',
         '-c:a', 'aac', '-b:a', '192k', '-t', str(duration), '-shortest',
         '-movflags', '+faststart', '-progress', 'pipe:1', str(partial)],
        cancelled, progress, max(120, duration*8), telemetry=telemetry)
    if output.exists():
        raise RuntimeError('Local output collision')
    partial.replace(output)
    return duration
