#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_validate_svg_negative.py -- four deliberate-failure tests for validate_svg.py.

Every test here constructs an SVG in `tmp_path` that violates exactly one rule of
the spec, runs the real CLI as a subprocess, and asserts BOTH:

  * the process exits with code 1, and
  * stdout names the rule that fired, with the shape of the artwork's problem.

There are no happy-path tests in this file on purpose. A validator that returns
zero is only proven useful by what it rejects, and by how precisely it says why.

Why a subprocess rather than calling ``main()``
-----------------------------------------------
``main()`` returns an int; only the process can prove an *exit code*, which is
what the pipeline branches on (0 = send it, 1 = file is bad, 2 = you called me
wrongly). Running the script the way the pipeline runs it also exercises
argument parsing and output formatting, so the test fails if the observable
contract drifts, not merely if the logic does.

Two deliberate choices in the fixtures
--------------------------------------
* **XML validity.** Each SVG is well-formed. The tests are about *spec*
  violations, not broken markup; a malformed document would fail earlier with
  SVG_PARSE_ERROR and the test would pass for the wrong reason.
* **``width="100px"`` with ``viewBox="0 0 100 100"``.** That makes one user unit
  exactly one CSS px, so a ``pt`` length passes through unchanged. It matters:
  with ``width="100mm"`` the same ``stroke-width="0.1pt"`` would resolve to
  0.3779pt, because the viewBox rescales the document. The geometry is chosen so
  the asserted number means what it says.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "validate_svg.py"

# 1 user unit = 1px, so a pt length needs no viewBox conversion.
CANVAS = 'width="100px" height="100px" viewBox="0 0 100 100"'

# A real 1x1 PNG, base64, so the raster case is a genuine embedded bitmap.
TINY_PNG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
            "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def run_cli(tmp_path, body, spec):
    """Run validate_svg.py as the pipeline does. Returns (exit_code, stdout)."""
    svg = tmp_path / "artwork.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" %s>\n%s\n</svg>\n'
        % (CANVAS, body),
        encoding="utf-8",
    )
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(svg), str(spec_path)],
        capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout


def geometry(**overrides):
    """A permissive geometry block, so only the rule under test can fire."""
    block = {"allow_raster_embed": True,
             "allow_open_paths": True,
             "min_stroke_width_pt": 0}
    block.update(overrides)
    return {"max_colors": 100, "geometry": block}


# ---------------------------------------------------------------------------
# 1. Raster embeds
# ---------------------------------------------------------------------------

def test_fails_on_raster_embed(tmp_path):
    """An <image> must fail when the spec demands vector-only artwork."""
    body = '<image id="photo" x="0" y="0" width="10" height="10" href="%s"/>' % TINY_PNG
    spec = {"max_colors": 100,
            "geometry": {"allow_raster_embed": False,
                         "allow_open_paths": True,
                         "min_stroke_width_pt": 0}}

    code, out = run_cli(tmp_path, body, spec)

    assert code == 1, "expected exit 1, got %d\n%s" % (code, out)
    assert "VALIDATION FAILED" in out, out
    assert "[RASTER_EMBED]" in out, out
    assert "embedded raster image" in out, out
    assert "vector-only" in out, out     # the reason, not just the verdict


# ---------------------------------------------------------------------------
# 2. Colour budget
# ---------------------------------------------------------------------------

def test_fails_on_excessive_colors(tmp_path):
    """Seven distinct fills must fail a budget of six, listing all seven."""
    fills = ["#000000", "#FFFFFF", "#FF0000", "#00FF00",
             "#0000FF", "#FFFF00", "#00FFFF"]
    assert len(set(fills)) == 7, "the fixture must contain 7 distinct colours"
    body = "\n".join(
        '<rect x="%d" y="0" width="10" height="10" fill="%s"/>' % (i * 10, c)
        for i, c in enumerate(fills)
    )
    spec = {"max_colors": 6, "geometry": {"allow_raster_embed": True,
                                          "allow_open_paths": True,
                                          "min_stroke_width_pt": 0}}

    code, out = run_cli(tmp_path, body, spec)

    assert code == 1, "expected exit 1, got %d\n%s" % (code, out)
    assert "[COLOR_COUNT]" in out, out
    assert "Color count 7 exceeds max 6" in out, out
    # Every offending colour should be named, or the user cannot act on it.
    for colour in ("#000000", "#ffffff", "#ff0000"):
        assert colour in out, "%s missing from the message:\n%s" % (colour, out)


# ---------------------------------------------------------------------------
# 3. Hairline strokes
# ---------------------------------------------------------------------------

def test_fails_on_hairline_stroke(tmp_path):
    """A 0.1pt stroke must fail a 1.5pt minimum, and report both numbers."""
    body = ('<path id="hairline" d="M 10 10 L 90 10 L 90 90 L 10 90 Z" '
            'stroke="#FF0000" stroke-width="0.1pt" fill="none"/>')
    spec = {"max_colors": 100,
            "geometry": {"allow_raster_embed": True,
                         "allow_open_paths": True,
                         "min_stroke_width_pt": 1.5}}

    code, out = run_cli(tmp_path, body, spec)

    assert code == 1, "expected exit 1, got %d\n%s" % (code, out)
    assert "[MIN_STROKE_WIDTH]" in out, out
    assert "stroke-width 0.1pt" in out, out        # what it is
    assert "below minimum 1.5pt" in out, out       # against what
    assert "hairline" in out, out                  # and which element


# ---------------------------------------------------------------------------
# 4. Open paths (cutting jobs)
# ---------------------------------------------------------------------------

def test_fails_on_open_path_vinyl(tmp_path):
    """An unclosed path must fail when open paths are disallowed."""
    body = ('<path id="cutline" d="M 20 20 L 80 80" stroke="#0000FF" '
            'stroke-width="2pt" fill="none"/>')
    spec = {"max_colors": 100,
            "geometry": {"allow_raster_embed": True,
                         "allow_open_paths": False,
                         "min_stroke_width_pt": 0}}

    code, out = run_cli(tmp_path, body, spec)

    assert code == 1, "expected exit 1, got %d\n%s" % (code, out)
    assert "[OPEN_PATH]" in out, out
    assert "is not closed" in out, out
    assert "cutline" in out, out
    # The start point is printed so the open contour can be found by hand.
    assert "(20, 20)" in out, out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
