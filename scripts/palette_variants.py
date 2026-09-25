#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
palette_variants.py -- generate palette variations of one finished SVG.

Chopshop-Aided-Design auxiliary layer.  Takes a completed trace (a validated
proof SVG, or any SVG you point it at) and re-colours it onto each palette in
scripts/palettes.json.  The CAD structure is never touched: no path, node,
viewBox or dimension is rewritten -- only fill / stroke / stop-color values --
so geometry, node count and physical size stay exactly as the chosen candidate
had them.  That is what makes it "aided design" rather than a redraw.

This is an *auxiliary* layer: it calls the existing pipeline scripts and none of
them call it.  It never edits
validate_svg.py, preflight.py, snap_colors.py or pipeline.sh, and it never picks
a winner -- the report orders by print-readiness gates, never by "looks best".

Colour mapping
--------------
Three sources of truth, highest priority first:

1. ``map`` in the palette entry, or ``--map src=dst`` on the command line.  An
   explicit pairing always wins.  This is the production path: no colour-space
   heuristic can recover *intent* (that this red is the blossom and should
   become plum, while that green is foliage and should become sage).
2. The ``background`` anchor: the largest-area source colour takes the
   palette's declared background.
3. The strategy for everything else -- ``area`` (rank the remaining source
   colours by rendered area against the remaining palette entries, in order) or
   ``nearest`` (closest colour; the same semantics snap_colors.py uses).

Every run writes ``mapping.json`` beside each variant showing exactly what was
chosen, so a heuristic result can be pinned into an explicit map in seconds.

Weights come from a real render (one Inkscape pass, reused for every palette).
Without Inkscape the weighting degrades to element counts and says so, rather
than silently guessing.

Usage
-----
::

    python3 scripts/palette_variants.py --list
    python3 scripts/palette_variants.py 05_final/art.svg
    python3 scripts/palette_variants.py 05_final/art.svg --only cool-luxe,warm-sunny
    python3 scripts/palette_variants.py --from-final 00-example --preflight
    python3 scripts/palette_variants.py art.svg --map '#c1272d=#5b2c6f,#3a5a32=#9caf88'

Exit codes: 0 = every variant written (and any requested preflight passed),
1 = a variant failed to write, or a requested preflight hit a hard gate,
2 = bad usage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter

from lxml import etree

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _path in (HERE, ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import snap_colors  # noqa: E402
import validate_svg  # noqa: E402

PROPS = ("fill", "stroke", "stop-color")
DEFAULT_LIBRARY = os.path.join(HERE, "palettes.json")
DEFAULT_SPEC = os.path.join(ROOT, "spec.json")

# The render exists only to measure area, so it needs no print resolution.
WEIGHT_DPI = 96
# How far a rendered pixel may sit from a declared colour and still count as
# that colour. Generous enough for antialiased edges, tight enough not to
# swallow a neighbouring flat fill.
WEIGHT_TOLERANCE = 16


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


_HEX = re.compile(r"^#[0-9a-f]{6}$")


def as_hex(raw):
    """Normalise a paint value, but only accept a plain ``#rrggbb``.

    ``validate_svg.normalize_color`` resolves CSS named colours and shorthand
    hex, and passes anything it does not recognise straight through -- so
    ``'not-a-colour'`` comes back unchanged rather than as ``None``.  Accepting
    that would let a typo reach the artwork (as ``fill="not-a-colour"``) or
    crash ``hex_to_rgb``.  Palette entries and ``--map`` values must be real
    colours, so require the canonical form here.
    """
    value = validate_svg.normalize_color(raw)
    if value and _HEX.match(value):
        return value
    return None


def luminance(colour):
    """WCAG relative luminance of '#rrggbb' -- for ordering, not gating."""
    def channel(value):
        value = value / 255.0
        if value <= 0.03928:
            return value / 12.92
        return ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = snap_colors.hex_to_rgb(colour)
    return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)


def parse_map(text):
    """'#aabbcc=#ddeeff,#112233=#445566' -> {source: target}."""
    mapping = {}
    for pair in (text or "").split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise ValueError("--map entry %r is not src=dst" % pair)
        source, target = pair.split("=", 1)
        source = as_hex(source.strip())
        target = as_hex(target.strip())
        if source is None or target is None:
            raise ValueError("--map entry %r has an unusable colour" % pair)
        mapping[source] = target
    return mapping


def write_json(path, payload):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=False)
        handle.write("\n")


# --------------------------------------------------------------------------
# Reading the source: declared colours and their rendered area
# --------------------------------------------------------------------------

def parse_svg(svg_path):
    """Return (tree, root, styles) using the same parser contract as the rest."""
    parser = etree.XMLParser(resolve_entities=False, no_network=True,
                             recover=False, huge_tree=True)
    tree = etree.parse(svg_path, parser)
    root = tree.getroot()
    return tree, root, validate_svg.StyleContext(root)


def declared_colours(root, styles):
    """{hex: element_count} -- every resolved paint value in the document."""
    found = {}
    index = 0
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        name = validate_svg._localname(element).lower()
        if name in ("style", "metadata"):
            continue
        index += 1
        for prop in PROPS:
            value, _source = styles.get(element, prop)
            colour = validate_svg.normalize_color(value)
            if colour is not None:
                found[colour] = found.get(colour, 0) + 1
    return found


def render_reference(svg_path, png_path, dpi):
    """Rasterise once for area measurement.  Returns True on success."""
    if shutil.which("inkscape") is None:
        return False
    argv = [
        "inkscape", svg_path,
        "--export-type=png",
        "--export-filename=%s" % png_path,
        "--export-dpi=%s" % dpi,
        "--export-background=#ffffff",
        "--export-background-opacity=255",
    ]
    env = dict(os.environ)
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", "disabled:")
    try:
        result = subprocess.run(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, timeout=900)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and os.path.exists(png_path)


def png_histogram(png_path):
    """{(r, g, b): pixel_count} for a rendered PNG."""
    from PIL import Image

    with Image.open(png_path) as image:
        image = image.convert("RGB")
        entries = image.getcolors(maxcolors=1 << 24)
        if entries is None:
            # More colours than we will count individually; quantise first.
            entries = image.quantize(colors=256).convert("RGB").getcolors(
                maxcolors=1 << 24) or []
    return {colour: count for count, colour in entries}


def weights_from_render(colours, histogram, tolerance=WEIGHT_TOLERANCE):
    """Pixel area covered by each declared colour."""
    weights = {}
    for colour in colours:
        target = snap_colors.hex_to_rgb(colour)
        total = 0
        for rgb, count in histogram.items():
            if (abs(rgb[0] - target[0]) <= tolerance
                    and abs(rgb[1] - target[1]) <= tolerance
                    and abs(rgb[2] - target[2]) <= tolerance):
                total += count
        weights[colour] = total
    return weights


def collect_weights(svg_path, root, styles, workdir):
    """Weights per declared colour, plus how they were obtained."""
    counts = declared_colours(root, styles)
    png_path = os.path.join(workdir, "reference.png")
    if render_reference(svg_path, png_path, WEIGHT_DPI):
        try:
            histogram = png_histogram(png_path)
        except Exception as exc:                      # noqa: BLE001 - report it
            return counts, "element count (render unreadable: %s)" % exc
        weights = weights_from_render(counts.keys(), histogram)
        if any(weights.values()):
            return weights, "rendered area @ %d dpi" % WEIGHT_DPI
        return counts, "element count (render had no matching pixels)"
    return counts, "element count (inkscape not installed)"


# --------------------------------------------------------------------------
# Mapping one palette onto the source
# --------------------------------------------------------------------------

def build_mapping(weights, palette, strategy, explicit):
    """Return ({source_hex: target_hex}, {source_hex: why})."""
    palette_colours = palette["colours"]
    background = palette.get("background") or palette_colours[0]
    mapping, why = {}, {}

    for source, target in (explicit or {}).items():
        mapping[source] = target
        why[source] = "explicit"

    # Heaviest first; ties broken by colour so the result is deterministic.
    ordered = sorted(weights.items(), key=lambda kv: (-kv[1], kv[0]))
    ordered = [colour for colour, _weight in ordered]

    if strategy == "area" and ordered:
        heaviest = ordered[0]
        if heaviest not in mapping:
            mapping[heaviest] = background
            why[heaviest] = "background anchor"
        remaining_sources = [c for c in ordered[1:] if c not in mapping]
        remaining_targets = [c for c in palette_colours if c != background]
        for position, source in enumerate(remaining_sources):
            if position < len(remaining_targets):
                mapping[source] = remaining_targets[position]
                why[source] = "area rank %d" % (position + 1)
            else:
                # More source colours than palette slots: collapse the tail
                # onto the closest remaining entry rather than inventing a
                # colour that is not in the palette.
                fallback = remaining_targets or palette_colours
                target, _gap = snap_colors.nearest_palette(source, fallback)
                mapping[source] = target
                why[source] = "collapsed onto nearest"
    else:
        for source in ordered:
            if source in mapping:
                continue
            target, _gap = snap_colors.nearest_palette(source, palette_colours)
            mapping[source] = target
            why[source] = "nearest"

    return mapping, why


def apply_mapping(root, styles, mapping):
    """Write mapped colours back through the CSS cascade.  Returns (changes, where)."""
    changes, where = Counter(), Counter()
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
            target = mapping.get(colour)
            if target is None or target == colour:
                continue
            spot = snap_colors.write_property(element, prop, target, source,
                                              declarations)
            where[spot] += 1
            changes[(colour, target)] += 1
    return changes, where


# --------------------------------------------------------------------------
# Palettes
# --------------------------------------------------------------------------

def load_library(path):
    with open(path, "r", encoding="utf-8") as handle:
        library = json.load(handle)
    palettes = []
    for entry in library.get("palettes") or []:
        colours = [c for c in (as_hex(x)
                               for x in (entry.get("colours") or [])) if c]
        if not colours:
            raise ValueError("palette %r has no usable colours"
                             % entry.get("id"))
        background = as_hex(entry.get("background") or "")
        if background not in colours:
            background = colours[0]
        pins = {}
        for source, target in (entry.get("map") or {}).items():
            source, target = as_hex(source), as_hex(target)
            if source and target:
                pins[source] = target
        palettes.append({
            "id": entry.get("id") or ("palette-%d" % (len(palettes) + 1)),
            "name": entry.get("name") or entry.get("id") or "",
            "note": entry.get("note") or "",
            "colours": colours,
            "background": background,
            "map": pins,
        })
    if not palettes:
        raise ValueError("%s defines no palettes" % path)
    return palettes


def resolve_from_final(stem):
    """Find the SVG a finished proof was built from, via its manifest."""
    manifest = os.path.join(ROOT, "05_final", "%s.manifest.json" % stem)
    if not os.path.exists(manifest):
        raise SystemExit("no such manifest: %s" % manifest)
    with open(manifest, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    source = (payload.get("input") or {}).get("file")
    if not source:
        raise SystemExit("%s records no input file" % manifest)
    path = source if os.path.isabs(source) else os.path.join(ROOT, source)
    if not os.path.exists(path):
        raise SystemExit("manifest points at a missing SVG: %s" % path)
    return path, manifest


# --------------------------------------------------------------------------
# Optional back half
# --------------------------------------------------------------------------

def run_preflight(svg_path, spec_path, workdir, dpi):
    """Run the real preflight as a subprocess; return its summary or None."""
    os.makedirs(workdir, exist_ok=True)
    json_path = os.path.join(workdir, "manifest.json")
    argv = [sys.executable, os.path.join(ROOT, "preflight.py"), svg_path,
            spec_path, "--workdir", workdir, "--json", json_path]
    if dpi:
        argv += ["--dpi", str(dpi)]
    env = dict(os.environ)
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", "disabled:")
    try:
        result = subprocess.run(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, env=env, timeout=1800)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"error": "could not run preflight: %s" % exc}
    if not os.path.exists(json_path):
        return {"error": "preflight wrote no manifest (exit %d)"
                         % result.returncode,
                "log_tail": result.stdout.decode("utf-8", "replace")[-1200:]}
    with open(json_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    stats = payload.get("stats") or {}
    return {
        "passed": bool(payload.get("passed")),
        "hard": stats.get("hard_findings"),
        "advisory": stats.get("advisory_findings"),
        "rendered_ink_colors": stats.get("rendered_ink_colors"),
        "rendered_declared_colors": stats.get("rendered_declared_colors"),
        "proof": (payload.get("artifacts") or {}).get("proof"),
        "print_pdf": (payload.get("artifacts") or {}).get("print_pdf"),
        "manifest": json_path,
    }


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def format_report(result, outdir):
    lines = []
    lines.append("=" * 72)
    lines.append("PALETTE VARIANTS   source: %s" % result["source"]["file"])
    lines.append("                   svg sha256: %s" % result["source"]["sha256"][:16])
    lines.append("                   weights from: %s" % result["weights_from"])
    lines.append("=" * 72)
    lines.append("")
    header = "%-14s %-26s %8s %8s %8s %6s" % (
        "palette", "name", "changed", "colours", "inks", "gates")
    lines.append(header)
    lines.append("-" * len(header))
    for variant in result["variants"]:
        pre = variant.get("preflight") or {}
        inks = pre.get("rendered_ink_colors")
        if pre.get("error"):
            gates = "error"
        elif pre:
            gates = "pass" if pre.get("passed") else "FAIL"
        else:
            gates = "-"
        lines.append("%-14s %-26s %8d %8d %8s %6s" % (
            variant["palette"], variant["name"][:26],
            variant["changed_values"], variant["palette_colours"],
            "-" if inks is None else inks, gates))
    lines.append("")
    if any(v.get("preflight") for v in result["variants"]):
        lines.append("Ordered by print-readiness (fewest hard gates, then fewest "
                     "advisories).")
        lines.append("This is not a quality ranking -- which variant looks best "
                     "is your call, not the pipeline's.")
    lines.append("")
    lines.append("Per-palette detail and the exact colour mapping:")
    for variant in result["variants"]:
        lines.append("  %s/" % variant["palette"])
        lines.append("    svg      %s" % variant["svg"])
        lines.append("    mapping  %s" % variant["mapping_file"])
        if variant.get("preflight", {}).get("proof"):
            lines.append("    proof    %s" % variant["preflight"]["proof"])
        for source, target in sorted(variant["mapping"].items()):
            lines.append("      %s -> %s   (%s)"
                         % (source, target, variant["why"].get(source, "")))
    lines.append("")
    lines.append("wrote %s" % os.path.join(outdir, "report.json"))
    lines.append("wrote %s" % os.path.join(outdir, "report.md"))
    return "\n".join(lines)


def format_markdown(result):
    out = []
    out.append("# Palette variants -- %s" % result["source"]["file"])
    out.append("")
    out.append("- source sha256: `%s`" % result["source"]["sha256"])
    out.append("- palette library: `%s` (%s)"
               % (result["library"]["path"], result["library"]["sha256"][:16]))
    out.append("- strategy: `%s`  |  weights from: %s"
               % (result["strategy"], result["weights_from"]))
    out.append("- geometry untouched: only fill/stroke/stop-color rewritten")
    out.append("")
    out.append("| palette | name | changed | colours | source colours | inks | gates |")
    out.append("| --- | --- | --- | --- | --- | --- | --- |")
    for variant in result["variants"]:
        pre = variant.get("preflight") or {}
        if pre.get("error"):
            gates = "error"
        elif pre:
            gates = "pass" if pre.get("passed") else "**FAIL**"
        else:
            gates = "-"
        out.append("| %s | %s | %d | %d | %d | %s | %s |" % (
            variant["palette"], variant["name"], variant["changed_values"],
            variant["palette_colours"], variant["source_colours"],
            pre.get("rendered_ink_colors", "-"), gates))
    out.append("")
    if any(v.get("preflight") for v in result["variants"]):
        out.append("Ordered by print-readiness (fewest hard gates, then fewest "
                   "advisories), **not** by which looks best -- the winner is a "
                   "human call.")
        out.append("")
    for variant in result["variants"]:
        out.append("## %s -- %s" % (variant["palette"], variant["name"]))
        out.append("")
        if variant.get("note"):
            out.append("%s" % variant["note"])
            out.append("")
        out.append("`%s`" % variant["svg"])
        out.append("")
        out.append("| from | to | why |")
        out.append("| --- | --- | --- |")
        for source, target in sorted(variant["mapping"].items()):
            out.append("| `%s` | `%s` | %s |"
                       % (source, target, variant["why"].get(source, "")))
        out.append("")
        if variant.get("unused"):
            out.append("Source colours with no visible area (left unchanged): "
                       + ", ".join("`%s`" % c for c in variant["unused"]))
            out.append("")
    return "\n".join(out)


def variant_sort_key(variant):
    """Order for reading, by print-readiness only -- never a quality judgement.

    Passed variants first, then fewest advisories, then palette id so the
    ordering is stable across runs.  Variants with no preflight (or a failed
    preflight run) sort last; they carry no readiness information at all.
    """
    pre = variant.get("preflight") or {}
    if not pre or pre.get("error"):
        return (2, 0, variant["palette"])
    return (0 if pre.get("passed") else 1,
            pre.get("advisory") or 0, variant["palette"])


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def generate(svg_path, library_path, outdir, strategy, explicit, only,
             spec_path, preflight_enabled, preflight_dpi, dry_run):
    tree, root, styles = parse_svg(svg_path)
    all_colours = declared_colours(root, styles)
    if not all_colours:
        print("no paint values found in %s" % svg_path, file=sys.stderr)
        return 2

    palettes = load_library(library_path)
    if only:
        wanted = {part.strip() for part in only.split(",") if part.strip()}
        palettes = [p for p in palettes if p["id"] in wanted]
        if not palettes:
            print("no palettes matched --only %s" % only, file=sys.stderr)
            return 2

    stem = os.path.splitext(os.path.basename(svg_path))[0]
    base = os.path.join(outdir, stem)
    if not dry_run:
        os.makedirs(base, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="palette-weights-") as scratch:
        weights, weights_from = collect_weights(svg_path, root, styles, scratch)

    print("=" * 72)
    print("palette variants    source: %s" % svg_path)
    print("                    spec:   %s" % spec_path)
    print("                    output: %s/" % base)
    print("=" * 72)
    print("colours in source: %d   weights from: %s" % (len(all_colours), weights_from))
    if not dry_run:
        print("")

    result = {
        "tool": "palette_variants.py",
        "source": {"file": svg_path, "sha256": sha256_file(svg_path),
                   "declared_colours": len(all_colours)},
        "library": {"path": library_path, "sha256": sha256_file(library_path)},
        "strategy": strategy,
        "weights_from": weights_from,
        "palettes": [p["id"] for p in palettes],
        "variants": [],
    }
    if dry_run:
        result["dry_run"] = True

    for palette in palettes:
        mapping, why = build_mapping(
            weights, palette, strategy,
            {**palette["map"], **(explicit or {})})

        variant_dir = os.path.join(base, palette["id"])
        variant_svg = os.path.join(variant_dir, "%s.svg" % palette["id"])
        mapping_file = os.path.join(variant_dir, "mapping.json")

        # Re-read for every palette: apply_mapping must never see a mutated tree.
        tree, root, styles = parse_svg(svg_path)
        changes, where = apply_mapping(root, styles, mapping)

        unused = sorted(c for c in all_colours
                        if weights.get(c, 0) == 0 or c not in mapping)

        entry = {
            "palette": palette["id"],
            "name": palette["name"],
            "note": palette["note"],
            "svg": variant_svg,
            "mapping_file": mapping_file,
            "palette_colours": len(palette["colours"]),
            "source_colours": len(all_colours),
            "changed_values": sum(changes.values()),
            "mapping": mapping,
            "why": why,
            "written_to": dict(where),
            "unused": unused,
        }

        if not dry_run:
            os.makedirs(variant_dir, exist_ok=True)
            tree.write(variant_svg, encoding="utf-8", xml_declaration=True)
            write_json(mapping_file, {
                "palette": palette["id"],
                "name": palette["name"],
                "note": palette["note"],
                "source": svg_path,
                "strategy": strategy,
                # Paste this straight into the palette's "map" to pin it.
                "map": mapping,
                "why": why,
            })
            if preflight_enabled:
                entry["preflight"] = run_preflight(
                    variant_svg, spec_path,
                    os.path.join(variant_dir, "preflight"), preflight_dpi)

        result["variants"].append(entry)
        print("  %-14s %-24s %6d values -> %s"
              % (palette["id"], palette["name"][:24], sum(changes.values()),
                 variant_svg if not dry_run else "(dry run)"))

    # Order for reading by print-readiness only. Never a quality judgement.
    result["variants"].sort(key=variant_sort_key)

    if not dry_run:
        write_json(os.path.join(base, "report.json"), result)
        with open(os.path.join(base, "report.md"), "w", encoding="utf-8") as handle:
            handle.write(format_markdown(result) + "\n")

    print("")
    print(format_report(result, base))

    failed = any((v.get("preflight") or {}).get("error") for v in result["variants"])
    hard = any((v.get("preflight") or {}).get("passed") is False
               for v in result["variants"])
    return 1 if (failed or hard) else 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="palette_variants.py",
        description="Generate palette variations of one SVG, keeping the CAD "
                    "structure identical (auxiliary Chopshop-Aided-Design "
                    "layer; never edits the pipeline scripts).")
    parser.add_argument("svg", nargs="?", help="SVG to re-colour")
    parser.add_argument("--from-final", default=None, metavar="STEM",
                        help="use the SVG a 05_final/<STEM>.manifest.json was "
                             "built from")
    parser.add_argument("--palettes", default=DEFAULT_LIBRARY,
                        help="palette library JSON (default: scripts/palettes.json)")
    parser.add_argument("--outdir", default=os.path.join(ROOT, "07_palettes"),
                        help="where variants land (default: 07_palettes/)")
    parser.add_argument("--only", default=None,
                        help="comma-separated palette ids to build")
    parser.add_argument("--strategy", choices=("area", "nearest"), default="area",
                        help="how to map colours the explicit map does not pin")
    parser.add_argument("--map", default=None, metavar="SRC=DST,...",
                        help="pin exact source->target pairs (wins over everything)")
    parser.add_argument("--spec", dest="spec", default=DEFAULT_SPEC,
                        help="spec.json used for --preflight (default: spec.json)")
    parser.add_argument("--preflight", action="store_true",
                        help="run the real back half on each variant")
    parser.add_argument("--preflight-dpi", type=int, default=None,
                        help="proof resolution for --preflight (default: spec)")
    parser.add_argument("--dry-run", action="store_true",
                        help="show the mapping, write nothing")
    parser.add_argument("--list", action="store_true",
                        help="list the palettes in the library and exit")
    args = parser.parse_args(argv)

    if args.list:
        try:
            palettes = load_library(args.palettes)
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print("palettes in %s:" % args.palettes)
        for palette in palettes:
            print("  %-14s %-26s bg %s  %s"
                  % (palette["id"], palette["name"], palette["background"],
                     " ".join(palette["colours"])))
        return 0

    svg_path = args.svg
    if args.from_final:
        svg_path, _manifest = resolve_from_final(args.from_final)
    if not svg_path:
        parser.error("give an SVG path or --from-final STEM")
    if not os.path.exists(svg_path):
        print("no such SVG: %s" % svg_path, file=sys.stderr)
        return 2

    try:
        explicit = parse_map(args.map)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    return generate(svg_path, args.palettes, args.outdir, args.strategy,
                    explicit, args.only, args.spec, args.preflight,
                    args.preflight_dpi, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
