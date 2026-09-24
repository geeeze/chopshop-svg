#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_preflight.py -- regression suite for the Layer B render preflight.

The v4.0 document (section 7) asks whether the two measurement traps found by
calibration are actually pinned down by tests. They were not: preflight.py had
no tests at all, so the traps were documented in prose only. Every test in
TestInkMeasurement exists to fail if one of those regressions returns:

* **Polarity.** Ghostscript\'s tiffsep plates use 255 = no ink, 0 = full ink.
  Getting it backwards produced a confident, meaningless 298% on bright orange.
  ``test_all_white_reads_as_no_ink`` / ``test_dark_page_reads_as_heavy_ink``
  fail immediately if the polarity is ever flipped.
* **ICC re-conversion.** Passing an output profile to Ghostscript rewrites
  already-CMYK values; a known 400% rich black read back as 293%. The synthetic
  CMYK fixture below has exact 100/280/400% patches, so
  ``test_cmyk_patches_are_read_back_exactly`` fails if a profile is applied on
  the CMYK path again.

Tests marked ``slow`` shell out to Inkscape and Ghostscript. They are the point
of the suite, so they are not skipped by default -- only the runtime is kept
small (150 dpi, 40mm documents).
"""

import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import preflight                      # noqa: E402
import validate_svg                   # noqa: E402
from preflight import ADVISORY, HARD  # noqa: E402

DPI = 150


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

BASE_SPEC = {
    "print_method": "screen_print",
    "max_colors": 6,
    "require_cmyk": False,
    "ink_limit_percent": 300,
    "geometry": {
        "min_stroke_width_pt": 1.5,
        "allow_raster_embed": False,
        "allow_open_paths": True,
        "gradient_handling": "vector_halftone",
    },
    "palette": ["#000000", "#ffffff", "#ff0000", "#0000ff", "#ffff00", "#00ff00"],
    "print": {"dpi": DPI, "require_embedded_fonts": False,
              "dark_garment_underbase": False, "count_white_as_ink": False},
}


@pytest.fixture
def workdir(tmp_path):
    return str(tmp_path / "work")


def write_spec(tmp_path, mutate=None):
    spec = json.loads(json.dumps(BASE_SPEC))
    if mutate:
        mutate(spec)
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return str(path)


def write_spec_to(tmp_path, name, mutate=None):
    """Like write_spec, but under an explicit filename."""
    spec = json.loads(json.dumps(BASE_SPEC))
    if mutate:
        mutate(spec)
    path = tmp_path / name
    path.write_text(json.dumps(spec), encoding="utf-8")
    return str(path)


def write_svg(tmp_path, name, body, width="40mm", height="40mm",
              viewbox="0 0 40 40"):
    doc = ('<svg xmlns="http://www.w3.org/2000/svg" '
           'xmlns:xlink="http://www.w3.org/1999/xlink" width="%s" height="%s" '
           'viewBox="%s">\n%s\n</svg>\n' % (width, height, viewbox, body))
    path = tmp_path / name
    path.write_text(doc, encoding="utf-8")
    return str(path)


def run(svg, spec, workdir):
    """Run preflight and return (hard, advisory, stats, notes)."""
    failures, notes, stats, _wd, options = preflight.preflight(
        svg, spec, workdir=workdir, dpi=DPI)
    hard, advisory = preflight.classify_findings(failures, options)
    return hard, advisory, stats, notes


def rules(findings):
    return {f["rule"] for f in findings}


@pytest.fixture(scope="session")
def cmyk_patches(tmp_path_factory):
    """A CMYK PDF with exactly 100%, 280% and 400% total-area-coverage patches.

    Built through PostScript so the CMYK values are literal, then written with
    LeaveColorUnchanged -- the same flag preflight uses for CMYK input.
    """
    d = tmp_path_factory.mktemp("cmyk")
    ps = d / "patches.ps"
    ps.write_text(
        "%!PS-Adobe-3.0\n"
        "<< /PageSize [300 100] >> setpagedevice\n"
        "0.5 0.3 0.2 0.0 setcmykcolor   0 0 100 100 rectfill\n"
        "0.7 0.7 0.7 0.7 setcmykcolor   100 0 100 100 rectfill\n"
        "1 1 1 1 setcmykcolor           200 0 100 100 rectfill\n"
        "showpage\n", encoding="utf-8")
    pdf = d / "patches.pdf"
    subprocess.run(
        ["gs", "-q", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite",
         "-dProcessColorModel=/DeviceCMYK",
         "-sColorConversionStrategy=LeaveColorUnchanged",
         "-o", str(pdf), str(ps)], check=True, capture_output=True)
    return str(pdf)


# --------------------------------------------------------------------------
# 1. Ink measurement: the calibration regressions
# --------------------------------------------------------------------------

class TestInkMeasurement:
    """These fail if the tiffsep polarity or the ICC handling regresses."""

    def test_all_white_reads_as_no_ink(self, tmp_path, workdir):
        """Polarity guard: if 255/0 are swapped, white reads as 400% ink."""
        svg = write_svg(tmp_path, "white.svg",
                        '<rect width="40" height="40" fill="#ffffff"/>')
        _hard, _adv, stats, _notes = run(svg, write_spec(tmp_path), workdir)
        assert stats["ink_tac_max_percent"] <= 1.0, (
            "a white page reported %.0f%% ink coverage -- the tiffsep plate "
            "polarity is probably inverted (255 means NO ink)"
            % stats["ink_tac_max_percent"])

    def test_dark_page_reads_as_heavy_ink(self, tmp_path, workdir):
        """The other half of the polarity guard."""
        svg = write_svg(tmp_path, "black.svg",
                        '<rect width="40" height="40" fill="#000000"/>')
        _hard, _adv, stats, _notes = run(svg, write_spec(tmp_path), workdir)
        assert stats["ink_tac_max_percent"] >= 200.0, (
            "a black page reported only %.0f%% ink coverage -- polarity suspect"
            % stats["ink_tac_max_percent"])

    def test_cmyk_patches_are_read_back_exactly(self, cmyk_patches, tmp_path,
                                                workdir):
        """The ICC re-conversion regression: 400% must not become 293%.

        Applying an output profile to CMYK input silently rewrites the values.
        With LeaveColorUnchanged and no profile, the 400% rich black patch must
        come back as 400%.
        """
        spec = write_spec(tmp_path, lambda s: s.update({"require_cmyk": True}))
        hard, _adv, stats, _notes = run(cmyk_patches, spec, workdir)
        assert stats["tac_is_estimate"] is False
        assert stats["tac_exact"] is True
        assert abs(stats["ink_tac_max_percent"] - 400.0) <= 2.0, (
            "expected the 400%% patch to read back as 400%%, got %.0f%% -- a "
            "profile is probably being applied to CMYK input, which is exactly "
            "the bug that turned 400%% into 293%%"
            % stats["ink_tac_max_percent"])

    def test_no_output_profile_applied_to_cmyk_input(self, cmyk_patches,
                                                     tmp_path, workdir):
        """Direct assertion on the mechanism, not just the number."""
        spec = write_spec(tmp_path, lambda s: s.update({"require_cmyk": True}))
        _hard, _adv, stats, _notes = run(cmyk_patches, spec, workdir)
        assert "ink_profile_used" not in stats, (
            "an ICC output profile was applied to CMYK input (%s); "
            "LeaveColorUnchanged exists precisely to avoid this"
            % stats.get("ink_profile_used"))

    def test_exact_reading_gates_a_tac_violation(self, cmyk_patches, tmp_path,
                                                 workdir):
        """On CMYK input the 300% limit is a hard gate, as v4.0 section 5 says."""
        spec = write_spec(tmp_path, lambda s: s.update({"require_cmyk": True}))
        hard, _adv, _stats, _notes = run(cmyk_patches, spec, workdir)
        assert preflight.RULE_INK in rules(hard), (
            "400% coverage on a 300% limit must be a hard finding on CMYK input")

    def test_rgb_reading_cannot_gate(self, tmp_path, workdir):
        """On RGB input the same limit must stay advisory, or it lies.

        Ghostscript\'s RGB->CMYK does no press-grade black generation, so
        nothing exceeds ~296% and a 300% limit can never fire usefully. The
        number is reported, but it must not fail the job.
        """
        svg = write_svg(tmp_path, "dark.svg",
                        '<rect width="40" height="40" fill="#000000"/>')
        hard, advisory, stats, _notes = run(svg, write_spec(tmp_path), workdir)
        assert stats["tac_is_estimate"] is True
        assert stats["tac_exact"] is False
        assert preflight.RULE_INK not in rules(hard), (
            "an RGB-derived ink estimate was treated as a gate")
        assert preflight.RULE_INK in rules(advisory), (
            "the RGB ink estimate should still be reported as advisory")

    def test_tighter_limit_bites_on_exact_input(self, cmyk_patches, tmp_path,
                                                workdir):
        """A lower limit must bite once the reading is exact."""
        spec = write_spec(tmp_path,
                          lambda s: s.update({"require_cmyk": True,
                                              "ink_limit_percent": 150}))
        _hard, _adv, stats, _notes = run(cmyk_patches, spec, workdir)
        assert stats["ink_tac_area_over_limit_percent"] > 0


class TestGarmentAssumption:
    """White is an ink on a dark garment and not on a light one.

    This is the only rule that depends on the GARMENT rather than the artwork,
    so it must be stated rather than silently decided, and its cost (one extra
    screen) must be given.
    """


    def test_uncounted_white_warns_it_assumes_light_fabric(self, tmp_path, workdir):
        """The garment assumption must be stated, not silently applied.

        Same artwork is one screen more on a dark garment. Nothing else in the
        report reveals that, so whenever white is in the artwork and uncounted
        the user has to be told which fabric was assumed and what it costs.
        """
        svg = write_svg(tmp_path, "wn.svg", """\
  <rect width="40" height="40" fill="#ffffff"/>
  <rect x="10" y="10" width="12" height="12" fill="#000000"/>""")
        spec = write_spec(tmp_path, lambda s: s["print"].update(
            {"dark_garment_underbase": False, "count_white_as_ink": False}))
        _hard, _adv, stats, notes = run(svg, spec, workdir)

        assert stats["rendered_screens"] == 1, stats
        assert "light" in stats["rendered_white_treated_as"], stats
        joined = " ".join(notes)
        assert "underbase" in joined, joined
        assert "dark" in joined.lower(), joined
        # The consequence, in screens: 1 now, 2 on a dark garment.
        assert "2 screens, not 1" in joined, joined

    def test_white_counted_leaves_no_fabric_warning(self, tmp_path, workdir):
        """No warning when the assumption is not being made."""
        svg = write_svg(tmp_path, "wn2.svg", """\
  <rect width="40" height="40" fill="#ffffff"/>
  <rect x="10" y="10" width="12" height="12" fill="#000000"/>""")
        spec = write_spec(tmp_path, lambda s: s["print"].update(
            {"dark_garment_underbase": True}))
        _hard, _adv, _stats, notes = run(svg, spec, workdir)
        assert "assumes the garment is white" not in " ".join(notes)

    def test_no_white_no_warning(self, tmp_path, workdir):
        """Artwork with no white raises no garment question at all."""
        svg = write_svg(tmp_path, "wn3.svg", """\
  <rect width="40" height="40" fill="#000000"/>
  <rect x="10" y="10" width="12" height="12" fill="#ff0000"/>""")
        spec = write_spec(tmp_path, lambda s: s["print"].update(
            {"dark_garment_underbase": False, "count_white_as_ink": False}))
        _hard, _adv, stats, notes = run(svg, spec, workdir)
        assert stats.get("rendered_white_note") is None, stats
        assert "assumes the garment is white" not in " ".join(notes)


class TestDarkGarmentUnderbase:
    """The underbase is a screen, and it has to be counted as one.

    A white underbase is printed first on a dark garment and sits beneath every
    other colour. If the flag were accepted without acting on it, the screen
    count would come out one short on exactly the jobs where that matters.
    """

    def test_underbase_implies_white_is_an_ink(self, tmp_path, workdir):
        """Derived, so a spec that only sets the underbase still counts right."""
        svg = write_svg(tmp_path, "ub1.svg", """\
  <rect width="40" height="40" fill="#000000"/>
  <rect x="10" y="10" width="20" height="20" fill="#ff0000"/>""")
        def mutate(s):
            s["print"]["dark_garment_underbase"] = True
            # The key must be ABSENT, not false, or it counts as an explicit
            # instruction and the derivation is (correctly) skipped.
            s["print"].pop("count_white_as_ink", None)

        spec = write_spec(tmp_path, mutate)
        _hard, _adv, stats, notes = run(svg, spec, workdir)
        assert stats["white_counted_as_ink"] is True
        assert stats["rendered_screens"] == 3, stats
        assert "count_white_as_ink" in " ".join(notes)

    def test_underbase_adds_a_screen_without_a_transparent_background(
            self, tmp_path, workdir):
        """A full-bleed design has no background to accidentally supply white.

        Counting the white proof background happened to work for transparent
        artwork and failed here, which is why the underbase is modelled as its
        own screen rather than inferred from pixels.
        """
        svg = write_svg(tmp_path, "ub2.svg", """\
  <rect width="40" height="40" fill="#ff0000"/>
  <rect x="10" y="10" width="12" height="12" fill="#000000"/>""")
        on = write_spec(tmp_path, lambda s: s["print"].update(
            {"dark_garment_underbase": True}))
        off = write_spec_to(tmp_path, "off.json", lambda s: s["print"].update(
            {"dark_garment_underbase": False, "count_white_as_ink": False}))

        _h1, _a1, stats_on, _n1 = run(svg, on, workdir)
        _h2, _a2, stats_off, _n2 = run(svg, off, workdir)
        assert stats_on["rendered_screens"] == 3, stats_on
        assert stats_off["rendered_screens"] == 2, stats_off

    def test_underbase_white_is_one_screen_not_two(self, tmp_path, workdir):
        """An explicit white element and the underbase are the same screen.

        The artwork using white does not mean the printer needs a second white:
        it is the same plate, so this must not count as 3.
        """
        svg = write_svg(tmp_path, "ub3.svg", """\
  <rect width="40" height="40" fill="#ffffff"/>
  <rect x="10" y="10" width="12" height="12" fill="#000000"/>""")
        spec = write_spec(tmp_path, lambda s: s["print"].update(
            {"dark_garment_underbase": True, "count_white_as_ink": True}))
        _hard, _adv, stats, _notes = run(svg, spec, workdir)
        assert stats["rendered_screens"] == 2, stats
        assert stats["rendered_white_screen"] is True

    def test_underbase_pushes_a_design_over_its_screen_budget(
            self, tmp_path, workdir):
        """The gate that matters: 6 colours plus an underbase is 7 screens."""
        blocks = "".join('<rect x="%d" y="0" width="6" height="40" fill="%s"/>'
                         % (i * 6, c) for i, c in enumerate(
                             ["#000000", "#ff0000", "#00ff00", "#0000ff",
                              "#ffff00", "#ff00ff"]))
        svg = write_svg(tmp_path, "ub4.svg", blocks)
        spec = write_spec(tmp_path, lambda s: s.update({"max_colors": 6})
                          or s["print"].update({"dark_garment_underbase": True}))
        hard, _adv, stats, _notes = run(svg, spec, workdir)
        assert stats["rendered_screens"] == 7, stats
        assert preflight.RULE_RENDERED_COLORS in rules(hard)
        msg = " ".join(f["detail"] for f in hard)
        assert "7 screens" in msg and "underbase" in msg, msg

    def test_contradiction_is_reported(self, tmp_path, workdir):
        """Underbase on but white explicitly not an ink: undercounts by one."""
        svg = write_svg(tmp_path, "ub5.svg",
                        '<rect width="40" height="40" fill="#000000"/>')
        spec = write_spec(tmp_path, lambda s: s["print"].update(
            {"dark_garment_underbase": True, "count_white_as_ink": False}))
        _hard, _adv, _stats, notes = run(svg, spec, workdir)
        joined = " ".join(notes)
        assert "CONTRADICTION" in joined, joined

    def test_coverage_is_flagged_as_a_lower_bound(self, tmp_path, workdir):
        """The CMYK estimate on paper cannot include the underbase laydown."""
        svg = write_svg(tmp_path, "ub6.svg",
                        '<rect width="40" height="40" fill="#000000"/>')
        spec = write_spec(tmp_path, lambda s: s["print"].update(
            {"dark_garment_underbase": True}))
        _hard, _adv, _stats, notes = run(svg, spec, workdir)
        joined = " ".join(notes)
        assert "understate" in joined or "lower bound" in joined, joined


# --------------------------------------------------------------------------
# 2. Colour space must come from the bytes, not from inkcov
# --------------------------------------------------------------------------

class TestColourSpace:
    def test_rgb_pdf_is_detected_as_rgb(self, tmp_path, workdir):
        """inkcov answers "CMYK" for an RGB file; the byte scan must not."""
        svg = write_svg(tmp_path, "rgb.svg",
                        '<rect width="40" height="40" fill="#c85028"/>')
        _hard, _adv, stats, _notes = run(svg, write_spec(tmp_path), workdir)
        assert stats["pdf_color_spaces"] == ["RGB"], stats["pdf_color_spaces"]

    def test_cmyk_pdf_is_detected_as_cmyk_and_prefix_stripped(
            self, cmyk_patches, tmp_path, workdir):
        """`DeviceCMYK` must normalise to `CMYK`, or `"CMYK" in spaces` fails."""
        spec = write_spec(tmp_path, lambda s: s.update({"require_cmyk": True}))
        _hard, _adv, stats, _notes = run(cmyk_patches, spec, workdir)
        assert "CMYK" in stats["pdf_color_spaces"]
        assert stats["pdf_color_spaces"] == ["CMYK"]

    def test_require_cmyk_fails_on_rgb_output(self, tmp_path, workdir):
        svg = write_svg(tmp_path, "rgb2.svg",
                        '<rect width="40" height="40" fill="#c85028"/>')
        spec = write_spec(tmp_path, lambda s: s.update({"require_cmyk": True}))
        hard, _adv, _stats, _notes = run(svg, spec, workdir)
        assert preflight.RULE_CMYK in rules(hard)


# --------------------------------------------------------------------------
# 3. Rendered colour counting: what Layer A structurally cannot see
# --------------------------------------------------------------------------

class TestRenderedColours:
    def test_gradient_is_caught_after_passing_layer_a(self, tmp_path, workdir):
        """The headline case from the v4.0 document.

        A two-stop gradient is declared as 2 colours, so Layer A passes it. It
        renders as hundreds, so Layer B must fail it.
        """
        svg = write_svg(tmp_path, "grad.svg", """  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#ff0000"/>
      <stop offset="1" stop-color="#ffff00"/>
    </linearGradient>
  </defs>
  <rect width="40" height="40" fill="url(#g)"/>""")
        spec = write_spec(tmp_path)

        layer_a_failures, _notes, stats_a = validate_svg.validate(svg, spec)
        assert stats_a["color_count"] == 2, stats_a.get("colors")
        assert not layer_a_failures, (
            "Layer A is expected to pass this file: %s" % layer_a_failures)

        hard, _adv, stats_b, _notes = run(svg, spec, workdir)
        assert preflight.RULE_CONTINUOUS_TONE in rules(hard), (
            "Layer B missed the gradient that Layer A structurally cannot see")
        assert stats_b["rendered_ink_colors"] > 2
        assert stats_b["rendered_continuous_tone"] is True

    def test_gradient_advisory_when_embedding_a_raster(self, tmp_path, workdir):
        """v4.0 section 5: advisory when gradient_handling is embedded_raster."""
        svg = write_svg(tmp_path, "grad2.svg", """  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#ff0000"/>
      <stop offset="1" stop-color="#ffff00"/>
    </linearGradient>
  </defs>
  <rect width="40" height="40" fill="url(#g)"/>""")
        spec = write_spec(tmp_path, lambda s: s["geometry"].update(
            {"gradient_handling": "embedded_raster"}))
        hard, advisory, _stats, _notes = run(svg, spec, workdir)
        assert preflight.RULE_CONTINUOUS_TONE not in rules(hard)
        assert preflight.RULE_CONTINUOUS_TONE in rules(advisory)

    def test_allow_gradients_silences_the_finding(self, tmp_path, workdir):
        svg = write_svg(tmp_path, "grad3.svg", """  <defs>
    <linearGradient id="g"><stop offset="0" stop-color="#ff0000"/>
      <stop offset="1" stop-color="#ffff00"/></linearGradient>
  </defs>
  <rect width="40" height="40" fill="url(#g)"/>""")
        spec = write_spec(tmp_path, lambda s: s["geometry"].update(
            {"allow_gradients": True}))
        hard, advisory, _stats, _notes = run(svg, spec, workdir)
        assert preflight.RULE_CONTINUOUS_TONE not in rules(hard) | rules(advisory)

    def test_flat_colours_merge_to_the_real_ink_count(self, tmp_path, workdir):
        """Antialiasing must not inflate a 4-colour design into more inks."""
        svg = write_svg(tmp_path, "flats.svg", """  <rect width="40" height="40" fill="#f5f0e6"/>
  <rect x="2" y="2" width="16" height="16" fill="#1b3a5c"/>
  <circle cx="28" cy="10" r="7" fill="#c1440e"/>
  <rect x="2" y="24" width="36" height="12" fill="#009e73"/>""")
        _hard, _adv, stats, _notes = run(svg, write_spec(tmp_path), workdir)
        assert stats["rendered_ink_colors"] == 4, (
            "expected 4 merged inks, got %d from %d significant colours: %s"
            % (stats["rendered_ink_colors"],
               stats["rendered_significant_colors"], stats["rendered_ink_hex"]))
        assert stats["rendered_continuous_tone"] is False

    def test_solid_grey_bar_is_an_ink_not_a_blend(self, tmp_path, workdir):
        """The false negative the 00_source batch exposed.

        A deliberate solid #5a5a5a bar was absorbed as an antialiasing blend.
        Neutral grey sits exactly on the line between #000000 and #ffffff, so
        the segment test flags it, and the bar was small next to the colours it
        bordered, so the size test agreed. Only its shape -- a filled region
        rather than a 1px ribbon -- says otherwise.
        """
        svg = write_svg(tmp_path, "greybar.svg", """\
  <rect width="40" height="40" fill="#ffffff"/>
  <rect x="2" y="2" width="36" height="6" fill="#000000"/>
  <rect x="2" y="18" width="36" height="5" fill="#5a5a5a"/>""")
        _hard, _adv, stats, _notes = run(svg, write_spec(tmp_path), workdir)
        inks = stats.get("rendered_ink_hex") or []
        assert "#5a5a5a" in inks, (
            "the solid grey bar was swallowed as a blend: %s" % inks)
        assert stats["rendered_ink_colors"] == 2, (
            "expected black + grey, got %s" % inks)

    def test_thin_antialiasing_ribbons_are_absorbed(self, tmp_path, workdir):
        """The other side of the same coin: 18 thin rules are ONE ink.

        A hairline lays down a chain of greys along each edge. Those ribbons
        have no interior, so they fold into the black line rather than being
        counted as four separate inks.
        """
        lines = "".join('<path id="l%d" d="M2,%d L38,%d" stroke="#000000" '
                        'stroke-width="0.4"/>' % (i, 4 + i * 2, 4 + i * 2)
                        for i in range(18))
        svg = write_svg(tmp_path, "rules.svg", lines)
        _hard, _adv, stats, _notes = run(svg, write_spec(tmp_path), workdir)
        assert stats["rendered_ink_colors"] == 1, (
            "thin rules reported %d inks (%s); antialiasing greys should fold "
            "into the line colour"
            % (stats["rendered_ink_colors"], stats.get("rendered_ink_hex")))

    def test_photograph_is_caught_as_continuous_tone(self, tmp_path, workdir):
        """The false negative that the 00_source batch exposed.

        A photograph has no single colour band large enough to see, so with a
        per-colour area threshold every one of its ~700k colours is discarded
        and the artwork reports as ONE ink -- passing a spot-colour gate it
        should never pass. Detected instead by the collective area of the
        sub-threshold colours (measured 44.4% for a photograph against <=0.20%
        for every flat file in the batch).
        """
        import numpy as np
        from PIL import Image

        rng = np.random.default_rng(11)
        h, w = 200, 160
        yy, xx = np.mgrid[0:h, 0:w]
        r = 90 + 120 * np.sin(xx / float(w) * 3.0)
        g = 110 + 100 * np.cos(yy / float(h) * 2.5)
        b = 80 + 130 * np.sin((xx + yy) / float(w + h) * 5.0)
        rgb = np.stack([r, g, b], -1).clip(0, 255)
        rgb = np.clip(rgb + rng.integers(-8, 9, rgb.shape), 0, 255).astype(np.uint8)
        Image.fromarray(rgb).save(str(tmp_path / "photo.png"))

        svg = write_svg(tmp_path, "photo.svg",
                        '<image id="p" x="2" y="2" width="36" height="36" '
                        'xlink:href="photo.png" preserveAspectRatio="none"/>')
        _hard, _adv, stats, _notes = run(svg, write_spec(tmp_path), workdir)
        assert stats["rendered_diffuse_tone"] is True, stats
        assert stats["rendered_continuous_tone"] is True
        assert stats["rendered_diffuse_area_percent"] > 3.0
        assert stats["rendered_diffuse_colors"] > 2000

    def test_flat_artwork_is_not_mistaken_for_tone(self, tmp_path, workdir):
        """The diffuse detector must not fire on antialiasing fringe."""
        svg = write_svg(tmp_path, "flat_ct.svg", """\
  <rect width="40" height="40" fill="#f5f0e6"/>
  <rect x="2" y="2" width="16" height="16" fill="#1b3a5c"/>
  <circle cx="28" cy="10" r="7" fill="#c1440e"/>
  <rect x="2" y="24" width="36" height="12" fill="#009e73"/>""")
        _hard, _adv, stats, _notes = run(svg, write_spec(tmp_path), workdir)
        assert stats["rendered_diffuse_tone"] is False, stats
        assert stats["rendered_continuous_tone"] is False


    def test_soft_mask_is_not_counted_as_a_second_image(self, tmp_path, workdir):
        """pdfimages prints a row for an image AND for its soft mask.

        Counting every row reported a single 1x1 image as "2 placed raster
        images" and inflated the resolution complaint with it.
        """
        import base64
        import struct
        import zlib

        # A 2x2 RGBA PNG: alpha forces Inkscape to emit an smask.
        raw = b"".join(b"\x00" + bytes([200, 80, 40, 255] * 2) for _ in range(2))

        def chunk(tag, data):
            body = tag + data
            return (struct.pack(">I", len(data)) + body
                    + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

        png = (b"\x89PNG\r\n\x1a\n"
               + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(raw))
               + chunk(b"IEND", b""))
        href = "data:image/png;base64," + base64.b64encode(png).decode()

        svg = write_svg(tmp_path, "alpha.svg",
                        '<image id="i" x="2" y="2" width="36" height="36" '
                        'href="%s" preserveAspectRatio="none"/>' % href)
        _hard, _adv, stats, _notes = run(svg, write_spec(tmp_path), workdir)
        assert len(stats["pdf_images"]) == 1, (
            "one <image> reported as %d placed bitmaps: %s"
            % (len(stats["pdf_images"]), stats["pdf_images"]))
        assert stats.get("pdf_image_masks", 0) >= 0

    def test_white_is_reported_as_paper_by_default(self, tmp_path, workdir):
        svg = write_svg(tmp_path, "white.svg", """\
  <rect width="40" height="40" fill="#000000"/>
  <rect x="10" y="10" width="20" height="20" fill="#ffffff"/>""")
        _hard, _adv, stats, _notes = run(svg, write_spec(tmp_path), workdir)
        # The wording now names the garment assumption it is making; assert on
        # the facts that matter rather than the exact phrase.
        assert stats["rendered_white_treated_as"].startswith("paper, not an ink")
        assert "light" in stats["rendered_white_treated_as"]
        assert "#ffffff" not in stats["rendered_ink_hex"]
        assert stats["rendered_ink_colors"] == 1

    def test_white_counts_as_an_ink_when_asked(self, tmp_path, workdir):
        """On a dark garment white is the underbase, i.e. a real screen."""
        svg = write_svg(tmp_path, "white2.svg", """\
  <rect width="40" height="40" fill="#000000"/>
  <rect x="10" y="10" width="20" height="20" fill="#ffffff"/>""")
        spec = write_spec(tmp_path,
                          lambda s: s["print"].update({"count_white_as_ink": True}))
        _hard, _adv, stats, _notes = run(svg, spec, workdir)
        assert stats["rendered_white_treated_as"] == "ink (declared in the artwork)"
        assert "#ffffff" in stats["rendered_ink_hex"]
        assert stats["rendered_ink_colors"] == 2

    def test_too_many_inks_fails_layer_b(self, tmp_path, workdir):
        """Eight real flat colours on a 6-ink budget is a hard gate."""
        blocks = "".join('<rect x="%d" y="0" width="4" height="40" fill="%s"/>'
                         % (i * 5, c) for i, c in enumerate(
                             ["#000000", "#ff0000", "#00ff00", "#0000ff",
                              "#ffff00", "#ff00ff", "#00ffff", "#808080"]))
        svg = write_svg(tmp_path, "many.svg", blocks)
        hard, _adv, stats, _notes = run(svg, write_spec(tmp_path), workdir)
        assert preflight.RULE_RENDERED_COLORS in rules(hard), stats
        assert stats["rendered_ink_colors"] > 6


# --------------------------------------------------------------------------
# 4. The gate table itself
# --------------------------------------------------------------------------

class TestGateTable:
    """v4.0 section 5 must be reflected in code, not just in the document."""

    def test_every_rule_gets_a_severity(self):
        everything = []
        for name in dir(validate_svg):
            if name.startswith("RULE_"):
                everything.append(getattr(validate_svg, name))
        for name in dir(preflight):
            if name.startswith("RULE_"):
                everything.append(getattr(preflight, name))
        assert everything
        for rule in set(everything):
            assert preflight.classify(rule, {}) in (HARD, ADVISORY), rule

    @pytest.mark.parametrize("rule", [
        validate_svg.RULE_RASTER, validate_svg.RULE_COLORS,
        validate_svg.RULE_STROKE, validate_svg.RULE_DEGENERATE,
        validate_svg.RULE_OPEN, validate_svg.RULE_SVG_PARSE,
        validate_svg.RULE_PATH_PARSE, validate_svg.RULE_DIMENSIONS,
        preflight.RULE_RENDERED_COLORS, preflight.RULE_IMAGE_RES,
        preflight.RULE_CMYK,
    ])
    def test_documented_hard_gates_are_hard(self, rule):
        assert preflight.classify(rule, {}) == HARD

    @pytest.mark.parametrize("rule", [
        validate_svg.RULE_NODES, validate_svg.RULE_PALETTE,
    ])
    def test_documented_advisories_are_advisory(self, rule):
        assert preflight.classify(rule, {}) == ADVISORY

    def test_font_embedding_follows_the_spec(self):
        assert preflight.classify(preflight.RULE_FONT,
                                  {"require_embedded_fonts": False}) == ADVISORY
        assert preflight.classify(preflight.RULE_FONT,
                                  {"require_embedded_fonts": True}) == HARD

    def test_ink_severity_follows_exactness(self):
        assert preflight.classify(preflight.RULE_INK, {"tac_exact": False}) == ADVISORY
        assert preflight.classify(preflight.RULE_INK, {"tac_exact": True}) == HARD

    def test_unknown_rule_fails_safe(self):
        assert preflight.classify("SOMETHING_NEW", {}) == HARD

    def test_layers_are_attributed(self):
        assert preflight.layer_of(validate_svg.RULE_STROKE) == preflight.LAYER_A
        assert preflight.layer_of(preflight.RULE_CONTINUOUS_TONE) == preflight.LAYER_B


# --------------------------------------------------------------------------
# 5. Layer A must still enforce the cutting rules (v4.0 section 7, question 1)
# --------------------------------------------------------------------------

class TestLayerAStillIntact:
    def test_open_paths_fail_for_vinyl(self, tmp_path):
        svg = write_svg(tmp_path, "open.svg",
                        '<path id="l" d="M2,2 L30,30" stroke="#000000" '
                        'stroke-width="2pt"/>')
        path = tmp_path / "spec.json"
        data = json.loads(json.dumps(BASE_SPEC))
        data["print_method"] = "vinyl"
        del data["geometry"]["allow_open_paths"]
        path.write_text(json.dumps(data), encoding="utf-8")
        failures, _notes, stats = validate_svg.validate(svg, str(path))
        assert validate_svg.RULE_OPEN in {r for r, _ in failures}
        assert stats["allow_open_paths"] is False
        assert "vinyl" in stats["open_paths_source"]

    def test_open_paths_allowed_for_screen_print(self, tmp_path):
        svg = write_svg(tmp_path, "open2.svg",
                        '<path id="l" d="M2,2 L30,30" stroke="#000000" '
                        'stroke-width="2pt"/>')
        data = json.loads(json.dumps(BASE_SPEC))
        data["geometry"]["allow_open_paths"] = True
        path = tmp_path / "spec2.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        failures, _notes, _stats = validate_svg.validate(svg, str(path))
        assert validate_svg.RULE_OPEN not in {r for r, _ in failures}

    def test_missing_allow_open_paths_stays_conservative(self, tmp_path):
        """No print_method and no allow_open_paths: keep the strict default."""
        svg = write_svg(tmp_path, "open3.svg",
                        '<path id="l" d="M2,2 L30,30" stroke="#000000" '
                        'stroke-width="2pt"/>')
        data = json.loads(json.dumps(BASE_SPEC))
        del data["geometry"]["allow_open_paths"]
        data.pop("print_method")
        path = tmp_path / "spec3.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        failures, _notes, _stats = validate_svg.validate(svg, str(path))
        assert validate_svg.RULE_OPEN in {r for r, _ in failures}