"""Build the shipped example source raster: 00_source/00-example.png

WHY NOT A PHOTOGRAPH
    The gates are a hard 6-colour screen print (spec.json: max_colors 6, with a
    palette of exactly black/white/red/blue/yellow/green). A tonal image
    cannot satisfy that by construction. The previous example was an engraved
    floral with 245 608 distinct colours, where even 256 colours cover only
    34% of the pixels. Its only "passing" candidates passed by discarding
    every bit of colour -- 1 declared colour, mae_art 114, i.e. a black
    silhouette. So it demonstrated nothing except that the gates reject
    photographs: true, but a useless thing for an example to show.

THE SUBJECT
    One disc, cut into four sectors, each pulled out along its own bisector by
    a different amount. A thing coming apart. That is the operation the whole
    pipeline performs on an image, drawn as the subject rather than explained
    in a caption -- so the idea carries without the repo having to state it.

TWO CONSTRAINTS DISCOVERED WHILE BUILDING THIS. Both measured, not guessed:

  * Pure #000000 is poison for INK_COVERAGE. Layer B separates to CMYK, and a
    solid black comes out near 400% coverage on its own -- enough to trip the
    300% limit by itself. The same design with a black ring reports 300% and
    3.4% of the sheet over the limit; swap the black for blue and the
    advisory disappears entirely. Hence: no black ink in this file.

  * Plates that overlap heavily also trip it, because coverage is summed per
    pixel. So the sectors are separated, never stacked.

An earlier draft drew a three-row "separation ladder" and it read as broken
rather than deconstructed -- the linking rules looked clipped where they
crossed shapes, and the corner ticks read as stray artefacts. One idea drawn
clearly beats a diagram of the idea.

Deterministic, offline, no network. Regenerate with:
    .venv/bin/python scripts/make_example_source.py
"""
import math
import os
import sys

from PIL import Image, ImageDraw

W = H = 1200
BG = "#FFFFFF"

# Only the spec palette. Black deliberately absent -- see the note above.
PLATES = ["#FFFF00", "#0000FF", "#FF0000", "#00FF00"]

canvas = Image.new("RGB", (W, H), BG)
img = ImageDraw.Draw(canvas)

cx = cy = W / 2
R = 300                      # outer radius of the intact disc
PULL = [0, 70, 145, 225]     # px each sector travels along its own bisector


def sector(r1, a0, a1, steps=64):
    """Wedge from the centre out to r1, spanning a0..a1 degrees."""
    pts = []
    for i in range(steps + 1):
        a = math.radians(a0 + (a1 - a0) * i / steps)
        pts.append((cx + r1 * math.cos(a), cy + r1 * math.sin(a)))
    return pts


# The four sectors, each nudged out along its own bisector. The first stays
# home so the disc still reads as a disc; the others walk away from it. The
# sectors are separated, never stacked.
for i, (a0, a1) in enumerate([(-135, -45), (-45, 45), (45, 135), (135, 225)]):
    mid_deg = (a0 + a1) / 2
    d = PULL[i]
    ox = cx + d * math.cos(math.radians(mid_deg))
    oy = cy + d * math.sin(math.radians(mid_deg))
    pts = [(px + (ox - cx), py + (oy - cy)) for px, py in sector(R, a0, a1)]
    img.polygon(pts, fill=PLATES[i])

# The core, left behind at the centre: the thing that got taken apart.
img.ellipse([cx - 58, cy - 58, cx + 58, cy + 58], fill=PLATES[0])

out = sys.argv[1] if len(sys.argv) > 1 else "00_source/00-example.png"
canvas.save(out, "PNG")

cols = canvas.getcolors(maxcolors=1 << 24) or []
black = sum(c for c, rgb in cols if max(rgb) < 40)
print("%s  %d distinct colours (%d black px)" % (os.path.basename(out), len(cols), black))
assert len(cols) <= 6, "must stay inside the 6-colour gate"
assert black == 0, "no black: it alone trips INK_COVERAGE"
print("  ok: <=6 colours, no black, no gradients, flat fills only")
