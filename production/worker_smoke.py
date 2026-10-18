"""Verify local CPU synthesis against the exact official model artifacts."""
from pathlib import Path
import tempfile

from production.workers import model_files


def main():
    from kokoro_onnx import Kokoro
    import soundfile as sf
    with tempfile.TemporaryDirectory(prefix='vps1-kokoro-') as tmp:
        models = Path(tmp)
        model_files(models)
        engine = Kokoro(str(models / 'kokoro-v1.0.int8.onnx'), str(models / 'voices-v1.0.bin'))
        audio, rate = engine.create('The voice service is ready.', voice='af_heart', speed=1.0, lang='en-us')
        assert len(audio) > 1000 and rate == 24000
        target = models / 'test.wav'
        sf.write(target, audio, rate)
        assert target.stat().st_size > 2048
        print('KOKORO_REAL_CPU_SYNTHESIS=PASS')


if __name__ == '__main__':
    main()
