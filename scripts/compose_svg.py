#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compose_svg.py -- vector-preserving merge of layers into one SVG.

THE COMPOSITION OPERATION. The pipeline traces one raster into one SVG and checks
that one SVG; until this script it could not MERGE several pieces into a single
printable artwork. The obvious way to add that is to rasterise the layers and
re-trace the composite -- which throws away the vectors the trace stage has just
produced (re-quantised colours, doubled node counts, lost text and geometry).
This script merges instead: every VECTOR LAYER STAYS A VECTOR.

How a merge works
-----------------
* The output is one ``<svg width height viewBox="0 0 W H">`` carrying the
  requested background as a full-canvas ``<rect>``, so the caller gets a canvas
  of exactly the size it asked for.
* A VECTOR layer's inline SVG is parsed and its inner content wrapped in
  ``<g transform="translate(x,y) scale(w/nw, h/nh)" opacity="...">``, where
  ``(nw, nh)`` is the layer's natural size: its ``viewBox`` when it has one,
  otherwise its ``width``/``height``. A viewBox with a non-zero origin is offset
  back into place, so a layer drawn around (100, 100) still lands where the
  caller asked. No raster round-trip, no node reduction, no colour re-sampling.
* A ``hue`` on a vector layer becomes an ``feColorMatrix type="hueRotate"``
  filter in ``<defs>`` (a unique id per layer). A FILTER ELEMENT, not a CSS
  ``filter:`` declaration: a rasteriser (rsvg, an Inkscape export) honours the
  former, so a hue that looks right in a browser preview is also the hue that
  ends up on the film.
* A RASTER layer embeds verbatim as ``<image href="data:..." x y width height
  opacity>``. ``src`` is always inline -- an SVG string or a data URI -- so
  compose NEVER touches the filesystem for layer content. ``hue`` on a raster
  layer is a documented FOLLOW-UP, not applied: it is reported in the findings
  (and the runner's log) rather than silently dropped.
* When the spec carries a ``palette``, the merged document's ``fill`` /
  ``stroke`` / ``stop-color`` are snapped to it through ``snap_colors.py``'s own
  colour maths (its ``PROPS`` / ``nearest_palette`` / ``write_property``, driven
  over the merged tree through the same CSS cascade) -- imported, not
  re-implemented. A colour within tolerance is snapped; a colour further away
  is a FINDING, not an error. The composite owns the palette, so a colour it did
  not ask for is reported and left in place for the operator to judge, never
  silently forced onto a nearest match and never a reason to refuse the merge.
  The canvas background is part of that pass (it is a ``fill``): a palette that
  omits the canvas colour reports it, which is the honest answer for a palette
  claiming to describe the whole artwork.

Limitations, stated rather than hidden
--------------------------------------
* ``hue`` on a RASTER layer is not applied yet -- a documented follow-up. It is
  recorded in the findings (and the runner's log) instead of being dropped.
* A layer's own ``id`` values are merged as they are, so two layers that both
  define ``#gradient1`` collide. The ids compose generates itself (one per hue
  filter) are checked against the merged tree and never collide; a caller
  handing in layers with colliding ids owns that.

Usage
-----
::

    python3 scripts/compose_svg.py composite.spec.json -o composite.svg
    python3 scripts/compose_svg.py - < composite.spec.json > composite.svg

Exit codes: 0 = merged (off-palette colours are reported, not fatal),
2 = bad usage (unreadable spec, bad arguments), 3 = invalid spec.

The library entry point is :func:`compose` (``compose(spec) -> str``); the runner
exposes it as ``POST /compose``. Requirements: stdlib + lxml only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

from lxml import etree

HERE = os.path.dirname(os.path.abspath(__file__))
for _path in (os.path.dirname(HERE), HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import snap_colors   # noqa: E402  (PROPS / nearest_palette / write_property)
import validate_svg  # noqa: E402  (normalize_color)

SVG_NS = "http://www.w3.org/2000/svg"

# The tolerance snap_colors.py itself defaults to (Euclidean sRGB): close enough
# to be the palette entry the artist meant, far enough that a genuinely
# different colour is reported instead of being snapped.
DEFAULT_TOLERANCE = 32.0

_PARSER = etree.XMLParser(resolve_entities=False, no_network=True,
                          recover=False, huge_tree=True)
_HEX = re.compile(r"^#[0-9a-f]{6}$")
_LENGTH = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


class SpecError(ValueError):
    """The compose spec is malformed, incomplete or non-numeric.

    Raised instead of guessing: a wrong transform or a wrong canvas size is a
    print fault that no later stage would catch, so compose refuses the spec.
    The CLI maps this to exit 3; the runner maps it to ``422 invalid_spec``.
    """


# -------------------------------------------------------------------------- #
# Small helpers                                                              #
# -------------------------------------------------------------------------- #

def _svg_tag(name):
    return "{%s}%s" % (SVG_NS, name)


def _localname(element):
    tag = element.tag
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _fmt(value):
    """Numbers the way a human writes them: 3000, not 3000.0."""
    value = float(value)
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


def _n(value, what):
    """A required number, or SpecError. Booleans are not numbers here."""
    if isinstance(value, bool) or value is None:
        raise SpecError("%s must be a number, got %r" % (what, value))
    try:
        return float(value)
    except (TypeError, ValueError):
        raise SpecError("%s must be a number, got %r" % (what, value))


def _length(value):
    """Leading number of an SVG length ('300', '100mm', '12.5px'), else None.

    The unit suffix is ignored: a source that declares width in mm and has no
    viewBox is sized by that number as user units. Documented rather than
    guessed at silently, and viewBox -- which carries no units -- wins whenever
    the source has one.
    """
    if value is None:
        return None
    match = _LENGTH.match(str(value).strip())
    return float(match.group(0)) if match else None


def _viewbox(value):
    parts = [p for p in re.split(r"[,\s]+", str(value).strip()) if p]
    if len(parts) != 4:
        raise SpecError("viewBox must hold 4 numbers, got %r" % (value,))
    try:
        return [float(part) for part in parts]
    except ValueError:
        raise SpecError("viewBox must hold 4 numbers, got %r" % (value,))


def _colour(raw):
    """A paint value that must be a real colour, not a typo.

    ``validate_svg.normalize_color`` passes an unrecognised keyword straight
    through (deliberately: an unknown keyword still occupies a colour slot in the
    budget), so the canonical ``#rrggbb`` form is required here -- the same rule
    ``palette_variants.as_hex`` enforces on a spec palette. Palette entries and
    the canvas background both go through it; without it a typo would become a
    ``fill`` the rasteriser ignores, or crash ``hex_to_rgb``, instead of naming
    the fault.
    """
    if raw is None:
        return None
    value = validate_svg.normalize_color(str(raw))
    return value if value and _HEX.match(value) else None


# -------------------------------------------------------------------------- #
# Reading the layers                                                         #
# -------------------------------------------------------------------------- #

def natural_size(layer_root):
    """The layer's natural size as ``(width, height, min_x, min_y)``.

    Its ``viewBox`` when it has one, otherwise its ``width``/``height`` --
    in that order, because a viewBox is the coordinate system the layer's
    content was actually drawn in, and the width/height attributes may be a
    physical size (``100mm``) that says nothing about it.

    Public because it is the rule a caller's own scaling has to agree with: a
    viewBox with a non-zero origin comes back as ``min_x``/``min_y`` so the
    generated transform can move that origin to (0, 0) before scaling.
    """
    box = layer_root.get("viewBox")
    if box:
        min_x, min_y, width, height = _viewbox(box)
        if width <= 0 or height <= 0:
            raise SpecError("viewBox has a non-positive size: %r" % (box,))
        return width, height, min_x, min_y
    width = _length(layer_root.get("width"))
    height = _length(layer_root.get("height"))
    if width is None or height is None:
        raise SpecError("layer SVG has neither a viewBox nor a width/height "
                        "to size it by")
    if width <= 0 or height <= 0:
        raise SpecError("layer SVG has a non-positive width/height: %r x %r"
                        % (layer_root.get("width"), layer_root.get("height")))
    return width, height, 0.0, 0.0


def _adopt_svg_namespace(element):
    """Put an unqualified subtree into the SVG namespace, in place.

    A layer SVG that omits ``xmlns`` parses with bare tag names (``rect``).
    Appended under our namespaced root it would serialise inside the SVG default
    namespace (so it renders) but stay a foreign node in the lxml tree, leaving
    the merge with two kinds of ``rect`` and later passes confused. Qualify it
    once, here.
    """
    for node in element.iter():
        tag = node.tag
        if isinstance(tag, str) and tag and "}" not in tag:
            node.tag = _svg_tag(tag)


def _parse_layer_svg(src, index):
    """Parse an inline layer SVG, or SpecError naming the layer."""
    try:
        layer_root = etree.fromstring(src.encode("utf-8"), _PARSER)
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise SpecError("layer %d is not well-formed XML: %s" % (index, exc))
    if not isinstance(layer_root.tag, str) or _localname(layer_root) != "svg":
        raise SpecError("layer %d src is not an <svg> document (root is <%s>)"
                        % (index, _localname(layer_root) or "?"))
    return layer_root


def _unique_id(root, wanted):
    """An id that cannot collide with one the layers already carry.

    Layers come from anywhere, so a generated filter id has to be checked
    against what is already in the merged tree rather than assumed free.
    """
    taken = {node.get("id") for node in root.iter() if isinstance(node.tag, str)}
    candidate = wanted
    suffix = 1
    while candidate in taken:
        suffix += 1
        candidate = "%s-%d" % (wanted, suffix)
    return candidate


def _add_hue_filter(root, filter_id, degrees):
    """Declare a hue rotation in ``<defs>`` and return the filter URL.

    ``feColorMatrix type="hueRotate"`` -- the SVG filter element -- rather than
    the CSS ``filter: hue-rotate()`` property, because a rasteriser (rsvg, an
    Inkscape export) honours the element and would silently ignore the CSS
    declaration, leaving the print unrotated while a browser preview looked
    right.
    """
    defs = root.find(_svg_tag("defs"))
    if defs is None:
        defs = etree.SubElement(root, _svg_tag("defs"))
    filt = etree.SubElement(defs, _svg_tag("filter"))
    filt.set("id", filter_id)
    # A hue rotation changes pixels, not the geometry the default filter region
    # (-10% .. 120%) is sized from; pin it to the object's own box so the filter
    # cannot clip or inflate a stroke that touches the canvas edge.
    filt.set("x", "0%")
    filt.set("y", "0%")
    filt.set("width", "100%")
    filt.set("height", "100%")
    # sRGB is what a CSS hue-rotate preview shows; the SVG default is linearRGB,
    # which renders the same number of degrees differently.
    filt.set("color-interpolation-filters", "sRGB")
    matrix = etree.SubElement(filt, _svg_tag("feColorMatrix"))
    matrix.set("type", "hueRotate")
    matrix.set("values", _fmt(degrees))
    return "url(#%s)" % filter_id


# -------------------------------------------------------------------------- #
# The merge                                                                  #
# -------------------------------------------------------------------------- #

def _build(spec):
    """Validate *spec* and build the merged tree.

    Returns ``(root, findings)``. ``findings`` is data, not prose -- snapped
    colours, colours left off palette, and anything the merge had to skip -- so
    the runner can log it and the CLI can print it.
    """
    if not isinstance(spec, dict):
        raise SpecError("spec must be a JSON object, got %s"
                        % type(spec).__name__)
    width = _n(spec.get("width"), "width")
    height = _n(spec.get("height"), "height")
    if width <= 0 or height <= 0:
        raise SpecError("width and height must be positive, got %r x %r"
                        % (spec.get("width"), spec.get("height")))
    layers = spec.get("layers")
    if not isinstance(layers, list):
        raise SpecError("layers must be a list, got %s" % type(layers).__name__)

    root = etree.Element(_svg_tag("svg"), nsmap={None: SVG_NS})
    root.set("width", _fmt(width))
    root.set("height", _fmt(height))
    root.set("viewBox", "0 0 %s %s" % (_fmt(width), _fmt(height)))

    background = spec.get("background", "transparent")
    if background not in (None, "", "transparent"):
        colour = _colour(background)
        if colour is None:
            raise SpecError("background must be a colour or 'transparent', "
                            "got %r" % (background,))
        rect = etree.SubElement(root, _svg_tag("rect"))
        rect.set("x", "0")
        rect.set("y", "0")
        rect.set("width", _fmt(width))
        rect.set("height", _fmt(height))
        rect.set("fill", colour)

    findings = {"layers": len(layers), "snapped": [], "off_palette": [],
                "ignored": []}

    for index, layer in enumerate(layers):
        if not isinstance(layer, dict):
            raise SpecError("layer %d is not an object" % index)
        kind = str(layer.get("type") or "")
        if kind not in ("svg", "raster"):
            raise SpecError("layer %d has an unknown type %r"
                            % (index, layer.get("type")))
        src = layer.get("src")
        if not isinstance(src, str) or not src.strip():
            raise SpecError("layer %d has no inline src" % index)
        x = _n(layer.get("x", 0), "layer %d x" % index)
        y = _n(layer.get("y", 0), "layer %d y" % index)
        w = _n(layer.get("w"), "layer %d w" % index)
        h = _n(layer.get("h"), "layer %d h" % index)
        if w <= 0 or h <= 0:
            raise SpecError("layer %d must have a positive w/h, got %r x %r"
                            % (index, layer.get("w"), layer.get("h")))
        opacity = _n(layer.get("opacity", 1.0), "layer %d opacity" % index)
        opacity = max(0.0, min(1.0, opacity))
        hue = _n(layer.get("hue", 0), "layer %d hue" % index)

        if kind == "svg":
            layer_root = _parse_layer_svg(src, index)
            natural_w, natural_h, min_x, min_y = natural_size(layer_root)
            group = etree.SubElement(root, _svg_tag("g"))
            transform = "translate(%s,%s) scale(%s,%s)" % (
                _fmt(x), _fmt(y), _fmt(w / natural_w), _fmt(h / natural_h))
            if min_x or min_y:
                # A viewBox with a non-zero origin: move that origin to (0, 0)
                # before scaling, or the content lands off-canvas by min_x*scale.
                transform += " translate(%s,%s)" % (_fmt(-min_x), _fmt(-min_y))
            group.set("transform", transform)
            group.set("opacity", _fmt(opacity))
            # Move the layer's content in BEFORE choosing a filter id: the id is
            # checked against what the merged tree already carries, and a layer
            # is free to define a '#compose-hue-<n>' of its own.
            for child in list(layer_root):
                _adopt_svg_namespace(child)
                group.append(child)
            if hue % 360:
                filter_id = _unique_id(root, "compose-hue-%d" % index)
                group.set("filter", _add_hue_filter(root, filter_id, hue))
        else:
            if not src.lstrip().startswith("data:"):
                raise SpecError("raster layer %d src must be an inline data: "
                                "URI, got %r" % (index, src[:40]))
            # hue on a raster layer is not applied yet: an feColorMatrix would
            # work here too, but the filter region has to be sized to the image
            # and the studio only sends hue for vector layers today. Reported,
            # never silently dropped.
            if hue % 360:
                findings["ignored"].append(
                    {"layer": index,
                     "reason": "hue is not applied to raster layers yet"})
            image = etree.SubElement(root, _svg_tag("image"))
            image.set("x", _fmt(x))
            image.set("y", _fmt(y))
            image.set("width", _fmt(w))
            image.set("height", _fmt(h))
            image.set("href", src)
            image.set("opacity", _fmt(opacity))

    _snap_to_palette(root, spec.get("palette"), findings)
    return root, findings


def _snap_to_palette(root, palette, findings):
    """Snap the merged document's paint to the composite's own palette.

    The colour maths is snap_colors': its ``PROPS``, ``nearest_palette`` and
    ``write_property``, driven here by the same cascade-aware walk (a
    ``validate_svg.StyleContext`` over the merged tree), so a colour declared in
    a layer's ``<style>`` block is resolved exactly as stage 3 resolves it and is
    written back where the cascade reads it. Only the ~15-line loop is mirrored
    rather than shared: ``snap_colors.snap()`` is CLI-shaped (a spec file in, a
    file out, printing, an exit code), and ``snap_colors.py`` is one of the
    print-check files AGENTS.md keeps off limits for edits -- so this composes
    its pieces instead of refactoring it. If that module ever learns a new paint
    property or a new colour source, this loop has to learn it too.

    Off-palette colours are recorded as findings and LEFT IN PLACE: the
    composite owns the palette, so a colour it did not ask for is a decision for
    the operator, not a reason to force a nearest match or to refuse the merge.
    """
    if palette in (None, []):
        findings["palette"] = []
        return
    if not isinstance(palette, list):
        raise SpecError("palette must be a list of colours, got %s"
                        % type(palette).__name__)
    normalised = []
    for entry in palette:
        colour = _colour(entry)
        if colour is None:
            raise SpecError("palette entry %r is not a colour" % (entry,))
        if colour not in normalised:
            normalised.append(colour)
    findings["palette"] = normalised

    styles = validate_svg.StyleContext(root)
    changes = {}        # (from, to) -> count
    unsnapped = {}      # colour -> [element labels]
    index = 0
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        if validate_svg._localname(element).lower() in ("style", "metadata"):
            continue
        index += 1
        declarations = dict(styles._styles.get(element) or {})
        for prop in snap_colors.PROPS:
            value, source = styles.get(element, prop)
            colour = validate_svg.normalize_color(value)
            if colour is None:
                continue
            target, gap = snap_colors.nearest_palette(colour, normalised)
            if colour == target:
                continue
            if gap > DEFAULT_TOLERANCE:
                unsnapped.setdefault(colour, []).append(
                    "%s %s" % (validate_svg._describe(element, index), prop))
                continue
            snap_colors.write_property(element, prop, target, source,
                                       declarations)
            changes[(colour, target)] = changes.get((colour, target), 0) + 1

    for (source, target), count in sorted(changes.items()):
        findings["snapped"].append(
            {"from": source, "to": target, "count": count})
    for colour, users in sorted(unsnapped.items()):
        nearest, gap = snap_colors.nearest_palette(colour, normalised)
        findings["off_palette"].append(
            {"colour": colour, "nearest": nearest, "distance": round(gap, 1),
             "count": len(users), "elements": users[:5]})


def compose(spec, report=None):
    """Merge ``spec``'s layers into one SVG document, returned as a string.

    The pure entry point: it reads no files, writes no files and prints nothing.
    ``report``, when given a dict, is filled in place with the findings (see
    :func:`_build`) so a caller can log the colours left off palette without
    re-parsing its own output.

    Raises :class:`SpecError` for a spec it will not guess at.
    """
    root, findings = _build(spec)
    if report is not None:
        report.clear()
        report.update(findings)
    return etree.tostring(root, xml_declaration=True, encoding="utf-8",
                          pretty_print=True).decode("utf-8")


# -------------------------------------------------------------------------- #
# CLI                                                                         #
# -------------------------------------------------------------------------- #

def _read_spec(path):
    if path in ("-", None):
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _print_findings(findings):
    """Report to stderr, so stdout stays exactly the SVG."""
    palette = findings.get("palette") or []
    if palette:
        print("palette: %s (tolerance %.1f, Euclidean sRGB)"
              % (", ".join(palette), DEFAULT_TOLERANCE), file=sys.stderr)
        for change in findings["snapped"]:
            print("  snapped %s -> %s x%d"
                  % (change["from"], change["to"], change["count"]),
                  file=sys.stderr)
        if not findings["snapped"] and not findings["off_palette"]:
            print("  nothing to snap: every colour is already on palette",
                  file=sys.stderr)
    for entry in findings["off_palette"]:
        print("off palette: %s (nearest %s is %.0f away) x%d"
              % (entry["colour"], entry["nearest"], entry["distance"],
                 entry["count"]), file=sys.stderr)
    if findings["off_palette"]:
        print("  off-palette colours are reported, not forced: fix the source, "
              "add them to the palette, or accept them as extra inks",
              file=sys.stderr)
    for entry in findings["ignored"]:
        print("layer %d: %s" % (entry["layer"], entry["reason"]),
              file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Merge layers into one vector-preserving SVG (the "
                    "composition operation).")
    parser.add_argument("spec", nargs="?", default="-",
                        help="spec JSON file, or - for stdin (default)")
    parser.add_argument("-o", "--out", default=None,
                        help="write the SVG here instead of stdout")
    args = parser.parse_args(argv)

    try:
        text = _read_spec(args.spec)
    except OSError as exc:
        print("cannot read %s: %s" % (args.spec, exc), file=sys.stderr)
        return 2
    try:
        spec = json.loads(text)
    except json.JSONDecodeError as exc:
        print("spec is not valid JSON: %s" % exc, file=sys.stderr)
        return 3

    report = {}
    try:
        svg = compose(spec, report=report)
    except SpecError as exc:
        print("invalid spec: %s" % exc, file=sys.stderr)
        return 3

    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as handle:
                handle.write(svg)
        except OSError as exc:
            print("cannot write %s: %s" % (args.out, exc), file=sys.stderr)
            return 2
        print("wrote %s (%d layer%s)" % (args.out, report.get("layers", 0),
                                         "" if report.get("layers") == 1
                                         else "s"), file=sys.stderr)
    else:
        sys.stdout.write(svg)

    _print_findings(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
