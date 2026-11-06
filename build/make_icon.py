"""Draw the app icon, build/flangecutter.ico: a mold cut in two, with a flange and bolts on each side.

Part A is blue and part B orange, the colors the app uses for parts. Only numpy and the standard
library are needed (no Pillow). Run it again after changing the drawing:

    python build/make_icon.py [output.ico] [preview.png]
"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

import numpy as np

SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 256)      # what Windows asks for at 100-300 % display scaling
BLUE, ORANGE, STEEL = (0x2F, 0x6F, 0xDE), (0xF2, 0x8C, 0x28), (0x4B, 0x55, 0x63)

# Boxes (x0, y0, x1, y1, corner radius) on a 256 x 256 canvas, y pointing down. Edges lie on a
# 16-unit grid, so the 16, 32, 48, 64 and 96 px images stay sharp.
# Each part = (wall + flange) - cavity; the cut is the gap from y = 112 to y = 144.
PART_A = [(64, 16, 192, 112, 24), (16, 80, 240, 112, 4)], (96, 48, 160, 140, 8)
PART_B = [(64, 144, 192, 240, 24), (16, 144, 240, 176, 4)], (96, 116, 160, 208, 8)
BOLTS = [(32, 64, 48, 192, 0), (16, 64, 64, 80, 4), (16, 176, 64, 192, 4)]      # shank, head, nut (left)
BOLTS += [(256 - x1, y0, 256 - x0, y1, r) for x0, y0, x1, y1, r in BOLTS]       # the same on the right


def inside(x: np.ndarray, y: np.ndarray, box: tuple) -> np.ndarray:
    """Points inside a box with rounded corners."""
    x0, y0, x1, y1, r = box
    dx = np.maximum(np.abs(x - (x0 + x1) / 2) - ((x1 - x0) / 2 - r), 0)
    dy = np.maximum(np.abs(y - (y0 + y1) / 2) - ((y1 - y0) / 2 - r), 0)
    return dx * dx + dy * dy <= r * r


def render(size: int) -> np.ndarray:
    """RGBA image (straight alpha) with 8 x 8 samples per pixel for smooth edges."""
    n = 8
    t = (np.arange(size * n) + 0.5) * (256 / (size * n))
    x, y = np.meshgrid(t, t)
    layers = []
    for boxes, cavity, color in ((*PART_A, BLUE), (*PART_B, ORANGE)):
        shape = (inside(x, y, boxes[0]) | inside(x, y, boxes[1])) & ~inside(x, y, cavity)
        layers.append((shape, color))
    layers.append((np.any([inside(x, y, b) for b in BOLTS], axis=0), STEEL))
    rgb, alpha = np.zeros((size, size, 3)), np.zeros((size, size))
    for shape, color in layers:                       # paint each layer over the ones below
        cover = shape.reshape(size, n, size, n).mean(axis=(1, 3))
        rgb = rgb * (1 - cover[..., None]) + np.array(color) / 255 * cover[..., None]
        alpha = alpha * (1 - cover) + cover
    straight = np.where(alpha[..., None] > 0, rgb / np.maximum(alpha, 1e-9)[..., None], 0)
    return np.round(np.dstack([straight, alpha]) * 255).astype(np.uint8)


def png(rgba: np.ndarray) -> bytes:
    h, w, _ = rgba.shape
    raw = b"".join(b"\x00" + row.tobytes() for row in rgba)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def dib(rgba: np.ndarray) -> bytes:
    """Classic 32-bit icon bitmap (bottom-up BGRA plus a 1-bit transparency mask)."""
    h, w, _ = rgba.shape
    pixels = rgba[::-1][..., [2, 1, 0, 3]].tobytes()
    mask = np.packbits(rgba[::-1, :, 3] == 0, axis=1)
    mask = np.pad(mask, ((0, 0), (0, -mask.shape[1] % 4))).tobytes()     # rows padded to 4 bytes
    return struct.pack("<IiiHHIIiiII", 40, w, 2 * h, 1, 32, 0, len(pixels) + len(mask), 0, 0, 0, 0) + pixels + mask


def ico(images: dict[int, bytes]) -> bytes:
    head = struct.pack("<HHH", 0, 1, len(images))
    offset, entries, blobs = 6 + 16 * len(images), b"", b""
    for size, data in images.items():
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset + len(blobs))
        blobs += data
    return head + entries + blobs


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("flangecutter.ico")
    images = {s: png(render(s)) if s >= 256 else dib(render(s)) for s in SIZES}
    out.write_bytes(ico(images))
    print(f"wrote {out} ({out.stat().st_size} bytes)")
    if len(sys.argv) > 2:
        Path(sys.argv[2]).write_bytes(png(render(256)))


if __name__ == "__main__":
    main()
