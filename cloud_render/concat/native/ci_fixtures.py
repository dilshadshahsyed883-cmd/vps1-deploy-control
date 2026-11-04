"""Small reproducible real PNG and PCM fixtures, using only Python's stdlib."""
import struct
import sys
import wave
import zlib
from pathlib import Path

root = Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=True)
def chunk(kind, data):
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
for name, color in [('a.png', (240, 60, 30)), ('b.png', (20, 100, 240))]:
    rows = b''
    for y in range(96):
        rows += b'\0' + b''.join(bytes(color if (x//16+y//16)%2 else (245,245,245)) for x in range(128))
    png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 128, 96, 8, 2, 0, 0, 0))
    (root/name).write_bytes(png + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))
with wave.open(str(root/'voice.wav'), 'wb') as voice:
    voice.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
    import math
    voice.writeframes(b''.join(struct.pack('<h', int(1500*math.sin(i*2*math.pi*440/16000))) for i in range(64000)))
