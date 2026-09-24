#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_snap_colors.py -- tests for the stage 3 colour snapper.

Colour snapping is the step that makes a 9-colour design printable on a 6-ink
screen job, so the two things worth pinning are:

* it writes the new colour to the place the cascade actually reads, including
  the awkward case of a colour that came from a stylesheet rule (which must NOT
  be edited, because it affects every element that matches it), and
* it refuses to snap a colour that is nowhere near the palette. A tool that
  guesses here silently recolours artwork.
"""

import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
for path in (ROOT, SCRIPTS):
    if path not in sys.path:
        sys.path.insert(0, path)

import validate_svg          # noqa: E402

PALETTE = ["#000000", "#FFFFFF", "#FF0000", "#0000FF"]


@pytest.fixture
def spec_file(tmp_path):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps({"max_colors": 6, "palette": PALETTE}), encoding="utf-8")
    return str(path)


def write(tmp_path, name, body, style="", rects=""):
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="100mm" '
           'viewBox="0 0 100 100">\n%s\n%s\n</svg>\n' % (style, body))
    path = tmp_path / name
    path.write_text(svg, encoding="utf-8")
    return str(path)


def run(svg, spec, out=None, extra=()):
    argv = [sys.executable, os.path.join(SCRIPTS, "snap_colors.py"), svg, spec]
    if out:
        argv += ["-o", out]
    argv += list(extra)
    return subprocess.run(argv, capture_output=True, text=True, cwd=ROOT)


class TestSnapping:
    def test_near_palette_attribute_colour_is_snapped(self, tmp_path, spec_file):
        svg = write(tmp_path, "a.svg",
                    '<rect width="50" height="50" fill="#fe0101"/>')
        proc = run(svg, spec_file, out=str(tmp_path / "out.svg"))
        assert proc.returncode == 0, proc.stdout + proc.stderr
        text = (tmp_path / "out.svg").read_text()
        assert "#ff0000" in text and "#fe0101" not in text
        assert "attribute" in proc.stdout

    def test_stylesheet_colour_is_overridden_not_rewritten(self, tmp_path,
                                                          spec_file):
        """Editing the rule would recolour every element it matches.

        So the inline attribute is used instead -- it wins in the cascade, and
        it is why the original rule text stays in the file untouched.
        """
        svg = write(tmp_path, "b.svg",
                    '<rect class="band" width="50" height="50"/>',
                    style='<style>.band { fill: #fe0101; }</style>')
        proc = run(svg, spec_file, out=str(tmp_path / "out.svg"))
        assert proc.returncode == 0, proc.stdout + proc.stderr
        text = (tmp_path / "out.svg").read_text()
        assert ".band { fill: #fe0101; }" in text, (
            "the stylesheet rule was edited; it must be left alone")
        assert 'style="fill:#ff0000"' in text
        assert "inline override of stylesheet rule" in proc.stdout

    def test_inline_style_is_updated_in_place(self, tmp_path, spec_file):
        svg = write(tmp_path, "c.svg",
                    '<rect width="50" height="50" style="fill:#fefefe"/>')
        run(svg, spec_file, out=str(tmp_path / "out.svg"))
        text = (tmp_path / "out.svg").read_text()
        assert "style=\"fill:#ffffff\"" in text

    def test_snapping_makes_the_validator_pass(self, tmp_path, spec_file):
        """The point of the exercise: an off-palette file becomes acceptable."""
        svg = write(tmp_path, "d.svg",
                    '<rect width="50" height="50" fill="#fe0101"/>'
                    '<rect x="50" width="50" height="50" fill="#000001"/>')
        before, _n, _s = validate_svg.validate(svg, spec_file)
        assert validate_svg.RULE_PALETTE in {r for r, _ in before}

        out = str(tmp_path / "out.svg")
        run(svg, spec_file, out=out)
        after, _n, stats = validate_svg.validate(out, spec_file)
        assert validate_svg.RULE_PALETTE not in {r for r, _ in after}
        assert stats["off_palette_colors"] == []

    def test_far_colour_is_reported_not_snapped(self, tmp_path, spec_file):
        svg = write(tmp_path, "e.svg",
                    '<rect width="50" height="50" fill="#2e4a62"/>')
        out = str(tmp_path / "out.svg")
        proc = run(svg, spec_file, out=out)
        assert proc.returncode == 1, proc.stdout
        assert "further than the tolerance" in proc.stdout
        assert "#2e4a62" in proc.stdout
        assert "#2e4a62" in (tmp_path / "out.svg").read_text()

    def test_force_snaps_anyway(self, tmp_path, spec_file):
        svg = write(tmp_path, "f.svg",
                    '<rect width="50" height="50" fill="#2e4a62"/>')
        out = str(tmp_path / "out.svg")
        proc = run(svg, spec_file, out=out, extra=["--force"])
        assert proc.returncode == 0, proc.stdout
        assert "#000000" in (tmp_path / "out.svg").read_text()

    def test_dry_run_writes_nothing(self, tmp_path, spec_file):
        svg = write(tmp_path, "g.svg",
                    '<rect width="50" height="50" fill="#fe0101"/>')
        proc = run(svg, spec_file, extra=["--dry-run"])
        assert proc.returncode == 0
        assert "dry run" in proc.stdout
        # no -o given and --dry-run, so even the input must be untouched
        assert "#fe0101" in open(svg).read()

    def test_missing_palette_is_a_usage_error(self, tmp_path):
        spec = tmp_path / "nopal.json"
        spec.write_text(json.dumps({"max_colors": 6}), encoding="utf-8")
        svg = write(tmp_path, "h.svg",
                    '<rect width="50" height="50" fill="#fe0101"/>')
        proc = run(svg, str(spec))
        assert proc.returncode == 2
        assert "no usable palette" in proc.stderr