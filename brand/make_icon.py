"""App icon: a TS404-style knob on the dark Pafu tile, pointer and arc in the pink to purple
gradient. Writes app.ico (setup, exe, shortcuts) and icon_512.png (README).
wizard*.png / corner*.png are the shared Pafu Igniters / Mickey Perry installer art."""
import math
import os
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
BLACK = (11, 11, 15)
PINK, PURPLE = (255, 45, 135), (180, 107, 255)
KNOB = (52, 59, 66)      # panel grey of index.html
KNOB_HI = (108, 117, 127)


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def draw(S=1024):
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, S - 1, S - 1), radius=S // 5, fill=BLACK + (255,))
    cx = cy = S / 2
    # value arc: 7 o'clock round to 5 o'clock, like FL's knobs
    start, end = 135, 405
    r_arc, w_arc = S * 0.40, S * 0.055
    for a in range(start, end):
        t = (a - start) / (end - start)
        d.arc((cx - r_arc, cy - r_arc, cx + r_arc, cy + r_arc), a, a + 1.5, fill=lerp(PINK, PURPLE, t) + (255,), width=int(w_arc))
    # knob body with a soft top-left highlight
    r = S * 0.30
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=KNOB_HI + (255,))
    d.ellipse((cx - r * 0.96, cy - r * 0.92, cx + r * 0.96, cy + r * 1.0), fill=KNOB + (255,))
    # pointer at about 70 %
    ang = math.radians(start + 0.7 * (end - start))
    x0, y0 = cx + math.cos(ang) * r * 0.35, cy + math.sin(ang) * r * 0.35
    x1, y1 = cx + math.cos(ang) * r * 0.92, cy + math.sin(ang) * r * 0.92
    d.line((x0, y0, x1, y1), fill=lerp(PINK, PURPLE, 0.7) + (255,), width=int(S * 0.045))
    d.ellipse((x1 - S * 0.022, y1 - S * 0.022, x1 + S * 0.022, y1 + S * 0.022), fill=lerp(PINK, PURPLE, 0.7) + (255,))
    return img


if __name__ == "__main__":
    tile = draw()
    out = tile.resize((256, 256), Image.LANCZOS)
    out.save(os.path.join(HERE, "app.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    tile.resize((512, 512), Image.LANCZOS).save(os.path.join(HERE, "icon_512.png"))
    print("app.ico, icon_512.png written")
