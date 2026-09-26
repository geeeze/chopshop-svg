#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
preflight.py -- one-command print preflight for an SVG.

Wraps validate_svg.py (declared-property checks) with the three things a
printer will actually reject a file for, which no amount of reading the SVG
source can tell you:

  1. How many inks the artwork needs WHEN RENDERED.  A two-stop gradient is
     declared as 2 colours but renders as hundreds, because the gradient
     interpolates across every value in between.  Screen printing, foiling,
     vinyl cutting and most spot-colour work cannot reproduce that.
  2. Total ink coverage (TAC) -- the sum of the C, M, Y and K plates at the
     darkest point.  Above the press limit the ink never dries and sheets
     set off onto each other.
  3. Whether fonts are embedded and whether placed bitmaps have enough
     resolution.  A 300-dpi image scaled up to fill an A3 sheet ends up at
     ~50 dpi and prints visibly soft.

Outputs (into --workdir, default '<svg dir>/preflight/')
  <name>.proof.png    raster proof, for eyeballing and for clients
  <name>.print.pdf    print PDF exported by Inkscape
  <name>.report.json  machine-readable report with --json

Usage
-----
::

    python3 preflight.py artwork.svg spec.json
    python3 preflight.py artwork.svg spec.json --dpi 300 --json

    # exit 0 = ready to send, exit 1 = reasons printed, exit 2 = bad usage

Spec keys consumed (all optional except the ones validate_svg.py needs)
---------------------------------------------------------------------
::

    {
      "max_colors": 6,                  # declared-colour budget AND the
                                        # rendered-ink budget
      "geometry": {
        "allow_raster_embed": false,
        "allow_open_paths": false,
        "min_stroke_width_pt": 1.5
      },
      "print": {                        # everything here is optional
        "dpi": 300,                     # proof / measurement resolution
        "ink_area_threshold_percent": 0.05,  # area a colour must cover to
                                             # count as an ink (kills
                                             # antialias fringe noise)
        "count_white_as_ink": false,    # paper white is not an ink
        "tac_limit_percent": 300,       # press limit for total area coverage
        "min_image_ppi": 300,           # effective resolution floor
        "require_embedded_fonts": true,
        "require_cmyk": false           # fail if the PDF is not CMYK
      }
    }

Which renderer is authoritative
-------------------------------
Inkscape 1.x renders the proof and exports the PDF.  It is the renderer the
designer sees, so what it produces is what the printer gets.  (librsvg /
rsvg-convert is faster but has documented fidelity gaps around filters and
text; it is not used here so there is exactly one answer to "what does this
look like".)

White is not always "no ink"
---------------------------
Ink count depends on the GARMENT, not only the artwork. No ink is no ink only
when the fabric is already white. On a dark garment, white is usually the first
ink down -- the underbase -- so the same file needs one more screen. Set
``print.dark_garment_underbase`` for dark shirts; the tool assumes light fabric
otherwise and warns whenever white is in the artwork but uncounted.

Honest limits
-------------
* TAC is ESTIMATED.  Ghostscript's RGB->CMYK conversion does not apply
  press-grade GCR/UCR: it builds a 50% grey from 145% of C+M+Y+K instead of
  roughly 50% K.  Treat the number as a relative warning, not a lab reading.
  If your printer needs real TAC figures, the artwork must genuinely be CMYK,
  converted once, with their profile -- not converted on the fly.
* The rendered colour count includes a colour only if it covers at least
  'ink_area_threshold_percent' of the canvas, so antialiased edges do not
  inflate it.  The raw distinct-value count is reported too, so nothing is
  hidden.
* Overlapping semi-transparent shapes produce blended colours that count as
  inks; multiply/overprint effects can only be judged by the printer.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone

import numpy as np
from PIL import Image

# validate_svg.py sits next to this file; it owns the declared-property rules.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate_svg  # noqa: E402

Image.MAX_IMAGE_PIXELS = None

# Rule names for the checks this script owns.
RULE_RENDERED_COLORS = "COLOR_COUNT_RENDERED"
RULE_CONTINUOUS_TONE = "CONTINUOUS_TONE"
RULE_INK = "INK_COVERAGE"
RULE_IMAGE_RES = "IMAGE_RESOLUTION"
RULE_FONT = "FONT_EMBED"
RULE_CMYK = "CMYK_REQUIRED"
RULE_RENDER = "RENDER_ERROR"
RULE_PDF = "PDF_ERROR"
RULE_TOOL = "MISSING_TOOL"

# Which layer of the pipeline produced a finding (v4.0 section 2).
LAYER_A = "source_validation"       # validate_svg.py, runs on the source
LAYER_B = "render_preflight"        # this module, runs on rendered output
LAYER_A_RULES = frozenset({
    validate_svg.RULE_RASTER, validate_svg.RULE_COLORS,
    validate_svg.RULE_STROKE, validate_svg.RULE_DEGENERATE,
    validate_svg.RULE_OPEN, validate_svg.RULE_SVG_PARSE,
    validate_svg.RULE_PATH_PARSE, validate_svg.RULE_NODES,
    validate_svg.RULE_PALETTE, validate_svg.RULE_DIMENSIONS,
    # Cross-layer: either side can raise these, attributed to A as the origin.
    validate_svg.RULE_SPEC, validate_svg.RULE_INPUT,
})

# Severities (v4.0 section 5).  Only HARD findings fail the job.
HARD = "hard"
ADVISORY = "advisory"

# Explicit types, so a bad value is reported rather than silently coerced.
_POPTION_TYPES = {
    "dpi": int,
    "ink_area_threshold_percent": float,
    "ink_merge_tolerance": float,
    "ink_blend_tolerance": float,
    "tone_min_area_percent": float,
    "tone_min_colors": int,
    "ramp_min_members": int,
    "allow_gradients": bool,
    "count_white_as_ink": bool,
    "dark_garment_underbase": bool,
    "tac_limit_percent": float,
    "min_image_ppi": float,
    "require_embedded_fonts": bool,
    "require_cmyk": bool,
    "print_method": str,
    "icc_profile_path": str,
    "gradient_handling": str,
    "tac_exact": bool,
}

DEFAULT_PRINT_OPTIONS = {
    "dpi": 300,
    # v4.0 top-level spec keys, resolved into the same namespace.
    "print_method": "",
    "icc_profile_path": None,
    "gradient_handling": "",
    # A reading is only exact once the input is genuinely CMYK; set at runtime.
    "tac_exact": False,
    # A colour must cover this share of the sheet before it counts as an ink.
    # Keeps antialiasing fringe out of the count.
    "ink_area_threshold_percent": 0.05,
    # Colours closer than this (Euclidean in sRGB, max 441) are treated as the
    # same ink.  ~20 merges rasterisation noise without merging deliberate
    # near-neighbour inks such as #1a1a1a against #2a2a2a.
    "ink_merge_tolerance": 20.0,
    # How far from the colour-space segment a boundary blend may sit and still
    # be recognised. Not zero, because a renderer may composite in a different
    # space from the one the check measures in: a measured antialiasing band
    # along a bar edge sat 12.25 units off the sRGB segment. This can be
    # generous because shape, not geometry, is what finally discriminates --
    # see _has_interior().
    "ink_blend_tolerance": 24.0,
    # A merged ink built from at least this many distinct colours is a smooth
    # transition, i.e. a gradient or a photograph.
    "ramp_min_members": 6,
    # Diffuse-tone detection: colours that are individually too small to count
    # as inks can still collectively cover most of the sheet -- that is a
    # photograph.  Calibrated from the batch in 00_source/ (see the docstring
    # of _detect_diffuse_tone for the measured numbers).
    "tone_min_area_percent": 3.0,
    "tone_min_colors": 2000,
    "allow_gradients": False,
    "count_white_as_ink": False,
    # Dark-garment printing lays a white underbase down first, so white is a
    # physical screen rather than paper. Setting this implies
    # count_white_as_ink (see the derivation in load_print_options) and adds a
    # caveat to the ink-coverage estimate, which is measured on paper.
    "dark_garment_underbase": False,
    "tac_limit_percent": 300.0,
    "min_image_ppi": 300.0,
    # v4.0 section 5 lists font substitution as advisory, so it does not fail
    # the job unless the spec asks it to.
    "require_embedded_fonts": False,
    "require_cmyk": False,
}

# Press reference profiles that ship with Debian's colord, best-known first.
CMYK_PROFILE_CANDIDATES = (
    "/usr/share/color/icc/colord/SWOP_TR003_coated_3.icc",
    "/usr/share/color/icc/colord/FOGRA45L_lwc.icc",
    "/usr/share/color/icc/colord/FOGRA29L_uncoated.icc",
    "/usr/share/color/icc/ghostscript/default_cmyk.icc",
)

# An antialiasing blend is an edge effect: it hugs the boundary between two
# fills, so it is thin. Shape is what tells it apart from a deliberate colour,
# not size -- see _has_interior(). This cap is only a final net, because a thin
# ribbon that somehow covers 5% of a sheet is not an edge artefact.
BLEND_MAX_SHARE_PERCENT = 5.0

# Cap the proof so a 300-dpi A0 sheet cannot exhaust memory (~180 MB at 1 byte/px).
MAX_PROOF_PIXELS = 45_000_000

# v4.0 section 5: which findings fail the job, and which are reported only.
_ALWAYS_HARD = frozenset({
    validate_svg.RULE_RASTER,       # raster where vectors are required
    validate_svg.RULE_COLORS,       # over the declared colour budget
    validate_svg.RULE_STROKE,       # hairline that will break up on press
    validate_svg.RULE_DEGENERATE,   # zero-area shape
    validate_svg.RULE_OPEN,         # open path in a cutting job
    validate_svg.RULE_SVG_PARSE,    # malformed SVG
    validate_svg.RULE_PATH_PARSE,   # unparseable path data
    validate_svg.RULE_SPEC,         # the spec itself is wrong
    validate_svg.RULE_INPUT,        # file missing
    validate_svg.RULE_DIMENSIONS,   # artwork is the wrong size
    RULE_RENDERED_COLORS,           # a gradient on a spot-colour job
    RULE_IMAGE_RES,                 # bitmap placed too coarsely
    RULE_CMYK,                      # the spec demanded CMYK
    RULE_RENDER, RULE_PDF, RULE_TOOL,
})

_ALWAYS_ADVISORY = frozenset({
    validate_svg.RULE_NODES,        # production quality, not printability
    validate_svg.RULE_PALETTE,      # a stray colour may be deliberate
})


def layer_of(rule):
    """Which pipeline layer does this rule belong to?"""
    return LAYER_A if rule in LAYER_A_RULES else LAYER_B


def classify(rule, options):
    """Return HARD or ADVISORY for one rule under the current spec.

    Three rules are conditional, and the conditions are the whole point:

    * CONTINUOUS_TONE is advisory only when the plan is to embed a raster
      (``gradient_handling: embedded_raster``), where smooth tone is expected.
      Otherwise a gradient on a spot-colour job is unprintable, so it is hard.
    * INK_COVERAGE is hard only when the reading is exact, i.e. the input was
      already CMYK.  An RGB-derived number cannot validate a 300% limit, so
      failing a job on it would be dishonest.
    * FONT_EMBED follows ``require_embedded_fonts``; v4.0 lists font
      substitution as advisory by default.
    """
    if rule == RULE_CONTINUOUS_TONE:
        return (ADVISORY if options.get("gradient_handling") == "embedded_raster"
                else HARD)
    if rule == RULE_INK:
        return HARD if options.get("tac_exact") else ADVISORY
    if rule == RULE_FONT:
        return HARD if options.get("require_embedded_fonts") else ADVISORY
    if rule in _ALWAYS_ADVISORY:
        return ADVISORY
    if rule in _ALWAYS_HARD:
        return HARD
    return HARD       # an unknown rule fails safe


def classify_findings(failures, options):
    """Split (rule, detail) findings into hard gates and advisories."""
    hard, advisory = [], []
    for rule, detail in failures:
        entry = {"layer": layer_of(rule), "rule": rule, "detail": detail,
                 "severity": classify(rule, options)}
        (hard if entry["severity"] == HARD else advisory).append(entry)
    return hard, advisory


def tool_versions():
    """Record the toolchain, so a manifest can be trusted later."""
    versions = {}
    for name, argv in (("inkscape", ["inkscape", "--version"]),
                       ("ghostscript", ["gs", "--version"]),
                       ("qpdf", ["qpdf", "--version"])):
        if shutil.which(argv[0]) is None:
            versions[name] = "not installed"
            continue
        try:
            _, out, err = run_tool(argv, check=False, timeout=30)
            first = (out or err).strip().splitlines()
            versions[name] = first[0][:80] if first else "unknown"
        except ToolError as exc:
            versions[name] = "unavailable (%s)" % exc
    return versions

TOOL_TIMEOUT = 600


# --------------------------------------------------------------------------
# Subprocess plumbing
# --------------------------------------------------------------------------

class ToolError(Exception):
    """An external tool failed or is missing."""


def run_tool(argv, timeout=TOOL_TIMEOUT, check=True):
    """Run a command, returning (returncode, stdout, stderr).

    Raises ToolError only when check=True and the command fails, so callers
    can decide whether a failure is fatal or merely a skipped check.
    """
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError as exc:
        raise ToolError("'%s' is not installed (%s)" % (argv[0], exc))
    except subprocess.TimeoutExpired:
        raise ToolError("'%s' timed out after %ds" % (argv[0], timeout))
    if check and proc.returncode != 0:
        raise ToolError("'%s' exited %d: %s"
                        % (argv[0], proc.returncode,
                           (proc.stderr or proc.stdout).strip()[:400]))
    return proc.returncode, proc.stdout, proc.stderr


def require_tools(names):
    """Return a list of missing tool names."""
    return [name for name in names if shutil.which(name) is None]


# --------------------------------------------------------------------------
# Spec
# --------------------------------------------------------------------------

def _coerce_option(key, value, where, options, failures):
    """Coerce one option to its declared type, reporting rather than guessing."""
    kind = _POPTION_TYPES.get(key)
    if kind is None:
        options[key] = value
        return True
    if kind is bool:
        options[key] = bool(value)
        return True
    if value is None:
        return True
    try:
        options[key] = kind(value)
        return True
    except (TypeError, ValueError):
        failures.append((validate_svg.RULE_SPEC,
                         "%s.%s must be %s, got %r"
                         % (where, key, kind.__name__, value)))
        return False


def load_print_options(spec_path, notes, failures):
    """Read print options from the v4.0 top-level keys and the 'print' block.

    v4.0 moved ``require_cmyk``, ``ink_limit_percent`` and ``icc_profile_path``
    to the top level of spec.json, and reads ``gradient_handling`` from
    ``geometry``.  The ``print`` block from earlier versions is still honoured
    as the detailed override: where both name the same setting the ``print``
    block wins, because it is the more specific block, and that is noted.
    """
    options = dict(DEFAULT_PRINT_OPTIONS)
    try:
        with open(spec_path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception:
        return options, None       # validate_svg reports the spec problem

    if not isinstance(raw, dict):
        return options, None

    # --- v4.0 top-level keys ---------------------------------------------
    top_keys = {
        "require_cmyk": "require_cmyk",
        "ink_limit_percent": "tac_limit_percent",
        "icc_profile_path": "icc_profile_path",
        "print_method": "print_method",
        "require_embedded_fonts": "require_embedded_fonts",
    }
    from_top = set()
    explicit = set()          # keys the user actually set, so a derived value
                              # never silently overrides a deliberate one
    for key, target in top_keys.items():
        if key not in raw or raw[key] is None:
            continue
        if _coerce_option(target, raw[key], "spec", options, failures):
            from_top.add(target)
            explicit.add(target)

    geometry = raw.get("geometry")
    if isinstance(geometry, dict):
        if geometry.get("gradient_handling") is not None:
            options["gradient_handling"] = str(geometry["gradient_handling"]).lower()
        if geometry.get("allow_gradients") is not None:
            options["allow_gradients"] = bool(geometry["allow_gradients"])

    block = raw.get("print")
    if block is None:
        if not from_top:
            notes.append("spec has no 'print' section; using defaults "
                         "(dpi=300, tac_limit=300%, min_image_ppi=300)")
        return options, raw
    if not isinstance(block, dict):
        failures.append((validate_svg.RULE_SPEC,
                         "'print' must be a JSON object, got %s"
                         % type(block).__name__))
        return options, raw

    for key, value in block.items():
        if key not in DEFAULT_PRINT_OPTIONS:
            notes.append("print.%s is not a known setting and was ignored" % key)
            continue
        if not _coerce_option(key, value, "print", options, failures):
            continue
        explicit.add(key)
        if key in from_top and str(raw.get(key)) != str(value):
            notes.append("both spec.%s and print.%s are set and differ; the "
                         "'print' block wins" % (key, key))

    # --- dark-garment underbase ------------------------------------------
    # A white underbase is printed first and sits beneath every other colour, so
    # on a dark garment white is a screen, not paper. Accepting the flag without
    # acting on it would leave the ink count one short on exactly the jobs where
    # that matters most.
    if options["dark_garment_underbase"]:
        if "count_white_as_ink" not in explicit:
            options["count_white_as_ink"] = True
            notes.append("dark_garment_underbase is set, so white is treated as "
                         "an ink (the underbase screen); set "
                         "print.count_white_as_ink explicitly to override")
        elif not options["count_white_as_ink"]:
            # Explicit beats derived, as everywhere else -- but this pair is
            # contradictory and the failure direction is an undercount, i.e. a
            # job that passes with one screen unaccounted for.
            notes.append("CONTRADICTION: dark_garment_underbase is true but "
                         "print.count_white_as_ink is explicitly false, so white "
                         "will NOT be counted. That undercounts the screens by "
                         "one on a dark garment")

    if options["icc_profile_path"] and not os.path.exists(options["icc_profile_path"]):
        notes.append("icc_profile_path %r does not exist; falling back to a "
                     "bundled press profile" % options["icc_profile_path"])
        options["icc_profile_path"] = None

    # --- fabric / substrate -------------------------------------------------
    # Which declared colour is the garment rather than a screen. Resolved here,
    # once, so the tracer, the manifest, the proof and Jev all read the same
    # answer -- and so a typo is reported rather than silently meaning "paper".
    options["palette"] = [str(c) for c in (raw.get("palette") or [])]
    options["substrate"] = None
    explicit_sub = (raw.get("print") or {}).get("substrate")
    idx_sub = (raw.get("print") or {}).get("substrate_index")
    try:
        if explicit_sub:
            value = str(explicit_sub).strip().upper()
            if not (value.startswith("#") and _hex_to_rgb(value)):
                if value not in [p.upper() for p in options["palette"]]:
                    raise ValueError(
                        "print.substrate %r is neither a #rrggbb colour nor a "
                        "member of spec.palette" % explicit_sub)
            options["substrate"] = value
        elif idx_sub is not None:
            idx = int(idx_sub)
            if not 0 <= idx < len(options["palette"]):
                raise ValueError(
                    "print.substrate_index %d is out of range for a %d-colour "
                    "palette" % (idx, len(options["palette"])))
            options["substrate"] = options["palette"][idx].upper()
    except (TypeError, ValueError) as exc:
        options["substrate"] = None
        notes.append("SUBSTRATE: %s -- ignoring it, so every declared colour "
                     "is treated as a screen" % exc)

    if options["substrate"]:
        notes.append("substrate %s is the fabric: it is not counted as an ink "
                     "and is excluded from the screen list"
                     % options["substrate"])
    elif (raw.get("print") or {}).get("dark_garment_underbase"):
        notes.append("no print.substrate is declared, so no colour is treated "
                     "as fabric. On a dark garment name the fabric colour "
                     "explicitly, or a traced background becomes an ink")
    return options, raw


def _hex_to_rgb(value):
    """'#rrggbb' -> (r, g, b), or None if malformed. Mirrors front_common."""
    text = (value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        return None
    try:
        return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def _has_interior(packed2d, value):
    """Does this colour form a filled region, rather than an edge?

    The discriminator that finally worked. Area alone cannot separate a thin
    antialiasing ribbon from a deliberate flat colour: a solid 1.2%-of-sheet
    grey bar was absorbed as a "blend" because neutral grey lies on the line
    between any number of colour pairs, and it was smaller than 25% of the ink
    it bordered. Geometry is equally ambiguous -- a 10% tint sits on the same
    line as a blend.

    Shape is not ambiguous. An antialiasing ribbon is 1-2 px wide, so eroding by
    one pixel annihilates it; a flat colour has interior and survives. Measured
    behaviour: thin 0.85pt rules have gritty edges but no interior, while the
    solid bar keeps thousands of interior pixels.
    """
    mask = packed2d == value
    if mask.shape[0] < 3 or mask.shape[1] < 3:
        return False
    inner = mask[1:-1, 1:-1]
    # 4-connected erosion: a pixel survives only if it and its neighbours match.
    return bool(np.any(inner & mask[:-2, 1:-1] & mask[2:, 1:-1]
                       & mask[1:-1, :-2] & mask[1:-1, 2:]))


def _blend_target(rgb, anchors, blend_tolerance):
    """Find the ink a stray colour is an antialiasing blend into, if any.

    A pixel on the boundary between two fills is a linear mix of their colours,
    so it lands on the segment joining the two in colour space.  Measured on the
    case that exposed this: a cream/green boundary blend sat 0.22 units off the
    segment (sRGB), i.e. essentially exactly on it.

    Without this, a 40mm four-colour document reported **seven** inks, because
    each boundary blend covered more than the area threshold -- a false
    rejection of printable artwork.  The threshold alone does not fix it,
    because how much of a sheet a boundary covers depends on the artwork's size.

    ``anchors`` is a list of (rgb, cluster_or_None); a None cluster means paper
    (white), which blends are absorbed *through* but never *into*.  Only used
    for single-colour clusters -- colours that already merged into a group are
    a ramp, not a boundary, and absorbing those would erase gradient detection.

    Returns ``(owner, other_endpoint, gap)`` or None.  Geometry alone is not
    enough to decide: a deliberate 10% tint lies on the same line as a blend,
    so the caller also applies the size test in BLEND_MAX_RATIO.

    Vectorised with numpy: the O(n²) pairwise projection loop is computed in
    one broadcast instead of nested Python iteration.  At the 500-cluster cap
    this replaces ~125k pure-Python inner iterations per stray colour with a
    handful of vectorised array ops.
    """
    n = len(anchors)
    if n < 2:
        return None

    pts = np.array([a[0] for a in anchors], dtype=np.float64)
    clusters = [a[1] for a in anchors]
    q = np.asarray(rgb, dtype=np.float64)

    # Pairwise AB vectors (pts[j] - pts[i]) and squared lengths.
    AB = pts[None, :, :] - pts[:, None, :]          # n×n×3
    AB_len2 = (AB ** 2).sum(axis=2)                   # n×n

    # Projection parameter t[i,j] = dot(q-A_i, A_j-A_i) / |A_j-A_i|^2.
    AC = q - pts                                      # n×3
    safe_len2 = np.where(AB_len2 > 0, AB_len2, 1.0)
    t = np.einsum('ik,ijk->ij', AC, AB) / safe_len2   # n×n

    # Valid: 0 < t < 1, separation > 2*tol, at least one anchor is ink (not
    # both paper).
    min_sep2 = (2.0 * blend_tolerance) ** 2
    is_ink = np.array([c is not None for c in clusters])
    has_ink = is_ink[:, None] | is_ink[None, :]
    valid = (t > 0.0) & (t < 1.0) & (AB_len2 > min_sep2) & has_ink

    if not np.any(valid):
        return None

    # Perpendicular gap = |q - projected| for valid pairs.
    projected = pts[:, None, :] + t[:, :, None] * AB  # n×n×3
    gap = np.sqrt(((q - projected) ** 2).sum(axis=2))
    gap_masked = np.where(valid & (gap <= blend_tolerance), gap, np.inf)

    if not np.any(np.isfinite(gap_masked)):
        return None

    # Minimum gap (first occurrence in row-major order, matching the original
    # i<j iteration's "first wins on tie" semantics).
    flat = int(np.argmin(gap_masked))
    i, j = divmod(flat, n)
    best_gap = float(gap_masked[i, j])

    a_cluster, b_cluster = clusters[i], clusters[j]
    if a_cluster is None:
        chosen, other = b_cluster, None
    elif b_cluster is None:
        chosen, other = a_cluster, None
    elif a_cluster["share"] >= b_cluster["share"]:
        chosen, other = a_cluster, b_cluster
    else:
        chosen, other = b_cluster, a_cluster

    if chosen is not None:
        return (chosen, other, best_gap)
    return None


# --------------------------------------------------------------------------
# Step 1: proof render (Inkscape)
# --------------------------------------------------------------------------

def render_proof(svg_path, png_path, dpi, failures):
    """Rasterise with Inkscape onto white.  Returns pixel dimensions or None."""
    argv = [
        "inkscape", svg_path,
        "--export-type=png",
        "--export-filename=%s" % png_path,
        "--export-dpi=%s" % dpi,
        "--export-background=#ffffff",
        "--export-background-opacity=255",
    ]
    try:
        run_tool(argv)
    except ToolError as exc:
        failures.append((RULE_RENDER, "could not rasterise the artwork: %s" % exc))
        return None
    if not os.path.exists(png_path):
        failures.append((RULE_RENDER,
                         "Inkscape reported success but wrote no proof PNG"))
        return None
    with Image.open(png_path) as image:
        return image.size


def rasterise_pdf(pdf_path, png_path, dpi, failures):
    """Rasterise a PDF with Ghostscript (used when the input is already a PDF)."""
    argv = [
        "gs", "-dNOPAUSE", "-dBATCH", "-dSAFER",
        "-sDEVICE=png16m",
        "-r%d" % dpi,
        "-dTextAlphaBits=4", "-dGraphicsAlphaBits=4",
        "-dUseCropBox",
        "-sOutputFile=%s" % png_path,
        pdf_path,
    ]
    try:
        run_tool(argv)
    except ToolError as exc:
        failures.append((RULE_RENDER, "could not rasterise the PDF: %s" % exc))
        return None
    if not os.path.exists(png_path):
        failures.append((RULE_RENDER, "Ghostscript wrote no proof PNG"))
        return None
    with Image.open(png_path) as image:
        return image.size


# --------------------------------------------------------------------------
# Step 2: rendered ink count
# --------------------------------------------------------------------------

def analyse_rendered_colours(png_path, options, max_colors, failures, stats):
    """Work out how many inks the rendered artwork really needs.

    Two separate questions, because they fail differently:

    * **Flat inks** -- colours merged within a perceptual tolerance, so
      antialiasing fringe (colours a few values off a neighbouring fill) does
      not inflate the count.  This is the number to compare with a spot-colour
      or screen-print budget.
    * **Continuous tone** -- no ink budget makes smooth tone printable as
      flats: it needs halftone screening, i.e. process printing.  Two detectors,
      because tone appears at two different scales, and the measured batch in
      00_source/ showed each one missing what the other caught:

      *Ramp*: one merged ink assembled from many near-neighbour colours, which
      is a gradient.  *Diffuse*: a photograph has no single band large enough to
      see, so every one of its colours falls below the per-colour area
      threshold -- 726,249 distinct colours covering 44.4% of the sheet, each
      individually too small to count.  Judging tone by per-colour area alone
      reported that photograph as **one ink**, passing it.  What separates a
      photograph from a flat design is the *collective* area of the
      sub-threshold colours: measured 44.4% for the photograph against 0.00 to
      0.20% for every flat file in the batch.  See ``tone_min_area_percent``
      and ``tone_min_colors``.

    Distances are Euclidean in sRGB, which is a rough perceptual proxy -- good
    enough to separate deliberate ink choices from rasterisation noise, not a
    substitute for a colour-managed proof.
    """
    with Image.open(png_path) as image:
        arr2d = np.asarray(image.convert("RGB"), dtype=np.uint8)

    rgb = arr2d.reshape(-1, 3)
    total_px = rgb.shape[0]
    # Kept two-dimensional as well: the blend test needs the neighbourhood of a
    # pixel to tell an edge ribbon from a solid region.
    packed2d = ((arr2d[:, :, 0].astype(np.uint32) << 16)
                | (arr2d[:, :, 1].astype(np.uint32) << 8)
                | arr2d[:, :, 2].astype(np.uint32))
    packed = packed2d.reshape(-1)
    values, counts = np.unique(packed, return_counts=True)
    shares = counts.astype(np.float64) / float(total_px) * 100.0

    def unpack(value):
        value = int(value)
        return ((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)

    def hexify(value):
        return "#%06x" % int(value)

    white = 0xFFFFFF
    count_white = options["count_white_as_ink"]
    # White in the render is ambiguous: it can be artwork, or it can be the
    # transparent background of the proof, which is rendered onto white paper.
    # Only the declared colours can tell them apart, so Layer A supplies that
    # fact (see preflight()). Treating background white as an artwork colour
    # invented an ink on every transparent-background file in the batch.
    white_in_render = bool((packed == white).any())
    white_in_artwork = bool(options.get("white_declared_in_artwork",
                                        white_in_render))
    white_is_ink = bool(count_white and white_in_artwork)
    declared_white = white_in_render        # kept for the reporting below
    threshold = float(options["ink_area_threshold_percent"])
    tolerance = float(options["ink_merge_tolerance"])

    significant = [(int(v), float(s)) for v, s in zip(values, shares)
                   if s >= threshold and (count_white or int(v) != white)]
    significant.sort(key=lambda item: -item[1])
    distinct_total = int(values.size)
    distinct_significant = len(significant)

    # The complement of the above: colours that are real ink but too small
    # individually to count.  Flat artwork puts only antialiasing fringe here;
    # a photograph puts *everything* here, which is why the collective area
    # below (not the per-colour area) is the signal.
    if count_white:
        diffuse_mask = shares < threshold
    else:
        diffuse_mask = (shares < threshold) & (values != white)
    diffuse_colors = int(diffuse_mask.sum())
    diffuse_area = float(shares[diffuse_mask].sum())

    clustered_note = None
    if distinct_significant > 8000:
        # Safety valve for photographs: aggregate to 6 bits/channel before the
        # greedy merge, which is O(n x clusters).
        buckets = {}
        for value, share in significant:
            r, g, b = unpack(value)
            key = ((r >> 2) << 12) | ((g >> 2) << 6) | (b >> 2)
            entry = buckets.get(key)
            if entry is None:
                buckets[key] = [value, share, 1]
            else:
                entry[1] += share
                entry[2] += 1
        significant = [tuple(entry) for entry in buckets.values()]
        significant.sort(key=lambda item: -item[1])
        clustered_note = ("clustering was pre-quantised to 6 bits per channel "
                          "because %d significant colours is too many to "
                          "compare pairwise" % distinct_significant)
    else:
        significant = [(v, s, 1) for v, s in significant]

    clusters = []
    for value, share, members in significant:
        r, g, b = unpack(value)
        placed = False
        for cluster in clusters:
            cr, cg, cb = cluster["rep_rgb"]
            if ((r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2) <= tolerance ** 2:
                cluster["share"] += share
                cluster["members"] += members
                placed = True
                break
        if not placed:
            clusters.append({"rep_rgb": (r, g, b), "rep": value,
                             "share": share, "members": members,
                             "absorbed": 0})

    # --- fold boundary blends back into the inks they sit between ----------
    blend_tolerance = float(options["ink_blend_tolerance"])
    absorbed = 0
    blend_skipped = None
    if blend_tolerance > 0 and clusters:
        if len(clusters) > 500:
            blend_skipped = ("%d colour groups is too many to test for blend "
                             "artefacts, so antialiasing blends were left in "
                             "the count" % len(clusters))
        else:
            anchors = [(c["rep_rgb"], c) for c in clusters]
            if not count_white:
                anchors.append(((255, 255, 255), None))     # blends to paper
            kept = []
            for cluster in clusters:
                # Cheapest tests first: the shape test rebuilds a full-image
                # mask, so an oversized or already-absorbed cluster must be
                # dismissed before it runs.
                if cluster["absorbed"] > 0:
                    kept.append(cluster)
                    continue
                if cluster["share"] > BLEND_MAX_SHARE_PERCENT:
                    kept.append(cluster)          # too big to be an edge
                    continue

                found = _blend_target(cluster["rep_rgb"], anchors, blend_tolerance)
                if found is None:
                    kept.append(cluster)
                    continue
                owner, _other, _gap = found
                # Shape decides, not area and not member count. Both earlier
                # heuristics failed on the batch in 00_source/:
                #   * a size ratio against a *neighbouring* colour let a chain of
                #     antialiasing greys veto its own links, so 18 thin rules
                #     reported 4 inks;
                #   * exempting clusters with members > 1 let two adjacent greys
                #     that merged under the tolerance count as a "ramp", so the
                #     same rules reported 2.
                # A gradient band or a flat fill is a filled region and has
                # interior, so it is protected without needing either heuristic.
                if (owner is not cluster
                        and not _has_interior(packed2d, cluster["rep"])):
                    owner["share"] += cluster["share"]
                    owner["absorbed"] += 1
                    absorbed += 1
                    continue
                kept.append(cluster)
            clusters = kept

    inks = [c for c in clusters if c["share"] >= threshold]
    inks.sort(key=lambda c: -c["share"])

    ramp_floor = int(options["ramp_min_members"])
    ramps = [c for c in inks if c["members"] >= ramp_floor]
    ramps.sort(key=lambda c: -c["share"])

    # --- screens, not merely distinct colours ------------------------------
    # On a dark garment an underbase is an EXTRA screen that no amount of
    # artwork colour can express. Counting the white proof background was
    # accidentally right for transparent artwork and plain wrong for a
    # full-bleed design, which has no background at all. So model it: each
    # non-white ink is a screen, and white is a screen if the artwork uses it
    # OR an underbase requires it (the two are the same physical screen, which
    # is why this is not simply +1).
    if not white_is_ink:
        # White here is the proof background, not artwork. Drop it, so the ink
        # list contains artwork inks only and the white screen is added back
        # explicitly below. Otherwise `rendered_ink_colors` silently included a
        # background and disagreed with the declared-colour count.
        inks = [c for c in inks if int(c["rep"]) != white]
    colored = [c for c in inks if int(c["rep"]) != white]
    white_screen = bool(white_is_ink or options.get("dark_garment_underbase"))
    screens = len(colored) + (1 if white_screen else 0)

    stats["rendered_white_screen"] = white_screen
    stats["rendered_screens"] = screens
    stats["rendered_colored_inks"] = len(colored)
    stats["rendered_distinct_colors"] = distinct_total
    stats["rendered_significant_colors"] = distinct_significant
    stats["rendered_ink_colors"] = len(inks)
    stats["rendered_ink_hex"] = [hexify(c["rep"]) for c in inks]
    stats["rendered_top_inks"] = [
        {"color": hexify(c["rep"]), "percent_of_canvas": round(c["share"], 3),
         "merged_colors": c["members"]} for c in inks[:12]
    ]
    stats["rendered_continuous_tone"] = bool(ramps)
    stats["rendered_ramp_colors"] = sum(c["members"] for c in ramps)
    stats["rendered_largest_ramp_percent"] = (
        round(ramps[0]["share"], 3) if ramps else 0.0)
    stats["rendered_merge_tolerance"] = tolerance
    stats["rendered_blend_tolerance"] = blend_tolerance
    stats["rendered_blends_absorbed"] = absorbed
    if blend_skipped:
        stats["rendered_blend_note"] = blend_skipped
    stats["rendered_area_threshold_percent"] = threshold
    stats["proof_pixels"] = total_px
    if clustered_note:
        stats["rendered_clustering_note"] = clustered_note

    if max_colors is not None and screens > max_colors:
        shown = ", ".join("%s %.2f%%" % (hexify(c["rep"]), c["share"])
                          for c in inks[:8])
        if len(inks) > 8:
            shown += ", ... (%d more)" % (len(inks) - 8)
        detail = ("Rendered artwork needs %d screen%s, spec allows %d. Largest "
                  "inks: %s"
                  % (screens, "" if screens == 1 else "s", max_colors, shown))
        if options.get("dark_garment_underbase"):
            detail += (". That is %d non-white ink%s plus the white underbase "
                       "screen" % (len(colored), "" if len(colored) == 1 else "s"))
        failures.append((RULE_RENDERED_COLORS, detail))

    diffuse_tone = (diffuse_area >= float(options["tone_min_area_percent"])
                    and diffuse_colors >= int(options["tone_min_colors"]))
    stats["rendered_diffuse_area_percent"] = round(diffuse_area, 4)
    stats["rendered_diffuse_colors"] = diffuse_colors
    stats["rendered_diffuse_tone"] = diffuse_tone
    stats["rendered_continuous_tone"] = bool(ramps) or diffuse_tone

    # White is paper on a white sheet, but it is very much an ink on a dark
    # garment -- and on a shirt job it is usually the underbase, i.e. the first
    # screen. Say which way it was read rather than letting the count quietly
    # come out one short.
    if declared_white:
        if white_is_ink:
            stats["rendered_white_treated_as"] = "ink (declared in the artwork)"
        elif options.get("dark_garment_underbase"):
            stats["rendered_white_treated_as"] = (
                "background only -- the underbase screen supplies white")
        elif not count_white:
            # This is an ASSUMPTION ABOUT THE GARMENT, not a fact about the
            # artwork: no ink is no ink only when the fabric is already white.
            # State the assumption and its consequence in screens, every time
            # white is involved, because the same file is 4 or 5 screens
            # depending on it and nothing else in the report reveals that.
            stats["rendered_white_treated_as"] = (
                "paper, not an ink (assumes a light/white garment)")
            stats["rendered_white_note"] = (
                "white is declared in the artwork but is NOT counted as an ink, "
                "which assumes the garment is white -- on white fabric that is "
                "correct, since paper and garment white need no screen. On a "
                "DARK garment white is very often the first ink down, laid as an "
                "underbase beneath everything else, so this job would need %s "
                "screen%s, not %s. Set print.dark_garment_underbase to true if "
                "these are dark shirts."
                % ((screens + 1), "" if (screens + 1) == 1 else "s", screens))
        else:
            stats["rendered_white_treated_as"] = (
                "background only -- no white is declared in the artwork")

    if options["allow_gradients"]:
        return

    if ramps:
        worst = ramps[0]
        failures.append((
            RULE_CONTINUOUS_TONE,
            "Continuous tone detected: a smooth transition spanning %d "
            "near-identical colours covering %.2f%% of the sheet (around %s). "
            "Gradients and photographs cannot be reproduced as %s flat "
            "colour%s - they need process/halftone printing. If your printer "
            "is doing CMYK process work, set print.allow_gradients to true."
            % (worst["members"], worst["share"], hexify(worst["rep"]),
               max_colors if max_colors is not None else "a small number of",
               "" if max_colors == 1 else "s"),
        ))
    elif diffuse_tone:
        # Photographic content: no single colour bands into a visible ramp, but
        # together the sub-threshold colours cover a large part of the sheet.
        failures.append((
            RULE_CONTINUOUS_TONE,
            "Continuous tone detected: %.1f%% of the sheet is covered by %d "
            "distinct colours, none of them individually large enough to count "
            "as an ink. That is photographic or airbrushed content, which no "
            "flat-colour budget can reproduce - it needs process/halftone "
            "screening. If your printer is doing CMYK process work, set "
            "print.allow_gradients to true."
            % (diffuse_area, diffuse_colors),
        ))


# --------------------------------------------------------------------------
# Step 3: ink coverage (Ghostscript separations)
# --------------------------------------------------------------------------

def pick_cmyk_profile(override=None):
    """Prefer the printer-supplied profile, else a bundled press reference.

    Only ever used when converting *from* RGB.  Applying a profile to input that
    is already CMYK rewrites the values (a measured 400% rich black came back as
    293%), so CMYK input is passed through with LeaveColorUnchanged instead.
    """
    if override:
        return override if os.path.exists(override) else None
    for path in CMYK_PROFILE_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def analyse_ink(pdf_path, workdir, dpi, options, failures, notes, stats,
                source_is_cmyk=False, profile_override=None):
    """Measure total area coverage from CMYK separations.

    Ghostscript's tiffsep device writes one grayscale plate per ink where
    255 means NO ink and 0 means full ink (verified against pure white, pure
    yellow and mid grey patches -- the polarity is not documented and getting
    it backwards silently turns the number into nonsense).

    Colour handling matters more than anything else here:

    * Already-CMYK input is passed through untouched (LeaveColorUnchanged, no
      output profile).  Forcing a conversion would push the values through an
      ICC profile and silently rewrite them -- a measured 400% rich black came
      back as 293%, which is how this was caught.
    * RGB input has to be converted, so the numbers are an estimate; see
      report_colour_space() for why they cannot be trusted as a gate.
    """
    sep_prefix = os.path.join(workdir, "sep")
    for stale in os.listdir(workdir):
        if stale.startswith("sep") and stale.endswith(".tif"):
            os.unlink(os.path.join(workdir, stale))

    argv = [
        "gs", "-dNOPAUSE", "-dBATCH", "-dSAFER",
        "-sDEVICE=tiffsep",
        "-r%d" % dpi,
        "-dProcessColorModel=/DeviceCMYK",
    ]
    if source_is_cmyk:
        argv.append("-sColorConversionStrategy=LeaveColorUnchanged")
    else:
        argv.append("-sColorConversionStrategy=CMYK")
        profile = pick_cmyk_profile(profile_override)
        if profile:
            argv.append("-sOutputICCProfile=%s" % profile)
            stats["ink_profile_used"] = profile
    argv += ["-sOutputFile=%s.tif" % sep_prefix, pdf_path]

    try:
        run_tool(argv)
    except ToolError as exc:
        notes.append("ink coverage not measured: %s" % exc)
        return

    plates = {}
    for ink in ("Cyan", "Magenta", "Yellow", "Black"):
        path = "%s(%s).tif" % (sep_prefix, ink)
        if not os.path.exists(path):
            notes.append("ink coverage not measured: Ghostscript wrote no %s "
                         "separation" % ink)
            return
        with Image.open(path) as image:
            plates[ink] = np.asarray(image.convert("L"), dtype=np.float32)

    if not plates:
        return

    shapes = {p.shape for p in plates.values()}
    if len(shapes) != 1:
        notes.append("ink coverage not measured: separations have mismatched "
                     "sizes %s" % sorted(shapes))
        return

    tac = np.zeros(next(iter(shapes)), dtype=np.float32)
    per_ink_mean = {}
    for ink, plate in plates.items():
        coverage = (255.0 - plate) / 255.0 * 100.0     # 255 = no ink
        per_ink_mean[ink] = float(coverage.mean())
        tac += coverage

    limit = float(options["tac_limit_percent"])
    over = float((tac > limit).mean() * 100.0)

    stats["ink_tac_max_percent"] = round(float(tac.max()), 1)
    stats["ink_tac_mean_percent"] = round(float(tac.mean()), 1)
    stats["ink_tac_area_over_limit_percent"] = round(over, 3)
    stats["ink_tac_limit_percent"] = limit
    stats["ink_plate_means_percent"] = {k: round(v, 1)
                                       for k, v in per_ink_mean.items()}

    if over > 0.0:
        if source_is_cmyk:
            explanation = ("Measured directly from the CMYK values in the file, "
                           "so this reading is exact.")
        else:
            explanation = ("This is a relative warning: the source is not CMYK, "
                           "so Ghostscript invented the separation. Convert with "
                           "your printer's profile and re-run on the PDF for an "
                           "exact reading.")
        failures.append((
            RULE_INK,
            "Total area coverage reaches %.0f%% (limit %.0f%%), and %.2f%% of "
            "the sheet is over the limit. Ink that heavy will not dry and will "
            "set off onto the sheets beneath it. %s"
            % (tac.max(), limit, over, explanation),
        ))
    for ink, mean in per_ink_mean.items():
        if mean > 95.0:
            notes.append("%s plate averages %.0f%% coverage - the design may "
                         "be relying on heavy ink overall" % (ink, mean))

    for stale in os.listdir(workdir):
        if stale.startswith("sep") and stale.endswith(".tif"):
            os.unlink(os.path.join(workdir, stale))


# --------------------------------------------------------------------------
# Step 4: PDF facts (fonts, images, colour space)
# --------------------------------------------------------------------------

def export_pdf(svg_path, pdf_path, failures):
    argv = ["inkscape", svg_path,
            "--export-type=pdf",
            "--export-filename=%s" % pdf_path]
    try:
        run_tool(argv)
    except ToolError as exc:
        failures.append((RULE_PDF, "could not export a print PDF: %s" % exc))
        return False
    if not os.path.exists(pdf_path):
        failures.append((RULE_PDF, "Inkscape wrote no PDF"))
        return False
    return True


def inspect_page(pdf_path, stats, notes):
    try:
        _, out, _ = run_tool(["pdfinfo", pdf_path])
    except ToolError as exc:
        notes.append("page geometry unknown: %s" % exc)
        return
    for line in out.splitlines():
        if line.lower().startswith("page size:"):
            stats["pdf_page_size"] = line.split(":", 1)[1].strip()
        elif line.lower().startswith("pages:"):
            stats["pdf_pages"] = int(line.split(":", 1)[1].strip())


def inspect_fonts(pdf_path, options, failures, stats, notes):
    try:
        _, out, _ = run_tool(["pdffonts", pdf_path])
    except ToolError as exc:
        notes.append("font embedding not verified: %s" % exc)
        return

    lines = out.splitlines()
    if len(lines) < 2:
        stats["pdf_fonts"] = []
        return

    header = lines[0]
    dash = lines[1] if set(lines[1].strip()) <= set("- ") else None
    emb_start = header.find("emb")
    end_start = header.find("uni")
    if emb_start == -1:
        notes.append("font embedding not verified: unexpected pdffonts output")
        return
    cut = end_start if end_start > emb_start else len(header)

    fonts = []
    for line in lines[2 if dash else 1:]:
        if not line.strip():
            continue
        name = line[:emb_start].strip()
        embedded_raw = line[emb_start:cut].strip().split()
        embedded = bool(embedded_raw) and embedded_raw[0].lower() == "yes"
        fonts.append({"name": name, "embedded": embedded})
    stats["pdf_fonts"] = fonts

    if not fonts:
        return
    missing = [f["name"] for f in fonts if not f["embedded"]]
    if missing:
        # Always reported; whether it fails the job is decided by
        # require_embedded_fonts (v4.0 lists this as advisory by default).
        severity_hint = ("the spec requires embedded fonts"
                         if options["require_embedded_fonts"]
                         else "advisory unless require_embedded_fonts is set")
        failures.append((
            RULE_FONT,
            "%d font%s not embedded (%s), so the printer will substitute them "
            "and the layout and glyph shapes will shift [%s]"
            % (len(missing), "" if len(missing) == 1 else "s",
               ", ".join(missing[:5]), severity_hint),
        ))


def inspect_images(pdf_path, options, failures, stats, notes):
    try:
        code, out, _ = run_tool(["pdfimages", "-list", pdf_path], check=False)
    except ToolError as exc:
        notes.append("image resolution not verified: %s" % exc)
        return
    if code != 0:
        notes.append("image resolution not verified: pdfimages failed")
        return

    lines = out.splitlines()
    if len(lines) < 2:
        stats["pdf_images"] = []
        return

    header = lines[0]
    ppi_start = header.find("x-ppi")
    if ppi_start == -1:
        notes.append("image resolution not verified: unexpected pdfimages output")
        return

    images = []
    skipped_masks = 0
    for line in lines[2:]:
        if not line.strip():
            continue
        cols = line.split()
        if len(cols) < 3:
            continue
        # Column 2 is the row type. A soft mask or mask is part of the image it
        # belongs to, not a second placed bitmap: pdfimages prints one row per
        # image AND one per mask, so counting every row reported a single 1x1
        # image as "2 placed raster images" with an inflated ppi complaint.
        kind = cols[2].lower()
        if kind in ("smask", "mask"):
            skipped_masks += 1
            continue
        try:
            width, height = int(cols[3]), int(cols[4])
            x_ppi, y_ppi = float(cols[12]), float(cols[13])
        except (IndexError, ValueError):
            continue
        images.append({"width_px": width, "height_px": height,
                       "x_ppi": round(x_ppi, 1), "y_ppi": round(y_ppi, 1)})
    stats["pdf_images"] = images
    stats["pdf_image_masks"] = skipped_masks

    floor = float(options["min_image_ppi"])
    weak = [i for i in images if min(i["x_ppi"], i["y_ppi"]) < floor]
    if weak:
        worst = min(weak, key=lambda i: min(i["x_ppi"], i["y_ppi"]))
        failures.append((
            RULE_IMAGE_RES,
            "%d placed raster image%s below %g ppi (worst: %dx%d px at %.0f ppi). "
            "It will print visibly soft"
            % (len(weak), "" if len(weak) == 1 else "s", floor,
               worst["width_px"], worst["height_px"],
               min(worst["x_ppi"], worst["y_ppi"])),
        ))


def detect_colour_space(pdf_path, workdir, notes):
    """Report the colour space the PDF actually declares.

    Ghostscript's inkcov device is NOT usable for this: it converts to CMYK
    before reporting, so it answers "CMYK" for an RGB file.  The names have to
    be read out of the PDF itself.  Object streams are decompressed with qpdf
    first when available, because names inside a compressed stream are not
    visible to a raw byte scan.
    """
    names = (b"/DeviceRGB", b"/DeviceCMYK", b"/DeviceGray", b"/ICCBased")
    found = set()

    def scan(path):
        try:
            with open(path, "rb") as handle:
                blob = handle.read()
        except OSError:
            return
        for name in names:
            if name in blob:
                label = name.decode("ascii").lstrip("/")
                # Normalise /DeviceCMYK -> CMYK so callers can test membership
                # against the plain names a print operator would use.
                if label.lower().startswith("device"):
                    label = label[len("device"):]
                found.add(label)

    scan(pdf_path)

    if not found and shutil.which("qpdf"):
        flat = os.path.join(workdir, "flattened.pdf")
        code, _, _ = run_tool(["qpdf", "--qdf", "--object-streams=disable",
                               pdf_path, flat], check=False)
        if code == 0 and os.path.exists(flat):
            scan(flat)
            os.unlink(flat)

    if not found:
        notes.append("the PDF's colour space could not be determined")
        return []

    if found == {"ICCBased"}:
        notes.append("the PDF uses ICC-based colour; check the embedded "
                     "profile with your printer")
    return sorted(found)


def report_colour_space(spaces, options, failures, notes, stats):
    stats["pdf_color_spaces"] = spaces
    if not spaces:
        return
    prose = "/".join(spaces)
    if "CMYK" not in spaces:
        notes.append("the PDF is %s, not CMYK. Total area coverage below is an "
                     "ESTIMATE and the tac_limit_percent gate CANNOT fire "
                     "usefully: Ghostscript has to invent a CMYK separation, it "
                     "does not apply press-grade GCR, and measured across "
                     "reproduction blacks it never exceeds about 296%% (a "
                     "proper rich black is 300-330%%). A 50%% grey also comes "
                     "out near 145%% ink instead of roughly 50%%. So: use the "
                     "coverage number as a relative warning only, ask your "
                     "printer for a profile, convert once, and re-run this on "
                     "the resulting PDF -- then the check is exact."
                     % prose)
    if options["require_cmyk"] and "CMYK" not in spaces:
        failures.append((
            RULE_CMYK,
            "spec requires CMYK output but the PDF declares %s. Convert with "
            "your printer's profile before sending" % prose,
        ))


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def preflight(svg_path, spec_path, workdir=None, dpi=None):
    """Run every check.

    Accepts either an SVG (the normal case) or a PDF.  PDF mode is for the end
    of the workflow: once the artwork has been converted to CMYK, checking the
    final file is the only way the ink-coverage and font checks mean anything.
    """
    failures = []
    notes = []
    stats = {}

    is_pdf_input = svg_path.lower().endswith(".pdf")

    missing = require_tools(["gs"] if is_pdf_input else ["inkscape", "gs"])
    if missing:
        failures.append((RULE_TOOL, "required tool%s not installed: %s"
                         % ("" if len(missing) == 1 else "s", ", ".join(missing))))

    options, raw_spec = load_print_options(spec_path, notes, failures)
    if dpi is not None:
        options["dpi"] = int(dpi)

    max_colors = None
    if isinstance(raw_spec, dict) and raw_spec.get("max_colors") is not None:
        try:
            max_colors = int(raw_spec["max_colors"])
        except (TypeError, ValueError):
            max_colors = None

    stats["input_kind"] = "pdf" if is_pdf_input else "svg"
    stats["dark_garment_underbase"] = None

    # --- 1. declared properties, via the existing validator -----------------
    if is_pdf_input:
        notes.append("input is a PDF: the source-level checks in "
                     "validate_svg.py (declared colours, stroke widths, open "
                     "paths) do not apply and were skipped")
        if not os.path.exists(svg_path):
            failures.append((validate_svg.RULE_INPUT,
                             "PDF file not found: %s" % svg_path))
            return failures, notes, stats, None, options
    else:
        static_failures, static_notes, static_stats = validate_svg.validate(
            svg_path, spec_path)
        failures.extend(static_failures)
        notes.extend(static_notes)
        stats["static"] = {k: v for k, v in static_stats.items() if k != "colors"}
        stats["declared_colors"] = static_stats.get("colors", [])
        if not os.path.exists(svg_path):
            return failures, notes, stats, None, options

    if workdir is None:
        base = os.path.dirname(os.path.abspath(svg_path))
        workdir = os.path.join(base, "preflight")
    try:
        os.makedirs(workdir, exist_ok=True)
    except OSError as exc:
        failures.append((RULE_RENDER, "cannot create work directory %s: %s"
                         % (workdir, exc)))
        return failures, notes, stats, None, options

    stem = os.path.splitext(os.path.basename(svg_path))[0]
    png_path = os.path.join(workdir, "%s.proof.png" % stem)
    pdf_path = svg_path if is_pdf_input else os.path.join(
        workdir, "%s.print.pdf" % stem)
    if is_pdf_input:
        stats["pdf_path"] = pdf_path
        stats["pdf_is_input"] = True

    if missing:
        return failures, notes, stats, None, options

    target_dpi = int(options["dpi"])

    # --- 2. raster proof + rendered ink count ------------------------------
    if is_pdf_input:
        rendered = rasterise_pdf(pdf_path, png_path, target_dpi, failures)
    else:
        rendered = render_proof(svg_path, png_path, target_dpi, failures)
    if rendered is not None:
        stats["proof_path"] = png_path
        try:
            analyse_rendered_colours(png_path, options, max_colors, failures, stats)
        except Exception as exc:                        # noqa: BLE001
            failures.append((RULE_RENDERED_COLORS,
                             "could not analyse the proof: %s: %s"
                             % (type(exc).__name__, exc)))

    # --- 3. print PDF, then everything that can be read off it -------------
    exported = True
    if not is_pdf_input:
        exported = export_pdf(svg_path, pdf_path, failures)
        if exported:
            stats["pdf_path"] = pdf_path
    if exported:
        inspect_page(pdf_path, stats, notes)
        inspect_fonts(pdf_path, options, failures, stats, notes)
        inspect_images(pdf_path, options, failures, stats, notes)
        spaces = detect_colour_space(pdf_path, workdir, notes)
        report_colour_space(spaces, options, failures, notes, stats)
        analyse_ink(pdf_path, workdir, target_dpi, options, failures, notes, stats,
                    source_is_cmyk="CMYK" in spaces,
                    profile_override=options.get("icc_profile_path"))

    declared = static_stats.get("colors") if not is_pdf_input else None
    if declared is not None:
        # Layer A read the source, so it knows whether white is really in the
        # artwork rather than merely being the background of the proof.
        options["white_declared_in_artwork"] = "#ffffff" in declared

    # Anything the render step recorded as needing the user's attention has to
    # reach stdout; a note that only exists in stats is a note nobody reads.
    if stats.get("rendered_white_note"):
        notes.append(stats["rendered_white_note"])

    if options["dark_garment_underbase"]:
        notes.append("dark_garment_underbase is set: the ink-coverage figure "
                     "measures a CMYK separation on paper, and cannot represent "
                     "the white underbase laid beneath the whole design, so the "
                     "total ink actually printed on the garment is understated "
                     "by that laydown. Treat the coverage number as a lower "
                     "bound, and ask the printer how they underbase.")

    stats["tac_is_estimate"] = "CMYK" not in (
        stats.get("pdf_color_spaces") or [])
    # A reading only becomes a gate when the numbers came from CMYK input.
    # On RGB input Ghostscript invents the separation, so INK_COVERAGE stays
    # advisory no matter how large the number looks.
    options["tac_exact"] = (not stats["tac_is_estimate"]
                            and "ink_tac_max_percent" in stats)
    stats["tac_exact"] = options["tac_exact"]
    stats["dark_garment_underbase"] = bool(options["dark_garment_underbase"])
    stats["white_counted_as_ink"] = bool(options["count_white_as_ink"])
    stats["dpi"] = target_dpi
    return failures, notes, stats, workdir, options


def _format_report(svg_path, failures, notes, stats, spec_path):
    out = []
    out.append("PREFLIGHT for %s" % svg_path)
    out.append("  spec: %s   dpi: %s" % (spec_path, stats.get("dpi", "?")))
    for key, label in (("proof_path", "proof"),
                       ("pdf_path", "print PDF")):
        if key in stats:
            out.append("  %s: %s" % (label, stats[key]))

    declared = stats.get("declared_colors") or []
    out.append("")
    out.append("DECLARED (source properties, from the spec)")
    out.append("  %d declared colour%s%s"
               % (len(declared), "" if len(declared) == 1 else "s",
                  (": " + ", ".join(declared)) if declared else ""))
    static = stats.get("static", {})
    if "failures" in static:
        out.append("  %d problem%s in the source itself"
                   % (static["failures"], "" if static["failures"] == 1 else "s"))

    if "rendered_ink_colors" in stats:
        out.append("")
        out.append("RENDERED (what actually prints, at %s dpi)" % stats.get("dpi"))
        out.append("  %d distinct pixel values, %d covering real area; merged "
                   "into %d flat ink%s at tolerance %g"
                   % (stats.get("rendered_distinct_colors", 0),
                      stats.get("rendered_significant_colors", 0),
                      stats.get("rendered_ink_colors", 0),
                      "" if stats.get("rendered_ink_colors", 0) == 1 else "s",
                      stats.get("rendered_merge_tolerance", 0)))
        if "rendered_screens" in stats:
            detail = "%d screen%s" % (stats["rendered_screens"],
                                      "" if stats["rendered_screens"] == 1 else "s")
            if stats.get("rendered_white_screen"):
                detail += " (includes white%s)" % (
                    " underbase" if stats.get("dark_garment_underbase")
                    else " as an artwork colour")
            out.append("  " + detail)
        if stats.get("rendered_white_treated_as"):
            # The value already names the reason; no second parenthetical.
            out.append("  white: %s" % stats["rendered_white_treated_as"])
        if stats.get("rendered_continuous_tone"):
            out.append("  CONTINUOUS TONE: %d colours form a smooth transition "
                       "over %.2f%% of the sheet"
                       % (stats.get("rendered_ramp_colors", 0),
                          stats.get("rendered_largest_ramp_percent", 0.0)))
        top = stats.get("rendered_top_inks") or []
        if top:
            out.append("  largest: " + ", ".join(
                "%s %.2f%%%s" % (t["color"], t["percent_of_canvas"],
                                 (" (%d merged)" % t["merged_colors"])
                                 if t.get("merged_colors", 1) > 1 else "")
                for t in top[:6]))

    if "ink_tac_max_percent" in stats:
        out.append("")
        if stats.get("tac_is_estimate"):
            out.append("INK (ESTIMATE -- source is not CMYK, see LOG notes)")
        else:
            out.append("INK (exact -- source is CMYK, values passed through)")
        out.append("  max total area coverage %.0f%%, mean %.0f%%, "
                   "%.2f%% of sheet over the %s%% limit"
                   % (stats["ink_tac_max_percent"], stats["ink_tac_mean_percent"],
                      stats["ink_tac_area_over_limit_percent"],
                      stats.get("ink_tac_limit_percent", "?")))
        plates = stats.get("ink_plate_means_percent") or {}
        if plates:
            out.append("  plate averages: " + ", ".join(
                "%s %.0f%%" % (k, v) for k, v in plates.items()))

    if "pdf_path" in stats:
        out.append("")
        out.append("PDF")
        n_images = len(stats.get("pdf_images") or [])
        n_fonts = len(stats.get("pdf_fonts") or [])
        n_embedded = sum(1 for f in (stats.get("pdf_fonts") or [])
                         if f.get("embedded"))
        out.append("  %s, %s page%s, %d placed raster image%s"
                   % (stats.get("pdf_page_size", "unknown size"),
                      stats.get("pdf_pages", 1),
                      "" if stats.get("pdf_pages", 1) == 1 else "s",
                      n_images, "" if n_images == 1 else "s"))
        out.append("  fonts: %d (%d embedded)" % (n_fonts, n_embedded))
        spaces = stats.get("pdf_color_spaces")
        if spaces:
            out.append("  colour space: %s" % "/".join(spaces))
    return "\n".join(out)


def build_manifest(svg_path, spec_path, findings, notes, stats, options):
    """Assemble the single machine-readable handoff document.

    Records both layers, every finding with its layer and severity, the
    artifacts produced, and the toolchain that produced them -- so a result can
    still be interpreted months later, when the Ghostscript version matters.
    """
    hard = [f for f in findings if f["severity"] == HARD]
    advisory = [f for f in findings if f["severity"] == ADVISORY]
    by_layer = {}
    for entry in findings:
        bucket = by_layer.setdefault(entry["layer"], {"hard": 0, "advisory": 0})
        bucket[entry["severity"]] += 1
    for layer in (LAYER_A, LAYER_B):
        by_layer.setdefault(layer, {"hard": 0, "advisory": 0})

    artifacts = {}
    if stats.get("proof_path"):
        artifacts["proof_png"] = stats["proof_path"]
    if stats.get("pdf_path"):
        artifacts["print_pdf"] = stats["pdf_path"]

    # The fabric colour travels with the manifest because every downstream
    # consumer needs it to read the inks: a printer, the studio's proof page,
    # and Jev all have to know that this one declared colour is the garment and
    # not a screen. Recording it once here beats each of them re-deriving it
    # from the spec and possibly disagreeing.
    substrate = options.get("substrate")
    palette = [str(c) for c in (options.get("palette") or [])]
    screens = [c for c in palette if c.upper() != str(substrate).upper()] \
        if substrate else list(palette)

    return {
        "tool": "preflight.py (v4.0 two-layer pipeline)",
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input": {"file": svg_path, "kind": stats.get("input_kind"),
                  "spec": spec_path},
        "passed": not hard,
        "substrate": {
            "colour": substrate,
            "is_fabric": bool(substrate),
            "palette": palette,
            "screens": screens,
            "note": ("this colour is the garment/substrate and is NOT laid down "
                     "as ink; it appears in the proof only as the material "
                     "showing through")
                    if substrate else
                    "no substrate declared; every declared colour is a screen",
        },
        "summary": {
            "hard": len(hard),
            "advisory": len(advisory),
            "by_layer": by_layer,
            "print_method": options.get("print_method") or None,
            "gradient_handling": options.get("gradient_handling") or None,
            "require_cmyk": bool(options.get("require_cmyk")),
            "tac_exact": bool(stats.get("tac_exact")),
        },
        "findings": findings,
        "artifacts": artifacts,
        "versions": tool_versions(),
        "stats": stats,
        "notes": notes,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="preflight.py",
        description="Two-layer print preflight for an SVG or PDF: source "
                    "validation plus rendered ink, coverage, font and image "
                    "measurement. Hard gates fail the job; advisories are "
                    "reported only.")
    parser.add_argument("input", help="path to the SVG (or a PDF, for an exact "
                                      "ink reading after conversion)")
    parser.add_argument("spec", help="path to spec.json")
    parser.add_argument("--dpi", type=int, default=None,
                        help="proof and measurement resolution (default: print.dpi "
                             "from the spec, else 300)")
    parser.add_argument("--workdir", default=None,
                        help="where to write the proof and PDF "
                             "(default: <input dir>/preflight/)")
    parser.add_argument("--json", dest="json_path", default=None,
                        help="write the unified layer A + layer B manifest here")
    args = parser.parse_args(argv)

    failures, notes, stats, workdir, options = preflight(
        args.input, args.spec, workdir=args.workdir, dpi=args.dpi)

    hard, advisory = classify_findings(failures, options)
    stats["hard_findings"] = len(hard)
    stats["advisory_findings"] = len(advisory)

    print(_format_report(args.input, failures, notes, stats, args.spec))

    if hard:
        print("")
        print("HARD GATES (%d) -- these fail the job" % len(hard))
        for entry in hard:
            print(" - [%s]: %s" % (entry["rule"], entry["detail"]))
    if advisory:
        print("")
        print("ADVISORY (%d) -- reported, not failing" % len(advisory))
        for entry in advisory:
            print(" - [%s]: %s" % (entry["rule"], entry["detail"]))

    if notes:
        print("")
        print("LOG")
        for note in notes:
            print("  - %s" % note)

    if args.json_path:
        payload = build_manifest(args.input, args.spec,
                                 [dict(e) for e in hard + advisory],
                                 notes, stats, options)
        try:
            parent = os.path.dirname(os.path.abspath(args.json_path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(args.json_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
            print("")
            print("manifest: %s" % args.json_path)
        except OSError as exc:
            print("could not write the manifest: %s" % exc)

    print("")
    if hard:
        print("PREFLIGHT FAILED for %s (%d hard gate%s, %d advisory)"
              % (args.input, len(hard), "" if len(hard) == 1 else "s",
                 len(advisory)))
        return 1
    print("PREFLIGHT PASSED for %s" % args.input)
    if advisory:
        print("  with %d advisor%s to review" % (len(advisory),
                                                 "y" if len(advisory) == 1
                                                 else "ies"))
    if workdir:
        print("  proof: %s" % os.path.join(
            workdir, os.path.splitext(os.path.basename(args.input))[0]
            + ".proof.png"))
    return 0


if __name__ == "__main__":
    sys.exit(main())