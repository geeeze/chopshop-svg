#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
validate_svg.py -- print-readiness validator for SVG artwork.

Validates an SVG file against a JSON specification describing what the target
print process accepts (colour budget, minimum stroke width, raster embeds,
open geometry).

Checks performed
----------------
1. Raster embeds   : anything that pulls a bitmap -- or a whole other document --
                     into the artwork: ``<image>`` anywhere (including nested
                     inside ``<pattern>``), ``<feImage>`` with a ``data:`` /
                     ``file:`` / external href, ``<foreignObject>`` (it
                     rasterises XHTML), an ``<image>``/``<use>`` whose href is a
                     non-fragment document or a ``data:`` URI, and a
                     ``url(data:image/...)`` reference in a ``<style>`` block or
                     an inline style.
2. Gradients       : when ``geometry.allow_gradients`` is false, either a
                     declared ``<linearGradient>``/``<radialGradient>`` or any
                     paint-server reference (``url(...)`` in ``fill``,
                     ``stroke``, ``stop-color``, ``flood-color``,
                     ``lighting-color``) is a hard failure (GRADIENT_NOT_ALLOWED).
3. Colour budget   : every unique colour in force for an element that paints --
                     ``fill``, ``stroke`` and ``stop-color`` (presentation
                     attributes, inline ``style`` attributes and simple CSS
                     rules in a ``<style>`` element), including the implicit
                     black of a shape whose cascade never sets ``fill`` and
                     ``currentColor`` resolved from the inherited ``color``
                     property -- is normalised to lowercase 6-digit hex and
                     counted.
4. Stroke widths   : shape elements that actually stroke are checked against the
                     minimum, with ``pt``/``px``/``mm``/``cm``/``in``/``pc``/``q``
                     units converted to points (1in = 96px = 72pt = 25.4mm).
5. Geometry        : <path> elements are parsed with ``svgpathtools``; open
                     paths and degenerate (zero-length / zero-area) paths are
                     flagged.

Advisory observations (notes, never failures)
---------------------------------------------
* ``EFFECT_REFERENCE``    : a ``filter`` / ``mask`` / ``clip-path`` reference
                            can change the ink and layer A cannot measure the
                            result; layer B renders it.
* ``TRANSLUCENT_PAINT``   : an opacity strictly between 0 and 1.  A spot-colour
                            screen can print a tint, so this is never a failure,
                            but it can move the flat-ink count.
* ``UNCHECKED_DEFINITION``: content inside a definition container that nothing
                            references is not stroke- or colour-checked, so
                            coverage is partial and the operator is told.
* ``STROKE_SCALE_TRANSFORM``: a ``transform="scale(...)"`` is in scope of a
                            stroked element, so the measured width is not the
                            printed one.

Exit codes
----------
0  validation passed
1  validation failed (all reasons printed to stdout)
2  bad command line usage

Known limitations (deliberate, reported rather than guessed)
------------------------------------------------------------
* ``stroke-width`` expressed as a percentage or in an unsupported unit (``em``,
  ``ex``, ``rem`` ...) cannot be resolved without a full viewport cascade, so it
  is reported as a failure with an explicit message instead of silently passing.
* ``stroke-width`` is measured in user units: a ``transform="scale(...)"`` on the
  element or an ancestor is *not* applied to it.
* CSS support covers type, ``.class``, ``#id`` and ``*`` selectors, including
  descendant chains and comma-separated groups. Child/sibling combinators,
  attribute selectors, pseudo-classes and pseudo-elements are skipped rather
  than mis-applied. ``!important`` **is** ranked, but only across the sources
  this module models: an inline ``!important`` declaration out-ranks an
  ``!important`` stylesheet rule, which out-ranks an ordinary inline
  declaration, which out-ranks an ordinary stylesheet rule of any specificity.
  The full CSS origin ladder (user-agent / user / author origins) is not
  modelled, because nothing in this pipeline emits those origins.
* ``@media print`` and ``@media all`` blocks are applied; other at-rules
  (``@media screen``, ``@page``, ``@font-face``, ``@keyframes``) are ignored.
* ``var(--x)`` references are not resolved.  A ``url(...)`` paint-server
  reference is reported by name, never resolved to a colour: with gradients
  banned it is a hard failure (GRADIENT_NOT_ALLOWED), and with them allowed the
  colours of a gradient are counted from its ``<stop>`` elements -- including
  the stops of a gradient that nothing references, because a declared colour is
  what the colour budget and the palette check are about (``snap_colors``
  rewrites ``stop-color`` for the same reason).  That is a deliberate
  over-report, and a colour alone can therefore never *under*-report the budget.
  A ``url(...)`` paint with a CSS fallback colour counts as the reference, not
  as the fallback.
* Implicit and inherited paint is counted, but the *rendered* result is not:
  ``currentColor`` with no ``color`` in scope resolves to the initial value,
  which every renderer this was measured against paints as black
  (``#000000``).  A colour that a later declaration overrides still occupies a
  budget slot, because the budget counts declarations, not final pixels.
* Paint inherited through a ``<use>`` is not modelled as instantiation: the
  referenced element is checked with the property values in force at its
  definition site, not those on the ``<use>``.  Content inside ``<defs>``,
  ``<symbol>``, ``<pattern>``, ``<mask>`` or ``<marker>`` is checked only when
  something references it (a ``<use href="#id">`` or a ``url(#id)``); when
  nothing does, it cannot print, so it is reported once under
  UNCHECKED_DEFINITION rather than raising a hard finding on invisible art.
  Content inside ``<clipPath>``, ``<filter>``, ``<style>``, ``<metadata>``,
  ``<title>``, ``<desc>``, ``<script>``, ``<view>`` or ``<cursor>`` can never
  ink, so it is never checked, referenced or not.
* A ``<filter>`` / ``<mask>`` / ``clip-path`` reference changes the ink in ways
  layer A cannot measure (blur, flood, luminance, clipping): it is reported once
  under EFFECT_REFERENCE and left to layer B's rendered proof.
* ``opacity="0"``, ``display="none"``, ``visibility:hidden`` and
  ``stroke-opacity="0"`` mean "does not paint" and are excluded from the stroke
  and colour checks.  ``visibility`` is honoured per element, so a descendant
  that sets ``visibility:visible`` paints again, exactly as CSS says.
* What this module still cannot prove, after all of the above: that the file
  *renders* as declared.  It reads declarations, so it cannot see a gradient's
  real ink (when one is allowed), a filter's output, a blend/composite, a font
  substitution, a stroke a RIP thins, or a colour outside its gamut.  Those are
  layer B's job -- ``preflight.py`` renders and measures.  Layer A's claim is
  only that the declared properties satisfy the spec, and that anything it
  cannot read is reported rather than passed.
* Every ``<path>`` that renders nothing (no ``d``, blank ``d``, or moveto-only
  ``d``) is reported under ZERO_AREA_PATH, since empty artwork is a preflight
  defect.

Usage (also echoed by ``--help``)::

    python3 validate_svg.py artwork.svg spec.json
    python3 validate_svg.py artwork.svg spec.json && echo "ready for press"
"""

from __future__ import annotations

import colorsys
import json
import os
import re
import sys

from lxml import etree
from svgpathtools import parse_path

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
XLINK_HREF = "{%s}href" % XLINK_NS

# SVG length units -> user units.  These are the CSS reference ratios
# (1in = 96 user units); they are NOT physical sizes.  A user unit only becomes
# a physical length through the viewBox mapping, which compute_document_scale()
# resolves.  See that function for why ignoring the viewBox is wrong in both
# directions.
UNIT_TO_USER = {
    "px": 1.0,
    "pt": 96.0 / 72.0,      # 1.333333 user units
    "pc": 16.0,             # 1 pica = 12pt
    "mm": 96.0 / 25.4,      # 3.779528
    "cm": 96.0 / 2.54,      # 37.795276
    "in": 96.0,
    "q": 96.0 / 101.6,      # 1 Q = 1/40 cm
}
DEFAULT_UNIT = "px"

# Finish the conversion: user units -> millimetres -> points.
MM_PER_USER_UNIT_DEFAULT = 25.4 / 96.0     # when nothing rescales the viewBox
PT_PER_MM = 72.0 / 25.4                    # 2.834645669

# print_method values that cut rather than ink, and therefore need closed shapes.
CUTTING_PRINT_METHODS = {"vinyl", "vinyl_cut", "plotter", "cutting", "cutter",
                         "sticker", "decal", "knife"}

# Length matching: number (int/float/scientific, optional sign) + optional unit.
_LENGTH_RE = re.compile(
    r"^\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*([a-zA-Z%]*)\s*$"
)

# Values that mean "no paint" (or "cannot be counted") for colour purposes.
NON_COLORS = {
    "none", "transparent", "currentcolor", "inherit", "initial", "unset",
    "revert", "revert-layer", "auto", "context-fill", "context-stroke",
    "child", "", "all",
}

_HEX_RE = re.compile(r"^#([0-9a-f]{3,8})$")
_FUNC_RE = re.compile(r"^(rgb|rgba|hsl|hsla)\s*\((.*)\)$", re.IGNORECASE)

# Geometry tolerances (SVG user units).
CLOSE_TOL = 1e-6      # start/end distance treated as "the same point"
EPS_LEN = 1e-9        # path length treated as zero
EPS_AREA = 1e-9       # enclosed area treated as zero

RULE_RASTER = "RASTER_EMBED"
RULE_COLORS = "COLOR_COUNT"
RULE_STROKE = "MIN_STROKE_WIDTH"
RULE_OPEN = "OPEN_PATH"
RULE_DEGENERATE = "ZERO_AREA_PATH"
RULE_SVG_PARSE = "SVG_PARSE_ERROR"
RULE_PATH_PARSE = "PATH_PARSE_ERROR"
RULE_SPEC = "SPEC_ERROR"
RULE_INPUT = "INPUT_ERROR"
RULE_NODES = "NODE_COUNT"
RULE_STROKE_SCALE = "STROKE_SCALE_TRANSFORM"
RULE_PALETTE = "PALETTE"
RULE_DIMENSIONS = "DIMENSIONS"

# v4.x: the source-level gate must enforce what the spec says, so a gradient that
# only layer B's rendered ink count used to catch is now a layer A hard failure.
# PINNED: the studio's help text and its proof-page checklist name this string.
RULE_GRADIENT = "GRADIENT_NOT_ALLOWED"

# Advisory tags.  These are written into ``notes``, never into ``failures``, so
# they can never fail a job in either layer -- which is the point: layer A cannot
# measure what they describe, and a hard finding on unmeasurable art would be a
# false failure.  RULE_EFFECT is PINNED for the same reason RULE_GRADIENT is.
RULE_EFFECT = "EFFECT_REFERENCE"
RULE_TRANSLUCENT = "TRANSLUCENT_PAINT"
RULE_UNCHECKED = "UNCHECKED_DEFINITION"

# --------------------------------------------------------------------------
# Paint scopes: which elements actually put ink on the sheet
# --------------------------------------------------------------------------

# Shape elements: the only elements whose own fill/stroke reach the sheet.
# A <g> declares paint for its children but paints nothing itself, so it is not
# here -- counting it would report the same hairline twice.
_SHAPE_ELEMENTS = frozenset({
    "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
    "text", "tspan", "textpath", "use",
})

# Containers whose content can NEVER ink, referenced or not: a clip path
# defines geometry, a filter defines pixels, a stylesheet defines rules.  Their
# subtrees are skipped for stroke and colour purposes.
#
# Members are lowercase on purpose.  The lookup lowercases the element name, and
# the previous spelling of this set held "clipPath"/"linearGradient"/
# "radialGradient" -- camelCase members that could never match, so a stroked
# path inside a <clipPath> was reported as a hairline.  A set whose members are
# unreachable is the bug; one case for both sides is the fix.
_INERT_CONTAINERS = frozenset({
    "clippath", "filter", "style", "metadata", "title", "desc", "script",
    "view", "cursor",
})

# A gradient paints through its <stop> declarations, so its stops still take
# part in the colour budget (that is what snap_colors rewrites); it holds no
# stroked shape.
_INK_RESOURCE_CONTAINERS = frozenset({"lineargradient", "radialgradient"})
_GRADIENT_ELEMENTS = _INK_RESOURCE_CONTAINERS

# Containers whose content DOES paint, but only when something references it
# (a <use href="#id"> or a url(#id) on fill/stroke/clip-path/mask/filter).
# Unreferenced, they cannot print: reported once as UNCHECKED_DEFINITION
# coverage rather than failed.
_DEFERRED_INK_CONTAINERS = frozenset({
    "defs", "symbol", "pattern", "mask", "marker",
})

# The paint properties that can name a paint server.
_PAINT_PROPERTIES = ("fill", "stroke", "stop-color", "flood-color",
                     "lighting-color")

# Effect references: they change the ink in ways a declaration reader cannot
# measure (blur, flood, luminance, clipping), so they are advisory only.
_EFFECT_PROPERTIES = ("filter", "mask", "clip-path")

# The initial value of ``color`` (CSS ``CanvasText``), used to resolve
# ``currentColor`` when nothing in scope declares ``color``.
_INITIAL_COLOR = "#000000"

_URL_RE = re.compile(r"url\(\s*(['\"]?)(?P<url>[^'\")]*)\1\s*\)", re.IGNORECASE)
_DATA_IMAGE_RE = re.compile(r"url\(\s*['\"]?data:image/", re.IGNORECASE)

# Inherited keywords that hand resolution back to the ancestor (or the initial
# value for ``initial``).
_INHERIT_KEYWORDS = ("inherit", "unset", "revert", "revert-layer")

# --------------------------------------------------------------------------
# CSS colour keywords (CSS Color Level 4 named colours)
# --------------------------------------------------------------------------

NAMED_COLORS = {
    "aliceblue": "f0f8ff", "antiquewhite": "faebd7", "aqua": "00ffff",
    "aquamarine": "7fffd4", "azure": "f0ffff", "beige": "f5f5dc",
    "bisque": "ffe4c4", "black": "000000", "blanchedalmond": "ffebcd",
    "blue": "0000ff", "blueviolet": "8a2be2", "brown": "a52a2a",
    "burlywood": "deb887", "cadetblue": "5f9ea0", "chartreuse": "7fff00",
    "chocolate": "d2691e", "coral": "ff7f50", "cornflowerblue": "6495ed",
    "cornsilk": "fff8dc", "crimson": "dc143c", "cyan": "00ffff",
    "darkblue": "00008b", "darkcyan": "008b8b", "darkgoldenrod": "b8860b",
    "darkgray": "a9a9a9", "darkgreen": "006400", "darkgrey": "a9a9a9",
    "darkkhaki": "bdb76b", "darkmagenta": "8b008b", "darkolivegreen": "556b2f",
    "darkorange": "ff8c00", "darkorchid": "9932cc", "darkred": "8b0000",
    "darksalmon": "e9967a", "darkseagreen": "8fbc8f", "darkslateblue": "483d8b",
    "darkslategray": "2f4f4f", "darkslategrey": "2f4f4f",
    "darkturquoise": "00ced1", "darkviolet": "9400d3", "deeppink": "ff1493",
    "deepskyblue": "00bfff", "dimgray": "696969", "dimgrey": "696969",
    "dodgerblue": "1e90ff", "firebrick": "b22222", "floralwhite": "fffaf0",
    "forestgreen": "228b22", "fuchsia": "ff00ff", "gainsboro": "dcdcdc",
    "ghostwhite": "f8f8ff", "gold": "ffd700", "goldenrod": "daa520",
    "gray": "808080", "green": "008000", "greenyellow": "adff2f",
    "grey": "808080", "honeydew": "f0fff0", "hotpink": "ff69b4",
    "indianred": "cd5c5c", "indigo": "4b0082", "ivory": "fffff0",
    "khaki": "f0e68c", "lavender": "e6e6fa", "lavenderblush": "fff0f5",
    "lawngreen": "7cfc00", "lemonchiffon": "fffacd", "lightblue": "add8e6",
    "lightcoral": "f08080", "lightcyan": "e0ffff",
    "lightgoldenrodyellow": "fafad2", "lightgray": "d3d3d3",
    "lightgreen": "90ee90", "lightgrey": "d3d3d3", "lightpink": "ffb6c1",
    "lightsalmon": "ffa07a", "lightseagreen": "20b2aa",
    "lightskyblue": "87cefa", "lightslategray": "778899",
    "lightslategrey": "778899", "lightsteelblue": "b0c4de",
    "lightyellow": "ffffe0", "lime": "00ff00", "limegreen": "32cd32",
    "linen": "faf0e6", "magenta": "ff00ff", "maroon": "800000",
    "mediumaquamarine": "66cdaa", "mediumblue": "0000cd",
    "mediumorchid": "ba55d3", "mediumpurple": "9370db",
    "mediumseagreen": "3cb371", "mediumslateblue": "7b68ee",
    "mediumspringgreen": "00fa9a", "mediumturquoise": "48d1cc",
    "mediumvioletred": "c71585", "midnightblue": "191970",
    "mintcream": "f5fffa", "mistyrose": "ffe4e1", "moccasin": "ffe4b5",
    "navajowhite": "ffdead", "navy": "000080", "oldlace": "fdf5e6",
    "olive": "808000", "olivedrab": "6b8e23", "orange": "ffa500",
    "orangered": "ff4500", "orchid": "da70d6", "palegoldenrod": "eee8aa",
    "palegreen": "98fb98", "paleturquoise": "afeeee",
    "palevioletred": "db7093", "papayawhip": "ffefd5", "peachpuff": "ffdab9",
    "peru": "cd853f", "pink": "ffc0cb", "plum": "dda0dd",
    "powderblue": "b0e0e6", "purple": "800080", "rebeccapurple": "663399",
    "red": "ff0000", "rosybrown": "bc8f8f", "royalblue": "4169e1",
    "saddlebrown": "8b4513", "salmon": "fa8072", "sandybrown": "f4a460",
    "seagreen": "2e8b57", "seashell": "fff5ee", "sienna": "a0522d",
    "silver": "c0c0c0", "skyblue": "87ceeb", "slateblue": "6a5acd",
    "slategray": "708090", "slategrey": "708090", "snow": "fffafa",
    "springgreen": "00ff7f", "steelblue": "4682b4", "tan": "d2b48c",
    "teal": "008080", "thistle": "d8bfd8", "tomato": "ff6347",
    "turquoise": "40e0d0", "violet": "ee82ee", "wheat": "f5deb3",
    "white": "ffffff", "whitesmoke": "f5f5f5", "yellow": "ffff00",
    "yellowgreen": "9acd32",
}


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _localname(element):
    """Tag name of *element* with any namespace prefix stripped.

    Returns "" for comments / processing instructions (whose ``tag`` is a
    callable rather than a string).
    """
    tag = element.tag
    if not isinstance(tag, str):
        return ""
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def _describe(element, index):
    """Human-readable reference to an element, e.g. ``Path id='p1'``.

    The tag keeps the spelling SVG gives it (``linearGradient``,
    ``foreignObject``), with the first letter upper-cased so a report reads as
    a sentence: an element name that is silently lower-cased is how
    ``<linearGradient>`` would be printed as ``Lineargradient``, which nobody
    would grep for.
    """
    local = _localname(element)
    name = (local[:1].upper() + local[1:]) if local else "Element"
    eid = element.get("id")
    if eid:
        return "%s id='%s'" % (name, eid)
    cls = element.get("class")
    if cls:
        return "%s(class='%s') #%d" % (name, cls, index)
    return "%s #%d (no id)" % (name, index)


def _fmt_num(value):
    """Compact numeric formatting for reports (0.5 -> '0.5', 2.8346 -> '2.835')."""
    text = "%.6f" % float(value)
    text = text.rstrip("0").rstrip(".")
    return text if text not in ("", "-") else "0"


# --------------------------------------------------------------------------
# Colour handling
# --------------------------------------------------------------------------

def _clamp_byte(value):
    return max(0, min(255, int(round(value))))


def _hex_from_rgb(r, g, b):
    return "#%02x%02x%02x" % (_clamp_byte(r), _clamp_byte(g), _clamp_byte(b))


def _alpha_is_zero(alpha_raw):
    """True when an alpha component is explicitly 0 (fully invisible paint)."""
    if alpha_raw is None:
        return False
    alpha_raw = alpha_raw.strip()
    try:
        if alpha_raw.endswith("%"):
            return float(alpha_raw[:-1]) <= 0.0
        value = float(alpha_raw)
        return value <= 0.0
    except ValueError:
        return False


def _parse_rgb_components(raw):
    """Parse 'r, g, b' / 'r g b' / '50%, 0%, 10%' into 0-255 channel values."""
    parts = [p for p in re.split(r"[,\s/]+", raw.strip()) if p]
    if len(parts) < 3:
        return None
    channels = []
    for part in parts[:3]:
        if part.endswith("%"):
            try:
                channels.append(float(part[:-1]) * 255.0 / 100.0)
            except ValueError:
                return None
        else:
            try:
                channels.append(float(part))
            except ValueError:
                return None
    return channels


def normalize_color(raw):
    """Normalise a paint value to lowercase ``#rrggbb``.

    Returns ``None`` for values that must not count towards the colour budget
    (``none``, ``transparent``, ``currentColor``, ``url(#gradient)`` references,
    fully transparent colours, ...).  Unknown keywords are returned verbatim in
    lowercase so that they still occupy a slot in the budget rather than being
    silently dropped.
    """
    if raw is None:
        return None
    value = str(raw).strip().strip("'\"").strip()
    if not value:
        return None
    lowered = value.lower()

    # Paint servers / CSS functions we cannot resolve, e.g. url(#gradient).
    if "url(" in lowered or lowered.startswith("var("):
        return None
    if lowered in NON_COLORS:
        return None

    hex_match = _HEX_RE.match(lowered)
    if hex_match:
        digits = hex_match.group(1)
        if len(digits) in (3, 4):
            digits = "".join(ch * 2 for ch in digits)
        if len(digits) == 8:
            if digits[6:8] == "00":          # fully transparent
                return None
            digits = digits[:6]
        if len(digits) == 6:
            return "#" + digits
        return None

    func_match = _FUNC_RE.match(lowered)
    if func_match:
        kind, body = func_match.group(1), func_match.group(2)
        parts = [p for p in re.split(r"[,\s/]+", body.strip()) if p]
        alpha = parts[3] if len(parts) >= 4 else None
        if _alpha_is_zero(alpha):
            return None
        if kind in ("rgb", "rgba"):
            channels = _parse_rgb_components(body)
            if channels is None:
                return "#" + lowered.lstrip("#")
            return _hex_from_rgb(*channels)
        # hsl() / hsla()
        if len(parts) >= 3:
            try:
                hue = float(parts[0].rstrip("deg")) % 360.0
                sat = float(parts[1].rstrip("%")) / 100.0
                light = float(parts[2].rstrip("%")) / 100.0
            except ValueError:
                return "#" + lowered
            r, g, b = colorsys.hls_to_rgb(hue / 360.0, light, sat)
            return _hex_from_rgb(r * 255.0, g * 255.0, b * 255.0)
        return "#" + lowered

    named = NAMED_COLORS.get(lowered)
    if named:
        return "#" + named

    # Unrecognised keyword: keep it in the budget so nothing is silently lost.
    return lowered


# --------------------------------------------------------------------------
# Style resolution: inline style > CSS rules > presentation attribute
# --------------------------------------------------------------------------

_DECL_RE = re.compile(r"([-\w]+)\s*:\s*([^;]+)", re.DOTALL)
_SIMPLE_SELECTOR_RE = re.compile(r"([.#]?)([-\w]+)")


def parse_style_block(style):
    """Parse ``fill:#fff; stroke-width:2pt`` into a property dict.

    Kept as the plain-value view of a declaration block; ``!important`` is
    handled by ``_parse_declarations``, which the cascade uses because a flag
    that is silently discarded is a cascade that resolves the wrong value.
    """
    return {prop: value for prop, (value, _important)
            in _parse_declarations(style).items()}


def _parse_declarations(style):
    """Parse a declaration block into ``{property: (value, important)}``.

    ``!important`` is *carried*, not stripped: CSS ranks it above ordinary
    declarations, and an inline ``fill:#0f0`` must lose to a stylesheet
    ``rect{fill:#f00 !important}``.  Last declaration wins per property, as CSS
    says.
    """
    result = {}
    if not style:
        return result
    for prop, value in _DECL_RE.findall(style):
        key = prop.strip().lower()
        if not key:
            continue
        text = value.strip()
        important = bool(re.search(r"!\s*important\s*$", text, re.IGNORECASE))
        if important:
            text = re.sub(r"!\s*important\s*$", "", text,
                          flags=re.IGNORECASE).strip()
        result[key] = (text, important)
    return result


def _flatten_css(text):
    """Strip comments and at-rules, keeping the bodies of print/all media blocks.

    Uses a brace-matching scan rather than a regex so that nesting is handled:
    ``@media print { ... }`` and ``@media all { ... }`` are inlined (a print
    validator *is* the print medium), while ``@media screen { ... }``,
    ``@page``, ``@font-face``, ``@keyframes`` and friends are dropped instead
    of being mistaken for ordinary rules.
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"@(?:charset|import|namespace)[^;{]*;", "", text,
                  flags=re.IGNORECASE)

    parts = []
    index = 0
    length = len(text)
    while index < length:
        brace = text.find("{", index)
        if brace == -1:
            break
        prelude = text[index:brace].strip()
        depth = 1
        cursor = brace + 1
        while cursor < length and depth:
            char = text[cursor]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            cursor += 1
        body = text[brace + 1:cursor - 1]

        if prelude.startswith("@"):
            lowered = prelude.lower()
            if lowered.startswith("@media") and ("print" in lowered
                                                 or "all" in lowered):
                parts.append(_flatten_css(body))
        else:
            parts.append("%s{%s}" % (prelude, body))
        index = cursor

    return "\n".join(parts)


def _parse_selector(selector):
    """Parse a selector into ``(specificity, [(tag, classes, id), ...])``.

    Descendant combinators are supported (the chain is matched right-to-left
    against ancestors, as CSS does).  Selector forms we do not model -- child
    and sibling combinators, attribute selectors, pseudo-classes and
    pseudo-elements -- return ``None`` so they are skipped rather than
    mis-applied.
    """
    selector = selector.strip()
    if not selector or re.search(r"[>+~\[\]:]", selector):
        return None

    compounds = []
    ids = class_count = tag_count = 0
    for part in selector.split():
        tag = None
        classes = set()
        ident = None
        for marker, name in _SIMPLE_SELECTOR_RE.findall(part):
            name = name.lower()
            if marker == ".":
                classes.add(name)
            elif marker == "#":
                ident = name
            else:
                tag = name
        if tag is None and not classes and ident is None:
            return None
        ids += 1 if ident else 0
        class_count += len(classes)
        tag_count += 1 if tag else 0
        compounds.append((tag, classes, ident))

    if not compounds:
        return None
    return (ids, class_count, tag_count), compounds


def _matches_compound(element, compound):
    tag, classes, ident = compound
    if tag is not None and tag != "*" and _localname(element).lower() != tag:
        return False
    if ident is not None and (element.get("id") or "") != ident:
        return False
    if classes:
        own = set((element.get("class") or "").lower().split())
        if not classes.issubset(own):
            return False
    return True


def _matches_chain(element, compounds):
    """Match a descendant chain right-to-left against the element's ancestors."""
    if not _matches_compound(element, compounds[-1]):
        return False
    wanted = len(compounds) - 1
    matched = 0
    for ancestor in element.iterancestors():
        if matched >= wanted:
            break
        if _matches_compound(ancestor, compounds[-2 - matched]):
            matched += 1
    return matched == wanted


class StyleContext(object):
    """Resolves CSS properties for elements, honouring the SVG cascade.

    Priority (highest first), exactly as CSS ranks them:

    1. an ``!important`` declaration in an inline ``style`` attribute,
    2. an ``!important`` rule from a <style> element (by specificity, later
       wins on a tie),
    3. an ordinary declaration in an inline ``style`` attribute,
    4. an ordinary rule from a <style> element (by specificity, later wins on
       a tie),

    then the presentation attribute, whose value loses to any stylesheet rule.
    Unsupported selector forms are skipped rather than guessed at.
    """

    # Cascade ranks.  Importance out-ranks specificity, which is why they are
    # separate numbers rather than one sort key: `rect{fill:#f00 !important}`
    # beats `style="fill:#0f0"` even though an inline declaration has no
    # selector of its own, and an important inline declaration beats both.
    _RANK_INLINE_IMPORTANT = 3
    _RANK_CSS_IMPORTANT = 2
    _RANK_INLINE = 1
    _RANK_CSS = 0

    def __init__(self, root):
        self._styles = {}
        self._rules = {}
        self._cache = {}
        for element in root.iter():
            if not isinstance(element.tag, str):
                continue
            self._styles[element] = _parse_declarations(element.get("style"))
            if _localname(element).lower() == "style":
                self._load_css(element)

    def _load_css(self, style_element):
        text = _flatten_css("".join(style_element.itertext()))
        for selector_group, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
            declarations = _parse_declarations(body)
            if not declarations:
                continue
            for selector in selector_group.split(","):
                parsed = _parse_selector(selector)
                if parsed is None:
                    continue
                specificity, compounds = parsed
                for prop, (value, important) in declarations.items():
                    self._rules.setdefault(prop, []).append(
                        (important, specificity, compounds, value))

    def get(self, element, prop):
        """Return (value, source) or (None, None) when the property is unset.

        Each element is queried for several properties (and stroke/stroke-width
        twice by different rules), so resolutions are memoised per instance.
        """
        key = (element, prop)
        if key in self._cache:
            return self._cache[key]

        result = self._resolve(element, prop)
        self._cache[key] = result
        return result

    def _resolve(self, element, prop):
        best_rank = None
        best_specificity = ()
        best_value = None
        best_source = None

        inline = self._styles.get(element)
        if inline and prop in inline:
            value, important = inline[prop]
            best_rank = (self._RANK_INLINE_IMPORTANT if important
                         else self._RANK_INLINE)
            best_value, best_source = value, "style"

        # Rules are stored in source order, so a later rule of equal
        # specificity overwrites an earlier one without a tie-breaker: the
        # comparison below is >=, and the list is walked front to back.
        for important, specificity, compounds, value in self._rules.get(prop, ()):
            if not _matches_chain(element, compounds):
                continue
            rank = (self._RANK_CSS_IMPORTANT if important else self._RANK_CSS)
            if best_rank is not None:
                if rank < best_rank:
                    continue
                if rank == best_rank and specificity < best_specificity:
                    continue
            best_rank = rank
            best_specificity = specificity
            best_value, best_source = value, "css"

        if best_value is not None:
            return best_value, best_source

        attr = element.get(prop)
        if attr is not None:
            return attr, "attr"
        return None, None


def _inherited_value(element, styles, prop, initial=None):
    """Resolve an inherited property, walking ancestors.

    ``stroke``, ``stroke-width``, ``stroke-opacity``, ``fill``,
    ``fill-opacity``, ``color`` and ``visibility`` are inherited properties, so
    one walk covers them all: the nearest declaration on the element or an
    ancestor wins, ``inherit``/``unset``/``revert`` hand resolution to the
    parent, and ``initial`` (or nothing declared at all) falls back to
    *initial*.

    Returns ``(value, source, element_that_declared_it_or_None)``.  Returning
    the declaring element lets a caller report where an inherited bound came
    from instead of pretending the element declared it.
    """
    node = element
    while node is not None:
        if isinstance(node.tag, str):
            value, source = styles.get(node, prop)
            if value is not None:
                text = str(value).strip().lower()
                if text in _INHERIT_KEYWORDS:
                    pass                      # keep walking up
                elif text == "initial" or text.startswith("initial "):
                    return initial, "initial", None
                else:
                    return value, source, node
        node = node.getparent()
    return initial, ("initial" if initial is not None else "unset"), None


# --------------------------------------------------------------------------
# Paint scopes: display/visibility, and what a definition container means
# --------------------------------------------------------------------------

def _paint_value(value):
    """A property value as a plain lowercase string ('' for unset)."""
    return str(value).strip() if value is not None else ""


def _opacity_fraction(raw):
    """Parse an opacity value to a 0..1 float, or None when unparseable.

    SVG opacity is a number or a percentage; anything else is left to the
    browser, so returning None here means "no claim", never "zero".
    """
    text = _paint_value(raw)
    if not text:
        return None
    try:
        if text.endswith("%"):
            return float(text[:-1]) / 100.0
        return float(text)
    except ValueError:
        return None


def _display_none(element, styles):
    """True when the element (and everything under it) renders nothing.

    ``display`` is not inherited, but a ``display:none`` subtree is never
    rendered no matter what a descendant declares, so this is a subtree rule.
    """
    value, _ = styles.get(element, "display")
    return _paint_value(value).rstrip(";").lower() == "none"


def _opacity_zero(element, styles):
    """True when ``opacity:0`` makes the element and its subtree invisible.

    ``opacity`` is a group property: it applies to the element *and its
    descendants* as one composite, so zero hides the whole subtree.
    """
    value, _ = styles.get(element, "opacity")
    fraction = _opacity_fraction(value)
    return fraction is not None and fraction <= 0.0


def _visibility_in_force(element, styles, inherited):
    """The ``visibility`` value in force, as "visible" or "hidden".

    ``visibility`` is inherited but *overridable*: a descendant may set
    ``visibility:visible`` and paint again inside a hidden parent, so a walker
    must carry this value down and let each element change it.
    """
    value, _ = styles.get(element, "visibility")
    text = _paint_value(value).lower()
    if text in ("hidden", "collapse"):
        return "hidden"
    if text == "visible":
        return "visible"
    if text == "initial":
        # The initial value of visibility is visible, so `initial` restores it.
        return "visible"
    return inherited


def _referenced_ids(root):
    """Every id the document references, as a set of strings.

    Covers both paths SVG uses to instantiate something: ``<use href="#id">``
    (and ``xlink:href``), and ``url(#id)`` in any attribute value -- a fill, a
    clip-path, a mask, a filter.  A definition container nothing references
    cannot put ink on the sheet, and this set is how that is decided.
    """
    referenced = set()
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        for raw in element.attrib.values():
            for match in _URL_RE.finditer(str(raw)):
                target = match.group("url").strip().strip("'\"")
                if target.startswith("#") and len(target) > 1:
                    referenced.add(target[1:])
        if _localname(element).lower() == "use":
            for raw in (element.get("href"), element.get(XLINK_HREF)):
                target = (raw or "").strip()
                if target.startswith("#") and len(target) > 1:
                    referenced.add(target[1:])
    return referenced


def _paint_scope(parent_scope, element, styles, referenced_ids):
    """Narrow the paint scope when descending into *element*.

    Scopes, from "puts ink on the sheet" down to "cannot":

    * ``painted``   -- the element's own fill/stroke reach the sheet.
    * ``resource``  -- a paint resource (a gradient): its <stop> declarations
                       count towards the colour budget, its geometry does not
                       stroke.
    * ``unchecked`` -- inside a definition container nothing references.  It
                       cannot print, so it is reported as partial coverage
                       (UNCHECKED_DEFINITION) rather than failed.
    * ``inert``     -- inside a container whose content can never ink
                       (<clipPath>, <filter>, <style>, <metadata> ...).
    * ``hidden``    -- ``display:none`` or ``opacity:0``: not rendered at all.
    """
    if parent_scope in ("hidden", "inert"):
        return parent_scope

    name = _localname(element).lower()
    if _display_none(element, styles) or _opacity_zero(element, styles):
        return "hidden"
    if name in _INERT_CONTAINERS:
        return "inert"
    if element.get("id") in referenced_ids:
        # A <use> or a url(#id) instantiates this subtree, even inside <defs>.
        return "painted"
    if name in _INK_RESOURCE_CONTAINERS:
        return "resource"
    if name in _DEFERRED_INK_CONTAINERS:
        return "unchecked"
    return parent_scope


def _walk_paint_scopes(root, styles, referenced_ids):
    """Yield ``(element, scope, visibility)`` for every element, in order.

    ``scope`` is the element's effective paint scope (see ``_paint_scope``),
    forced to "hidden" when the inherited ``visibility`` in force hides it;
    ``visibility`` is that value, so a caller can hand it to children (which
    may set ``visibility:visible`` and paint again).
    """
    def walk(element, scope, visibility):
        child_visibility = _visibility_in_force(element, styles, visibility)
        container_scope = _paint_scope(scope, element, styles, referenced_ids)
        effective = ("hidden" if child_visibility == "hidden"
                     else container_scope)
        yield element, effective, child_visibility
        for child in element:
            if isinstance(child.tag, str):
                for item in walk(child, container_scope, child_visibility):
                    yield item

    for item in walk(root, "painted", "visible"):
        yield item


# --------------------------------------------------------------------------
# Length parsing
# --------------------------------------------------------------------------

def _length_attr_to_mm(raw):
    """Convert a width/height attribute to millimetres, or None if it can't be."""
    if raw is None:
        return None
    match = _LENGTH_RE.match(str(raw))
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2).lower()
    if unit == "":
        unit = "px"
    if unit not in UNIT_TO_USER:
        return None
    return number * UNIT_TO_USER[unit] * MM_PER_USER_UNIT_DEFAULT


def compute_document_scale(root):
    """How many millimetres does one user unit cover in this document?

    SVG resolves a length to *user units*; the viewBox then maps user units
    onto the physical viewport given by width/height.  Physical thickness
    therefore depends on both, and reading a stroke-width as if one user unit
    were one CSS px is wrong whenever the viewBox rescales -- measured against
    Inkscape renders, in both directions:

      ``width="210mm" viewBox="0 0 210 297"`` -> 1 unit = 1.0000mm, so a
        unitless ``stroke-width="1"`` prints at **2.835pt**, not the 0.75pt a
        px assumption reports (a false failure on perfectly printable art).
      ``width="100mm" viewBox="0 0 1000 1000"`` -> 1 unit = 0.1000mm, so the
        same value prints at **0.283pt**, which a px assumption reports as a
        safe-looking 0.75pt (a false pass on a hairline).

    Returns ``(mm_per_unit, description, note_or_None)``.
    """
    viewbox = root.get("viewBox") or root.get("viewbox")
    width_mm = _length_attr_to_mm(root.get("width"))
    height_mm = _length_attr_to_mm(root.get("height"))

    if not viewbox:
        note = None
        if width_mm is not None and width_mm > 0:
            note = ("document is %.2fmm wide but declares no viewBox, so all "
                    "lengths are read as CSS px (1px = %.4fmm)"
                    % (width_mm, MM_PER_USER_UNIT_DEFAULT))
        return (MM_PER_USER_UNIT_DEFAULT,
                "no viewBox, so 1 user unit = 1px = %.4fmm"
                % MM_PER_USER_UNIT_DEFAULT, note)

    parts = re.split(r"[\s,]+", viewbox.strip())
    if len(parts) != 4:
        return (MM_PER_USER_UNIT_DEFAULT,
                "viewBox %r is unparseable, assuming 1 user unit = 1px"
                % viewbox,
                "viewBox %r is not four numbers, so stroke widths fall back to "
                "a 96dpi assumption" % viewbox)
    try:
        vb_w, vb_h = float(parts[2]), float(parts[3])
    except ValueError:
        return (MM_PER_USER_UNIT_DEFAULT,
                "viewBox %r is unparseable, assuming 1 user unit = 1px"
                % viewbox,
                "viewBox %r has non-numeric extents" % viewbox)

    if vb_w <= 0:
        return (MM_PER_USER_UNIT_DEFAULT,
                "viewBox width is %g, assumed 1 user unit = 1px" % vb_w,
                "viewBox %r has no usable width" % viewbox)

    derived = False
    if width_mm is None:
        if height_mm is None or vb_h <= 0:
            return (MM_PER_USER_UNIT_DEFAULT,
                    "no width/height, assumed 1 user unit = 1px",
                    "document declares no physical size, so stroke widths "
                    "cannot be tied to a real measurement (add width/height in "
                    "mm for an exact check)")
        width_mm = height_mm * vb_w / vb_h
        derived = True

    scale = width_mm / vb_w
    desc = ("viewBox '0 0 %g %g' on %.2fmm%s, so 1 user unit = %.4fmm"
            % (vb_w, vb_h, width_mm, " (derived from height)" if derived else "",
               scale))

    note = None
    if height_mm is not None and vb_h > 0:
        y_scale = height_mm / vb_h
        if abs(y_scale - scale) > scale * 0.001:
            extra = ""
            if root.get("preserveAspectRatio", "").strip().lower().startswith("none"):
                extra = " (preserveAspectRatio='none')"
            note = ("the x and y scales differ (%.4f vs %.4f mm per user unit)"
                    "%s, so stroke width renders non-uniformly; it is reported "
                    "on the x axis" % (scale, y_scale, extra))
    return scale, desc, note


def length_to_pt(raw, mm_per_unit=MM_PER_USER_UNIT_DEFAULT):
    """Convert an SVG length string to physical points.

    Two stages, because SVG has two:

    1. The declared value becomes *user units* via the CSS reference ratios
       (``UNIT_TO_USER``).  ``1pt`` is 1.3333 user units, not 1.
    2. User units become millimetres via the document's ``mm_per_unit`` scale,
       then points.

    Skipping stage 2 is the classic error -- see ``compute_document_scale``.

    Returns ``(points, error_message)``; exactly one of the two is ever set.
    """
    if raw is None:
        return None, "missing value"
    text = str(raw).strip()
    if text.lower() in ("inherit", ""):
        return None, "value is not a length (%r)" % raw
    match = _LENGTH_RE.match(text)
    if not match:
        return None, "cannot parse length %r" % raw
    number = float(match.group(1))
    unit = match.group(2).lower()
    if unit == "%":
        return None, ("percentage length %r cannot be resolved without the "
                      "viewport cascade" % raw)
    if unit == "":
        unit = DEFAULT_UNIT
    if unit not in UNIT_TO_USER:
        return None, ("unsupported unit %r (supported: %s)"
                      % (unit, ", ".join(sorted(UNIT_TO_USER))))
    if number < 0:
        return None, "negative stroke-width %r is invalid in SVG" % raw
    user_units = number * UNIT_TO_USER[unit]
    return user_units * mm_per_unit * PT_PER_MM, None


# --------------------------------------------------------------------------
# Geometry helpers (svgpathtools)
# --------------------------------------------------------------------------

def _subpaths(path):
    """Split a Path into continuous subpaths, tolerating library failures."""
    try:
        subs = path.continuous_subpaths()
        if subs:
            return list(subs)
    except Exception:
        pass
    return [path]


def _safe_length(obj):
    """Total length of a Path, falling back to chord lengths if need be."""
    try:
        return float(obj.length())
    except Exception:
        pass
    total = 0.0
    try:
        for segment in obj:
            try:
                total += float(segment.length())
            except Exception:
                total += abs(segment.end - segment.start)
    except Exception:
        return 0.0
    return total


def _path_is_closed(path):
    """True when the path (or subpath) returns to its start point."""
    try:
        return bool(path.isclosed())
    except Exception:
        try:
            return abs(path.start - path.end) <= CLOSE_TOL
        except Exception:
            return False


def _safe_area(path):
    """Signed enclosed area of a closed path, or None if unobtainable."""
    try:
        return abs(float(path.area()))
    except Exception:
        return None


# --------------------------------------------------------------------------
# Spec loading
# --------------------------------------------------------------------------

def load_spec(spec_path, failures, notes):
    """Load spec.json, filling in documented defaults for any missing key."""
    if not os.path.exists(spec_path):
        failures.append((RULE_INPUT, "spec file not found: %s" % spec_path))
        return None
    try:
        with open(spec_path, "r", encoding="utf-8") as handle:
            spec = json.load(handle)
    except ValueError as exc:
        failures.append((RULE_SPEC, "spec file is not valid JSON (%s): %s"
                         % (spec_path, exc)))
        return None
    except OSError as exc:
        failures.append((RULE_SPEC, "cannot read spec file %s: %s"
                         % (spec_path, exc)))
        return None

    if not isinstance(spec, dict):
        failures.append((RULE_SPEC, "spec root must be a JSON object, got %s"
                         % type(spec).__name__))
        return None

    geometry = spec.get("geometry")
    if geometry is None:
        notes.append("spec has no 'geometry' section; using defaults "
                     "(allow_raster_embed=false, allow_open_paths=false, "
                     "min_stroke_width_pt=0)")
        geometry = {}
        spec["geometry"] = geometry
    if not isinstance(geometry, dict):
        failures.append((RULE_SPEC, "'geometry' must be a JSON object, got %s"
                         % type(geometry).__name__))
        return None

    print_method = str(spec.get("print_method", "") or "").strip().lower()
    is_cutting = print_method in CUTTING_PRINT_METHODS

    if "allow_open_paths" in geometry:
        allow_open = bool(geometry["allow_open_paths"])
        open_source = "geometry.allow_open_paths"
    elif is_cutting:
        # A cut job is made of closed outlines by definition.
        allow_open = False
        open_source = "print_method=%s requires closed outlines" % print_method
    elif print_method:
        # A printing method puts ink on a sheet, so open strokes are legitimate.
        allow_open = True
        open_source = "print_method=%s tolerates open strokes" % print_method
    else:
        # Nothing said either way: stay conservative, as before.
        allow_open = False
        open_source = "default (neither allow_open_paths nor print_method given)"

    validation_block = spec.get("validation")
    gradient_source = ("geometry.allow_gradients" if "allow_gradients" in geometry
                      else "default (geometry.allow_gradients is not set)")
    resolved = {
        "allow_raster_embed": bool(geometry.get("allow_raster_embed", False)),
        "allow_gradients": bool(geometry.get("allow_gradients", False)),
        "allow_gradients_source": gradient_source,
        "allow_open_paths": allow_open,
        "open_paths_source": open_source,
        "min_stroke_width_pt": geometry.get("min_stroke_width_pt"),
        "max_colors": spec.get("max_colors"),
        "print_method": print_method,
        "max_nodes_per_path": geometry.get("max_nodes_per_path"),
        "palette": spec.get("palette"),
        "dimensions": spec.get("dimensions"),
        "gradient_handling": str(geometry.get("gradient_handling", "") or "").lower(),
        "ink_limit_percent": spec.get("ink_limit_percent"),
        "require_cmyk": spec.get("require_cmyk"),
        "icc_profile_path": spec.get("icc_profile_path"),
        "validation": validation_block if isinstance(validation_block, dict) else {},
    }

    if "allow_raster_embed" not in geometry:
        notes.append("spec is missing geometry.allow_raster_embed; assuming false")
    if "allow_open_paths" not in geometry:
        if is_cutting:
            notes.append("print_method=%s requires closed outlines, so open "
                         "paths are a hard failure" % print_method)
        else:
            notes.append("spec has no geometry.allow_open_paths and print_method "
                         "is not a cutting method: open paths are allowed")
    elif is_cutting and allow_open:
        # Explicit beats derived, so the setting is honoured -- but the pair is
        # physically contradictory: a plotter cannot cut a line that never
        # closes, and a cutter with an open contour is a failed job, not a
        # warning. Say so loudly rather than letting the explicit key quietly
        # disable the cutting rule.
        notes.append("CONTRADICTION: print_method=%s requires closed outlines, "
                     "but geometry.allow_open_paths is explicitly true, so open "
                     "paths will NOT be reported. Set it to false unless this "
                     "job is not actually being cut" % print_method)

    if resolved["min_stroke_width_pt"] is None:
        notes.append("spec is missing geometry.min_stroke_width_pt; "
                     "no minimum stroke width is enforced")
        resolved["min_stroke_width_pt"] = 0.0
    else:
        try:
            resolved["min_stroke_width_pt"] = float(resolved["min_stroke_width_pt"])
        except (TypeError, ValueError):
            failures.append((RULE_SPEC, "geometry.min_stroke_width_pt must be a "
                             "number, got %r" % geometry.get("min_stroke_width_pt")))
            return None
        if resolved["min_stroke_width_pt"] < 0:
            failures.append((RULE_SPEC, "geometry.min_stroke_width_pt must not "
                             "be negative, got %s"
                             % _fmt_num(resolved["min_stroke_width_pt"])))
            return None

    if resolved["max_colors"] is None:
        notes.append("spec is missing max_colors; colour count is not limited")
    else:
        try:
            resolved["max_colors"] = int(resolved["max_colors"])
        except (TypeError, ValueError):
            failures.append((RULE_SPEC, "max_colors must be an integer, got %r"
                             % spec.get("max_colors")))
            return None
        if resolved["max_colors"] < 0:
            failures.append((RULE_SPEC, "max_colors must not be negative, got %d"
                             % resolved["max_colors"]))
            return None

    # --- v4.0 stage keys -------------------------------------------------
    mnp = resolved["max_nodes_per_path"]
    if mnp is not None:
        try:
            resolved["max_nodes_per_path"] = int(mnp)
        except (TypeError, ValueError):
            failures.append((RULE_SPEC, "geometry.max_nodes_per_path must be an "
                             "integer, got %r" % (mnp,)))
            return None
        if resolved["max_nodes_per_path"] <= 0:
            failures.append((RULE_SPEC, "geometry.max_nodes_per_path must be "
                             "positive, got %d" % resolved["max_nodes_per_path"]))
            return None

    if resolved["palette"] is not None:
        if not isinstance(resolved["palette"], list):
            failures.append((RULE_SPEC, "palette must be a list of colour "
                             "strings, got %s" % type(resolved["palette"]).__name__))
            return None
        normalised = []
        for entry in resolved["palette"]:
            colour = normalize_color(entry)
            if colour is None:
                notes.append("palette entry %r is not a usable colour and was "
                             "ignored" % (entry,))
                continue
            normalised.append(colour)
        resolved["palette"] = normalised or None
        if resolved["palette"] is None:
            notes.append("palette contains no usable colours; the palette check "
                         "is disabled")

    if resolved["dimensions"] is not None:
        if not isinstance(resolved["dimensions"], dict):
            failures.append((RULE_SPEC, "dimensions must be a JSON object, got %s"
                             % type(resolved["dimensions"]).__name__))
            return None
        parsed = {}
        for key in ("width_mm", "height_mm"):
            if resolved["dimensions"].get(key) is None:
                continue
            try:
                parsed[key] = float(resolved["dimensions"][key])
            except (TypeError, ValueError):
                failures.append((RULE_SPEC, "dimensions.%s must be a number, "
                                 "got %r" % (key, resolved["dimensions"][key])))
                return None
        resolved["dimensions"] = parsed or None
        if resolved["dimensions"] is None:
            notes.append("spec has a 'dimensions' block with no width_mm or "
                         "height_mm, so the size check is disabled")

    if resolved["ink_limit_percent"] is not None:
        try:
            resolved["ink_limit_percent"] = float(resolved["ink_limit_percent"])
        except (TypeError, ValueError):
            failures.append((RULE_SPEC, "ink_limit_percent must be a number, "
                             "got %r" % (resolved["ink_limit_percent"],)))
            return None
        if resolved["ink_limit_percent"] <= 0:
            failures.append((RULE_SPEC, "ink_limit_percent must be positive, got "
                             "%s" % _fmt_num(resolved["ink_limit_percent"])))
            return None

    return resolved


# --------------------------------------------------------------------------
# Rule 1: raster embeds
# --------------------------------------------------------------------------

def _href_of(element):
    """The href in force for *element*, across the href and xlink:href forms."""
    return (element.get("href") or element.get(XLINK_HREF) or "").strip()


def _href_is_external(href):
    """True for an href that pulls in a whole other document or a data: URI.

    A ``#fragment`` stays inside this document (and can be vector), so it is
    not external; anything else has to be fetched and cannot be audited here.
    """
    text = (href or "").strip().strip("'\"")
    if not text:
        return False
    return not text.startswith("#")


def check_raster_embeds(root, failures):
    """Report every raster or rasterising source, not only ``<image>``.

    An ``<image>`` was the whole of this check, which let three ways of getting
    a bitmap into the artwork through with zero findings: an ``<feImage>``
    inside a ``<filter>``, a ``<foreignObject>`` (which rasterises arbitrary
    XHTML/CSS), and a ``<use>`` naming an external document.  All of them, plus
    a ``url(data:image/...)`` in a stylesheet or an inline style, are counted
    here -- one rule, one count, one message.
    """
    offenders = []                 # (element, kind, href_or_note)
    patterns_with_images = []

    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        name = _localname(element).lower()

        if name == "image":
            offenders.append((element, "image", _href_of(element) or "(no href)"))
        elif name == "feimage":
            href = _href_of(element)
            if _href_is_external(href):
                offenders.append((element, "feImage", href))
        elif name == "foreignobject":
            offenders.append((element, "foreignObject",
                              "renders XHTML/CSS, so it rasterises"))
        elif name == "use":
            href = _href_of(element)
            if _href_is_external(href):
                offenders.append((element, "use", href))
        elif name == "pattern":
            for descendant in element.iter():
                if isinstance(descendant.tag, str) and \
                        _localname(descendant).lower() == "image":
                    patterns_with_images.append(element)
                    break

        # A stylesheet or an inline style can pull in a bitmap as a paint
        # server; url(#id) is a document-internal reference and is left alone.
        style_text = element.get("style") or ""
        if name == "style":
            style_text = "".join(element.itertext())
        for match in _DATA_IMAGE_RE.finditer(style_text):
            offenders.append((element, "style url()",
                              _shorten(style_text[match.start():])))

    if not offenders:
        return

    count = len(offenders)
    only_images = all(kind == "image" for _e, kind, _h in offenders)
    if only_images:
        detail = ("Found %d embedded raster image%s but spec requires "
                  "vector-only artwork (allow_raster_embed=false)"
                  % (count, "" if count == 1 else "s"))
    else:
        detail = ("Found %d embedded raster source%s (an <image>, <feImage>, "
                  "<foreignObject>, a non-fragment <use>/<image> href, or a "
                  "data: image URL in a style) but spec requires vector-only "
                  "artwork (allow_raster_embed=false)"
                  % (count, "" if count == 1 else "s"))
    if patterns_with_images:
        detail += "; %d inside <pattern> definitions" % len(patterns_with_images)

    samples = []
    for position, (element, kind, note) in enumerate(offenders[:5], 1):
        label = _describe(element, position)
        if kind == "style url()":
            label += " (%s)" % kind
        samples.append("%s -> %s" % (label, _shorten(note)))
    if samples:
        detail += ". Offenders: " + "; ".join(samples)
    if count > 5:
        detail += " (plus %d more)" % (count - 5)
    failures.append((RULE_RASTER, detail))


def _shorten(text, limit=60):
    text = str(text)
    return text if len(text) <= limit else text[:limit - 3] + "..."


# --------------------------------------------------------------------------
# Rule 2: colour count
# --------------------------------------------------------------------------

def _paint_server_reference(value):
    """The raw ``url(...)`` target in a paint value, or None."""
    match = _URL_RE.search(_paint_value(value))
    if not match:
        return None
    return match.group("url").strip().strip("'\"") or "(empty)"


def _normalise_paint(raw, element, styles):
    """Normalise one paint declaration, resolving ``currentColor``.

    ``currentColor`` is a colour, not a reason to skip one: it resolves to the
    inherited ``color`` property (or that property's initial value, black, when
    nothing declares it), so ``fill="currentColor"`` art is counted instead of
    reading as using no ink at all.
    """
    text = _paint_value(raw)
    if not text:
        return None
    if text.lower() in ("currentcolor", "current-color"):
        inherited, _source, _declared_on = _inherited_value(
            element, styles, "color", _INITIAL_COLOR)
        return normalize_color(inherited if inherited is not None
                               else _INITIAL_COLOR)
    return normalize_color(text)


def check_color_count(root, styles, max_colors, failures, stats=None, notes=None):
    """Count every colour in force for an element that actually paints.

    Three things the count used to miss, all of which made a budget look
    smaller than it is -- and a black-fill artwork look like no ink at all:

    * the implicit black of a shape whose cascade never sets ``fill`` (the SVG
      initial value is black), skipped when the fill in force is ``none``/
      ``transparent`` or its opacity is zero;
    * ``currentColor``, resolved from the inherited ``color`` property;
    * nothing else that paints, because the walk now skips ``display:none``
      subtrees and definition content nothing references.

    ``stop-color`` is counted for a gradient's stops whether or not the gradient
    is referenced: that declaration is what ``snap_colors`` rewrites and what
    the palette check has to see (see the module docstring).
    """
    used = {}      # normalised colour -> set of paint properties that used it
    referenced = _referenced_ids(root)
    hidden = 0
    implicit = 0
    currentcolor = 0

    for element, scope, _visibility in _walk_paint_scopes(root, styles,
                                                          referenced):
        if scope in ("hidden", "inert", "unchecked"):
            if scope == "hidden":
                hidden += 1
            continue
        name = _localname(element).lower()

        if name == "stop":
            # A gradient declares its ink through <stop>, whether the gradient
            # is referenced ("painted") or merely declared ("resource"): that
            # declaration is what the budget and the palette check are about.
            value, _source = styles.get(element, "stop-color")
            colour = _normalise_paint(value, element, styles)
            if colour:
                if _paint_value(value).lower() in ("currentcolor",
                                                   "current-color"):
                    currentcolor += 1
                used.setdefault(colour, set()).add("stop-color")
            continue

        if scope == "resource":
            # Any other content of a paint resource is not ink of its own.
            continue

        if name not in _SHAPE_ELEMENTS:
            continue

        # fill: an unset fill is not "no colour", it is black.
        fill_raw, _fill_source, _fill_node = _inherited_value(
            element, styles, "fill", None)
        fill_opacity, _source, _node = _inherited_value(
            element, styles, "fill-opacity", "1")
        fill_fraction = _opacity_fraction(fill_opacity)
        if fill_raw is None:
            if fill_fraction is None or fill_fraction > 0.0:
                implicit += 1
                used.setdefault(_INITIAL_COLOR, set()).add("fill (implicit)")
        elif fill_fraction is None or fill_fraction > 0.0:
            if _paint_value(fill_raw).lower() in ("currentcolor",
                                                 "current-color"):
                currentcolor += 1
            colour = _normalise_paint(fill_raw, element, styles)
            if colour:
                used.setdefault(colour, set()).add("fill")

        # stroke: same inheritance, but a fully transparent stroke inks nothing.
        stroke_raw, _stroke_source, _stroke_node = _inherited_value(
            element, styles, "stroke", None)
        stroke_opacity, _source, _node = _inherited_value(
            element, styles, "stroke-opacity", "1")
        stroke_fraction = _opacity_fraction(stroke_opacity)
        if stroke_raw is not None and (
                stroke_fraction is None or stroke_fraction > 0.0):
            if _paint_value(stroke_raw).lower() in ("currentcolor",
                                                    "current-color"):
                currentcolor += 1
            colour = _normalise_paint(stroke_raw, element, styles)
            if colour:
                used.setdefault(colour, set()).add("stroke")

    if stats is not None:
        stats["implicit_fill_elements"] = implicit
        stats["currentcolor_elements"] = currentcolor
        stats["hidden_elements"] = hidden

    if max_colors is None:
        return len(used), used
    if len(used) > max_colors:
        listed = ", ".join(sorted(used))
        if len(listed) > 400:
            listed = listed[:400] + " ..."
        failures.append((
            RULE_COLORS,
            "Color count %d exceeds max %d (used: %s)"
            % (len(used), max_colors, listed),
        ))
    return len(used), used


# --------------------------------------------------------------------------
# Rule 2b: gradients, which the spec can ban outright
# --------------------------------------------------------------------------

def _css_paint_references(style_element):
    """``(prop, url_target)`` for every paint declaration in one <style> block.

    ``url(#id)`` in a *paint* property is a paint server; the same syntax in
    ``clip-path``/``mask``/``filter`` is an effect, not paint, and is reported
    by the effect advisory instead of here.
    """
    found = []
    text = _flatten_css("".join(style_element.itertext()))
    for _selector_group, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
        for prop, (value, _important) in _parse_declarations(body).items():
            if prop not in _PAINT_PROPERTIES:
                continue
            target = _paint_server_reference(value)
            if target is not None:
                found.append((prop, target))
    return found


def check_gradients(root, styles, allow_gradients, failures, notes, stats,
                    source=None):
    """Enforce ``geometry.allow_gradients`` at the source level.

    Until now the key was parsed and then never read: a two-stop gradient
    painted with ``fill="url(#g)"`` passed layer A with zero findings, so
    ``--layer-a-only`` (and a spec with ``validation.run_preflight: false``) let
    continuous tone through the gate that claims to enforce the spec.  Only
    layer B's rendered ink count caught it.

    Both halves of the declaration are reported: the gradient *elements* the
    document declares, and the sites that *paint* with a paint server.  A
    ``url(...)`` that cannot be resolved to an element in this document is
    reported too -- the honest answer is "this paints with something I cannot
    read", never a silent pass.  With ``allow_gradients`` true nothing here
    fails: process/CMYK work is allowed to carry tone.
    """
    declared = []
    references = []                # (element, prop, target)

    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        name = _localname(element).lower()
        if name in _GRADIENT_ELEMENTS:
            declared.append(element)
        for prop in _PAINT_PROPERTIES:
            value, _source = styles.get(element, prop)
            target = _paint_server_reference(value)
            if target is not None:
                references.append((element, prop, target))
        if name == "style":
            for prop, target in _css_paint_references(element):
                references.append((element, prop, target))

    stats["gradient_definitions"] = len(declared)
    stats["gradient_paint_references"] = len(references)
    if allow_gradients:
        return

    gradient_ids = {element.get("id") for element in declared}
    element_kinds = {}
    for element in root.iter():
        if isinstance(element.tag, str) and element.get("id"):
            element_kinds.setdefault(element.get("id"), _localname(element))
    source_text = source or "geometry.allow_gradients"
    if declared:
        samples = "; ".join(
            "%s (id=%r)" % (_describe(element, position),
                            element.get("id") or "(none)")
            for position, element in enumerate(declared[:5], 1))
        detail = ("%d gradient definition%s found but geometry.allow_gradients "
                  "is false (%s): this job is flat spot colour, so a gradient "
                  "cannot be reproduced. Offenders: %s"
                  % (len(declared), "" if len(declared) == 1 else "s",
                     source_text, samples))
        if len(declared) > 5:
            detail += " (plus %d more)" % (len(declared) - 5)
        failures.append((RULE_GRADIENT, detail))

    if references:
        samples = []
        for position, (element, prop, target) in enumerate(references[:5], 1):
            samples.append("%s %s='url(%s)' -> %s"
                           % (_describe(element, position), prop, target,
                              _describe_paint_target(target, gradient_ids,
                                                     element_kinds)))
        detail = ("%d paint-server reference%s found but "
                  "geometry.allow_gradients is false (%s); these paints are "
                  "unresolvable to a flat colour and cannot be checked as one. "
                  "Offenders: %s"
                  % (len(references), "" if len(references) == 1 else "s",
                     source_text, "; ".join(samples)))
        if len(references) > 5:
            detail += " (plus %d more)" % (len(references) - 5)
        failures.append((RULE_GRADIENT, detail))


def _describe_paint_target(target, gradient_ids, element_kinds):
    """Say what a ``url(...)`` resolves to, or that it cannot be resolved.

    The answer has to be honest about *what* it found: a reference to a
    ``<pattern>`` is a paint server but not a gradient, and saying "gradient"
    about it would be a wrong report even if the failure is right.
    """
    if target.startswith("#"):
        target_id = target[1:]
        if target_id in gradient_ids:
            return "the declared gradient id=%r" % target_id
        kind = element_kinds.get(target_id)
        if kind:
            return ("<%s id=%r>, a paint server that is not a gradient"
                    if kind in ("pattern",) else
                    "<%s id=%r>, which is not a paint server"
                    ) % (kind, target_id)
        return "nothing: no element with id=%r in this document" % target_id
    return "an external or data: paint server (%s)" % _shorten(target)


# --------------------------------------------------------------------------
# Rule 2c: advisory observations (never failures)
# --------------------------------------------------------------------------

def check_definition_coverage(root, styles, notes, stats):
    """Report once when definition content was skipped, rather than failing it.

    Content inside ``<defs>``/``<symbol>``/``<pattern>``/``<mask>``/
    ``<marker>`` that nothing instantiates cannot print, so a hairline inside
    it is not a print defect -- a hard finding there was a false failure on
    artwork that prints fine.  The operator is still told that coverage is
    partial, once, with a count.
    """
    referenced = _referenced_ids(root)
    skipped = []
    containers = set()
    for element, scope, _visibility in _walk_paint_scopes(root, styles,
                                                          referenced):
        if scope != "unchecked":
            continue
        name = _localname(element).lower()
        if name in _DEFERRED_INK_CONTAINERS:
            containers.add(_localname(element))
        elif name in _SHAPE_ELEMENTS:
            skipped.append(element)

    stats["unchecked_definition_elements"] = len(skipped)
    if not skipped:
        return
    where = "/".join(sorted(containers)) or "definition containers"
    notes.append(
        "%s: %d shape element%s inside unreferenced <%s> content were not "
        "stroke- or colour-checked -- nothing in this document instantiates "
        "them, so they cannot print, and reporting them as failures would be a "
        "false finding on invisible art. Coverage of this file is therefore "
        "partial; use a <use> or a url(#id) reference if that content is meant "
        "to print" % (RULE_UNCHECKED, len(skipped),
                      "" if len(skipped) == 1 else "s", where))


def check_translucency(root, styles, notes, stats):
    """Record a partly transparent paint, without failing it.

    An opacity strictly between 0 and 1 is not a defect: a spot-colour screen
    prints a tint of the same ink.  It does move the *rendered* ink count
    though (two tones of one ink can measure as two), so it is worth knowing
    before layer B reports it.  Zero opacity is a different thing entirely --
    that paints nothing, and the paint checks skip it.
    """
    referenced = _referenced_ids(root)
    translucent = []
    for element, scope, _visibility in _walk_paint_scopes(root, styles,
                                                          referenced):
        if scope != "painted":
            continue
        fraction = _opacity_fraction(styles.get(element, "opacity")[0])
        if fraction is not None and 0.0 < fraction < 1.0:
            translucent.append((element, "opacity", fraction))
            continue
        for prop in ("fill-opacity", "stroke-opacity"):
            value, _source, _node = _inherited_value(element, styles, prop, None)
            fraction = _opacity_fraction(value)
            if fraction is not None and 0.0 < fraction < 1.0:
                translucent.append((element, prop, fraction))

    stats["translucent_elements"] = len(translucent)
    if not translucent:
        return
    samples = "; ".join("%s %s=%s" % (_describe(element, position), prop,
                                      _fmt_num(fraction))
                        for position, (element, prop, fraction)
                        in enumerate(translucent[:5], 1))
    notes.append(
        "%s: %d element%s paint with an opacity between 0 and 1 (%s). That is "
        "not a failure -- a spot-colour screen prints a tint -- but the "
        "rendered ink count may see the tints as extra inks, so check layer B's "
        "number against the screens you agreed"
        % (RULE_TRANSLUCENT, len(translucent),
           "" if len(translucent) == 1 else "s", samples))


def check_effects(root, styles, notes, stats):
    """Report filter/mask/clip-path references once, as advice.

    A filter can blur, flood or offset the ink; a mask can scale it away; a
    clip path can cut it.  Layer A reads declarations, so it cannot measure any
    of that -- which makes this an advisory in both layers, never a hard
    finding.  The rendered proof (layer B) is where these become numbers.
    """
    referenced = _referenced_ids(root)
    effects = []
    for element, scope, _visibility in _walk_paint_scopes(root, styles,
                                                          referenced):
        if scope not in ("painted", "resource"):
            continue
        for prop in _EFFECT_PROPERTIES:
            value, _source = styles.get(element, prop)
            text = _paint_value(value)
            if text and text.lower() not in ("none", "inherit", "initial",
                                             "unset"):
                effects.append((element, prop, text))

    stats["effect_references"] = len(effects)
    if not effects:
        return
    samples = "; ".join("%s %s=%s" % (_describe(element, position), prop,
                                      _shorten(value, 40))
                        for position, (element, prop, value)
                        in enumerate(effects[:5], 1))
    notes.append(
        "%s: %d filter/mask/clip-path reference%s found (%s). Layer A cannot "
        "measure what these do to the ink -- blur, flood, luminance or clipping "
        "-- so this is reported rather than failed; layer B's rendered proof is "
        "where the effect becomes visible, and a filter or mask that changes the "
        "ink count is layer B's finding to make"
        % (RULE_EFFECT, len(effects), "" if len(effects) == 1 else "s", samples))


# --------------------------------------------------------------------------
# Rule 3: minimum stroke width
# --------------------------------------------------------------------------

_NOT_STROKED_VALUES = ("none", "transparent")


def check_stroke_widths(root, styles, min_pt, failures,
                        mm_per_unit=MM_PER_USER_UNIT_DEFAULT, scale_desc=None):
    """Check every stroked shape's stroke-width against the minimum.

    ``stroke``, ``stroke-width`` and ``stroke-opacity`` are inherited
    properties, so each shape is resolved against its whole ancestor chain: a
    <path> that picks up its stroke from a parent <g> is checked with the width
    actually in force for it.

    Only *shape* elements are reported.  A <g> carrying stroke-width paints
    nothing itself, so reporting it as well would double-count one hairline; and
    an element that cannot ink -- inside a <clipPath>, under display:none, with
    stroke-opacity 0, or inside definition content nothing references -- is not
    reported at all.  Those were the false MIN_STROKE_WIDTH findings (see the
    module docstring for what is skipped and why).

    Widths are resolved against ``mm_per_unit`` -- the document's real
    user-unit scale -- so a viewBox-rescaled file is measured in true
    millimetres rather than an assumed 96dpi.  The basis is printed with each
    failure so the number can be checked by hand.
    """
    referenced = _referenced_ids(root)
    counter = [0]
    thin = []          # (label, points, width_raw, width_source) for the summary

    for element, scope, _visibility in _walk_paint_scopes(root, styles,
                                                          referenced):
        if scope != "painted":
            continue
        if _localname(element).lower() not in _SHAPE_ELEMENTS:
            continue

        stroke_raw, _stroke_source, _stroke_node = _inherited_value(
            element, styles, "stroke", None)
        stroke_text = _paint_value(stroke_raw)
        if not stroke_text or stroke_text.lower() in _NOT_STROKED_VALUES:
            continue

        # An invisible stroke is not a hairline: stroke-opacity:0 lays no ink.
        stroke_opacity, _source, _node = _inherited_value(
            element, styles, "stroke-opacity", "1")
        fraction = _opacity_fraction(stroke_opacity)
        if fraction is not None and fraction <= 0.0:
            continue

        width_raw, width_source, _width_node = _inherited_value(
            element, styles, "stroke-width", None)
        if width_raw is None:
            width_raw, width_source = "1", "default (1px)"
        elif width_source == "initial":
            width_source = "default (1px)"

        counter[0] += 1
        label = _describe(element, counter[0])
        points, error = length_to_pt(width_raw, mm_per_unit)
        if error:
            failures.append((
                RULE_STROKE,
                "%s is stroked but its stroke-width %r cannot be used: %s"
                % (label, width_raw, error),
            ))
        elif points < min_pt:
            thin.append((label, points, width_raw, width_source))

    # A hairline pattern repeated across a sheet produces one finding per path,
    # which buries the message. Report the first few in full, then summarise:
    # the fix is the same for all of them.
    limit = 10
    for label, points, width_raw, width_source in thin[:limit]:
        failures.append((
            RULE_STROKE,
            "%s has stroke-width %spt, below minimum %spt "
            "(stroke-width=%r from %s; %s)"
            % (label, _fmt_num(points), _fmt_num(min_pt), width_raw, width_source,
               scale_desc or "assuming 1 user unit = 1px"),
        ))
    if len(thin) > limit:
        rest = thin[limit:]
        values = sorted({_fmt_num(points) for _l, points, _w, _s in thin})
        failures.append((
            RULE_STROKE,
            "...and %d more stroked element%s below the %spt minimum "
            "(stroke widths in this file: %spt). Fixing all of them is the same "
            "change: raise stroke-width, or convert the hairlines to filled "
            "shapes"
            % (len(rest), "" if len(rest) == 1 else "s", _fmt_num(min_pt),
               ", ".join(values[:8])),
        ))


def _has_scale_transform(element):
    """True if *element* or any ancestor has a transform containing scale()."""
    node = element
    while node is not None:
        tr = node.get("transform")
        if tr and "scale(" in tr.lower():
            return True
        parent = node.getparent()
        if parent is None or not isinstance(parent.tag, str):
            break
        node = parent
    return False


def check_stroke_scale_advisory(root, styles, notes, stats):
    """Emit an advisory when a scale transform is in scope of a stroked element.

    The stroke-width check measures in user units and does not apply
    ``transform="scale(...)"`` on the element or its ancestors.  A
    ``stroke-width="10"`` inside ``<g transform="scale(0.01)">`` is ~0.28pt
    in reality but passes a 1.5pt minimum.  VTracer output only uses
    ``translate`` so the front half is safe, but hand-authored or
    Illustrator SVGs are not.  This advisory makes the gap visible without
    failing the job (the real width cannot be computed without a full
    transform stack).
    """
    referenced = _referenced_ids(root)
    warned = []

    for element, scope, _visibility in _walk_paint_scopes(root, styles,
                                                          referenced):
        if scope != "painted":
            continue
        if _localname(element).lower() not in _SHAPE_ELEMENTS:
            continue
        stroke_raw, _source, _node = _inherited_value(element, styles,
                                                      "stroke", None)
        stroke_text = _paint_value(stroke_raw)
        if not stroke_text or stroke_text.lower() in _NOT_STROKED_VALUES:
            continue
        if _has_scale_transform(element):
            warned.append(element)

    for position, element in enumerate(warned[:10], 1):
        notes.append(
            "%s: %s is stroked but a transform=scale() is in scope; the "
            "stroke-width is measured in user units and the scale is not "
            "applied, so the real stroke may be thinner than reported"
            % (RULE_STROKE_SCALE, _describe(element, position)))
    if len(warned) > 10:
        notes.append(
            "%s: ...and %d more stroked element%s with a scale transform "
            "in scope" % (RULE_STROKE_SCALE, len(warned) - 10,
                          "" if len(warned) - 10 == 1 else "s"))
    stats["stroke_scale_advisories"] = len(warned)


# --------------------------------------------------------------------------
# Rule 4: path geometry
# --------------------------------------------------------------------------

def check_path_geometry(root, allow_open_paths, failures,
                        max_nodes_per_path=None, stats=None):
    """Check path closure, degenerate shapes, and node counts.

    ``max_nodes_per_path`` is a production limit rather than a print-correctness
    one: a traced path with thousands of nodes slows a RIP to a crawl and cannot
    be hand-edited.  It is reported under NODE_COUNT so the pipeline can rank it
    separately from the findings that make a file unprintable at all.
    """
    index = 0
    total_nodes = 0
    most_nodes = 0
    over_limit = []
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        if _localname(element).lower() != "path":
            continue
        index += 1
        label = _describe(element, index)

        data = element.get("d")
        if data is None or not data.strip():
            failures.append((
                RULE_DEGENERATE,
                "%s draws nothing: its 'd' attribute is %s (empty paths are "
                "zero-area by definition)"
                % (label, "missing" if data is None else "blank"),
            ))
            continue

        try:
            path = parse_path(data)
        except Exception as exc:                      # noqa: BLE001 - report all
            failures.append((RULE_PATH_PARSE, "%s has unparseable 'd' data: %s: %s"
                             % (label, type(exc).__name__, exc)))
            continue

        try:
            if not len(path):
                failures.append((RULE_DEGENERATE,
                                 "%s has empty path data (no drawable segments)"
                                 % label))
                continue
        except Exception:
            pass

        node_count = 0
        try:
            node_count = len(path)
        except Exception:
            pass
        total_nodes += node_count
        most_nodes = max(most_nodes, node_count)
        if max_nodes_per_path is not None and node_count > max_nodes_per_path:
            over_limit.append((label, node_count))

        closed = _path_is_closed(path)
        total_length = _safe_length(path)

        if total_length <= EPS_LEN:
            failures.append((
                RULE_DEGENERATE,
                "%s has zero length and zero area (degenerate shape, "
                "d=%r)" % (label, _shorten(data, 48)),
            ))
            continue

        if not allow_open_paths:
            open_subpaths = []
            subpaths = _subpaths(path)
            for sub in subpaths:
                if _safe_length(sub) <= EPS_LEN:
                    continue          # stray moveto stub: draws nothing
                if not _path_is_closed(sub):
                    open_subpaths.append(sub)
            if open_subpaths:
                try:
                    start = open_subpaths[0][0].start
                    where = "starting at (%s, %s)" % (_fmt_num(start.real),
                                                      _fmt_num(start.imag))
                except Exception:
                    where = "open"
                detail = "%s is not closed (%d open subpath%s %s)" % (
                    label, len(open_subpaths),
                    "" if len(open_subpaths) == 1 else "s", where)
                failures.append((RULE_OPEN, detail))

        if closed:
            area = _safe_area(path)
            if area is not None and area <= EPS_AREA:
                failures.append((
                    RULE_DEGENERATE,
                    "%s is closed but encloses zero area (collapsed or "
                    "hairline shape, d=%r)" % (label, _shorten(data, 48)),
                ))

    # Paths are reported worst-first and capped: a traced illustration can put
    # hundreds of paths over the limit and a wall of lines helps nobody.
    if over_limit and max_nodes_per_path is not None:
        over_limit.sort(key=lambda item: -item[1])
        for label, count in over_limit[:5]:
            failures.append((
                RULE_NODES,
                "%s has %d nodes, above the limit of %d (heavily traced paths "
                "slow the RIP and cannot be hand-edited)"
                % (label, count, max_nodes_per_path),
            ))
        if len(over_limit) > 5:
            worst_label, worst_count = over_limit[0]
            failures.append((
                RULE_NODES,
                "...and %d more path%s over the %d-node limit (worst: %s with "
                "%d nodes)"
                % (len(over_limit) - 5,
                   "" if len(over_limit) - 5 == 1 else "s",
                   max_nodes_per_path, worst_label, worst_count),
            ))

    if stats is not None:
        stats["paths"] = index
        stats["path_nodes_total"] = total_nodes
        stats["path_nodes_max"] = most_nodes
        stats["paths_over_node_limit"] = len(over_limit)


# --------------------------------------------------------------------------
# Rules 5 and 6: physical size, and conformance to an agreed palette
# --------------------------------------------------------------------------

def check_dimensions(root, expected, mm_per_unit, failures, stats, tolerance_mm=0.5):
    """Compare the artwork's real physical size with the size that was ordered.

    Prepress rejections are frequently a size mix-up rather than an artwork
    fault, and the mismatch is easy to miss by eye.  Falls back to the viewBox
    extents scaled by ``mm_per_unit`` when width/height are absent.
    """
    actual_w = _length_attr_to_mm(root.get("width"))
    actual_h = _length_attr_to_mm(root.get("height"))

    if actual_w is None or actual_h is None:
        viewbox = root.get("viewBox") or ""
        parts = re.split(r"[\s,]+", viewbox.strip()) if viewbox.strip() else []
        if len(parts) == 4:
            try:
                vb_w, vb_h = float(parts[2]), float(parts[3])
            except ValueError:
                vb_w = vb_h = 0.0
            if vb_w > 0 and vb_h > 0:
                if actual_w is None:
                    actual_w = vb_w * mm_per_unit
                if actual_h is None:
                    actual_h = vb_h * mm_per_unit

    stats["size_mm"] = [None if actual_w is None else round(actual_w, 3),
                        None if actual_h is None else round(actual_h, 3)]
    if actual_w is None or actual_h is None:
        stats["size_check"] = "skipped: the artwork declares no measurable size"
        return

    checked = 0
    for key, actual, axis in (("width_mm", actual_w, "width"),
                              ("height_mm", actual_h, "height")):
        expected_mm = expected.get(key)
        if expected_mm is None:
            continue
        checked += 1
        if abs(actual - expected_mm) > tolerance_mm:
            failures.append((
                RULE_DIMENSIONS,
                "artwork %s is %.2fmm but the spec orders %.2fmm (out by "
                "%+.2fmm, tolerance %.2fmm)"
                % (axis, actual, expected_mm, actual - expected_mm, tolerance_mm),
            ))
    stats["size_check"] = ("%d dimension%s compared" % (checked, "" if checked == 1 else "s")
                           if checked else "skipped: spec names no dimension")


def check_palette(colors, palette, failures, stats):
    """Report declared colours that sit outside the agreed palette.

    Advisory by design (see the pipeline's gate table): an off-palette colour
    may be deliberate, but on a screen-print job it usually means a stray
    picker colour that will silently become an extra ink -- and an extra charge.
    """
    if not palette:
        return
    stats["palette"] = list(palette)
    allowed = set(palette)
    unexpected = sorted(set(colors) - allowed)
    stats["off_palette_colors"] = unexpected
    stats["palette_conformance"] = (
        "all colours on palette" if not unexpected
        else "%d of %d declared colours off palette" % (len(unexpected), len(colors)))
    if unexpected:
        failures.append((
            RULE_PALETTE,
            "%d declared colour%s not in the spec palette: %s (palette: %s)"
            % (len(unexpected), "" if len(unexpected) == 1 else "s",
               ", ".join(unexpected[:10]), ", ".join(palette[:10])),
        ))


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def validate(svg_path, spec_path):
    """Run every rule.  Returns (failures, notes, stats)."""
    failures = []
    notes = []
    stats = {}

    spec = load_spec(spec_path, failures, notes)
    if spec is None:
        return failures, notes, stats

    if not os.path.exists(svg_path):
        failures.append((RULE_INPUT, "SVG file not found: %s" % svg_path))
        return failures, notes, stats

    try:
        parser = etree.XMLParser(resolve_entities=False, no_network=True,
                                 recover=False, huge_tree=True)
        root = etree.parse(svg_path, parser).getroot()
    except etree.XMLSyntaxError as exc:
        line = getattr(getattr(exc, "error_log", None), "last_error", None)
        position = ""
        if line is not None:
            position = " at line %s column %s" % (line.line, line.column)
        failures.append((RULE_SVG_PARSE,
                         "malformed XML in %s%s: %s" % (svg_path, position, exc)))
        return failures, notes, stats
    except OSError as exc:
        failures.append((RULE_SVG_PARSE, "cannot read %s: %s" % (svg_path, exc)))
        return failures, notes, stats
    except Exception as exc:                          # noqa: BLE001
        failures.append((RULE_SVG_PARSE, "cannot parse %s: %s: %s"
                         % (svg_path, type(exc).__name__, exc)))
        return failures, notes, stats

    if _localname(root).lower() != "svg":
        failures.append((RULE_SVG_PARSE,
                         "root element is <%s>, expected <svg>" % _localname(root)))
        return failures, notes, stats

    try:
        styles = StyleContext(root)
    except Exception as exc:                          # noqa: BLE001
        failures.append((RULE_SVG_PARSE, "could not build the style cascade: "
                         "%s: %s" % (type(exc).__name__, exc)))
        return failures, notes, stats

    mm_per_unit, scale_desc, scale_note = compute_document_scale(root)
    stats["mm_per_user_unit"] = round(mm_per_unit, 6)
    stats["scale_basis"] = scale_desc
    if scale_note:
        notes.append(scale_note)

    if not spec["allow_raster_embed"]:
        try:
            check_raster_embeds(root, failures)
        except Exception as exc:                      # noqa: BLE001
            failures.append((RULE_RASTER, "raster check crashed: %s: %s"
                             % (type(exc).__name__, exc)))

    # allow_gradients was parsed and then never read, which let a gradient
    # through the "source-level" gate entirely. It is enforced here, and the
    # counts are recorded either way so the manifest shows what was there.
    try:
        check_gradients(root, styles, spec["allow_gradients"], failures, notes,
                        stats, source=spec["allow_gradients_source"])
    except Exception as exc:                          # noqa: BLE001
        failures.append((RULE_GRADIENT, "gradient check crashed: %s: %s"
                         % (type(exc).__name__, exc)))

    try:
        colour_count, colours = check_color_count(root, styles, spec["max_colors"],
                                                  failures, stats, notes)
        stats["color_count"] = colour_count
        stats["colors"] = sorted(colours)
    except Exception as exc:                          # noqa: BLE001
        failures.append((RULE_COLORS, "colour check crashed: %s: %s"
                         % (type(exc).__name__, exc)))

    try:
        check_stroke_widths(root, styles, spec["min_stroke_width_pt"], failures,
                            mm_per_unit, scale_desc)
    except Exception as exc:                          # noqa: BLE001
        failures.append((RULE_STROKE, "stroke width check crashed: %s: %s"
                         % (type(exc).__name__, exc)))

    try:
        check_stroke_scale_advisory(root, styles, notes, stats)
    except Exception as exc:                          # noqa: BLE001
        notes.append("stroke scale advisory check crashed: %s: %s"
                     % (type(exc).__name__, exc))

    # Advisory observations: notes by contract, so a crash here must never turn
    # into a failure -- the findings they describe are unmeasurable by design.
    for advisory in (check_translucency, check_effects, check_definition_coverage):
        try:
            advisory(root, styles, notes, stats)
        except Exception as exc:                      # noqa: BLE001
            notes.append("%s advisory check crashed: %s: %s"
                         % (advisory.__name__, type(exc).__name__, exc))

    try:
        check_path_geometry(root, spec["allow_open_paths"], failures,
                            spec["max_nodes_per_path"], stats)
    except Exception as exc:                          # noqa: BLE001
        failures.append((RULE_OPEN, "geometry check crashed: %s: %s"
                        % (type(exc).__name__, exc)))

    try:
        check_palette(stats.get("colors") or [], spec["palette"], failures, stats)
    except Exception as exc:                          # noqa: BLE001
        failures.append((RULE_PALETTE, "palette check crashed: %s: %s"
                         % (type(exc).__name__, exc)))

    if spec["dimensions"]:
        try:
            check_dimensions(root, spec["dimensions"], mm_per_unit, failures, stats)
        except Exception as exc:                      # noqa: BLE001
            failures.append((RULE_DIMENSIONS, "size check crashed: %s: %s"
                             % (type(exc).__name__, exc)))
    else:
        stats["size_check"] = "skipped: spec has no dimensions block"

    stats["allow_open_paths"] = spec["allow_open_paths"]
    stats["open_paths_source"] = spec["open_paths_source"]
    stats["allow_gradients"] = spec["allow_gradients"]
    stats["print_method"] = spec["print_method"]
    stats["failures"] = len(failures)
    return failures, notes, stats


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv and argv[0] in ("-h", "--help"):
        print(_cli.__doc__.strip())
        return 0
    if len(argv) != 2:
        print("usage: python3 %s <file.svg> <spec.json>"
              % os.path.basename(sys.argv[0] or "validate_svg.py"))
        return 2

    svg_path, spec_path = argv
    try:
        failures, notes, stats = validate(svg_path, spec_path)
    except Exception as exc:                          # noqa: BLE001 - last resort
        failures = [(RULE_SVG_PARSE, "internal error: %s: %s"
                     % (type(exc).__name__, exc))]
        notes = []

    for note in notes:
        print("NOTE: %s" % note)

    if failures:
        print("VALIDATION FAILED for %s:" % svg_path)
        for rule, detail in failures:
            print(" - [%s]: %s" % (rule, detail))
        return 1

    print("VALIDATION PASSED for %s" % svg_path)
    return 0


def _cli():
    """Usage example
    -------------
    ::

        $ cat spec.json
        {
          "max_colors": 6,
          "geometry": {
            "allow_raster_embed": false,
            "allow_open_paths": false,
            "min_stroke_width_pt": 1.5
          }
        }

        $ python3 validate_svg.py logo.svg spec.json
        VALIDATION FAILED for logo.svg:
         - [RASTER_EMBED]: Found 1 embedded raster image but spec requires vector-only artwork (allow_raster_embed=false); Image #0 (no id) -> data:image/png;base64,iVBOR...
         - [GRADIENT_NOT_ALLOWED]: 1 gradient definition found but geometry.allow_gradients is false (geometry.allow_gradients): this job is flat spot colour, so a gradient cannot be reproduced. Offenders: LinearGradient id='g' (id='g')
         - [COLOR_COUNT]: Color count 8 exceeds max 6 (used: #000000, #00ff00, #1a1a1a, #333333, #4d4d4d, #ffffff, #ff0000, silver)
         - [MIN_STROKE_WIDTH]: Path id='rule' has stroke-width 0.5pt, below minimum 1.5pt (stroke-width='0.5' from attr)
         - [OPEN_PATH]: Path id='outline' is not closed (1 open subpath starting at (10, 10))
         - [ZERO_AREA_PATH]: Path id='sliver' is closed but encloses zero area (collapsed or hairline shape, d='M10,10 L20,20 Z')
        $ echo $?
        1

        $ python3 validate_svg.py clean.svg spec.json
        VALIDATION PASSED for clean.svg
        $ echo $?
        0

    Exit codes: 0 = passed, 1 = failed (reasons on stdout), 2 = bad usage.
    """
    return None


if __name__ == "__main__":
    sys.exit(main())