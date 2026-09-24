#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_validate_svg.py -- pytest suite for validate_svg.py.

Covers one passing document and every failure mode the validator must catch:
raster embeds (plain <image> and <image> inside <pattern>), colour budget,
minimum stroke width (with unit conversion), open paths, zero-area /
zero-length shapes, and malformed XML.

Every test drives the real CLI entry point (``validate_svg.main``) and asserts
on both the exit code and the exact rule tag printed to stdout, so the tests
fail if the output contract drifts, not just if the logic does.

Run with:  python3 -m pytest test_validate_svg.py -v
"""

import json
import sys

import pytest

from validate_svg import main

# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------

STRICT_SPEC = {
    "max_colors": 6,
    "geometry": {
        "allow_raster_embed": False,
        "allow_open_paths": False,
        "min_stroke_width_pt": 1.5,
    },
}


def write(path, text):
    path.write_text(text, encoding="utf-8")
    return str(path)


@pytest.fixture
def spec_file(tmp_path):
    """A strict default spec; tests may override it."""
    def _make(spec=None):
        target = tmp_path / "spec.json"
        target.write_text(json.dumps(spec if spec is not None else STRICT_SPEC),
                          encoding="utf-8")
        return str(target)
    return _make


@pytest.fixture
def svg_file(tmp_path):
    def _make(body, width="100", height="100", extra_root=""):
        doc = (
            '<svg xmlns="http://www.w3.org/2000/svg" '
            'xmlns:xlink="http://www.w3.org/1999/xlink" '
            'width="%s" height="%s" viewBox="0 0 %s %s"%s>\n%s\n</svg>\n'
            % (width, height, width, height, extra_root, body)
        )
        return write(tmp_path / "input.svg", doc)
    return _make


def run(capsys, svg_path, spec_path):
    """Invoke the CLI; return (exit_code, stdout)."""
    code = main([svg_path, spec_path])
    return code, capsys.readouterr().out


def assert_failure(capsys, svg_path, spec_path, rule):
    code, out = run(capsys, svg_path, spec_path)
    assert code == 1, "expected exit 1, got %d\n%s" % (code, out)
    assert out.startswith("VALIDATION FAILED for %s:" % svg_path), out
    assert "[%s]" % rule in out, "missing rule %s in:\n%s" % (rule, out)
    return out


# --------------------------------------------------------------------------
# 1. The happy path
# --------------------------------------------------------------------------

class TestPassingDocument:
    def test_clean_svg_passes(self, capsys, svg_file, spec_file):
        svg = svg_file(
            '<g fill="#ffffff" stroke="#000000" stroke-width="2">'
            '<rect x="10" y="10" width="80" height="80"/>'
            '</g>'
            '<path id="square" d="M0,0 L10,0 L10,10 L0,10 Z" '
            'fill="#ffffff" stroke="#000000" stroke-width="1.5pt"/>'
        )
        code, out = run(capsys, svg, spec_file())
        assert code == 0
        assert out.strip() == "VALIDATION PASSED for %s" % svg

    def test_at_limit_colour_count_passes(self, capsys, svg_file, spec_file):
        """Exactly max_colors is allowed; only exceeding it fails."""
        fills = ["#000000", "#111111", "#222222", "#333333", "#444444", "#555555"]
        body = "".join('<rect x="0" y="0" width="1" height="1" fill="%s"/>' % c
                       for c in fills)
        code, out = run(capsys, svg_file(body), spec_file())
        assert code == 0, out

    def test_stroke_at_exact_minimum_passes(self, capsys, svg_file, spec_file):
        """min_stroke_width_pt is inclusive: 1.5pt exactly is fine."""
        svg = svg_file('<rect width="10" height="10" stroke="#000000" '
                       'stroke-width="1.5pt"/>')
        code, out = run(capsys, svg, spec_file())
        assert code == 0, out


# --------------------------------------------------------------------------
# 2. Raster embeds
# --------------------------------------------------------------------------

class TestRasterEmbeds:
    def test_plain_image_element_fails(self, capsys, svg_file, spec_file):
        svg = svg_file('<image x="0" y="0" width="10" height="10" '
                       'xlink:href="data:image/png;base64,iVBORw0KGgo="/>')
        out = assert_failure(capsys, svg, spec_file(), "RASTER_EMBED")
        assert "Found 1 embedded raster image" in out

    def test_image_inside_pattern_fails(self, capsys, svg_file, spec_file):
        svg = svg_file(
            '<defs><pattern id="p" width="4" height="4" '
            'patternUnits="userSpaceOnUse">'
            '<image x="0" y="0" width="4" height="4" href="tile.png"/>'
            '</pattern></defs>'
            '<rect width="50" height="50" fill="url(#p)"/>'
        )
        out = assert_failure(capsys, svg, spec_file(), "RASTER_EMBED")
        assert "inside <pattern>" in out

    def test_multiple_images_counted(self, capsys, svg_file, spec_file):
        body = "".join('<image x="0" y="0" width="1" height="1" href="%d.png"/>' % i
                       for i in range(3))
        out = assert_failure(capsys, svg_file(body), spec_file(), "RASTER_EMBED")
        assert "Found 3 embedded raster images" in out

    def test_raster_allowed_by_spec_passes(self, capsys, svg_file, spec_file):
        spec = json.loads(json.dumps(STRICT_SPEC))
        spec["geometry"]["allow_raster_embed"] = True
        svg = svg_file('<image x="0" y="0" width="10" height="10" href="a.png"/>'
                       '<rect width="5" height="5" fill="#ffffff"/>')
        code, out = run(capsys, svg, spec_file(spec))
        assert code == 0, out

    def test_vector_only_document_has_no_raster_failure(self, capsys, svg_file,
                                                        spec_file):
        svg = svg_file('<use href="#sym"/><rect width="5" height="5" fill="#ffffff"/>')
        code, out = run(capsys, svg, spec_file())
        assert code == 0, out


# --------------------------------------------------------------------------
# 3. Colour count
# --------------------------------------------------------------------------

class TestColorCount:
    def test_too_many_colours_fails(self, capsys, svg_file, spec_file):
        colours = ["#000000", "#ff0000", "#00ff00", "#0000ff", "#ffff00",
                   "#ff00ff", "#00ffff", "#123456"]
        body = "".join('<rect x="0" y="0" width="1" height="1" fill="%s"/>' % c
                       for c in colours)
        out = assert_failure(capsys, svg_file(body), spec_file(), "COLOR_COUNT")
        assert "Color count 8 exceeds max 6" in out

    def test_short_hex_normalised_to_six_digits(self, capsys, svg_file, spec_file):
        """#fff and #ffffff are one colour, not two."""
        body = ('<rect width="1" height="1" fill="#fff"/>'
                '<rect width="1" height="1" fill="#FFFFFF"/>')
        code, out = run(capsys, svg_file(body), spec_file())
        assert code == 0, out

    def test_stroke_and_stop_color_counted(self, capsys, svg_file, spec_file):
        colours = ["#000000", "#ff0000", "#00ff00", "#0000ff", "#ffff00",
                   "#ff00ff", "#00ffff"]
        body = "".join('<rect width="1" height="1" stroke="%s" stroke-width="2"/>' % c
                       for c in colours[:4])
        body += (
            '<defs><linearGradient id="g">'
            + "".join('<stop offset="%d" stop-color="%s"/>' % (i, c)
                      for i, c in enumerate(colours[4:]))
            + '</linearGradient></defs>'
        )
        out = assert_failure(capsys, svg_file(body), spec_file(), "COLOR_COUNT")
        assert "Color count 7 exceeds max 6" in out

    def test_rgb_and_named_colours_normalise(self, capsys, svg_file, spec_file):
        """rgb(255,0,0), #ff0000 and 'red' collapse to a single colour."""
        body = ('<rect width="1" height="1" fill="rgb(255,0,0)"/>'
                '<rect width="1" height="1" fill="#FF0000"/>'
                '<rect width="1" height="1" fill="red"/>'
                '<rect width="1" height="1" fill="#00f"/>'
                '<rect width="1" height="1" fill="none"/>'
                '<rect width="1" height="1" fill="transparent"/>'
                '<rect width="1" height="1" fill="currentColor"/>')
        code, out = run(capsys, svg_file(body), spec_file())
        assert code == 0, out

    def test_colour_in_style_block_counted(self, capsys, svg_file, spec_file):
        colours = ["#000000", "#ff0000", "#00ff00", "#0000ff", "#ffff00",
                   "#ff00ff", "#00ffff"]
        body = "<style>" + " ".join(".c%d { fill: %s; }" % (i, c)
                                    for i, c in enumerate(colours)) + "</style>"
        body += "".join('<rect class="c%d" width="1" height="1"/>' % i
                        for i in range(len(colours)))
        out = assert_failure(capsys, svg_file(body), spec_file(), "COLOR_COUNT")
        assert "Color count 7 exceeds max 6" in out

    def test_inline_style_fill_counted(self, capsys, svg_file, spec_file):
        """An inline style must beat the presentation attribute."""
        body = ('<rect width="1" height="1" fill="#000000" '
                'style="fill:#ff0000"/>')
        spec = {"max_colors": 1, "geometry": {"min_stroke_width_pt": 0,
                                             "allow_open_paths": True,
                                             "allow_raster_embed": False}}
        code, out = run(capsys, svg_file(body), spec_file(spec))
        assert code == 0, out


# --------------------------------------------------------------------------
# 4. Minimum stroke width
# --------------------------------------------------------------------------

class TestStrokeWidth:
    def test_thin_stroke_fails_with_numbers(self, capsys, svg_file, spec_file):
        svg = svg_file('<path id="rule" d="M0,0 L10,10" stroke="#000000" '
                       'stroke-width="0.5pt"/>')
        out = assert_failure(capsys, svg, spec_file(), "MIN_STROKE_WIDTH")
        assert "Path id='rule'" in out
        assert "stroke-width 0.5pt" in out
        assert "below minimum 1.5pt" in out
        assert "from attr" in out

    def test_unitless_value_is_treated_as_px(self, capsys, svg_file, spec_file):
        """1.5 (no unit) is 1.5px = 1.125pt, so it must fail a 1.5pt minimum."""
        svg = svg_file('<rect width="10" height="10" stroke="#000000" '
                       'stroke-width="1.5"/>')
        out = assert_failure(capsys, svg, spec_file(), "MIN_STROKE_WIDTH")
        assert "stroke-width 1.125pt" in out

    @pytest.mark.parametrize("value,points", [
        ("0.5pt", 0.5),      # explicit points
        ("0.5px", 0.375),    # px -> pt = 0.75
        ("0.1mm", 0.283465),  # mm -> pt ~= 2.8346
        ("0.01cm", 0.283465),
        ("0.005in", 0.36),
        ("0.5", 0.375),      # unitless defaults to px
    ])
    def test_unit_conversion_detects_thin_strokes(self, capsys, svg_file,
                                                  spec_file, value, points):
        svg = svg_file('<rect width="10" height="10" stroke="#000000" '
                       'stroke-width="%s"/>' % value)
        out = assert_failure(capsys, svg, spec_file(), "MIN_STROKE_WIDTH")
        assert ("%spt" % ("%.6f" % points).rstrip("0").rstrip(".")) in out, out

    @pytest.mark.parametrize("value,points", [
        ("2pt", 2.0),
        ("2px", 1.5),
        ("1mm", 2.834646),
        ("0.1cm", 2.834646),
        ("0.05in", 3.6),
        ("2", 1.5),
    ])
    def test_adequate_strokes_pass(self, capsys, svg_file, spec_file, value, points):
        svg = svg_file('<rect width="10" height="10" stroke="#000000" '
                       'stroke-width="%s"/>' % value)
        code, out = run(capsys, svg, spec_file())
        assert code == 0, out

    def test_missing_stroke_width_defaults_to_one_px(self, capsys, svg_file,
                                                     spec_file):
        """No stroke-width => 1px => 0.75pt, which is below the 1.5pt minimum."""
        svg = svg_file('<rect width="10" height="10" stroke="#000000"/>')
        out = assert_failure(capsys, svg, spec_file(), "MIN_STROKE_WIDTH")
        assert "stroke-width 0.75pt" in out
        assert "default (1px)" in out

    def test_inline_style_stroke_width_checked(self, capsys, svg_file, spec_file):
        ok = svg_file('<path id="s" d="M0,0 L10,10 L0,10 Z" '
                      'style="stroke:#000000; stroke-width:2pt"/>')
        code, out = run(capsys, ok, spec_file())
        assert code == 0, out

        thin = svg_file('<path id="s" d="M0,0 L10,10 L0,10 Z" stroke="#000000" '
                        'stroke-width="9pt" style="stroke-width:0.5mm"/>')
        out = assert_failure(capsys, thin, spec_file(), "MIN_STROKE_WIDTH")
        assert "0.5mm" in out
        assert "Path id='s'" in out

    def test_stroke_none_is_not_checked(self, capsys, svg_file, spec_file):
        svg = svg_file('<rect width="10" height="10" stroke="none" '
                       'stroke-width="0.1pt"/>')
        code, out = run(capsys, svg, spec_file())
        assert code == 0, out

    def test_inherited_stroke_width_checked(self, capsys, svg_file, spec_file):
        """A path stroked by its parent <g> at 0.5pt must still fail."""
        svg = svg_file('<g stroke="#000000" stroke-width="0.5pt">'
                       '<path id="inherits" d="M0,0 L10,10"/></g>')
        out = assert_failure(capsys, svg, spec_file(), "MIN_STROKE_WIDTH")
        assert "id='inherits'" in out

    def test_percentage_stroke_width_reported_not_ignored(self, capsys, svg_file,
                                                          spec_file):
        svg = svg_file('<rect width="10" height="10" stroke="#000000" '
                       'stroke-width="10%"/>')
        out = assert_failure(capsys, svg, spec_file(), "MIN_STROKE_WIDTH")
        assert "percentage" in out

    def test_svg_without_strokes_passes_even_with_minimum(self, capsys, svg_file,
                                                          spec_file):
        svg = svg_file('<rect width="10" height="10" fill="#000000"/>')
        code, out = run(capsys, svg, spec_file())
        assert code == 0, out


# --------------------------------------------------------------------------
# 5. Geometry: open paths and degenerate shapes
# --------------------------------------------------------------------------

class TestGeometry:
    def test_open_path_fails(self, capsys, svg_file, spec_file):
        svg = svg_file('<path id="outline" d="M10,10 L20,10 L20,20"/>')
        out = assert_failure(capsys, svg, spec_file(), "OPEN_PATH")
        assert "Path id='outline' is not closed" in out

    def test_closed_path_passes(self, capsys, svg_file, spec_file):
        svg = svg_file('<path id="ok" d="M10,10 L20,10 L20,20 Z"/>')
        code, out = run(capsys, svg, spec_file())
        assert code == 0, out

    def test_open_paths_allowed_by_spec_passes(self, capsys, svg_file, spec_file):
        spec = json.loads(json.dumps(STRICT_SPEC))
        spec["geometry"]["allow_open_paths"] = True
        spec["geometry"]["min_stroke_width_pt"] = 0
        svg = svg_file('<path id="line" d="M0,0 L50,50" stroke="#000000" '
                       'stroke-width="2"/>')
        code, out = run(capsys, svg, spec_file(spec))
        assert code == 0, out

    def test_partially_open_multi_subpath_fails(self, capsys, svg_file, spec_file):
        svg = svg_file('<path id="mixed" d="M0,0 L10,0 L10,10 Z M20,20 L30,20"/>')
        out = assert_failure(capsys, svg, spec_file(), "OPEN_PATH")
        assert "1 open subpath" in out

    def test_zero_area_collapsed_shape_fails(self, capsys, svg_file, spec_file):
        """Closed, but a straight line: encloses no area, cannot print."""
        svg = svg_file('<path id="sliver" d="M10,10 L20,20 Z"/>')
        out = assert_failure(capsys, svg, spec_file(), "ZERO_AREA_PATH")
        assert "id='sliver'" in out
        assert "zero area" in out

    def test_zero_length_shape_fails(self, capsys, svg_file, spec_file):
        svg = svg_file('<path id="dot" d="M10,10 L10,10 Z"/>')
        out = assert_failure(capsys, svg, spec_file(), "ZERO_AREA_PATH")
        assert "id='dot'" in out

    def test_malformed_path_data_reported(self, capsys, svg_file, spec_file):
        svg = svg_file('<path id="broken" d="M0,0 Q q zzz"/>')
        out = assert_failure(capsys, svg, spec_file(), "PATH_PARSE_ERROR")
        assert "id='broken'" in out

    def test_path_without_d_fails_as_zero_area(self, capsys, svg_file, spec_file):
        """A <path> that renders nothing is an empty (zero-area) shape."""
        svg = svg_file('<path id="empty"/>')
        out = assert_failure(capsys, svg, spec_file(), "ZERO_AREA_PATH")
        assert "draws nothing" in out

    def test_blank_d_fails_as_zero_area(self, capsys, svg_file, spec_file):
        svg = svg_file('<path id="blank" d="   "/>')
        out = assert_failure(capsys, svg, spec_file(), "ZERO_AREA_PATH")
        assert "'d' attribute is blank" in out

    def test_moveto_only_path_fails_as_zero_area(self, capsys, svg_file, spec_file):
        svg = svg_file('<path id="justmove" d="M10,10"/>')
        out = assert_failure(capsys, svg, spec_file(), "ZERO_AREA_PATH")
        assert "no drawable segments" in out


# --------------------------------------------------------------------------
# 6. Malformed XML and malformed specs
# --------------------------------------------------------------------------

class TestBadInputs:
    def test_malformed_xml_fails_cleanly(self, capsys, tmp_path, spec_file):
        broken = write(tmp_path / "broken.svg",
                       '<svg xmlns="http://www.w3.org/2000/svg"><rect width="10"')
        out = assert_failure(capsys, broken, spec_file(), "SVG_PARSE_ERROR")
        assert "malformed XML" in out

    def test_unclosed_tag_fails_cleanly(self, capsys, tmp_path, spec_file):
        broken = write(tmp_path / "unclosed.svg",
                       '<svg xmlns="http://www.w3.org/2000/svg">'
                       '<g><rect width="1" height="1"/></svg>')
        assert_failure(capsys, broken, spec_file(), "SVG_PARSE_ERROR")

    def test_missing_svg_file_fails(self, capsys, tmp_path, spec_file):
        out = assert_failure(capsys, str(tmp_path / "nope.svg"), spec_file(),
                             "INPUT_ERROR")
        assert "not found" in out

    def test_missing_spec_file_fails(self, capsys, svg_file, tmp_path):
        svg = svg_file('<rect width="1" height="1"/>')
        assert_failure(capsys, svg, str(tmp_path / "nope.json"), "INPUT_ERROR")

    def test_invalid_spec_json_fails(self, capsys, svg_file, tmp_path):
        bad = write(tmp_path / "bad.json", "{ max_colors: 3, }")
        assert_failure(capsys, svg_file('<rect width="1" height="1"/>'), bad,
                       "SPEC_ERROR")

    def test_non_svg_root_fails(self, capsys, tmp_path, spec_file):
        doc = write(tmp_path / "html.svg",
                    '<html xmlns="http://www.w3.org/1999/xhtml"><body/></html>')
        assert_failure(capsys, doc, spec_file(), "SVG_PARSE_ERROR")

    def test_xml_entity_expansion_not_resolved(self, capsys, tmp_path, spec_file):
        """External entities must not be fetched; resolve_entities=False."""
        doc = ('<?xml version="1.0"?><!DOCTYPE svg ['
               '<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
               '<svg xmlns="http://www.w3.org/2000/svg">'
               '<text id="t">&xxe;</text></svg>')
        target = write(tmp_path / "xxe.svg", doc)
        code, out = run(capsys, target, spec_file())
        assert "root:x:" not in out
        assert code in (0, 1)


# --------------------------------------------------------------------------
# 7. Cascade, spec defaults and reporting behaviour
# --------------------------------------------------------------------------

class TestSpecAndReporting:
    def test_css_class_rule_applies_stroke_width(self, capsys, svg_file, spec_file):
        svg = svg_file(
            '<style>.thin { stroke: #000000; stroke-width: 0.5pt; }</style>'
            '<path id="csspath" class="thin" d="M0,0 L10,10"/>'
        )
        out = assert_failure(capsys, svg, spec_file(), "MIN_STROKE_WIDTH")
        assert "id='csspath'" in out

    def test_descendant_selector_applied(self, capsys, svg_file, spec_file):
        """'g .y' must reach the path inside the <g>, not be skipped."""
        svg = svg_file(
            '<style>g .y { stroke: #000000; stroke-width: 0.5pt; }</style>'
            '<g><path id="inner" class="y" d="M0,0 L10,10 L0,10 Z"/></g>'
        )
        out = assert_failure(capsys, svg, spec_file(), "MIN_STROKE_WIDTH")
        assert "id='inner'" in out

    def test_descendant_selector_does_not_leak_outside_ancestor(self,
                                                                capsys, svg_file,
                                                                spec_file):
        """The same rule must NOT apply to a path with no matching ancestor."""
        svg = svg_file(
            '<style>g .y { stroke: #000000; stroke-width: 0.5pt; }</style>'
            '<path id="outside" class="y" d="M0,0 L10,10 L0,10 Z"/>'
        )
        code, out = run(capsys, svg, spec_file())
        assert code == 0, out

    def test_specificity_beats_source_order(self, capsys, svg_file, spec_file):
        """#id (1,0,0) wins over .cls (0,1,0) even though the class comes later."""
        svg = svg_file(
            '<style>.s { stroke: #000000; stroke-width: 0.5pt; }'
            ' #keep { stroke-width: 4pt; }</style>'
            '<path id="keep" class="s" d="M0,0 L10,10 L0,10 Z"/>'
        )
        code, out = run(capsys, svg, spec_file())
        assert code == 0, out

    def test_media_print_rules_are_applied(self, capsys, svg_file, spec_file):
        svg = svg_file(
            '<style>@media print { .x { stroke: #000; stroke-width: 0.5pt; } }'
            '</style><path id="pr" class="x" d="M0,0 L10,10 L0,10 Z"/>'
        )
        out = assert_failure(capsys, svg, spec_file(), "MIN_STROKE_WIDTH")
        assert "id='pr'" in out

    def test_media_screen_rules_are_ignored(self, capsys, svg_file, spec_file):
        """A screen-only rule must not be applied to a print file."""
        svg = svg_file(
            '<style>@media screen { .x { stroke: #000; stroke-width: 9pt; } }'
            '.x { stroke: #000; stroke-width: 4pt; }</style>'
            '<path id="sc" class="x" d="M0,0 L10,10 L0,10 Z"/>'
        )
        code, out = run(capsys, svg, spec_file())
        assert code == 0, out

    def test_keyframe_and_font_face_blocks_ignored(self, capsys, svg_file, spec_file):
        svg = svg_file(
            '<style>@keyframes fade { 0% { stroke-width: 0.1pt; } }'
            '@font-face { font-family: x; }</style>'
            '<path id="kf" d="M0,0 L10,10 L0,10 Z" stroke="#000" stroke-width="4pt"/>'
        )
        code, out = run(capsys, svg, spec_file())
        assert code == 0, out

    def test_child_combinator_selectors_are_skipped(self, capsys, svg_file,
                                                    spec_file):
        """Unsupported selector forms must be ignored, never mis-applied."""
        svg = svg_file(
            '<style>g > .z { stroke: #000; stroke-width: 0.5pt; }</style>'
            '<g><path id="kid" class="z" d="M0,0 L10,10 L0,10 Z"/></g>'
        )
        code, out = run(capsys, svg, spec_file())
        assert code == 0, out

    def test_inline_style_overrides_css_rule(self, capsys, svg_file, spec_file):
        svg = svg_file(
            '<style>.s { stroke: #000000; stroke-width: 0.5pt; }</style>'
            '<path id="p" class="s" style="stroke-width:4pt" '
            'd="M0,0 L10,10 L0,10 Z"/>'
        )
        code, out = run(capsys, svg, spec_file())
        assert code == 0, out

    def test_multiple_failures_all_reported(self, capsys, svg_file, spec_file):
        """One bad document must surface every distinct rule, not just the first."""
        colours = ["#000000", "#ff0000", "#00ff00", "#0000ff", "#ffff00",
                   "#ff00ff", "#00ffff"]
        body = "".join('<rect x="0" y="0" width="1" height="1" fill="%s"/>' % c
                       for c in colours)
        body += ('<image x="0" y="0" width="1" height="1" href="a.png"/>'
                 '<path id="thin" d="M0,0 L10,10" stroke="#000000" '
                 'stroke-width="0.5pt"/>'
                 '<path id="open" d="M10,10 L20,10"/>'
                 '<path id="sliver" d="M10,10 L20,20 Z"/>')
        code, out = run(capsys, svg_file(body), spec_file())
        assert code == 1
        for rule in ("RASTER_EMBED", "COLOR_COUNT", "MIN_STROKE_WIDTH",
                     "OPEN_PATH", "ZERO_AREA_PATH"):
            assert "[%s]" % rule in out, "missing %s in:\n%s" % (rule, out)

    def test_missing_geometry_section_uses_defaults(self, capsys, svg_file,
                                                    tmp_path, spec_file):
        """No geometry section => raster and open paths disallowed, no min width."""
        svg = svg_file('<path id="line" d="M0,0 L10,10" stroke="#000000" '
                       'stroke-width="0.1pt"/>'
                       '<rect width="5" height="5" fill="#000000"/>')
        spec = spec_file({"max_colors": 6})
        code, out = run(capsys, svg, spec)
        assert code == 1
        assert "[OPEN_PATH]" in out
        assert "[MIN_STROKE_WIDTH]" not in out      # min width defaults to 0
        assert "NOTE:" in out

    def test_missing_max_colors_skips_colour_limit(self, capsys, svg_file, spec_file):
        body = "".join('<rect width="1" height="1" fill="#%02x%02x%02x"/>'
                       % (i, i, i) for i in range(20))
        spec = {"geometry": {"allow_open_paths": True,
                             "allow_raster_embed": False,
                             "min_stroke_width_pt": 0}}
        code, out = run(capsys, svg_file(body), spec_file(spec))
        assert code == 0, out
        assert "[COLOR_COUNT]" not in out

    def test_colour_count_is_exact_not_approximate(self, capsys, svg_file, spec_file):
        """Deliberate over-count guard: duplicates must not inflate the total.

        #abc, #ABC, #aabbcc and rgb(170,187,204) are all the same colour
        (#abc expands to #aabbcc, not to #abcdef).
        """
        body = ('<rect width="1" height="1" fill="#abc"/>'
                '<rect width="1" height="1" fill="#ABC"/>'
                '<rect width="1" height="1" fill="#aabbcc"/>'
                '<rect width="1" height="1" fill="rgb(170,187,204)"/>')
        spec = {"max_colors": 1, "geometry": {"allow_open_paths": True,
                                             "allow_raster_embed": False,
                                             "min_stroke_width_pt": 0}}
        code, out = run(capsys, svg_file(body), spec_file(spec))
        assert code == 0, out

    def test_distinct_short_hex_are_separate_colours(self, capsys, svg_file,
                                                    spec_file):
        """#abc and #abcdef differ; the short form must not be mis-expanded."""
        body = ('<rect width="1" height="1" fill="#abc"/>'
                '<rect width="1" height="1" fill="#abcdef"/>')
        spec = {"max_colors": 1, "geometry": {"allow_open_paths": True,
                                             "allow_raster_embed": False,
                                             "min_stroke_width_pt": 0}}
        out = assert_failure(capsys, svg_file(body), spec_file(spec), "COLOR_COUNT")
        assert "#aabbcc" in out and "#abcdef" in out

    def test_usage_error_returns_2(self, capsys):
        assert main([]) == 2
        assert main(["only.svg"]) == 2
        assert "usage:" in capsys.readouterr().out

    def test_help_returns_0(self, capsys):
        assert main(["--help"]) == 0
        assert "usage" in capsys.readouterr().out.lower()

    def test_success_output_is_single_line_on_stdout(self, capsys, svg_file,
                                                    spec_file):
        svg = svg_file('<rect width="5" height="5" fill="#000000"/>')
        code, out = run(capsys, svg, spec_file())
        assert code == 0
        assert out.strip().splitlines()[-1] == "VALIDATION PASSED for %s" % svg


# --------------------------------------------------------------------------
# 8. Unit resolution against the viewBox (v4.0 stage 4: "viewBox-aware unit
#    conversion").  The expected values below are NOT derived from the formula
#    -- they were MEASURED by rendering each document with Inkscape at 600 dpi
#    and integrating the ink coverage across the stroke.  They are the ground
#    truth the implementation has to match.
# --------------------------------------------------------------------------

def _scale_doc(tmp_path, width, height_mm, viewbox, stroke_width, y=10):
    doc = ('<svg xmlns="http://www.w3.org/2000/svg" width="%s" height="%smm" '
           'viewBox="%s"><path d="M10,%d L30,%d" stroke="#000000" '
           'stroke-width="%s"/></svg>' % (width, height_mm, viewbox, y, y,
                                          stroke_width))
    path = tmp_path / "scale.svg"
    path.write_text(doc, encoding="utf-8")
    return str(path)


def _reported_pt(tmp_path, capsys, width, height_mm, viewbox, stroke_width, y=10):
    """Run the validator with a huge minimum so the reported value is printed."""
    svg = _scale_doc(tmp_path, width, height_mm, viewbox, stroke_width, y)
    spec = tmp_path / "min.json"
    spec.write_text(json.dumps({
        "max_colors": 6,
        "geometry": {"allow_raster_open": True, "allow_open_paths": True,
                     "allow_raster_embed": False, "min_stroke_width_pt": 1000},
    }), encoding="utf-8")
    main([svg, str(spec)])
    out = capsys.readouterr().out
    for line in out.splitlines():
        if "has stroke-width" in line:
            return float(line.split("has stroke-width ")[1].split("pt")[0])
    raise AssertionError("no stroke-width reported:\n%s" % out)


class TestUnitScale:
    """A user unit is not a CSS px once a viewBox rescales the document.

    The old behaviour read every unitless/px length as 0.75pt. Measured against
    Inkscape, that was wrong in both directions: on an A4 document with a
    millimetre viewBox it under-reported by 3.8x (rejecting printable art), and
    on a document whose viewBox is larger than its physical size it
    over-reported (passing a hairline).
    """

    @pytest.mark.parametrize("width,height_mm,viewbox,stroke_width,measured_pt", [
        # A4 pixel viewBox: 1 unit = 1px, so 1pt is genuinely 1pt.
        ("595px", "297", "0 0 595 842", "1pt", 1.0009),
        # A4 millimetre viewBox: 1 unit = 1.0000mm.
        ("210mm", "297", "0 0 210 297", "1", 2.8353),
        ("210mm", "297", "0 0 210 297", "1pt", 3.7802),
        ("210mm", "297", "0 0 210 297", "1mm", 10.7139),
        # 595-unit viewBox on 210mm: 1 unit = 0.3529mm.
        ("210mm", "297", "0 0 595 842", "1", 1.0009),
        # viewBox ten times the physical size: 1 unit = 0.1000mm.
        ("100mm", "100", "0 0 1000 1000", "1", 0.2833),
        ("100mm", "100", "0 0 1000 1000", "1pt", 0.3784),
        # 1 unit = 1mm directly.
        ("100mm", "50", "0 0 100 50", "1", 2.8344),
    ])
    def test_reported_width_matches_the_render(self, tmp_path, capsys, width,
                                               height_mm, viewbox, stroke_width,
                                               measured_pt):
        got = _reported_pt(tmp_path, capsys, width, height_mm, viewbox,
                           stroke_width)
        assert abs(got - measured_pt) < 0.02, (
            "reported %.4fpt but Inkscape measures %.4fpt for width=%s "
            "viewBox=%r stroke-width=%r"
            % (got, measured_pt, width, viewbox, stroke_width))

    def test_no_viewbox_keeps_the_old_px_behaviour(self, tmp_path, capsys):
        """Without a viewBox, 1 user unit is still 1 CSS px = 0.75pt."""
        svg = tmp_path / "novb.svg"
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="100" '
                       'height="100"><path d="M10,10 L30,10" stroke="#000000" '
                       'stroke-width="1"/></svg>', encoding="utf-8")
        spec = tmp_path / "s.json"
        spec.write_text(json.dumps({
            "max_colors": 6,
            "geometry": {"allow_open_paths": True, "min_stroke_width_pt": 1000,
                         "allow_raster_embed": False}}), encoding="utf-8")
        main([str(svg), str(spec)])
        out = capsys.readouterr().out
        assert "stroke-width 0.75pt" in out

    def test_false_failure_is_fixed(self, tmp_path, capsys):
        """Regression: printable art must not be rejected.

        300mm with a 300-unit viewBox gives 1 unit = 1mm, so stroke-width="1"
        is 2.8346pt and passes a 1.5pt minimum. The old px assumption reported
        0.75pt and failed a perfectly printable file.
        """
        svg = _scale_doc(tmp_path, "300mm", "400", "0 0 300 400", "1")
        spec = tmp_path / "s.json"
        spec.write_text(json.dumps({
            "max_colors": 6,
            "geometry": {"allow_open_paths": True, "allow_raster_embed": False,
                         "min_stroke_width_pt": 1.5}}), encoding="utf-8")
        code = main([svg, str(spec)])
        out = capsys.readouterr().out
        assert code == 0, "false failure on printable art:\n%s" % out

    def test_false_pass_is_fixed(self, tmp_path, capsys):
        """Regression: a hairline must not be waved through.

        300mm with a 3000-unit viewBox gives 1 unit = 0.1mm, so
        stroke-width="3" is only 0.85pt. The old px assumption reported
        3 x 0.75 = 2.25pt and passed it.
        """
        svg = _scale_doc(tmp_path, "300mm", "400", "0 0 3000 4000", "3")
        spec = tmp_path / "s.json"
        spec.write_text(json.dumps({
            "max_colors": 6,
            "geometry": {"allow_open_paths": True, "allow_raster_embed": False,
                         "min_stroke_width_pt": 1.5}}), encoding="utf-8")
        code = main([svg, str(spec)])
        out = capsys.readouterr().out
        assert code == 1, "hairline waved through:\n%s" % out
        assert "[MIN_STROKE_WIDTH]" in out
        assert "0.850394pt" in out, out
        # The scale basis must be printed, so the number can be checked by hand.
        assert "1 user unit = 0.1000mm" in out, out

    def test_scale_basis_is_reported(self, tmp_path, capsys):
        """The reader must be able to check the number by hand."""
        svg = _scale_doc(tmp_path, "210mm", "297", "0 0 210 297", "0.5")
        spec = tmp_path / "s.json"
        spec.write_text(json.dumps({
            "max_colors": 6,
            "geometry": {"allow_open_paths": True, "allow_raster_embed": False,
                         "min_stroke_width_pt": 1.5}}), encoding="utf-8")
        main([svg, str(spec)])
        out = capsys.readouterr().out
        assert "1 user unit = 1.0000mm" in out, out

    def test_non_uniform_scale_is_noted(self, tmp_path, capsys):
        """x and y scales that differ need flagging, not silent guessing."""
        svg = tmp_path / "nu.svg"
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="200mm" '
                       'height="100mm" viewBox="0 0 100 100">'
                       '<path d="M10,10 L30,10" stroke="#000000" '
                       'stroke-width="2"/></svg>', encoding="utf-8")
        spec = tmp_path / "s.json"
        spec.write_text(json.dumps({
            "max_colors": 6,
            "geometry": {"allow_open_paths": True, "allow_raster_embed": False,
                         "min_stroke_width_pt": 0}}), encoding="utf-8")
        main([svg, str(spec)])
        out = capsys.readouterr().out
        assert "scales differ" in out, out


# --------------------------------------------------------------------------
# 9. The remaining v4.0 stage-4 rules
# --------------------------------------------------------------------------

def _spec_for(tmp_path, **geometry):
    spec = {"max_colors": 6, "geometry": {"allow_raster_embed": False,
                                         "allow_open_paths": True}}
    spec["geometry"].update(geometry)
    path = tmp_path / "g.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return str(path)


class TestNodeLimit:
    def test_path_over_the_node_limit_is_reported(self, tmp_path, capsys):
        # 40 line segments -> 40 nodes.
        d = "M0,0 " + " ".join("L%d,1" % i for i in range(1, 41)) + " Z"
        svg = tmp_path / "nodes.svg"
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="100" '
                       'height="100" viewBox="0 0 100 100"><path id="busy" d="%s" '
                       'fill="#000000"/></svg>' % d, encoding="utf-8")
        code = main([str(svg), _spec_for(tmp_path, max_nodes_per_path=10)])
        out = capsys.readouterr().out
        assert code == 1
        assert "[NODE_COUNT]" in out
        assert "id='busy'" in out

    def test_under_the_node_limit_passes(self, tmp_path, capsys):
        svg = tmp_path / "ok.svg"
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="100" '
                       'height="100" viewBox="0 0 100 100">'
                       '<path id="ok" d="M0,0 L10,0 L10,10 Z" fill="#000000"/>'
                       '</svg>', encoding="utf-8")
        code = main([str(svg), _spec_for(tmp_path, max_nodes_per_path=10)])
        assert code == 0, capsys.readouterr().out

    def test_many_violations_are_capped(self, tmp_path, capsys):
        body = "".join(
            '<path id="p%d" d="M0,0 %s Z" fill="#000000"/>'
            % (i, " ".join("L%d,1" % k for k in range(1, 21)))
            for i in range(9))
        svg = tmp_path / "many.svg"
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="100" '
                       'height="100" viewBox="0 0 100 100">%s</svg>' % body,
                       encoding="utf-8")
        main([str(svg), _spec_for(tmp_path, max_nodes_per_path=5)])
        out = capsys.readouterr().out
        node_lines = [l for l in out.splitlines() if "[NODE_COUNT]" in l]
        assert len(node_lines) == 6, "5 details plus one summary line: %s" % node_lines
        assert "and 4 more paths" in out


class TestDimensions:
    def test_wrong_size_fails(self, tmp_path, capsys):
        svg = tmp_path / "size.svg"
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="100mm" '
                       'height="100mm" viewBox="0 0 100 100">'
                       '<rect width="50" height="50" fill="#000000"/></svg>',
                       encoding="utf-8")
        spec = {"max_colors": 6, "dimensions": {"width_mm": 300, "height_mm": 400},
                "geometry": {"allow_raster_embed": False, "allow_open_paths": True}}
        path = tmp_path / "d.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        code = main([str(svg), str(path)])
        out = capsys.readouterr().out
        assert code == 1
        assert "[DIMENSIONS]" in out
        assert "100.00mm" in out and "300.00mm" in out

    def test_matching_size_passes(self, tmp_path, capsys):
        svg = tmp_path / "size2.svg"
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="300mm" '
                       'height="400mm" viewBox="0 0 300 400">'
                       '<rect width="150" height="100" fill="#000000"/></svg>',
                       encoding="utf-8")
        spec = {"max_colors": 6, "dimensions": {"width_mm": 300, "height_mm": 400},
                "geometry": {"allow_raster_embed": False, "allow_open_paths": True}}
        path = tmp_path / "d2.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        assert main([str(svg), str(path)]) == 0, capsys.readouterr().out


class TestPalette:
    def test_off_palette_colour_is_reported(self, tmp_path, capsys):
        svg = tmp_path / "pal.svg"
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="100" '
                       'height="100" viewBox="0 0 100 100">'
                       '<rect width="50" height="50" fill="#ff0000"/>'
                       '<rect x="50" width="50" height="50" fill="#3d3d3d"/>'
                       '</svg>', encoding="utf-8")
        spec = {"max_colors": 6, "palette": ["#000000", "#ff0000"],
                "geometry": {"allow_raster_embed": False, "allow_open_paths": True}}
        path = tmp_path / "p.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        code = main([str(svg), str(path)])
        out = capsys.readouterr().out
        assert code == 1
        assert "[PALETTE]" in out and "#3d3d3d" in out

    def test_on_palette_passes(self, tmp_path, capsys):
        svg = tmp_path / "pal2.svg"
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="100" '
                       'height="100" viewBox="0 0 100 100">'
                       '<rect width="50" height="50" fill="red"/>'
                       '<rect x="50" width="50" height="50" fill="#000"/></svg>',
                       encoding="utf-8")
        spec = {"max_colors": 6, "palette": ["#000000", "#ff0000"],
                "geometry": {"allow_raster_embed": False, "allow_open_paths": True}}
        path = tmp_path / "p2.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        # red expands to #ff0000 and #000 to #000000, so both are on palette
        assert main([str(svg), str(path)]) == 0, capsys.readouterr().out


class TestPrintMethod:
    def test_vinyl_requires_closed_paths(self, tmp_path, capsys):
        svg = tmp_path / "v.svg"
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="100" '
                       'height="100" viewBox="0 0 100 100">'
                       '<path id="cut" d="M10,10 L90,10" stroke="#000000" '
                       'stroke-width="2"/></svg>', encoding="utf-8")
        spec = {"max_colors": 6, "print_method": "vinyl",
                "geometry": {"allow_raster_embed": False,
                             "min_stroke_width_pt": 0}}
        path = tmp_path / "v.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        code = main([str(svg), str(path)])
        out = capsys.readouterr().out
        assert code == 1 and "[OPEN_PATH]" in out
        assert "vinyl" in out

    def test_screen_print_tolerates_open_strokes(self, tmp_path, capsys):
        svg = tmp_path / "s.svg"
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="100" '
                       'height="100" viewBox="0 0 100 100">'
                       '<path id="line" d="M10,10 L90,10" stroke="#000000" '
                       'stroke-width="2"/></svg>', encoding="utf-8")
        spec = {"max_colors": 6, "print_method": "screen_print",
                "geometry": {"allow_raster_embed": False,
                             "min_stroke_width_pt": 0}}
        path = tmp_path / "s2.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        assert main([str(svg), str(path)]) == 0, capsys.readouterr().out

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))