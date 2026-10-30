"""Draws btlab's PWA icons with the standard library only (SPEC.md §20.5): python scripts/make_icons.py.

A rising line on a dark square. The mark stays inside the centre 70%, so the 512 px icon also serves as maskable.
"""

import struct
import zlib
from pathlib import Path

ICONS = Path(__file__).resolve().parents[1] / "btlab" / "static" / "icons"
SIZES = {"icon-192.png": 192, "icon-512.png": 512, "apple-touch-icon.png": 180}
BACKGROUND = (15, 23, 42)
BASELINE = (100, 116, 139)
LINE = (34, 197, 94)
# Points as fractions of the side, inside the maskable safe zone.
RISING = ((0.24, 0.64), (0.42, 0.50), (0.56, 0.57), (0.76, 0.32))
GROUND = ((0.22, 0.74), (0.78, 0.74))
WIDTH = 0.055


def main() -> None:
    ICONS.mkdir(parents=True, exist_ok=True)
    for name, size in SIZES.items():
        (ICONS / name).write_bytes(png(size, draw(size)))
        print(f"wrote {ICONS / name}")


def draw(size: int) -> list[bytearray]:
    """RGB rows: each stroke covers a pixel by its distance from the stroke, which smooths the edges."""
    rows = [bytearray(bytes(BACKGROUND) * size) for _ in range(size)]
    for colour, points in ((BASELINE, GROUND), (LINE, RISING)):
        segments = [((a[0] * size, a[1] * size), (b[0] * size, b[1] * size)) for a, b in zip(points, points[1:])]
        half = WIDTH * size / 2
        for y in range(size):
            for x in range(size):
                distance = min(_distance(x + 0.5, y + 0.5, *segment) for segment in segments)
                cover = min(1.0, max(0.0, half + 0.5 - distance))
                if cover:
                    offset = 3 * x
                    rows[y][offset:offset + 3] = bytes(
                        round(old + (new - old) * cover) for old, new in zip(rows[y][offset:offset + 3], colour))
    return rows


def _distance(x: float, y: float, start: tuple[float, float], end: tuple[float, float]) -> float:
    (ax, ay), (bx, by) = start, end
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
    return ((x - ax - t * dx) ** 2 + (y - ay - t * dy) ** 2) ** 0.5


def png(size: int, rows: list[bytearray]) -> bytes:
    """An 8-bit RGB PNG, each row with filter 0."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    data = zlib.compress(b"".join(b"\x00" + bytes(row) for row in rows), 9)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", data) + chunk(b"IEND", b"")


if __name__ == "__main__":
    main()
