"""Generate the application icon in every format the installers need.

Written by hand, with no image library, for two reasons: adding Pillow to the
build just to draw a thermometer is a poor trade, and a generated icon is a
text file in the repository rather than three binary blobs nobody can diff.

    python packaging/make_icons.py            # writes icon.png, .ico, .icns

PNG, ICO and ICNS are all simple containers. PNG is chunks with CRCs; ICO is a
small directory followed by embedded PNGs; ICNS is a magic word followed by
type-tagged PNGs. Everything below is those three layouts written out.
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Palette: the board is a scientific instrument, so a cool, calm mark rather
# than a photographic one. Background circle, glass, mercury, scale ticks.
BACKGROUND = (24, 34, 45, 255)
GLASS = (232, 238, 243, 255)
FLUID = (214, 69, 65, 255)
TICK = (120, 138, 154, 255)
TRANSPARENT = (0, 0, 0, 0)

#: Sizes actually used: ICO wants small ones, ICNS and Linux want large ones.
PNG_SIZES = (16, 32, 48, 64, 128, 256, 512, 1024)
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
#: ICNS type tags, per Apple's icon format. The "ic" family is PNG-encoded.
ICNS_TYPES = {
    16: b"icp4", 32: b"icp5", 64: b"icp6", 128: b"ic07",
    256: b"ic08", 512: b"ic09", 1024: b"ic10",
}


def _blend(dst: tuple[int, ...], src: tuple[int, ...], alpha: float) -> tuple[int, ...]:
    a = max(0.0, min(1.0, alpha))
    return tuple(round(d + (s - d) * a) for d, s in zip(dst, src, strict=True))


def _coverage(distance: float, edge: float) -> float:
    """Antialiasing: how much of a pixel a shape covers near its boundary.

    ``distance`` is signed, negative inside. Feathering over one pixel is all
    an icon needs and keeps the arithmetic obvious.
    """
    return max(0.0, min(1.0, 0.5 - distance / max(edge, 1e-6)))


def draw(size: int) -> bytearray:
    """Render the icon at ``size`` pixels square as RGBA rows."""
    s = float(size)
    px = 1.0 / s                     # one pixel, in normalised units
    pixels = bytearray(size * size * 4)

    # Geometry in a 0..1 square, so it scales exactly.
    bulb_cx, bulb_cy, bulb_r = 0.5, 0.735, 0.145
    stem_w = 0.088                   # glass outer width
    stem_top = 0.16
    fluid_w = 0.046
    fluid_top = 0.30                 # reading a little over half scale
    fluid_bulb_r = 0.105

    for y in range(size):
        fy = (y + 0.5) / s
        row = y * size * 4
        for x in range(size):
            fx = (x + 0.5) / s
            colour = TRANSPARENT

            # Rounded-square background, the shape macOS and Windows expect.
            corner = 0.22
            dx = abs(fx - 0.5) - (0.5 - corner)
            dy = abs(fy - 0.5) - (0.5 - corner)
            outside = ((max(dx, 0.0) ** 2 + max(dy, 0.0) ** 2) ** 0.5
                       + min(max(dx, dy), 0.0) - corner + 0.02)
            cover = _coverage(outside, px)
            if cover <= 0.0:
                continue
            colour = _blend(TRANSPARENT, BACKGROUND, cover)

            # Glass: a capsule from the stem top down into the bulb.
            in_stem = abs(fx - bulb_cx) - stem_w / 2
            stem_d = max(in_stem, stem_top - fy, fy - bulb_cy)
            bulb_d = ((fx - bulb_cx) ** 2 + (fy - bulb_cy) ** 2) ** 0.5 - bulb_r
            glass_d = min(stem_d, bulb_d)
            colour = _blend(colour, GLASS, _coverage(glass_d, px))

            # Scale ticks down the right of the stem, drawn before the fluid so
            # they read as marks on the glass rather than floating alongside it.
            for i in range(6):
                ty = stem_top + 0.06 + i * 0.062
                tick_len = 0.055 if i % 2 == 0 else 0.034  # long tick every other one
                tick_d = max(
                    abs(fy - ty) - 0.006,
                    (bulb_cx + stem_w / 2 + 0.012) - fx,
                    fx - (bulb_cx + stem_w / 2 + 0.012 + tick_len),
                )
                colour = _blend(colour, TICK, _coverage(tick_d, px))

            # Fluid column and its bulb, inside the glass.
            fluid_stem = max(abs(fx - bulb_cx) - fluid_w / 2,
                             fluid_top - fy, fy - bulb_cy)
            fluid_bulb = (((fx - bulb_cx) ** 2 + (fy - bulb_cy) ** 2) ** 0.5
                          - fluid_bulb_r)
            colour = _blend(colour, FLUID, _coverage(min(fluid_stem, fluid_bulb), px))

            i = row + x * 4
            pixels[i:i + 4] = bytes(colour)
    return pixels


def _chunk(tag: bytes, payload: bytes) -> bytes:
    body = tag + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))


def encode_png(size: int, pixels: bytes) -> bytes:
    """Minimal RGBA PNG: one IHDR, one IDAT, one IEND."""
    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)  # filter type 0 (None): an icon compresses fine without
        raw += pixels[y * stride:(y + 1) * stride]
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", header)
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + _chunk(b"IEND", b""))


def encode_ico(pngs: dict[int, bytes]) -> bytes:
    """ICO holding PNG-compressed images (Vista and later read these)."""
    sizes = sorted(pngs)
    out = struct.pack("<HHH", 0, 1, len(sizes))          # reserved, type=icon, count
    offset = len(out) + 16 * len(sizes)
    entries, blobs = b"", b""
    for size in sizes:
        data = pngs[size]
        # 256 is stored as 0 in the single-byte dimension fields.
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0,
                               1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    return out + entries + blobs


def encode_icns(pngs: dict[int, bytes]) -> bytes:
    """ICNS holding PNG images under their Apple type tags."""
    body = b""
    for size, tag in sorted(ICNS_TYPES.items()):
        data = pngs.get(size)
        if data is None:
            continue
        body += tag + struct.pack(">I", len(data) + 8) + data
    return b"icns" + struct.pack(">I", len(body) + 8) + body


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE
    out_dir.mkdir(parents=True, exist_ok=True)

    pngs = {size: encode_png(size, bytes(draw(size))) for size in PNG_SIZES}
    # A few ICO sizes are not in the PNG set; render those too.
    for size in ICO_SIZES:
        pngs.setdefault(size, encode_png(size, bytes(draw(size))))

    (out_dir / "icon.png").write_bytes(pngs[512])
    (out_dir / "icon.ico").write_bytes(encode_ico({s: pngs[s] for s in ICO_SIZES}))
    (out_dir / "icon.icns").write_bytes(
        encode_icns({s: pngs[s] for s in ICNS_TYPES if s in pngs}))
    for size in (16, 32, 48, 64, 128, 256, 512):
        (out_dir / f"icon-{size}.png").write_bytes(pngs[size])

    for name in ("icon.png", "icon.ico", "icon.icns"):
        path = out_dir / name
        print(f"  {path.relative_to(out_dir.parent) if out_dir.parent in path.parents else path}"
              f"  {path.stat().st_size:,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
