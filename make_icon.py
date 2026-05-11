#!/usr/bin/env python3
"""Generate lofi.iconset and lofi.icns — pure stdlib, no external dependencies."""
import struct, subprocess, zlib
from pathlib import Path

REPO = Path(__file__).parent

BG = (0x0c, 0x0c, 0x16)
GRADIENT = [
    (0x50, 0x50, 0xa0),
    (0x60, 0x60, 0xb8),
    (0x70, 0x70, 0xd0),
    (0x87, 0x87, 0xff),
    (0x9f, 0x9f, 0xff),
    (0xb5, 0xb5, 0xff),
    (0xc9, 0xaf, 0xf5),
    (0xd7, 0xaf, 0xd7),
]

def _lerp_color(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))

def _gradient(zone):
    idx = zone * (len(GRADIENT) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(GRADIENT) - 1)
    return _lerp_color(GRADIENT[lo], GRADIENT[hi], idx - lo)

def _make_pixels(size):
    n = 7
    pad = size * 0.13
    area_w = size - 2 * pad
    area_h = size - 2 * pad
    gap_frac = 0.30
    bar_w = area_w / (n + (n - 1) * gap_frac)
    gap = bar_w * gap_frac
    fracs = [0.28, 0.50, 0.70, 0.90, 0.70, 0.50, 0.28]

    bars = []
    for i, h in enumerate(fracs):
        x1 = pad + i * (bar_w + gap)
        bar_h = area_h * h
        y_bot = size - pad
        bars.append((x1, x1 + bar_w, y_bot - bar_h, y_bot))

    buf = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            r, g, b = BG
            for bx1, bx2, y_top, y_bot in bars:
                if bx1 <= x < bx2 and y_top <= y < y_bot:
                    zone = (y_bot - y) / (y_bot - y_top)
                    r, g, b = _gradient(zone)
                    break
            off = (y * size + x) * 4
            buf[off:off + 4] = (r, g, b, 255)
    return buf

def _png_chunk(tag, data):
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', crc)

def write_png(path, size):
    buf = _make_pixels(size)
    rows = b''.join(b'\x00' + bytes(buf[y * size * 4:(y + 1) * size * 4]) for y in range(size))
    ihdr = struct.pack('>II', size, size) + bytes([8, 6, 0, 0, 0])
    with open(path, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n')
        f.write(_png_chunk(b'IHDR', ihdr))
        f.write(_png_chunk(b'IDAT', zlib.compress(rows, 6)))
        f.write(_png_chunk(b'IEND', b''))

def main():
    iconset = REPO / 'lofi.iconset'
    iconset.mkdir(exist_ok=True)
    specs = [
        ('icon_16x16.png',      16),
        ('icon_16x16@2x.png',   32),
        ('icon_32x32.png',      32),
        ('icon_32x32@2x.png',   64),
        ('icon_128x128.png',    128),
        ('icon_128x128@2x.png', 256),
        ('icon_256x256.png',    256),
        ('icon_256x256@2x.png', 512),
        ('icon_512x512.png',    512),
        ('icon_512x512@2x.png', 1024),
    ]
    seen = {}
    for fname, size in specs:
        p = str(iconset / fname)
        if size not in seen:
            print(f'  rendering {size}x{size}…')
            write_png(p, size)
            seen[size] = p
        else:
            import shutil
            shutil.copy(seen[size], p)
    subprocess.run(['iconutil', '-c', 'icns', str(iconset), '-o', str(REPO / 'lofi.icns')], check=True)
    print('Generated lofi.icns')

if __name__ == '__main__':
    main()
