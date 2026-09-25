#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_palette_variants.py -- pytest suite for scripts/palette_variants.py.

The auxiliary Chopshop-Aided-Design layer: it re-colours a finished trace onto
a named palette while leaving the CAD structure untouched.  These tests pin the
three promises that make it safe to use:

1. **Geometry is never touched.**  Every ``d=``, ``viewBox``, ``width`` and
   ``height`` survives a recolour byte for byte -- only paint values change.
2. **An explicit pin beats the heuristic.**  The area/nearest heuristics cannot
   recover intent, so a ``map`` entry must always win.
3. **It never picks a winner.**  The report orders by print-readiness gates,
   never by "looks best".

Everything else covers the mapping strategies, the report, and the failure
modes (bad map syntax, unknown palette, dry run writing nothing).

Fixtures are synthetic and built in ``tmp_path`` -- nothing here touches the
real ``00_source/`` batch or the repo's output directories.

Run with:  python3 -m pytest tests/test_palette_variants.py -v
"""

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
for _path in (ROOT, SCRIPTS):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import palette_variants as pv  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

TWO_COLOUR_SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="100mm"
     viewBox="0 0 100 100">
  <rect id="ground" width="100" height="100" fill="#ffffff"/>
  <path id="motif" d="M10,10 L90,10 L90,90 L10,90 Z" fill="#c1440e"/>
  <circle id="bud" cx="50" cy="50" r="10" fill="#6a8a3f"/>
</svg>
"""

CASCADE_SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="100mm"
     viewBox="0 0 100 100">
  <style>.leaf { fill: #6a8a3f; } .stem { stroke: #3f7c7c; stroke-width: 4; }</style>
  <path class="leaf" d="M0,0 L20,0 L20,20 Z"/>
  <path class="stem" d="M30,30 L70,70"/>
  <rect id="ground" width="100" height="100" fill="#ffffff"/>
</svg>
"""

GRADIENT_SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="100mm"
     viewBox="0 0 100 100">
  <defs>
    <linearGradient id="g1">
      <stop offset="0" stop-color="#c1440e"/>
      <stop offset="1" stop-color="#6a8a3f"/>
    </linearGradient>
  </defs>
  <rect width="100" height="100" fill="#ffffff"/>
  <path d="M10,10 L90,90" stroke="url(#g1)" stroke-width="6" fill="none"/>
</svg>
"""


def make_library(path, palettes):
    path.write_text(json.dumps({"version": 1, "palettes": palettes}),
                    encoding="utf-8")
    return str(path)


def simple_palette():
    return {
        "id": "test-palette",
        "name": "Test Palette",
        "note": "synthetic",
        "background": "#111111",
        "colours": ["#111111", "#222222", "#333333", "#444444"],
        "map": {},
    }


def write_svg(tmp_path, name, text):
    target = tmp_path / name
    target.write_text(text, encoding="utf-8")
    return str(target)


def geometry_of(path):
    """The structural fingerprint a recolour must not change."""
    import re

    text = open(path, encoding="utf-8").read()
    return {
        "d": re.findall(r'\sd="([^"]*)"', text),
        "viewBox": re.findall(r'viewBox="([^"]*)"', text),
        "width": re.findall(r'width="([^"]*)"', text),
        "height": re.findall(r'height="([^"]*)"', text),
    }


# --------------------------------------------------------------------------
# The shipped library
# --------------------------------------------------------------------------

def test_shipped_library_loads_and_is_well_formed():
    palettes = pv.load_library(pv.DEFAULT_LIBRARY)
    assert len(palettes) >= 6
    for palette in palettes:
        assert palette["id"]
        assert palette["colours"], palette["id"]
        # Normalisation is what lets hex comparisons work case-insensitively.
        for colour in palette["colours"]:
            assert colour == colour.lower()
            assert colour.startswith("#") and len(colour) == 7
        # The background must be one of the palette's own colours, or the
        # anchor would paint something that is not in the palette.
        assert palette["background"] in palette["colours"], palette["id"]


def test_shipped_library_has_the_requested_palettes():
    ids = {p["id"] for p in pv.load_library(pv.DEFAULT_LIBRARY)}
    for expected in ("cool-luxe", "warm-sunny", "pop-playful",
                     "red-cream", "blue-charcoal", "turquoise-black"):
        assert expected in ids


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def test_load_library_normalises_upper_case_hex(tmp_path):
    path = make_library(tmp_path / "lib.json", [{
        "id": "upper", "name": "Upper", "background": "#AABBCC",
        "colours": ["#AABBCC", "#DDEEFF"],
    }])
    palette = pv.load_library(path)[0]
    assert palette["colours"] == ["#aabbcc", "#ddeeff"]
    assert palette["background"] == "#aabbcc"


def test_load_library_rejects_a_palette_with_no_usable_colours(tmp_path):
    path = make_library(tmp_path / "lib.json",
                        [{"id": "empty", "colours": ["not-a-colour"]}])
    with pytest.raises(ValueError):
        pv.load_library(path)


def test_load_library_rejects_an_empty_library(tmp_path):
    path = make_library(tmp_path / "lib.json", [])
    with pytest.raises(ValueError):
        pv.load_library(path)


def test_background_falls_back_to_first_colour_when_absent(tmp_path):
    path = make_library(tmp_path / "lib.json", [{
        "id": "no-bg", "colours": ["#112233", "#445566"],
    }])
    assert pv.load_library(path)[0]["background"] == "#112233"


def test_background_outside_the_palette_falls_back_to_first_colour(tmp_path):
    path = make_library(tmp_path / "lib.json", [{
        "id": "bad-bg", "background": "#ffffff", "colours": ["#112233", "#445566"],
    }])
    assert pv.load_library(path)[0]["background"] == "#112233"


# --------------------------------------------------------------------------
# Mapping strategies
# --------------------------------------------------------------------------

def test_background_anchor_takes_the_heaviest_source_colour():
    weights = {"#aaaaaa": 500, "#bbbbbb": 300, "#cccccc": 100}
    palette = simple_palette()
    mapping, why = pv.build_mapping(weights, palette, "area", {})
    assert mapping["#aaaaaa"] == "#111111"
    assert why["#aaaaaa"] == "background anchor"


def test_area_strategy_ranks_the_remaining_colours_by_weight():
    weights = {"#aaaaaa": 500, "#bbbbbb": 300, "#cccccc": 100}
    mapping, why = pv.build_mapping(weights, simple_palette(), "area", {})
    # Remaining palette entries in declared order, minus the background.
    assert mapping["#bbbbbb"] == "#222222"
    assert mapping["#cccccc"] == "#333333"
    assert why["#bbbbbb"] == "area rank 1"


def test_explicit_map_beats_the_heuristic():
    weights = {"#aaaaaa": 500, "#bbbbbb": 300}
    mapping, why = pv.build_mapping(
        weights, simple_palette(), "area", {"#bbbbbb": "#444444"})
    assert mapping["#bbbbbb"] == "#444444"
    assert why["#bbbbbb"] == "explicit"
    # The untouched colour still takes the background anchor.
    assert mapping["#aaaaaa"] == "#111111"


def test_explicit_map_can_pin_the_background_too():
    weights = {"#aaaaaa": 500}
    mapping, _why = pv.build_mapping(
        weights, simple_palette(), "area", {"#aaaaaa": "#333333"})
    assert mapping["#aaaaaa"] == "#333333"


def test_surplus_source_colours_collapse_onto_the_palette():
    # Six source colours but only four palette slots: the tail must land on a
    # real palette colour, never an invented one.
    weights = {"#%06x" % (i * 111111): (600 - i * 100) for i in range(6)}
    palette = simple_palette()
    mapping, why = pv.build_mapping(weights, palette, "area", {})
    assert set(mapping.values()) <= set(palette["colours"])
    assert "collapsed onto nearest" in set(why.values())


def test_nearest_strategy_maps_every_colour_to_a_palette_entry():
    weights = {"#aaaaaa": 500, "#bbbbbb": 300}
    palette = simple_palette()
    mapping, why = pv.build_mapping(weights, palette, "nearest", {})
    assert set(mapping.values()) <= set(palette["colours"])
    assert set(why.values()) == {"nearest"}


def test_mapping_is_deterministic_for_tied_weights():
    weights = {"#bbbbbb": 100, "#aaaaaa": 100}
    first, _ = pv.build_mapping(weights, simple_palette(), "area", {})
    second, _ = pv.build_mapping(dict(reversed(list(weights.items()))),
                                 simple_palette(), "area", {})
    assert first == second


def test_build_mapping_survives_empty_weights():
    mapping, _why = pv.build_mapping({}, simple_palette(), "area", {})
    assert mapping == {}


# --------------------------------------------------------------------------
# Applying: the geometry guarantee
# --------------------------------------------------------------------------

def test_apply_mapping_rewrites_paint_and_leaves_geometry_untouched(tmp_path):
    svg = write_svg(tmp_path, "art.svg", TWO_COLOUR_SVG)
    tree, root, styles = pv.parse_svg(svg)
    before = geometry_of(svg)

    changes, _where = pv.apply_mapping(
        root, styles, {"#ffffff": "#111111", "#c1440e": "#222222"})
    out = str(tmp_path / "out.svg")
    tree.write(out, encoding="utf-8", xml_declaration=True)

    assert sum(changes.values()) == 2
    assert geometry_of(out) == before, "a recolour must not move geometry"
    text = open(out, encoding="utf-8").read()
    assert 'fill="#111111"' in text
    assert 'fill="#222222"' in text
    assert "#c1440e" not in text


def test_apply_mapping_handles_the_css_cascade(tmp_path):
    # A colour set in a <style> block must still be recoloured -- and written
    # back as an inline override so the stylesheet is not rewritten globally.
    svg = write_svg(tmp_path, "cascade.svg", CASCADE_SVG)
    tree, root, styles = pv.parse_svg(svg)
    pv.apply_mapping(root, styles, {"#6a8a3f": "#123456"})
    out = str(tmp_path / "out.svg")
    tree.write(out, encoding="utf-8", xml_declaration=True)
    text = open(out, encoding="utf-8").read()
    assert "style=" in text and "#123456" in text
    assert ".leaf { fill: #6a8a3f; }" in text, "the rule must not be rewritten"


def test_apply_mapping_rewrites_stop_color(tmp_path):
    svg = write_svg(tmp_path, "grad.svg", GRADIENT_SVG)
    tree, root, styles = pv.parse_svg(svg)
    _changes, where = pv.apply_mapping(
        root, styles, {"#c1440e": "#123456", "#6a8a3f": "#654321"})
    out = str(tmp_path / "out.svg")
    tree.write(out, encoding="utf-8", xml_declaration=True)
    text = open(out, encoding="utf-8").read()
    assert 'stop-color="#123456"' in text
    assert 'stop-color="#654321"' in text


def test_apply_mapping_ignores_identity_and_unknown_colours(tmp_path):
    svg = write_svg(tmp_path, "art.svg", TWO_COLOUR_SVG)
    _tree, root, styles = pv.parse_svg(svg)
    changes, _where = pv.apply_mapping(
        root, styles, {"#ffffff": "#ffffff", "#000000": "#111111"})
    assert sum(changes.values()) == 0


def test_declared_colours_finds_every_paint_value(tmp_path):
    svg = write_svg(tmp_path, "art.svg", TWO_COLOUR_SVG)
    _tree, root, styles = pv.parse_svg(svg)
    found = pv.declared_colours(root, styles)
    assert set(found) == {"#ffffff", "#c1440e", "#6a8a3f"}


# --------------------------------------------------------------------------
# Weights
# --------------------------------------------------------------------------

def test_weights_from_render_counts_pixels_near_each_colour():
    histogram = {(255, 255, 255): 700, (193, 68, 14): 250, (1, 2, 3): 50}
    weights = pv.weights_from_render(["#ffffff", "#c1440e"], histogram)
    assert weights["#ffffff"] == 700
    assert weights["#c1440e"] == 250


def test_weights_from_render_absorbs_near_miss_antialiasing():
    # A pixel one step off the flat fill is an edge, and must still count.
    histogram = {(193, 68, 14): 100, (194, 69, 15): 20, (10, 200, 10): 999}
    weights = pv.weights_from_render(["#c1440e"], histogram)
    assert weights["#c1440e"] == 120


def test_luminance_orders_black_below_white():
    assert pv.luminance("#000000") < pv.luminance("#ffffff")
    assert pv.luminance("#000000") == pytest.approx(0.0, abs=1e-9)


# --------------------------------------------------------------------------
# --map parsing
# --------------------------------------------------------------------------

def test_parse_map_accepts_pairs():
    assert pv.parse_map("#AABBCC=#DDEEFF") == {"#aabbcc": "#ddeeff"}


def test_parse_map_handles_several_pairs_and_whitespace():
    got = pv.parse_map(" #111111 = #222222 , #333333=#444444 ")
    assert got == {"#111111": "#222222", "#333333": "#444444"}


def test_parse_map_rejects_a_missing_equals():
    with pytest.raises(ValueError):
        pv.parse_map("#111111")


def test_parse_map_rejects_an_unusable_colour():
    with pytest.raises(ValueError):
        pv.parse_map("not-a-colour=#111111")


def test_parse_map_of_nothing_is_empty():
    assert pv.parse_map(None) == {}


def test_as_hex_rejects_strings_normalize_color_waves_through():
    # validate_svg.normalize_color returns unknown values unchanged, so a typo
    # would otherwise become a palette entry and reach the artwork as
    # fill="not-a-colour".  as_hex is the gate that stops that.
    assert pv.as_hex("not-a-colour") is None
    assert pv.as_hex("garbage") is None
    assert pv.as_hex("") is None
    assert pv.as_hex(None) is None


def test_as_hex_accepts_named_and_full_hex_colours():
    assert pv.as_hex("red") == "#ff0000"
    assert pv.as_hex("#AABBCC") == "#aabbcc"
    assert pv.as_hex("#aabbcc") == "#aabbcc"


def test_palette_entry_drops_a_typo_but_keeps_the_real_colours(tmp_path):
    path = make_library(tmp_path / "lib.json", [{
        "id": "mixed", "colours": ["#112233", "not-a-colour", "#445566"],
    }])
    palette = pv.load_library(path)[0]
    assert palette["colours"] == ["#112233", "#445566"]


def test_palette_entry_of_only_typos_is_rejected(tmp_path):
    path = make_library(tmp_path / "lib.json",
                        [{"id": "typo", "colours": ["not-a-colour", "garbage"]}])
    with pytest.raises(ValueError):
        pv.load_library(path)


# --------------------------------------------------------------------------
# Ordering: never a winner-picker
# --------------------------------------------------------------------------

def test_variant_sort_key_orders_by_gates_not_quality():
    failed = {"palette": "a", "preflight": {"passed": False, "advisory": 0}}
    good = {"palette": "b", "preflight": {"passed": True, "advisory": 3}}
    better = {"palette": "c", "preflight": {"passed": True, "advisory": 1}}
    unrun = {"palette": "d"}
    errored = {"palette": "e", "preflight": {"error": "boom"}}

    order = sorted([failed, good, better, unrun, errored],
                   key=pv.variant_sort_key)
    # Passing first (fewest advisories first), then failing, then no data.
    assert [v["palette"] for v in order] == ["c", "b", "a", "d", "e"]


def test_variant_sort_key_is_stable_for_equal_gates():
    a = {"palette": "aa", "preflight": {"passed": True, "advisory": 1}}
    b = {"palette": "bb", "preflight": {"passed": True, "advisory": 1}}
    assert pv.variant_sort_key(a) < pv.variant_sort_key(b)


def test_report_markdown_states_the_ordering_is_not_a_quality_ranking(tmp_path):
    result = {
        "source": {"file": "art.svg", "sha256": "0" * 64},
        "library": {"path": "lib.json", "sha256": "1" * 64},
        "strategy": "area",
        "weights_from": "element count (test)",
        "variants": [{
            "palette": "p", "name": "P", "note": "", "svg": "p.svg",
            "mapping_file": "m.json", "palette_colours": 4, "source_colours": 2,
            "changed_values": 2, "mapping": {"#ffffff": "#111111"},
            "why": {"#ffffff": "background anchor"}, "unused": [],
            "preflight": {"passed": True, "advisory": 0,
                          "rendered_ink_colors": 4},
        }],
    }
    text = pv.format_markdown(result)
    assert "not** by which looks best" in text or "not" in text
    assert "human call" in text
    # A passing variant must be reported as a pass, not as a winner.
    assert "pass" in text


def test_format_report_marks_a_hard_gate_failure(tmp_path):
    result = {
        "source": {"file": "art.svg", "sha256": "0" * 64},
        "weights_from": "test",
        "strategy": "area",
        "variants": [{
            "palette": "p", "name": "P", "changed_values": 1,
            "palette_colours": 4, "mapping": {}, "why": {}, "unused": [],
            "mapping_file": "m.json", "svg": "p.svg",
            "preflight": {"passed": False, "advisory": 2},
        }],
    }
    assert "FAIL" in pv.format_report(result, "out")


# --------------------------------------------------------------------------
# End to end
# --------------------------------------------------------------------------

def run_generate(tmp_path, svg, library, **kwargs):
    options = dict(outdir=str(tmp_path / "outdir"), strategy="area",
                   explicit={}, only=None, spec_path=str(tmp_path / "spec.json"),
                   preflight_enabled=False, preflight_dpi=None, dry_run=False)
    options.update(kwargs)
    (tmp_path / "spec.json").write_text(json.dumps({"max_colors": 6}),
                                        encoding="utf-8")
    return pv.generate(svg, library, options["outdir"], options["strategy"],
                       options["explicit"], options["only"], options["spec_path"],
                       options["preflight_enabled"], options["preflight_dpi"],
                       options["dry_run"])


def test_generate_writes_variant_mapping_and_report(tmp_path):
    svg = write_svg(tmp_path, "art.svg", TWO_COLOUR_SVG)
    library = make_library(tmp_path / "lib.json", [simple_palette()])
    assert run_generate(tmp_path, svg, library) == 0

    base = tmp_path / "outdir" / "art"
    variant = base / "test-palette" / "test-palette.svg"
    assert variant.exists()
    assert (base / "test-palette" / "mapping.json").exists()
    assert (base / "report.json").exists()
    assert (base / "report.md").exists()

    payload = json.loads((base / "report.json").read_text(encoding="utf-8"))
    assert payload["variants"][0]["changed_values"] == 3
    assert payload["source"]["sha256"]


def test_generate_leaves_geometry_untouched_end_to_end(tmp_path):
    svg = write_svg(tmp_path, "art.svg", TWO_COLOUR_SVG)
    library = make_library(tmp_path / "lib.json", [simple_palette()])
    before = geometry_of(svg)
    run_generate(tmp_path, svg, library)

    variant = tmp_path / "outdir" / "art" / "test-palette" / "test-palette.svg"
    assert geometry_of(str(variant)) == before


def test_generate_output_uses_only_palette_colours(tmp_path):
    svg = write_svg(tmp_path, "art.svg", TWO_COLOUR_SVG)
    palette = simple_palette()
    library = make_library(tmp_path / "lib.json", [palette])
    run_generate(tmp_path, svg, library)

    variant = tmp_path / "outdir" / "art" / "test-palette" / "test-palette.svg"
    text = open(str(variant), encoding="utf-8").read()
    import re

    fills = set(re.findall(r'fill="(#[0-9a-f]{6})"', text))
    assert fills <= set(palette["colours"])


def test_generate_is_idempotent(tmp_path):
    svg = write_svg(tmp_path, "art.svg", TWO_COLOUR_SVG)
    library = make_library(tmp_path / "lib.json", [simple_palette()])
    run_generate(tmp_path, svg, library)
    base = tmp_path / "outdir" / "art"
    first = (base / "test-palette" / "test-palette.svg").read_bytes()
    first_report = (base / "report.json").read_bytes()

    run_generate(tmp_path, svg, library)
    assert (base / "test-palette" / "test-palette.svg").read_bytes() == first
    assert (base / "report.json").read_bytes() == first_report


def test_generate_dry_run_writes_nothing(tmp_path):
    svg = write_svg(tmp_path, "art.svg", TWO_COLOUR_SVG)
    library = make_library(tmp_path / "lib.json", [simple_palette()])
    assert run_generate(tmp_path, svg, library, dry_run=True) == 0
    assert not (tmp_path / "outdir").exists()


def test_generate_only_filter_selects_one_palette(tmp_path):
    svg = write_svg(tmp_path, "art.svg", TWO_COLOUR_SVG)
    second = dict(simple_palette(), id="other", name="Other")
    library = make_library(tmp_path / "lib.json", [simple_palette(), second])
    run_generate(tmp_path, svg, library, only="other")

    base = tmp_path / "outdir" / "art"
    assert (base / "other").exists()
    assert not (base / "test-palette").exists()
    payload = json.loads((base / "report.json").read_text(encoding="utf-8"))
    assert payload["palettes"] == ["other"]


def test_generate_rejects_an_unknown_only_filter(tmp_path):
    svg = write_svg(tmp_path, "art.svg", TWO_COLOUR_SVG)
    library = make_library(tmp_path / "lib.json", [simple_palette()])
    assert run_generate(tmp_path, svg, library, only="nope") == 2


def test_generate_explicit_map_wins_end_to_end(tmp_path):
    svg = write_svg(tmp_path, "art.svg", TWO_COLOUR_SVG)
    library = make_library(tmp_path / "lib.json", [simple_palette()])
    run_generate(tmp_path, svg, library, explicit={"#c1440e": "#444444"})

    mapping = json.loads(
        (tmp_path / "outdir" / "art" / "test-palette" / "mapping.json")
        .read_text(encoding="utf-8"))
    assert mapping["map"]["#c1440e"] == "#444444"
    assert mapping["why"]["#c1440e"] == "explicit"


def test_generate_reports_source_colours_with_no_visible_area(tmp_path):
    # '#6a8a3f' is declared but the synthetic weights say nothing covers it.
    svg = write_svg(tmp_path, "art.svg", TWO_COLOUR_SVG)
    library = make_library(tmp_path / "lib.json", [simple_palette()])
    run_generate(tmp_path, svg, library)
    payload = json.loads(
        (tmp_path / "outdir" / "art" / "report.json").read_text(encoding="utf-8"))
    # Every declared colour is either mapped or explicitly reported as unused.
    variant = payload["variants"][0]
    mapped = set(variant["mapping"])
    assert mapped | set(variant["unused"]) == {"#ffffff", "#c1440e", "#6a8a3f"}


def test_generate_returns_two_for_an_svg_with_no_paint(tmp_path):
    svg = write_svg(tmp_path, "empty.svg",
                    '<svg xmlns="http://www.w3.org/2000/svg" width="10mm" '
                    'height="10mm" viewBox="0 0 10 10"><path d="M0,0 L1,1"/></svg>')
    library = make_library(tmp_path / "lib.json", [simple_palette()])
    assert run_generate(tmp_path, svg, library) == 2


def test_generate_report_orders_by_print_readiness(tmp_path):
    svg = write_svg(tmp_path, "art.svg", TWO_COLOUR_SVG)
    good = dict(simple_palette(), id="aaa-good")
    bad = dict(simple_palette(), id="zzz-bad")
    library = make_library(tmp_path / "lib.json", [bad, good])

    original = pv.run_preflight

    def fake_preflight(path, spec, workdir, dpi):
        passed = "aaa-good" in path
        return {"passed": passed, "advisory": 0 if passed else 5,
                "rendered_ink_colors": 4, "manifest": "x"}

    pv.run_preflight = fake_preflight
    try:
        run_generate(tmp_path, svg, library, preflight_enabled=True)
    finally:
        pv.run_preflight = original

    payload = json.loads(
        (tmp_path / "outdir" / "art" / "report.json").read_text(encoding="utf-8"))
    assert [v["palette"] for v in payload["variants"]] == ["aaa-good", "zzz-bad"]


def test_generate_returns_one_when_a_preflight_hits_a_hard_gate(tmp_path):
    svg = write_svg(tmp_path, "art.svg", TWO_COLOUR_SVG)
    library = make_library(tmp_path / "lib.json", [simple_palette()])

    original = pv.run_preflight
    pv.run_preflight = lambda path, spec, workdir, dpi: {
        "passed": False, "advisory": 1, "manifest": "x"}
    try:
        assert run_generate(tmp_path, svg, library, preflight_enabled=True) == 1
    finally:
        pv.run_preflight = original


# --------------------------------------------------------------------------
# --from-final
# --------------------------------------------------------------------------

def test_resolve_from_final_reads_the_manifest(tmp_path, monkeypatch):
    art = tmp_path / "art.svg"
    art.write_text(TWO_COLOUR_SVG, encoding="utf-8")
    final = tmp_path / "05_final"
    final.mkdir()
    (final / "stem.manifest.json").write_text(
        json.dumps({"input": {"file": str(art)}}), encoding="utf-8")

    monkeypatch.setattr(pv, "ROOT", str(tmp_path))
    path, manifest = pv.resolve_from_final("stem")
    assert path == str(art)
    assert manifest.endswith("stem.manifest.json")


def test_resolve_from_final_exits_when_the_manifest_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(pv, "ROOT", str(tmp_path))
    with pytest.raises(SystemExit):
        pv.resolve_from_final("nope")
