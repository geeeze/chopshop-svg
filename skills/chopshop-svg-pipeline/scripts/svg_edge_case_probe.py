#!/usr/bin/env python3
"""Run an SVG validator over a batch of degenerate documents.

Every case declares what SHOULD happen (pass, or fail with a specific rule
tag). Mismatches are listed at the end, so a validator that quietly accepts a
file it should reject -- or crashes instead of reporting -- shows up before you
declare it done.

Usage:
  python3 svg_edge_case_probe.py --cmd ".venv/bin/python validate_svg.py {svg} {spec}"
  python3 svg_edge_case_probe.py --cmd "..." --spec spec.json --keep

The command template must contain {svg} and {spec}. Cases with their own spec
override get a per-case spec file; the rest share one written from --spec, or a
strict default. Exit code is 1 if any case behaved unexpectedly.

Stdlib only. Files are written under TMPDIR unless --dir is given.
"""

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile

SVG_OPEN = ('<svg xmlns="http://www.w3.org/2000/svg" '
            'xmlns:xlink="http://www.w3.org/1999/xlink" '
            'width="100" height="100" viewBox="0 0 100 100">\n')

DEFAULT_SPEC = {
    "max_colors": 6,
    "geometry": {
        "allow_raster_embed": False,
        "allow_open_paths": False,
        "min_stroke_width_pt": 1.5,
    },
}

# Each case: name, body (wrapped in <svg> unless it is already a document),
# expectation ('pass' | 'fail:RULE' | 'any'), optional note, spec override.
CASES = [
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

    ("stroke_none_not_checked",
     '<rect width="10" height="10" stroke="none" stroke-width="0.1pt"/>',
     "pass", None, None),
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

    ("raster_image", '<image x="0" y="0" width="10" height="10" href="a.png"/>'
     '<rect width="5" height="5" fill="#000000"/>', "fail:RASTER_EMBED",
     None, None),
    ("image_in_pattern",
     '<defs><pattern id="p" width="4" height="4" patternUnits="userSpaceOnUse">'
     '<image x="0" y="0" width="4" height="4" href="tile.png"/></pattern></defs>'
     '<rect width="50" height="50" fill="#ffffff"/>', "fail:RASTER_EMBED",
     None, None),

    ("colour_budget",
     "".join('<rect width="1" height="1" fill="#%06x"/>' % (i * 0x10203)
             for i in range(8)), "fail:COLOR_COUNT", None, None),
    ("short_hex_dedupes",
     '<rect width="1" height="1" fill="#abc"/>'
     '<rect width="1" height="1" fill="#aabbcc"/>'
     '<rect width="1" height="1" fill="rgb(170,187,204)"/>', "pass",
     "#abc expands to #aabbcc", None),
    ("ignored_paint_keywords",
     '<rect width="1" height="1" fill="none"/>'
     '<rect width="1" height="1" fill="transparent"/>'
     '<rect width="1" height="1" fill="currentColor"/>', "pass", None, None),

    ("bom_utf8", '\ufeff<path d="M0,0 L5,0 L5,5 Z" fill="#000000"/>', "pass",
     None, None),
    ("nested_svg_open_path",
     '<svg xmlns="http://www.w3.org/2000/svg" x="0" y="0" width="50" '
     'height="50"><path d="M0,0 L5,5"/></svg>', "fail:OPEN_PATH", None, None),
    ("external_entity_not_resolved",
     '<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY xxe SYSTEM '
     '"file:///etc/passwd">]><svg xmlns="http://www.w3.org/2000/svg">'
     '<text>&xxe;</text></svg>', "any",
     "output must not contain file contents", None),
    ("malformed_xml",
     '<svg xmlns="http://www.w3.org/2000/svg"><rect width="10"',
     "fail:SVG_PARSE_ERROR", None, None),
    ("not_an_svg_root",
     '<html xmlns="http://www.w3.org/1999/xhtml"><body/></html>',
     "fail:SVG_PARSE_ERROR", None, None),
    ("scaled_transform_not_applied",
     '<g transform="scale(0.1)"><path d="M0,0 L100,0 L100,100 Z" '
     'stroke="#000000" stroke-width="1"/></g>', "any",
     "documented limit: user units, transform not flattened", None),
    ("unknown_colour_keyword",
     '<rect width="5" height="5" fill="chartreusey"/>', "any",
     "unknown keyword must occupy a palette slot, not vanish", None),
    ("missing_geometry_section",
     '<path id="l" d="M0,0 L5,5" stroke="#000000" stroke-width="0.1pt"/>',
     "fail:OPEN_PATH", "defaults must be stated as NOTEs", {"max_colors": 2}),
]


def build_case(case, directory, shared_spec):
    name, body, expect, note, spec_override = case
    svg_path = os.path.join(directory, name + ".svg")
    is_document = body.lstrip().startswith(("<", "<?xml"))
    document = body if is_document else SVG_OPEN + body + "\n</svg>\n"
    with open(svg_path, "w", encoding="utf-8") as handle:
        handle.write(document)

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
        if expect.startswith("fail:"):
            rule = expect.split(":", 1)[1]
            ok = completed.returncode != 0 and ("[%s]" % rule) in output
        elif expect == "pass":
            ok = completed.returncode == 0
        else:
            ok = True

        print("%-11s %-32s exit=%d expected=%s"
              % ("OK" if ok else "UNEXPECTED", name, completed.returncode,
                 expect))
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
