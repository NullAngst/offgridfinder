#!/usr/bin/env python3
# OffGridFinder icon generator.
# Copyright (C) 2026 Zack (NullAngst). Licensed under the GNU GPL v3 or later.
"""
Draws the OffGridFinder icon with Pillow and writes every format the build needs:

    assets/icon.png       1024x1024 master
    assets/icon-256.png   window icon used by Tk at runtime
    assets/icon.ico       Windows (16 to 256 px)
    assets/icon.icns      macOS

Run from the repository root:  python3 tools/make_icon.py
The design is a map pin sitting on concentric range rings, on a dark green tile.
"""

import math
import os
import sys

from PIL import Image, ImageDraw, ImageFilter

SS = 4                      # supersampling factor for smooth edges
SIZE = 1024
BG_TOP = (38, 70, 53)
BG_BOTTOM = (21, 40, 30)
RING = (170, 205, 170)
PIN = (236, 124, 44)
PIN_SHADE = (196, 92, 24)
WHITE = (250, 247, 240)


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def draw_master():
    s = SIZE * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))

    # Rounded-square tile with a vertical gradient.
    grad = Image.new("RGBA", (s, s))
    gd = ImageDraw.Draw(grad)
    for y in range(s):
        gd.line([(0, y), (s, y)], fill=lerp(BG_TOP, BG_BOTTOM, y / s) + (255,))
    mask = Image.new("L", (s, s), 0)
    margin = int(s * 0.04)
    ImageDraw.Draw(mask).rounded_rectangle([margin, margin, s - margin, s - margin],
                                           radius=int(s * 0.2), fill=255)
    img.paste(grad, (0, 0), mask)

    # Range rings centered where the pin touches down.
    cx, cy = s * 0.5, s * 0.66
    rings = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    rd = ImageDraw.Draw(rings)
    for i, (r, alpha, width) in enumerate(((0.40, 70, 0.012), (0.28, 115, 0.014), (0.16, 170, 0.016))):
        rr = r * s
        rd.ellipse([cx - rr, cy - rr * 0.42, cx + rr, cy + rr * 0.42],
                   outline=RING + (alpha,), width=int(width * s))
    # Radius tick: a dashed line from the center out to the outer ring.
    dash, gap = s * 0.025, s * 0.018
    x = cx
    while x < cx + 0.40 * s - dash:
        rd.line([(x, cy), (x + dash, cy)], fill=RING + (190,), width=int(0.012 * s))
        x += dash + gap
    rd.ellipse([cx + 0.40 * s - 0.02 * s, cy - 0.02 * s, cx + 0.40 * s + 0.02 * s, cy + 0.02 * s],
               fill=RING + (230,))
    rings.putalpha(Image.composite(rings.getchannel("A"), Image.new("L", (s, s), 0), mask))
    img = Image.alpha_composite(img, rings)

    # Soft shadow under the pin.
    shadow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse([cx - 0.07 * s, cy - 0.022 * s, cx + 0.07 * s, cy + 0.022 * s],
                                   fill=(0, 0, 0, 120))
    img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(s * 0.01)))

    # Map pin: circle head plus a tapered point that meets the ring center.
    pin = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    pd = ImageDraw.Draw(pin)
    head_r = 0.17 * s
    hx, hy = cx, s * 0.36
    tip = (cx, cy - 0.005 * s)
    ang = math.radians(38)
    left = (hx - head_r * math.cos(ang), hy + head_r * math.sin(ang))
    right = (hx + head_r * math.cos(ang), hy + head_r * math.sin(ang))
    pd.polygon([left, right, tip], fill=PIN + (255,))
    pd.ellipse([hx - head_r, hy - head_r, hx + head_r, hy + head_r], fill=PIN + (255,))
    # Shade the right half for depth.
    shade = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shade)
    sd.pieslice([hx - head_r, hy - head_r, hx + head_r, hy + head_r], -90, 90, fill=PIN_SHADE + (255,))
    sd.polygon([(hx, hy), right, tip], fill=PIN_SHADE + (255,))
    sd.polygon([(hx, hy - head_r), (hx, tip[1]), (hx + head_r, hy)], fill=PIN_SHADE + (255,))
    shade.putalpha(Image.composite(shade.getchannel("A"), Image.new("L", (s, s), 0), pin.getchannel("A")))
    pin = Image.alpha_composite(pin, Image.blend(Image.new("RGBA", (s, s), (0, 0, 0, 0)), shade, 0.55))
    # Hole in the pin head.
    hole_r = 0.065 * s
    ImageDraw.Draw(pin).ellipse([hx - hole_r, hy - hole_r, hx + hole_r, hy + hole_r], fill=WHITE + (255,))
    img = Image.alpha_composite(img, pin)

    return img.resize((SIZE, SIZE), Image.LANCZOS)


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = os.path.join(root, "assets")
    os.makedirs(out, exist_ok=True)
    master = draw_master()
    master.save(os.path.join(out, "icon.png"), optimize=True)
    master.resize((256, 256), Image.LANCZOS).save(os.path.join(out, "icon-256.png"), optimize=True)
    master.save(os.path.join(out, "icon.ico"),
                sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    try:
        master.save(os.path.join(out, "icon.icns"))
    except Exception as e:  # older Pillow builds may lack ICNS writing
        print(f"Could not write icon.icns: {e}", file=sys.stderr)
    print(f"Icons written to {out}")


if __name__ == "__main__":
    main()
