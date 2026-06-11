"""Generates the Script Index Updater brand icon (icon.ico + icon.png).

Design: amber radar sweep on dark slate, matching the app's mission-control
theme - rounded-square badge, double radar ring, gradient sweep wedge,
crosshair ticks, and a "target acquired" blip.

Run:  python make_icon.py
"""
import math
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
S = 2048                      # supersampled canvas
C = S // 2                    # center
AMBER = (245, 166, 35)
BLIP = (255, 200, 110)


def rounded_badge():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    # vertical gradient #16202e -> #0a0e15
    top, bottom = (22, 32, 46), (10, 14, 21)
    grad = Image.new("RGBA", (S, S))
    px = grad.load()
    for y in range(S):
        t = y / (S - 1)
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        for x in range(S):
            px[x, y] = (r, g, b, 255)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=255)
    img.paste(grad, (0, 0), mask)
    # subtle border
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([6, 6, S - 7, S - 7], radius=int(S * 0.22),
                        outline=(58, 76, 98, 255), width=14)
    return img


def polar(r, deg):
    a = math.radians(deg)
    return (C + r * math.cos(a), C + r * math.sin(a))


def main():
    img = rounded_badge()
    d = ImageDraw.Draw(img, "RGBA")

    ring_r = int(S * 0.295)
    inner_r = int(S * 0.185)

    # sweep wedge: 70 deg fading toward the trailing edge, leading edge at -15 deg
    sweep = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sweep)
    start, end = -90, -18
    steps = 72
    for i in range(steps):
        a0 = start + (end - start) * i / steps
        a1 = start + (end - start) * (i + 1) / steps
        t = (i / steps) ** 1.4
        alpha = int(12 + 215 * t)
        col = (int(AMBER[0] + (255 - AMBER[0]) * t * .7),
               int(AMBER[1] + (205 - AMBER[1]) * t * .7),
               int(AMBER[2] + (95 - AMBER[2]) * t * .7))
        sd.pieslice([C - ring_r, C - ring_r, C + ring_r, C + ring_r],
                    a0, a1 + 0.6, fill=col + (alpha,))
    img.alpha_composite(sweep)

    # radar rings
    w_outer, w_inner = 44, 22
    d.ellipse([C - ring_r, C - ring_r, C + ring_r, C + ring_r],
              outline=AMBER + (235,), width=w_outer)
    d.ellipse([C - inner_r, C - inner_r, C + inner_r, C + inner_r],
              outline=AMBER + (110,), width=w_inner)

    # crosshair ticks at the four cardinal points
    for ang in (0, 90, 180, 270):
        p1 = polar(ring_r - 70, ang)
        p2 = polar(ring_r + 70, ang)
        d.line([p1, p2], fill=AMBER + (160,), width=30)

    # center dot
    cr = int(S * 0.040)
    d.ellipse([C - cr, C - cr, C + cr, C + cr], fill=AMBER + (255,))

    # blip (target acquired) between the rings, lower-right of the sweep
    bx, by = polar(int(S * 0.243), -47)
    br = int(S * 0.027)
    d.ellipse([bx - br * 1.7, by - br * 1.7, bx + br * 1.7, by + br * 1.7],
              outline=BLIP + (130,), width=14)
    d.ellipse([bx - br, by - br, bx + br, by + br], fill=BLIP + (255,))

    final = img.resize((1024, 1024), Image.LANCZOS)
    final.save(os.path.join(HERE, "icon.png"))
    final.save(os.path.join(HERE, "icon.ico"), format="ICO",
               sizes=[(256, 256), (128, 128), (64, 64), (48, 48),
                      (32, 32), (24, 24), (16, 16)])
    print("wrote icon.png (1024) and icon.ico (16-256)")


if __name__ == "__main__":
    main()
