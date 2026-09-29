#!/usr/bin/env python3
"""Run an SVG validator over a batch of degenerate documents.

Every case declares what SHOULD happen (pass, or fail with a specific rule
tag). Mismatches are listed at the end, so a validator that quietly accepts a
file it should reject -- or crashes instead of reporting -- shows up before you
declare it done.

Usage:
  python3 svg_edge_case_probe.py --cmd ".venv/bin/python validate_svg.py {svg} {spec}"
  python3 svg_edge_case_probe.py --cmd "..." --spec spec.json --dir out/

The command template must contain {svg} and {spec}. Cases with their own spec
override get a per-case spec file; the rest share one written from --spec, or a
strict default. Exit code is 1 if any case behaved unexpectedly.

Case bodies are SVG FRAGMENTS (child markup) and are wrapped in a minimal
`<svg>` root, because that is how a fragment is written into a real file. A case
that is deliberately a whole document -- a nested `<svg>`, an XML declaration
plus DOCTYPE, a truncated document, a non-SVG root -- declares itself by having
`svg` or `html` as its root element and is written out unwrapped. A leading BOM
is placed BEFORE the wrapper, so the BOM case tests a BOM-prefixed document
rather than a BOM in the middle of one.

Getting that wrap decision wrong is not a cosmetic bug: when a bare
`<path .../>` case was written unwrapped it became the document ROOT, the
validator answered `[SVG_PARSE_ERROR] root element is <path>, expected <svg>` or
"Extra content at the end of the document", and the probe reported the
validator's complaint as the case's outcome. Every fragment case failed the same
way, so the probe could not tell a validator change from its own malformation.

Expectations:
  pass              exit 0
  fail:RULE_TAG     non-zero exit AND `[RULE_TAG]` somewhere in the output
  any               any outcome; for a case whose point is a documented limit
  <base>-not:TEXT   as above, AND TEXT must not appear in the output (used to
                    assert that an external entity was not resolved)

Stdlib only. Files are written under TMPDIR unless --dir is given.
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile

SVG_OPEN = ('<svg xmlns="http://www.w3.org/2000/svg" '
            'xmlns:xlink="http://www.w3.org/1999/xlink" '
            'width="100" height="100" viewBox="0 0 100 100">\n')

# A case body is a whole document (not a fragment) when its root element is one
# of these.  `svg` covers a nested <svg>, an XML-declaration + DOCTYPE document
# and a truncated one; `html` covers the deliberate non-SVG root case.
DOCUMENT_ROOTS = ("svg", "html")

# Root element of a body, skipping a BOM, an XML declaration, a DOCTYPE and
# leading comments.  "Does the body start with '<'" is NOT the test: so does a
# bare <path/>, and wrapping on that is exactly the bug this probes for.
_ROOT_RE = re.compile(
    r"\s*(?:<\?xml[^>]*\?>\s*"
    r"|<!DOCTYPE(?:[^\[>]|\[[^\]]*\])*>\s*"
    r"|<!--.*?-->\s*)*"
    r"<\s*([A-Za-z][\w:.-]*)", re.DOTALL)

DEFAULT_SPEC = {
    "max_colors": 6,
    "geometry": {
        "allow_raster_embed": False,
        "allow_open_paths": False,
        "min_stroke_width_pt": 1.5,
    },
}

# Each case: name, body (wrapped in <svg> unless its root is svg/html),
# expectation, optional note, optional spec override.
CASES = [
    # --- geometry that is fine -------------------------------------------------
    ("closed_triangle", '<path d="M0,0 L10,0 L10,10 Z" fill="#000000"/>',
     "pass", None, None),
    ("curve_closed", '<path d="M0,0 C10,0 10,10 0,10 Z" fill="#000000"/>',
     "pass", None, None),
    ("arc_closed", '<path d="M0,0 A5,5 0 0 1 10,0 Z" fill="#000000"/>',
     "pass", None, None),
    ("self_intersecting_star",
     '<path d="M10,0 L12.5,7.5 L20,7.5 L14,12 L16,20 L10,15 L4,20 L6,12 L0,7.5 '
     'L7.5,7.5 Z" fill="#000000"/>', "pass", None, None),
    ("two_closed_subpaths",
     '<path d="M0,0 L5,0 L5,5 Z M10,10 L15,10 L15,15 Z" fill="#000000"/>',
     "pass", None, None),

    # --- geometry that must be rejected ---------------------------------------
    ("open_path", '<path id="open" d="M10,10 L20,10"/>', "fail:OPEN_PATH",
     None, None),
    ("open_subpath_beside_closed",
     '<path id="mixed" d="M0,0 L10,0 L10,10 Z M20,20 L30,20"/>',
     "fail:OPEN_PATH", "closure must be judged per subpath", None),
    ("open_curve", '<path id="c" d="M0,0 C10,0 10,10 0,10"/>', "fail:OPEN_PATH",
     None, None),
    ("moveto_only", '<path id="m" d="M10,10"/>', "fail:ZERO_AREA_PATH",
     None, None),
    ("blank_d", '<path id="blank" d="   "/>', "fail:ZERO_AREA_PATH", None, None),
    ("no_d_attribute", '<path id="empty"/>', "fail:ZERO_AREA_PATH", None, None),
    ("collapsed_zero_area",
     '<path id="sliver" d="M10,10 L20,20 Z" fill="#000000"/>',
     "fail:ZERO_AREA_PATH", None, None),
    ("zero_length", '<path id="dot" d="M10,10 L10,10 Z"/>', "fail:ZERO_AREA_PATH",
     None, None),
    ("malformed_path_data", '<path id="broken" d="M0,0 Q q zzz"/>',
     "fail:PATH_PARSE_ERROR", None, None),

    # --- stroke widths --------------------------------------------------------
    ("stroke_none_not_checked",
     '<rect width="10" height="10" stroke="none" stroke-width="0.1pt"/>',
     "pass", "no stroke paints, so there is no width to measure", None),
    ("zero_width", '<rect width="10" height="10" stroke="#000000" '
     'stroke-width="0"/>', "fail:MIN_STROKE_WIDTH", None, None),
    ("negative_width",
     '<rect width="10" height="10" stroke="#000000" stroke-width="-5pt"/>',
     "fail:MIN_STROKE_WIDTH", "invalid markup, must not be silently accepted",
     None),
    ("em_width", '<rect width="10" height="10" stroke="#000000" '
     'stroke-width="1em"/>', "fail:MIN_STROKE_WIDTH",
     "unresolvable unit must be reported, not skipped", None),
    ("percent_width",
     '<rect width="10" height="10" stroke="#000000" stroke-width="5%"/>',
     "fail:MIN_STROKE_WIDTH", None, None),
    ("nonsense_width",
     '<rect width="10" height="10" stroke="#000000" stroke-width="thick"/>',
     "fail:MIN_STROKE_WIDTH", None, None),
    ("unitless_is_px", '<rect width="10" height="10" stroke="#000000" '
     'stroke-width="1.5"/>', "fail:MIN_STROKE_WIDTH",
     "1.5 unitless = 1.5px = 1.125pt", None),
    ("inherited_thin_stroke",
     '<g stroke="#000000" stroke-width="0.5pt"><path id="inherits" '
     'd="M0,0 L10,10 L0,10 Z"/></g>', "fail:MIN_STROKE_WIDTH",
     "stroke/stroke-width inherit from an ancestor", None),
    ("style_block_thin_stroke",
     '<style>.hairline { stroke: #000000; stroke-width: 0.4pt; }</style>'
     '<path id="csspath" class="hairline" d="M0,0 L10,10 L0,10 Z"/>',
     "fail:MIN_STROKE_WIDTH", "a <style> value must beat element.get()", None),
    ("descendant_selector",
     '<style>g .y { stroke: #000000; stroke-width: 0.5pt; }</style>'
     '<g><path id="inner" class="y" d="M0,0 L10,10 L0,10 Z"/></g>',
     "fail:MIN_STROKE_WIDTH", None, None),
    ("media_print_applied",
     '<style>@media print { .x { stroke: #000; stroke-width: 0.5pt; } }</style>'
     '<path class="x" d="M0,0 L10,10 L0,10 Z"/>', "fail:MIN_STROKE_WIDTH",
     None, None),
    ("media_screen_ignored",
     '<style>@media screen { .x { stroke: #000; stroke-width: 9pt; } }'
     '.x { stroke: #000; stroke-width: 4pt; }</style>'
     '<path id="sc" class="x" d="M0,0 L10,10 L0,10 Z"/>', "pass",
     "a screen-only rule must not affect a print check", None),
    ("keyframes_ignored",
     '<style>@keyframes fade { 0% { stroke-width: 0.1pt; } }'
     '@font-face { font-family: x; }</style>'
     '<path d="M0,0 L10,10 L0,10 Z" stroke="#000000" stroke-width="4pt"/>',
     "pass", None, None),
    ("inline_style_beats_css",
     '<style>.s { stroke: #000; stroke-width: 0.5pt; }</style>'
     '<path id="p" class="s" style="stroke-width:4pt" '
     'd="M0,0 L10,10 L0,10 Z"/>', "pass", None, None),

    # --- !important is RANKED, not stripped (inline > stylesheet, important
    # --- > ordinary, across the sources this module models) -------------------
    ("important_stylesheet_beats_inline",
     '<style>.s { stroke: #000000; stroke-width: 0.5pt !important; }</style>'
     '<path class="s" style="stroke-width:4pt" d="M0,0 L10,10 L0,10 Z"/>',
     "fail:MIN_STROKE_WIDTH",
     "an !important stylesheet rule out-ranks an ordinary inline declaration",
     None),
    ("inline_important_beats_stylesheet_important",
     '<style>.s { stroke: #000000; stroke-width: 0.5pt !important; }</style>'
     '<path class="s" style="stroke-width:4pt !important" '
     'd="M0,0 L10,10 L0,10 Z"/>', "pass",
     "inline !important is the top of the ladder this module models", None),

    # --- paint that cannot ink is not measured --------------------------------
    ("hidden_elements_not_stroke_checked",
     '<g display="none"><path stroke="#000000" stroke-width="0.1pt" '
     'd="M0,0 L5,0 L5,5 Z"/></g>'
     '<rect width="5" height="5" fill="#000000" opacity="0"/>'
     '<rect width="5" height="5" fill="#000000" visibility="hidden"/>'
     '<rect width="5" height="5" fill="#000000"/>', "pass",
     "display:none / opacity=0 / visibility:hidden paint nothing, so nothing "
     "inside them is measured", None),
    ("zero_stroke_opacity_not_checked",
     '<rect width="5" height="5" fill="#000000" stroke="#000000" '
     'stroke-width="0" stroke-opacity="0"/>', "pass",
     "a fully transparent stroke inks nothing", None),

    # --- the colour budget counts declarations, including the ones the naive
    # --- version skipped: implicit black and currentColor ---------------------
    ("implicit_black_fill_counts",
     '<path d="M0,0 L5,0 L5,5 Z"/>', "fail:COLOR_COUNT",
     "a shape whose cascade never sets fill is BLACK, not 'no colour' -- a "
     "budget of 0 proves the implicit fill is counted",
     {"max_colors": 0}),
    ("currentcolor_resolves_from_inherited_color",
     '<g color="#000000"><path d="M0,0 L5,0 L5,5 Z" fill="currentColor"/></g>',
     "fail:COLOR_COUNT",
     "currentColor resolves from the inherited color property (initial black), "
     "so it is counted rather than skipped", {"max_colors": 0}),
    ("ignored_paint_keywords",
     '<rect width="1" height="1" fill="none"/>'
     '<rect width="1" height="1" fill="transparent"/>', "pass",
     "none/transparent occupy no palette slot", None),
    ("unknown_colour_keyword",
     '<rect width="5" height="5" fill="chartreusey"/>', "fail:COLOR_COUNT",
     "an unknown keyword must occupy a palette slot, not vanish",
     {"max_colors": 0}),

    # --- raster embeds: the rule covers more than <image> --------------------
    ("raster_image", '<image x="0" y="0" width="10" height="10" href="a.png"/>'
     '<rect width="5" height="5" fill="#000000"/>', "fail:RASTER_EMBED",
     None, None),
    ("image_in_pattern",
     '<defs><pattern id="p" width="4" height="4" patternUnits="userSpaceOnUse">'
     '<image x="0" y="0" width="4" height="4" href="tile.png"/></pattern></defs>'
     '<rect width="50" height="50" fill="#ffffff"/>', "fail:RASTER_EMBED",
     None, None),
    ("feimage_raster",
     '<defs><filter id="f"><feImage href="data:image/png;base64,iVBORw0KGgo="/>'
     '</filter></defs><rect width="10" height="10" fill="#000000"/>',
     "fail:RASTER_EMBED",
     "<image> was the whole check once; <feImage> used to pass silently",
     None),
    ("foreignobject_raster",
     '<foreignObject x="0" y="0" width="10" height="10">'
     '<div xmlns="http://www.w3.org/1999/xhtml">x</div></foreignObject>'
     '<rect width="5" height="5" fill="#000000"/>', "fail:RASTER_EMBED",
     "<foreignObject> rasterises XHTML/CSS", None),
    ("external_use_raster",
     '<use href="other.svg#thing"/><rect width="5" height="5" fill="#000000"/>',
     "fail:RASTER_EMBED",
     "a <use> naming another document cannot be audited here", None),
    ("data_image_in_style",
     '<style>.x { fill: url(data:image/png;base64,iVBORw0KGgo=); }</style>'
     '<rect width="5" height="5" class="x"/>', "fail:RASTER_EMBED",
     "a data: bitmap reached the file through a stylesheet", None),

    # --- gradients are a LAYER A hard rule when the spec bans them -----------
    ("gradient_declared",
     '<defs><linearGradient id="g"><stop offset="0" stop-color="#000000"/>'
     '<stop offset="1" stop-color="#ffffff"/></linearGradient></defs>'
     '<rect width="10" height="10" fill="#000000"/>',
     "fail:GRADIENT_NOT_ALLOWED", None, None),
    ("gradient_paint_reference",
     '<defs><linearGradient id="g"><stop offset="0" stop-color="#000000"/>'
     '<stop offset="1" stop-color="#ffffff"/></linearGradient></defs>'
     '<rect width="10" height="10" fill="url(#g)"/>',
     "fail:GRADIENT_NOT_ALLOWED",
     "the PAINT SITE must be reported too, not only the declaration", None),
    ("radial_gradient_declared",
     '<defs><radialGradient id="r"><stop offset="0" stop-color="#000000"/>'
     '<stop offset="1" stop-color="#ffffff"/></radialGradient></defs>'
     '<rect width="10" height="10" fill="#000000"/>',
     "fail:GRADIENT_NOT_ALLOWED", None, None),
    ("paint_server_reference_in_style_block",
     '<style>.graded { fill: url(#g); }</style>'
     '<defs><linearGradient id="g"><stop offset="0" stop-color="#000000"/>'
     '<stop offset="1" stop-color="#ffffff"/></linearGradient></defs>'
     '<rect width="10" height="10" class="graded"/>',
     "fail:GRADIENT_NOT_ALLOWED", "a <style> block is a declaration site too",
     None),
    ("unresolvable_paint_reference",
     '<rect width="10" height="10" fill="url(#nowhere)"/>',
     "fail:GRADIENT_NOT_ALLOWED",
     "'this paints with something I cannot read' is never a silent pass", None),
    ("gradients_allowed_passes",
     '<defs><linearGradient id="g"><stop offset="0" stop-color="#000000"/>'
     '<stop offset="1" stop-color="#ffffff"/></linearGradient></defs>'
     '<rect width="10" height="10" fill="url(#g)"/>', "pass",
     "CMYK process work may carry tone: allow_gradients=true says so",
     {"max_colors": 6,
      "geometry": {"allow_gradients": True, "allow_raster_embed": False,
                   "allow_open_paths": False, "min_stroke_width_pt": 1.5}}),

    # --- advisories must NEVER fail ------------------------------------------
    # They are written to `notes`, not `failures`.  Routing one through
    # `failures` would silently turn it into a hard gate, because
    # preflight.classify() fails SAFE TO HARD for a rule it does not know.
    ("effect_reference_is_advisory",
     '<defs><filter id="blur"><feGaussianBlur stdDeviation="1"/></filter></defs>'
     '<rect width="10" height="10" fill="#000000" filter="url(#blur)"/>',
     "pass", "EFFECT_REFERENCE is a note: layer A cannot measure a filter", None),
    ("translucent_paint_is_advisory",
     '<rect width="10" height="10" fill="#000000" opacity="0.5"/>', "pass",
     "TRANSLUCENT_PAINT is a note -- a spot-colour screen prints a tint", None),
    ("unchecked_definition_is_advisory",
     '<defs><path id="unused" d="M0,0 L5,0 L5,5 Z" stroke="#000000" '
     'stroke-width="0.1pt"/></defs><rect width="5" height="5" '
     'fill="#000000"/>', "pass",
     "unreferenced <defs> content cannot print, so a hairline in it is not a "
     "defect -- coverage is reported as partial instead", None),

    # --- parse-level cases ----------------------------------------------------
    ("bom_utf8", '\ufeff<path d="M0,0 L5,0 L5,5 Z" fill="#000000"/>', "pass",
     "the BOM goes before the <svg> root, not into the middle of the document",
     None),
    ("nested_svg_open_path",
     '<svg xmlns="http://www.w3.org/2000/svg" x="0" y="0" width="50" '
     'height="50"><path d="M0,0 L5,5"/></svg>', "fail:OPEN_PATH", None, None),
    ("external_entity_not_resolved",
     '<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY xxe SYSTEM '
     '"file:///etc/passwd">]><svg xmlns="http://www.w3.org/2000/svg">'
     '<text>&xxe;</text></svg>', "any-not:root:",
     "the entity must not be resolved: no file contents may reach the output",
     None),
    ("malformed_xml",
     '<svg xmlns="http://www.w3.org/2000/svg"><rect width="10"',
     "fail:SVG_PARSE_ERROR", None, None),
    ("not_an_svg_root",
     '<html xmlns="http://www.w3.org/1999/xhtml"><body/></html>',
     "fail:SVG_PARSE_ERROR", None, None),
    ("scaled_transform_not_applied",
     '<g transform="scale(0.1)"><path d="M0,0 L100,0 L100,100 Z" '
     'stroke="#000000" stroke-width="1"/></g>', "fail:MIN_STROKE_WIDTH",
     "documented limit: measured in user units, the scale is NOT flattened, so "
     "1 unit reads as 0.75pt", None),
    ("missing_geometry_section",
     '<path id="l" d="M0,0 L5,5" stroke="#000000" stroke-width="0.1pt"/>',
     "fail:OPEN_PATH", "defaults must be stated as NOTEs", {"max_colors": 2}),
]

def is_document(body):
    """True when the case body is already a whole document (not a fragment)."""
    match = _ROOT_RE.match(body.lstrip("\ufeff"))
    return bool(match) and match.group(1).lower() in DOCUMENT_ROOTS


def build_document(body):
    """The bytes to write for one case body, BOM first when there is one."""
    bom = body.startswith("\ufeff")
    if bom:
        body = body[1:]
    if is_document(body):
        return ("\ufeff" if bom else "") + body
    return ("\ufeff" if bom else "") + SVG_OPEN + body + "\n</svg>\n"


def evaluate(expect, returncode, output):
    """Did run behave as expected?  Returns (ok, why_not)."""
    forbid = None
    if "-not:" in expect:
        expect, forbid = expect.split("-not:", 1)
    if forbid and forbid in output:
        return False, "output contains %r, which it must not" % forbid
    if expect.startswith("fail:"):
        rule = expect.split(":", 1)[1]
        if returncode == 0:
            return False, "expected a non-zero exit carrying [%s]" % rule
        if ("[%s]" % rule) not in output:
            return False, "no [%s] in the output" % rule
        return True, None
    if expect == "pass":
        if returncode != 0:
            return False, "expected exit 0"
        return True, None
    return True, None


def build_case(case, directory, shared_spec):
    name, body, expect, note, spec_override = case
    svg_path = os.path.join(directory, name + ".svg")
    with open(svg_path, "w", encoding="utf-8") as handle:
        handle.write(build_document(body))

    if spec_override is None:
        spec_path = shared_spec
    else:
        spec_path = os.path.join(directory, name + ".spec.json")
        with open(spec_path, "w", encoding="utf-8") as handle:
            json.dump(spec_override, handle, indent=2)
    return name, svg_path, spec_path, expect, note


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cmd", required=True,
                        help="validator command template with {svg} and {spec}")
    parser.add_argument("--spec", help="spec.json to share across cases")
    parser.add_argument("--dir", help="directory to generate fixtures into")
    args = parser.parse_args()

    if "{svg}" not in args.cmd or "{spec}" not in args.cmd:
        parser.error("--cmd must contain both {svg} and {spec}")

    directory = args.dir or tempfile.mkdtemp(prefix="svg-probe-")
    if args.dir:
        os.makedirs(directory, exist_ok=True)
    print("fixtures: %s" % directory)

    if args.spec:
        shared_spec = args.spec
    else:
        shared_spec = os.path.join(directory, "spec.json")
        with open(shared_spec, "w", encoding="utf-8") as handle:
            json.dump(DEFAULT_SPEC, handle, indent=2)

    problems = []
    for case in CASES:
        name, svg_path, spec_path, expect, note = build_case(case, directory,
                                                             shared_spec)
        command = args.cmd.format(svg=shlex.quote(svg_path),
                                  spec=shlex.quote(spec_path))
        completed = subprocess.run(command, shell=True, capture_output=True,
                                   text=True)
        output = (completed.stdout or "") + (completed.stderr or "")
        ok, why = evaluate(expect, completed.returncode, output)

        print("%-11s %-32s exit=%d expected=%s" % (
            "OK" if ok else "UNEXPECTED", name, completed.returncode,
            expect if not why else "%s  (%s)" % (expect, why)))
        if not ok:
            problems.append(name)
            for line in output.strip().splitlines()[:6]:
                print("             | %s" % line)
        if note:
            print("             note: %s" % note)
        if ok and completed.returncode not in (0, 1, 2):
            print("             warning: exit code %d is not a documented code"
                  % completed.returncode)

    print("\n%d/%d cases behaved as expected"
          % (len(CASES) - len(problems), len(CASES)))
    if problems:
        print("review these: %s" % ", ".join(problems))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
