#!/usr/bin/env python3
"""Turns the simulator's photo session (raw RGB565 frames written when
IOS_SHOTS=<folder> is set) into the PNG and GIF images of the README.

    python3 scripts/photos.py <raw folder> <output folder> [scale]
"""
import sys
from pathlib import Path

from PIL import Image

W, H = 320, 240


def load(path):
    data = path.read_bytes()
    img = Image.new("RGB", (W, H))
    px = img.load()
    for i in range(W * H):
        v = data[2 * i] | (data[2 * i + 1] << 8)
        r, g, b = (v >> 11) & 31, (v >> 5) & 63, v & 31
        px[i % W, i // W] = ((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))
    return img


def main():
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    scale = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    dst.mkdir(parents=True, exist_ok=True)
    gifs = {}
    for raw in sorted(src.glob("*.raw")):
        name = raw.stem
        if name.startswith("gif"):
            prefix = name.rsplit("_", 1)[0]
            gifs.setdefault(prefix, []).append(raw)
            continue
        img = load(raw).resize((W * scale, H * scale), Image.NEAREST)
        img.save(dst / f"{name}.png")
    for prefix, frames in gifs.items():
        imgs = [load(f).resize((W * 3 // 2, H * 3 // 2), Image.NEAREST) for f in frames]
        delay = 40
        imgs[0].save(dst / f"{prefix[3:]}.gif", save_all=True, append_images=imgs[1:],
                     duration=delay, loop=0, optimize=True)
    print(f"{dst}: done")


if __name__ == "__main__":
    main()
