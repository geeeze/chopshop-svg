#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
snap_colors.py -- snap every fill/stroke/stop-color to the spec palette.

Pipeline stage 3 (cleanup). This is the half of the SVGO step that matters for
spot-colour print, implemented in pure Python so it needs no Node.js runtime
(scripts/svgo_print.yml covers the rest wherever node is available).

Why it matters: on a screen-print job each distinct colour is a separate screen
and a separate charge. A stray picker colour that is three values off the
intended red is not a visual problem, but it *is* a seventh ink -- and the
rendered-ink gate in preflight.py will (correctly) fail the job for it. Snapping
fixes the cause rather than silencing the check.

Rules respected
---------------
* The effective colour is resolved through the CSS cascade, exactly as
  validate_svg.py does, so a colour set in a <style> block is seen.
* A colour is written back where it came from. When it came from a stylesheet,
  an inline style attribute is added instead of editing the rule -- inline wins
  in the cascade, so this changes the result without rewriting the file\'s CSS.
* Colours further than --tolerance (Euclidean in sRGB) from every palette entry
  are reported and left alone unless --force is given. Guessing is worse than
  reporting: a "nearest" colour 200 units away is a different colour.
* Nothing else in the document is touched: no geometry rewriting, no viewBox or
  width/height changes, so stroke-width and physical size stay meaningful.

Usage
-----
::

    python3 scripts/snap_colors.py art.svg spec.json --dry-run
    python3 scripts/snap_colors.py art.svg spec.json -o 03_cleaned/art.svg
    python3 scripts/snap_colors.py art.svg spec.json --tolerance 40 --force

Exit codes: 0 = nothing left unsnapped, 1 = colours remain off palette,
2 = bad usage.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from lxml import etree

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import validate_svg  # noqa: E402

PROPS = ("fill", "stroke", "stop-color")


def hex_to_rgb(colour):
    """'#rrggbb' -> (r, g, b)."""
    value = colour.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def rgb_to_hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(c)))) for c in rgb)


def distance(a, b):
    """Euclidean distance in sRGB.  A rough perceptual proxy, deliberately."""
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def nearest_palette(colour, palette):
    """Return (palette_colour, distance) for the closest entry."""
    rgb = hex_to_rgb(colour)
    best = None
    for entry in palette:
        gap = distance(rgb, hex_to_rgb(entry))
        if best is None or gap < best[1]:
            best = (entry, gap)
    return best


def serialise_style(declarations):
    return "; ".join("%s:%s" % (k, v) for k, v in declarations.items())


def write_property(element, prop, value, source, declarations):
    """Write a resolved colour back to the place the cascade read it from.

    Returns a short description of where it went, for the report.
    """
    if source == "attr":
        element.set(prop, value)
        return "attribute"
    if source == "style":
        declarations[prop] = value
        element.set("style", serialise_style(declarations))
        return "inline style"
    if source == "css":
        # Editing the rule would affect every element it matches; an inline
        # declaration out-ranks the stylesheet for this element only.
        declarations[prop] = value
        element.set("style", serialise_style(declarations))
        return "inline override of stylesheet rule"
    return "nowhere"


def snap(svg_path, spec_path, tolerance, force, out_path, dry_run):
    with open(spec_path, "r", encoding="utf-8") as handle:
        spec = json.load(handle)
    palette = []
    for entry in (spec.get("palette") or []):
        colour = validate_svg.normalize_color(entry)
        if colour:
            palette.append(colour)
    if not palette:
        print("spec.json has no usable palette; nothing to snap to", file=sys.stderr)
        return 2

    parser = etree.XMLParser(resolve_entities=False, no_network=True,
                            recover=False, huge_tree=True)
    try:
        tree = etree.parse(svg_path, parser)
    except etree.XMLSyntaxError as exc:
        print("cannot parse %s: %s" % (svg_path, exc), file=sys.stderr)
        return 2
    root = tree.getroot()
    styles = validate_svg.StyleContext(root)

    changes = {}        # (from, to) -> count
    unsnapped = {}      # colour -> [element labels]
    where = {}
    index = 0

    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        name = validate_svg._localname(element).lower()
        if name in ("style", "metadata"):
            continue
        index += 1
        declarations = dict(styles._styles.get(element) or {})
        for prop in PROPS:
            value, source = styles.get(element, prop)
            colour = validate_svg.normalize_color(value)
            if colour is None:
                continue
            target, gap = nearest_palette(colour, palette)
            if colour == target:
                continue
            if gap > tolerance and not force:
                unsnapped.setdefault(colour, []).append(
                    "%s %s" % (validate_svg._describe(element, index), prop))
                continue
            if not dry_run:
                spot = write_property(element, prop, target, source, declarations)
                where[spot] = where.get(spot, 0) + 1
            changes[(colour, target)] = changes.get((colour, target), 0) + 1

    print("palette: %s" % ", ".join(palette))
    print("tolerance: %.1f (Euclidean sRGB; 441 is the maximum possible)"
          % tolerance)
    if changes:
        print("\nsnapped %d value%s:" % (sum(changes.values()),
                                         "" if sum(changes.values()) == 1 else "s"))
        for (src, dst), count in sorted(changes.items(), key=lambda kv: -kv[1]):
            print("  %s -> %s   x%d" % (src, dst, count))
    else:
        print("\nnothing to snap: every colour is already on palette or within "
              "tolerance")

    if unsnapped:
        print("\n%d colour%s further than the tolerance and left unchanged:"
              % (len(unsnapped), "" if len(unsnapped) == 1 else "s"))
        for colour, users in sorted(unsnapped.items()):
            target, gap = nearest_palette(colour, palette)
            print("  %s (nearest %s is %.0f away) used by %s"
                  % (colour, target, gap, ", ".join(users[:3])))
        print("  raise --tolerance, fix the artwork, or accept them as extra inks")
        print("  (and add them to the palette if they are deliberate)")

    if where:
        print("\nwritten to: " + ", ".join("%s x%d" % (k, v)
                                            for k, v in sorted(where.items())))

    if dry_run:
        print("\ndry run: nothing written")
    else:
        target_path = out_path or svg_path
        # Honour -o even when nothing needed snapping: a caller that asked for
        # an output file should get one, not silence.
        if changes or target_path != svg_path:
            tree.write(target_path, encoding="utf-8", xml_declaration=True)
            print("\nwrote %s%s" % (target_path,
                                    "" if changes else " (unchanged copy)"))
        else:
            print("\nnothing to write: no changes and no output path given")

    return 1 if unsnapped else 0


def main():
    parser = argparse.ArgumentParser(
        description="Snap SVG colours to the palette in spec.json (pipeline "
                    "stage 3).")
    parser.add_argument("svg", help="SVG to read")
    parser.add_argument("spec", help="spec.json carrying the palette")
    parser.add_argument("-o", "--out", default=None,
                        help="write here instead of overwriting the input")
    parser.add_argument("--tolerance", type=float, default=32.0,
                        help="max sRGB distance to snap (default 32)")
    parser.add_argument("--force", action="store_true",
                        help="snap regardless of distance (audit the report!)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change, write nothing")
    args = parser.parse_args()
    return snap(args.svg, args.spec, args.tolerance, args.force, args.out,
                args.dry_run)


if __name__ == "__main__":
    sys.exit(main())