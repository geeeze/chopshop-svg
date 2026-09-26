#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for scripts/trace_sweep.py (front-half stage 2).

Uses the vtracer Python API (installed in .venv) as the tracer.  Synthetic
fixtures only; nothing here touches 00_source/.
"""

import importlib.util
import json
import os
import sys

import pytest
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
for p in (ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import trace_sweep  # noqa: E402
from lxml import etree  # noqa: E402

# find_spec rather than try/import: pyflakes still flags an unused import even
# with a `# noqa` comment, because noqa is flake8's feature, not pyflakes'.
_has_vtracer = importlib.util.find_spec("vtracer") is not None

pytestmark = pytest.mark.skipif(
    not _has_vtracer, reason="vtracer not installed")


def make_spec(tmp_path, allow_gradients=False, palette=None, sweep_max=None,
              sweep=None, name="spec.json"):
    spec = {
        "print_method": "screen_print",
        "max_colors": 6,
        "dimensions": {"width_mm": 30, "height_mm": 30},
        "geometry": {"allow_gradients": allow_gradients},
        "palette": palette if palette is not None
        else ["#000000", "#FFFFFF", "#FF0000"],
        "print": {"dpi": 100, "sweep_max_candidates": sweep_max or 12},
    }
    if sweep is not None:
        spec["print"]["sweep"] = sweep
    path = tmp_path / name
    path.write_text(json.dumps(spec), encoding="utf-8")
    return str(path)


def make_two_colour_png(path, size=(96, 96)):
    img = Image.new("RGB", size, "#ffffff")
    for x in range(16, 64):
        for y in range(16, 64):
            img.putpixel((x, y), (0, 0, 0))
    img.save(path, "PNG")
    return str(path)


def make_substrate_png(path, size=(96, 96)):
    """Mostly black with a white square: the inverse of make_two_colour_png.

    Used to check that a DECLARED substrate is honoured even when it is not
    the dominant colour, which is exactly the dark-garment case.
    """
    img = Image.new("RGB", size, "#000000")
    for x in range(16, 64):
        for y in range(16, 64):
            img.putpixel((x, y), (255, 255, 255))
    img.save(path, "PNG")
    return str(path)


def _run(tmp_path, png, spec, sweep=None, workers=None):
    out_dir = tmp_path / "traced"
    argv = [png, spec, "--out-dir", str(out_dir)]
    if sweep:
        argv += ["--sweep", sweep]
    if workers is not None:
        argv += ["--workers", str(workers)]
    code = trace_sweep.main(argv)
    return code, str(out_dir)


def _is_valid_svg(path):
    try:
        root = etree.parse(path).getroot()
        return etree.QName(root).localname == "svg"
    except Exception:
        return False


def test_two_colour_png_produces_at_least_three_candidates(tmp_path):
    png = make_two_colour_png(tmp_path / "art.png")
    spec = make_spec(tmp_path)
    code, out_dir = _run(tmp_path, png, spec)

    assert code == 0
    svgs = sorted(f for f in os.listdir(out_dir) if f.endswith(".svg"))
    assert len(svgs) >= 3


def test_every_candidate_is_valid_svg(tmp_path):
    png = make_two_colour_png(tmp_path / "art.png")
    spec = make_spec(tmp_path)
    code, out_dir = _run(tmp_path, png, spec)

    assert code == 0
    svgs = sorted(f for f in os.listdir(out_dir) if f.endswith(".svg"))
    assert svgs
    for name in svgs:
        assert _is_valid_svg(os.path.join(out_dir, name)), name


def test_sweep_json_records_exact_parameters(tmp_path):
    png = make_two_colour_png(tmp_path / "art.png")
    spec = make_spec(tmp_path)
    code, out_dir = _run(tmp_path, png, spec)

    assert code == 0
    sweep = json.load(open(os.path.join(out_dir, "sweep.json"), encoding="utf-8"))
    candidates = sweep["candidates"]
    assert len(candidates) >= 3

    # every candidate has exact params and a source sha256, and the file exists
    source_sha = sweep["input"]["sha256"]
    for cand in candidates:
        assert cand["source_sha256"] == source_sha
        assert isinstance(cand["params"], dict)
        assert os.path.exists(os.path.join(out_dir, cand["file"]))

    # params must be a faithful expansion of the semantic labels
    for cand in candidates:
        p = cand["params"]
        assert p["colormode"] == ("binary" if cand["preset"] == "bw" else "color")
        assert p["filter_speckle"] == cand["filter_speckle"]
        assert p["hierarchical"] == cand["hierarchical"]

    # the palette axis really varies: at least one candidate uses it, one not
    pal_flags = {c["use_palette"] for c in candidates}
    assert pal_flags == {True, False}

    # cap respected
    assert len(candidates) <= 12


def test_reproducible_across_runs(tmp_path):
    png = make_two_colour_png(tmp_path / "art.png")
    spec = make_spec(tmp_path)
    code_a, dir_a = _run(tmp_path, png, spec)
    code_b, dir_b = _run(tmp_path, png, spec)
    assert code_a == 0 and code_b == 0

    files_a = sorted(f for f in os.listdir(dir_a) if f.endswith(".svg"))
    files_b = sorted(f for f in os.listdir(dir_b) if f.endswith(".svg"))
    assert files_a == files_b and files_a

    for name in files_a:
        a = open(os.path.join(dir_a, name), "rb").read()
        b = open(os.path.join(dir_b, name), "rb").read()
        assert a == b, "candidate %s differs between runs" % name


def test_photo_preset_dropped_when_gradients_disallowed(tmp_path):
    png = make_two_colour_png(tmp_path / "art.png")
    spec = make_spec(tmp_path, allow_gradients=False)
    code, out_dir = _run(tmp_path, png, spec)
    assert code == 0
    sweep = json.load(open(os.path.join(out_dir, "sweep.json"), encoding="utf-8"))
    presets = {c["preset"] for c in sweep["candidates"]}
    assert "photo" not in presets


def test_photo_preset_included_when_gradients_allowed(tmp_path):
    png = make_two_colour_png(tmp_path / "art.png")
    spec = make_spec(tmp_path, allow_gradients=True)
    code, out_dir = _run(tmp_path, png, spec)
    assert code == 0
    sweep = json.load(open(os.path.join(out_dir, "sweep.json"), encoding="utf-8"))
    presets = {c["preset"] for c in sweep["candidates"]}
    assert "photo" in presets


def test_exit_3_when_no_tracer_available(tmp_path, monkeypatch):
    png = make_two_colour_png(tmp_path / "art.png")
    spec = make_spec(tmp_path)
    monkeypatch.setattr(trace_sweep, "detect_backend", lambda: (None, []))
    code = trace_sweep.main([png, spec, "--out-dir", str(tmp_path / "traced")])
    assert code == 3


def test_preset_params_override():
    """A sweep JSON with preset_params overrides/adds preset parameter sets."""
    sweep = {
        "version": 1,
        "presets": ["poster_flat"],
        "preset_params": {
            "poster_flat": {
                "colormode": "color", "mode": "spline",
                "color_precision": 2, "layer_difference": 16,
            }
        },
        "filter_speckle": [2],
        "hierarchical": ["cutout"],
        "use_palette": [False],
        "max_candidates": 12,
    }
    spec = {
        "print_method": "screen_print", "max_colors": 6,
        "geometry": {"allow_gradients": False}, "palette": [],
    }
    available = {"colormode", "mode", "color_precision", "layer_difference",
                 "filter_speckle", "hierarchical"}
    candidates, skipped = trace_sweep.build_candidates(
        sweep, spec, available, "test")
    assert len(candidates) == 1
    assert candidates[0]["preset"] == "poster_flat"
    assert candidates[0]["params"]["color_precision"] == 2
    assert candidates[0]["params"]["layer_difference"] == 16


def test_round_robin_truncation_distributes_speckles():
    """With a palette and cap 12, speckle=16 is still traced (not all
    speckle=2).  Before the round-robin fix the cap took only speckle=2."""
    sweep = {
        "version": 1,
        "presets": ["bw", "poster"],
        "filter_speckle": [2, 8, 16],
        "hierarchical": ["cutout"],
        "use_palette": [False, True],
        "max_candidates": 12,
    }
    spec = {
        "print_method": "screen_print", "max_colors": 6,
        "geometry": {"allow_gradients": False},
        "palette": ["#000000", "#FFFFFF"],
    }
    available = {"colormode", "mode", "color_precision", "layer_difference",
                 "filter_speckle", "hierarchical"}
    candidates, _ = trace_sweep.build_candidates(
        sweep, spec, available, "test")
    # Total = 2 presets × 3 speckles × 1 hier × 2 palettes = 12, capped at 12
    speckles = {c["filter_speckle"] for c in candidates}
    assert speckles == {2, 8, 16}, "round-robin must include all speckles"


def make_pitch_spec(tmp_path, pitch_shift=True, palette=None):
    spec = {
        "print_method": "screen_print",
        "max_colors": 6,
        "dimensions": {"width_mm": 30, "height_mm": 30},
        "geometry": {"allow_gradients": False},
        "palette": palette if palette is not None
        else ["#000000", "#FFFFFF", "#FF0000"],
        "print": {"dpi": 100, "sweep_max_candidates": 12,
                  "pitch_shift": pitch_shift},
    }
    path = tmp_path / "spec_pitch.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return str(path)


def test_pitch_shift_traces_all_three_variants(tmp_path):
    png = make_two_colour_png(tmp_path / "art.png")
    spec = make_pitch_spec(tmp_path)
    code, out_dir = _run(tmp_path, png, spec)

    assert code == 0
    sweep = json.load(open(os.path.join(out_dir, "sweep.json"), encoding="utf-8"))
    cands = sweep["candidates"]
    assert {c.get("variant", "source") for c in cands} == {
        "source", "inverse", "pitch"}

    # the palette axis is retired: every candidate traces its variant raster
    assert all(c["use_palette"] is False for c in cands)

    # every candidate names its own variant input, and it exists on disk
    for c in cands:
        assert c["variant_input"]
        assert os.path.exists(c["variant_input"])

    variants_dir = os.path.join(out_dir, ".variants")
    assert os.path.exists(os.path.join(variants_dir, "source.inverse.png"))
    assert os.path.exists(os.path.join(variants_dir, "source.pitch.png"))


def test_inverse_variant_inverts_artwork_and_leaves_substrate_clear(tmp_path):
    """The inverse twin must not turn the background into ink.

    This replaces a test that asserted the opposite -- it pinned
    "white background -> black" as correct behaviour, which is the bug. On a
    substrate-dominated source that produced a full-bleed flood: measured on
    the shipped example, the old inverse came out 92.06% black and preflight
    read 91.93% of the sheet over the 300% limit, while the original read
    0.00%. Both still reported PASSED, because INK_COVERAGE only gates on CMYK
    input.

    The substrate is now identified and left fully transparent, so there is no
    background ink to miscount at all.
    """
    png = make_two_colour_png(tmp_path / "art.png")
    spec = make_pitch_spec(tmp_path)
    code, out_dir = _run(tmp_path, png, spec)
    assert code == 0

    src = Image.open(png).convert("RGB")
    inv_path = os.path.join(out_dir, ".variants", "source.inverse.png")
    inv = Image.open(inv_path)
    assert inv.size == src.size
    # RGBA now: the substrate is carried as transparency, not as a colour.
    assert inv.mode == "RGBA"

    # Background pixel: transparent in the twin, and NOT black.
    r, g, b, a = inv.getpixel((0, 0))
    assert a == 0, "substrate must be transparent, not an ink"
    assert (r, g, b) != (0, 0, 0), "substrate must not be inverted to black"

    # Artwork pixel: opaque, and inverted from black to light.
    r, g, b, a = inv.getpixel((32, 32))
    assert a == 255, "artwork must stay opaque"
    assert src.getpixel((32, 32)) == (0, 0, 0)
    assert (r, g, b) == (255, 255, 255), "artwork must be inverted"

    # The point of the fix: opaque area is the artwork only, not the sheet.
    import numpy as np
    alpha = np.asarray(inv.split()[-1])
    opaque_frac = float((alpha > 0).sum()) / alpha.size
    assert opaque_frac < 0.5, (
        "inverse twin is mostly opaque (%.1f%%) -- the background is being "
        "inked again" % (100 * opaque_frac))


def test_inverse_variant_honours_declared_substrate(tmp_path):
    """A declared print.substrate decides what is the fabric.

    Without one, the most common colour is inferred, which is right for flat
    artwork. With one, that colour stays clear even if it is not the majority.
    """
    png = make_substrate_png(tmp_path / "dark.png")
    spec = make_pitch_spec(tmp_path)
    # #FFFFFF is the fabric here, but the image is mostly black.
    raw = json.loads(open(spec, encoding="utf-8").read())
    raw["print"]["substrate"] = "#FFFFFF"
    open(spec, "w", encoding="utf-8").write(json.dumps(raw))

    code, out_dir = _run(tmp_path, png, spec)
    assert code == 0
    inv = Image.open(os.path.join(out_dir, ".variants", "source.inverse.png"))
    # (2, 2) is the black majority -- outside the white square -- so it is
    # artwork: opaque, and inverted from black to light.
    r, _g, _b, a = inv.getpixel((2, 2))
    assert a == 255, "the black field is artwork, not the declared substrate"
    assert r > 200, "artwork must be inverted"
    # (48, 48) is inside the white square, which IS the declared substrate,
    # so it stays clear even though it is a minority colour here.
    assert inv.getpixel((48, 48))[3] == 0, (
        "declared substrate must stay transparent even when it is not the "
        "dominant colour")


def test_pitch_variant_uses_only_palette_colours(tmp_path):
    png = make_two_colour_png(tmp_path / "art.png")
    spec = make_pitch_spec(tmp_path)
    code, out_dir = _run(tmp_path, png, spec)
    assert code == 0

    import numpy as np
    pitch = Image.open(os.path.join(
        out_dir, ".variants", "source.pitch.png")).convert("RGB")
    arr = np.asarray(pitch).reshape(-1, 3)
    pal = {(0, 0, 0), (255, 255, 255), (255, 0, 0)}
    assert set(map(tuple, arr.tolist())) <= pal


def test_pitch_shift_without_palette_skips_pitch_variant():
    sweep = {
        "version": 1,
        "presets": ["poster"],
        "filter_speckle": [8],
        "hierarchical": ["cutout"],
        "use_palette": [False, True],
        "max_candidates": 12,
    }
    spec = {
        "print_method": "screen_print", "max_colors": 6,
        "geometry": {"allow_gradients": False},
        "palette": [],
        "print": {"pitch_shift": True},
    }
    available = {"colormode", "mode", "color_precision", "layer_difference",
                 "filter_speckle", "hierarchical"}
    candidates, skipped = trace_sweep.build_candidates(
        sweep, spec, available, "test")
    assert {c["variant"] for c in candidates} == {"source", "inverse"}
    # the palette axis is retired even though the sweep asks for it
    assert all(c["use_palette"] is False for c in candidates)
    assert "variant:pitch" in skipped


def test_pitch_shift_off_keeps_legacy_palette_axis(tmp_path):
    png = make_two_colour_png(tmp_path / "art.png")
    spec = make_pitch_spec(tmp_path, pitch_shift=False)
    code, out_dir = _run(tmp_path, png, spec)
    assert code == 0
    sweep = json.load(open(os.path.join(out_dir, "sweep.json"), encoding="utf-8"))
    pal_flags = {c["use_palette"] for c in sweep["candidates"]}
    assert pal_flags == {True, False}
    # no variant churn: every candidate is the plain source
    assert all(c.get("variant", "source") == "source"
               for c in sweep["candidates"])


def test_parallel_matches_sequential(tmp_path):
    # The same sweep run with one worker and with several must produce
    # byte-identical candidate SVGs and identical sweep records (modulo the
    # timestamp).  This guards the pool path against ordering races.
    png = make_two_colour_png(tmp_path / "art.png")
    spec = make_spec(tmp_path)
    code_s, dir_s = _run(tmp_path, png, spec, workers=1)
    code_p, dir_p = _run(tmp_path, png, spec, workers=4)
    assert code_s == 0 and code_p == 0

    files_s = sorted(f for f in os.listdir(dir_s) if f.endswith(".svg"))
    files_p = sorted(f for f in os.listdir(dir_p) if f.endswith(".svg"))
    assert files_s == files_p and files_s
    for name in files_s:
        a = open(os.path.join(dir_s, name), "rb").read()
        b = open(os.path.join(dir_p, name), "rb").read()
        assert a == b, "candidate %s differs between workers=1 and workers=4" % name

    s = json.load(open(os.path.join(dir_s, "sweep.json"), encoding="utf-8"))
    p = json.load(open(os.path.join(dir_p, "sweep.json"), encoding="utf-8"))
    s["generated"] = p["generated"] = ""
    assert s == p


# --------------------------------------------------------------------------
# spec-driven sweep (spec.print.sweep) -- the axes the studio's JSON builder
# exposes.  The sweep belongs in the job contract; --sweep stays for a
# by-hand run and wins over the spec.
# --------------------------------------------------------------------------

def _presets_in(out_dir):
    sweep = json.load(open(os.path.join(out_dir, "sweep.json"), encoding="utf-8"))
    return sorted({c["preset"] for c in sweep["candidates"]})


def test_spec_presets_limit_the_sweep(tmp_path):
    png = make_two_colour_png(tmp_path / "art.png")
    spec = make_spec(tmp_path, sweep={"presets": ["poster"]})
    code, out_dir = _run(tmp_path, png, spec)

    assert code == 0
    assert _presets_in(out_dir) == ["poster"]


def test_spec_sweep_leaves_unmentioned_axes_at_default(tmp_path):
    """A partial print.sweep object must not drop the axes it omits."""
    png = make_two_colour_png(tmp_path / "art.png")
    spec = make_spec(tmp_path, sweep={"presets": ["bw"]})
    code, out_dir = _run(tmp_path, png, spec)

    assert code == 0
    sweep = json.load(open(os.path.join(out_dir, "sweep.json"), encoding="utf-8"))
    speckles = sorted({c["filter_speckle"] for c in sweep["candidates"]})
    assert speckles == sorted(trace_sweep.DEFAULT_SWEEP["filter_speckle"])


def test_spec_sweep_max_candidates_is_the_cap(tmp_path):
    png = make_two_colour_png(tmp_path / "art.png")
    spec = make_spec(tmp_path, sweep={"max_candidates": 3})
    code, out_dir = _run(tmp_path, png, spec)

    assert code == 0
    svgs = [f for f in os.listdir(out_dir) if f.endswith(".svg")]
    assert len(svgs) <= 3


def test_legacy_sweep_max_candidates_still_caps(tmp_path):
    """print.sweep_max_candidates predates print.sweep and is still honoured."""
    png = make_two_colour_png(tmp_path / "art.png")
    spec = make_spec(tmp_path, sweep={"presets": ["bw"]}, sweep_max=3)
    code, out_dir = _run(tmp_path, png, spec)

    assert code == 0
    svgs = [f for f in os.listdir(out_dir) if f.endswith(".svg")]
    assert len(svgs) <= 3


def test_sweep_file_overrides_spec_sweep(tmp_path):
    png = make_two_colour_png(tmp_path / "art.png")
    spec = make_spec(tmp_path, sweep={"presets": ["bw"]})
    override = tmp_path / "override.json"
    override.write_text(json.dumps({"presets": ["poster"]}), encoding="utf-8")
    code, out_dir = _run(tmp_path, png, spec, sweep=str(override))

    assert code == 0
    assert _presets_in(out_dir) == ["poster"]


def test_spec_sweep_that_is_not_an_object_is_refused(tmp_path):
    png = make_two_colour_png(tmp_path / "art.png")
    spec = make_spec(tmp_path, sweep=["bw"])
    code, _ = _run(tmp_path, png, spec)

    assert code == 2
