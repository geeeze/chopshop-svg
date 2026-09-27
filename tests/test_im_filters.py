#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_im_filters.py -- pytest suite for scripts/im_filters.py.

The auxiliary front-half filter bench: ImageMagick passes that produce
candidate INPUTS to prep_raster.py/trace_sweep.py.  Raster in, raster out; it
measures, never recommends.

These tests pin the things that were WRONG in the first implementation and
would be silently wrong again, because in each case the failure mode is
"reports success and produces the same bytes":

1. **``identity`` is a byte copy.**  The first version ran a no-op ImageMagick
   pass, and the alpha flatten inside it moved 36 931 pixels of an 800x450
   test image (max delta 94).  A control that transforms the image cannot tell
   you whether a measured difference came from the filter.
2. **A dither axis must actually differ.**  ImageMagick 7's default dither is
   Riemersma, so ``-dither Riemersma`` produces byte-identical output and a
   "dithered" preset built on it is a fake.  ``test_no_preset_uses_the_inert
   _dither`` makes that a regression pin.
3. **The flatten is a reading, not a hidden step.**  It has its own preset so
   its cost is measurable.
4. **A failed variant is reported, not raised.**  One bad preset must not take
   down the bench.

Fixtures are synthetic and built in ``tmp_path`` -- nothing here touches the
real ``00_source/`` batch or the repo's output directories.  Tests that need
the engine carry ``needs_imagemagick`` so a host without it still runs clean.

Run with:  python3 -m pytest tests/test_im_filters.py -v
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

import im_filters as imf  # noqa: E402

PIL = pytest.importorskip("PIL", reason="Pillow is a front-half dependency")
from PIL import Image  # noqa: E402

# The task tuple run_preset() takes is (driver, src, outdir, preset,
# flatten_hex, palettes) -- the driver must go in slot 0, not the tmp_path.
ENGINE = imf.engine_argv()[0] or "magick"

needs_imagemagick = pytest.mark.skipif(
    imf.engine_argv()[0] is None,
    reason="ImageMagick not installed")


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

def write_png(path, colours, size=(64, 48), bands=((0, 4), (4, 8))):
    """A tiny flat-banded PNG: N vertical bands, one colour each."""
    image = Image.new("RGB", size)
    width = size[0] // len(colours)
    for index, colour in enumerate(colours):
        for x in range(index * width, size[0] if index == len(colours) - 1
                       else (index + 1) * width):
            for y in range(size[1]):
                image.putpixel((x, y), colour)
    image.save(path)
    return path


def write_alpha_png(path, size=(32, 32)):
    """An RGBA PNG with a feathered edge -- the flatten's reason to exist."""
    image = Image.new("RGBA", size, (255, 0, 0, 255))
    for y in range(size[1]):
        for x in range(size[0] // 2):
            image.putpixel((x, y), (255, 0, 0, 128))
    image.save(path)
    return path


def write_ramp_png(path, size=(96, 64)):
    """A smooth multi-colour ramp: MORE colours than the presets reduce to.

    Needed for any dither assertion.  A flat three-colour fixture has nothing
    to quantise, so ``-colors 6`` leaves it untouched and an inert dither and
    a working one produce identical bytes -- a green test that proves nothing.
    """
    image = Image.new("RGB", size)
    for y in range(size[1]):
        for x in range(size[0]):
            image.putpixel((x, y), (x * 255 // (size[0] - 1),
                                    y * 255 // (size[1] - 1),
                                    (x + y) * 255 // (size[0] + size[1] - 2)))
    image.save(path)
    return path


THREE_FLAT = [(255, 0, 0), (0, 0, 0), (251, 251, 242)]


# --------------------------------------------------------------------------
# The control -- the trap that was live in the first implementation
# --------------------------------------------------------------------------

def test_identity_is_a_byte_copy(tmp_path):
    """The control must not transform the image. Pinned; it did once."""
    src = write_png(str(tmp_path / "art.png"), THREE_FLAT)
    entry = imf.run_preset((ENGINE, src, str(tmp_path), "identity", None, None))
    assert "error" not in entry, entry
    with open(src, "rb") as handle:
        original = handle.read()
    with open(entry["output"], "rb") as handle:
        copied = handle.read()
    assert copied == original


def test_identity_ignores_the_flatten_request(tmp_path):
    """Even with a background, identity stays byte-identical.

    If it flattened, every variant would be measured against a transformed
    control and the flatten's own cost would hide inside all of them.
    """
    src = write_alpha_png(str(tmp_path / "alpha.png"))
    entry = imf.run_preset((ENGINE, src, str(tmp_path), "identity", None,
                            "#ffffff"))
    assert "error" not in entry, entry
    assert open(entry["output"], "rb").read() == open(src, "rb").read()


def test_flatten_changes_an_alpha_source(tmp_path):
    """The flatten is a real transform, so it must show up as one."""
    src = write_alpha_png(str(tmp_path / "alpha.png"))
    entry = imf.run_preset((ENGINE, src, str(tmp_path), "flatten", None,
                            "#ffffff"))
    if "error" in entry:
        pytest.skip("ImageMagick unavailable: %s" % entry["error"])
    assert open(entry["output"], "rb").read() != open(src, "rb").read()


# --------------------------------------------------------------------------
# argv construction (pure -- no engine needed)
# --------------------------------------------------------------------------

def test_filter_argv_appends_the_flatten_last():
    argv = imf.filter_argv("magick", "in.png", "out.png", "flat6", "#ffffff")
    assert argv[0] == "magick"
    assert argv[1] == "in.png"
    assert argv[-1] == "out.png"
    # The flatten must come AFTER the filter, or the filter operates on a
    # composited image rather than the artwork.
    assert argv.index("-background") > argv.index("-colors")


def test_filter_argv_without_a_background_omits_the_flatten():
    argv = imf.filter_argv("magick", "in.png", "out.png", "flat6", None)
    assert "-background" not in argv
    assert "-alpha" not in argv


def test_flat_presets_state_a_dither_explicitly():
    """No reducing preset may rely on IM's implicit default dither."""
    for name in ("flat6", "flat8", "denoise-flat"):
        assert "-dither" in imf.PRESETS[name], name


def test_no_preset_uses_the_inert_dither():
    """Riemersma is IM 7's default, so naming it changes nothing.

    Measured: -dither Riemersma -colors 6 and -colors 6 are byte-identical, as
    are their -remap equivalents.  A preset claiming to dither via Riemersma
    would report success while emitting the undithered bytes.
    """
    for name, argv in imf.PRESETS.items():
        if "-dither" in argv:
            assert argv[argv.index("-dither") + 1] != "Riemersma", name
    for name, (_key, dither) in imf.REMAPPED.items():
        assert dither != "Riemersma", name


def test_remap_preset_needs_its_palette():
    with pytest.raises(ValueError):
        imf.filter_argv("magick", "in.png", "out.png", "remap-auto", None, {})
    argv = imf.filter_argv("magick", "in.png", "out.png", "remap-auto", None,
                           {"auto": "/tmp/pal.png"})
    assert argv[argv.index("-remap") + 1] == "/tmp/pal.png"


def test_variants_are_forced_to_truecolour():
    """`-colors`, `-posterize` and `-remap` all write INDEXED PNGs by default.

    Indexed output is what made prep_raster's background check degrade to
    "could not sample" (getpixel returns a palette index, an int), so every
    variant except the byte-exact control must come out RGB.
    """
    for preset, palettes in (("remap-auto", {"auto": "/tmp/pal.png"}),
                             ("flat6", None), ("poster6", None),
                             ("denoise-flat", None), ("flat6-dither", None)):
        argv = imf.filter_argv("magick", "i", "o", preset, None, palettes)
        assert "-define" in argv, preset
        assert argv[argv.index("-define") + 1] == "png:color-type=2", preset


def test_identity_argv_carries_no_defines():
    """The control stays untouched -- it is copied, not transcoded."""
    argv = imf.filter_argv("magick", "i", "o", "identity")
    assert "-define" not in argv


def test_remap_dither_variants_differ_in_argv():
    """The two readings on each palette axis must be distinguishable."""
    palettes = {"auto": "/tmp/pal.png"}
    plain = imf.filter_argv("magick", "i", "o", "remap-auto", None, palettes)
    dith = imf.filter_argv("magick", "i", "o", "remap-auto-dither", None,
                           palettes)
    assert plain != dith
    assert plain[plain.index("-dither") + 1] == "None"
    assert dith[dith.index("-dither") + 1] == "FloydSteinberg"


def test_preset_dither_reports_the_axis():
    assert imf.preset_dither("flat6") == "None"
    assert imf.preset_dither("flat6-dither") == "FloydSteinberg"
    assert imf.preset_dither("remap-spec") == "None"
    assert imf.preset_dither("median3") is None


# --------------------------------------------------------------------------
# Palette strips
# --------------------------------------------------------------------------

def test_write_palette_strip(tmp_path):
    path = str(tmp_path / "pal.png")
    assert imf.write_palette_strip(path, ["#FF0000", "#00FF00"])
    with Image.open(path) as image:
        assert image.size == (2, 1)
        assert image.convert("RGB").getpixel((1, 0)) == (0, 255, 0)


def test_write_palette_strip_drops_malformed_colours(tmp_path):
    """A typo must not reach the artwork -- same rule as palette_variants."""
    path = str(tmp_path / "pal.png")
    assert imf.write_palette_strip(path, ["not-a-colour", "#FF0000"])
    with Image.open(path) as image:
        assert image.size == (1, 1)


def test_write_palette_strip_with_nothing_usable(tmp_path):
    path = str(tmp_path / "pal.png")
    assert imf.write_palette_strip(path, ["nope", "#GGGGGG"]) is False
    assert not os.path.exists(path)


def test_source_palette_is_usable_hex(tmp_path):
    src = write_png(str(tmp_path / "art.png"), THREE_FLAT)
    colours = imf.source_palette(src, count=3)
    assert colours, "no palette derived from a three-colour image"
    for colour in colours:
        assert imf.front_common.hex_to_rgb(colour) is not None, colour


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------

def test_colour_stats_on_flat_bands(tmp_path):
    src = write_png(str(tmp_path / "art.png"), THREE_FLAT)
    stats = imf.colour_stats(src)
    assert stats["distinct_colours"] == 3
    assert stats["colours_at_95pct"] <= 3


def test_edge_energy_is_a_noise_measure(tmp_path):
    """A noisy image must score higher than a flat one, or the column lies."""
    import random

    flat = write_png(str(tmp_path / "flat.png"), THREE_FLAT)
    noisy_path = str(tmp_path / "noisy.png")
    random.seed(1234)
    image = Image.new("RGB", (64, 48))
    for y in range(48):
        for x in range(64):
            value = random.choice([0, 255])
            image.putpixel((x, y), (value, value, value))
    image.save(noisy_path)

    assert imf.edge_energy(flat) < imf.edge_energy(noisy_path)


def test_png_meta_records_editor_chunks(tmp_path):
    """sRGB/gAMA/cHRM mark an editor round-trip (GIMP), not a trace render."""
    src = write_png(str(tmp_path / "art.png"), THREE_FLAT)
    meta = imf.png_meta(src)
    assert meta["bytes"] > 0
    assert meta["size"] == [64, 48]
    assert isinstance(meta["chunks"], list)


# --------------------------------------------------------------------------
# Ordering -- a measurement order, never a recommendation
# --------------------------------------------------------------------------

def test_sort_key_puts_failures_last():
    good = {"preset": "a", "colours_at_95pct": 5, "edge_energy": 1.0}
    bad = {"preset": "b", "error": "boom"}
    assert sorted([bad, good], key=imf.sort_key)[0]["preset"] == "a"


def test_sort_key_prefers_fewer_colours_then_less_noise():
    coarse = {"preset": "c", "colours_at_95pct": 4, "edge_energy": 9.0}
    noisy = {"preset": "n", "colours_at_95pct": 4, "edge_energy": 1.0}
    wide = {"preset": "w", "colours_at_95pct": 900, "edge_energy": 0.1}
    order = [e["preset"] for e in sorted([wide, noisy, coarse],
                                         key=imf.sort_key)]
    assert order == ["n", "c", "w"]


# --------------------------------------------------------------------------
# Failure modes
# --------------------------------------------------------------------------

def test_missing_engine_exits_three(tmp_path, monkeypatch):
    """No ImageMagick must be rc 3 with an install hint, not a traceback."""
    src = write_png(str(tmp_path / "art.png"), THREE_FLAT)
    monkeypatch.setattr(imf, "engine_argv", lambda: (None, None))
    assert imf.build(src, str(tmp_path), ["flat6"], False, "#ffffff", 1,
                     False) == 3


def test_unknown_preset_is_refused(tmp_path):
    src = write_png(str(tmp_path / "art.png"), THREE_FLAT)
    assert imf.build(src, str(tmp_path), ["not-a-preset"], False, "#ffffff", 1,
                     False) == 2


def test_missing_source_is_refused(tmp_path):
    assert imf.build(str(tmp_path / "nope.png"), str(tmp_path), ["flat6"],
                     False, "#ffffff", 1, False) == 2


def test_bad_preset_is_reported_not_raised(tmp_path, monkeypatch):
    """One failing variant must not take down the bench."""
    src = write_png(str(tmp_path / "art.png"), THREE_FLAT)

    def explode(task):
        preset = task[3]
        if preset == "median3":
            return {"preset": preset, "output": "x", "error": "engine died"}
        return {"preset": preset, "output": "x", "bytes": 1}

    monkeypatch.setattr(imf, "run_preset", explode)
    rc = imf.build(src, str(tmp_path), ["median3", "median5"], False,
                   "#ffffff", 1, False)
    assert rc == 1
    with open(os.path.join(str(tmp_path), "art", "filters.json")) as handle:
        payload = json.load(handle)
    assert any(v.get("error") for v in payload["variants"])
    assert len(payload["variants"]) == 2


def test_list_does_not_need_a_raster(capsys):
    assert imf.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "flat6" in out and "remap-auto" in out


# --------------------------------------------------------------------------
# End to end, against the real engine
# --------------------------------------------------------------------------

@needs_imagemagick
def test_build_writes_variants_and_a_report(tmp_path):
    src = write_png(str(tmp_path / "art.png"), THREE_FLAT)
    rc = imf.build(src, str(tmp_path), ["identity", "flatten", "flat6"],
                   False, "#ffffff", 1, False)
    assert rc == 0
    base = os.path.join(str(tmp_path), "art")
    for name in ("identity.png", "flatten.png", "flat6.png",
                 "filters.json", "filters.md"):
        assert os.path.exists(os.path.join(base, name)), name
    with open(os.path.join(base, "filters.json")) as handle:
        payload = json.load(handle)
    assert payload["tool"] == "im_filters.py"
    assert payload["engine"]["version"]
    assert len(payload["variants"]) == 3


@needs_imagemagick
def test_flat6_actually_reduces_colours(tmp_path):
    """The preset must do what its name says, measured on the output."""
    src = write_ramp_png(str(tmp_path / "art.png"))
    before = imf.colour_stats(src)["colours_at_95pct"]
    rc = imf.build(src, str(tmp_path), ["flat6"], False, "#ffffff", 1, False)
    assert rc == 0
    out = os.path.join(str(tmp_path), "art", "flat6.png")
    after = imf.colour_stats(out)["colours_at_95pct"]
    assert after <= 6, after
    assert after < before, "the ramp fixture did not need reducing (%d)" % before


@needs_imagemagick
def test_dither_axis_changes_the_bytes(tmp_path):
    """The regression pin for the inert-Riemersma trap, end to end.

    If a future edit reintroduces an inert dither name, these two outputs
    collapse to the same bytes and this test fails.  The fixture must carry
    more colours than the preset keeps, or there is nothing to dither.
    """
    src = write_ramp_png(str(tmp_path / "art.png"))
    rc = imf.build(src, str(tmp_path), ["flat6", "flat6-dither"], False,
                   "#ffffff", 1, False)
    assert rc == 0
    base = os.path.join(str(tmp_path), "art")
    with open(os.path.join(base, "flat6.png"), "rb") as handle:
        plain = handle.read()
    with open(os.path.join(base, "flat6-dither.png"), "rb") as handle:
        dither = handle.read()
    assert plain != dither


@needs_imagemagick
def test_remap_presets_write_their_palette_strip(tmp_path):
    """The strip the chosen presets need is written."""
    src = write_png(str(tmp_path / "art.png"), THREE_FLAT)
    rc = imf.build(src, str(tmp_path), ["remap-auto", "remap-auto-dither"],
                   False, "#ffffff", 1, False, spec_palette=("#FF0000",
                                                             "#000000"))
    assert rc == 0
    base = os.path.join(str(tmp_path), "art")
    assert os.path.exists(os.path.join(base, imf.PALETTE_FILES["auto"]))


@needs_imagemagick
def test_only_the_palette_strips_in_use_are_built(tmp_path):
    """A strip for a preset that is not running must not appear.

    Also pins that a strip from an earlier run is not silently reused when the
    spec palette changed -- the auto strip is rebuilt from the source every
    run precisely so a stale palette cannot remap the artwork.
    """
    src = write_ramp_png(str(tmp_path / "art.png"))
    rc = imf.build(src, str(tmp_path), ["remap-auto"], False, "#ffffff", 1,
                   False, spec_palette=("#FF0000",))
    assert rc == 0
    base = os.path.join(str(tmp_path), "art")
    assert os.path.exists(os.path.join(base, imf.PALETTE_FILES["auto"]))
    assert not os.path.exists(os.path.join(base, imf.PALETTE_FILES["spec"]))

    rc = imf.build(src, str(tmp_path), ["remap-spec"], False, "#ffffff", 1,
                   False, spec_palette=("#FF0000",))
    assert rc == 0
    assert os.path.exists(os.path.join(base, imf.PALETTE_FILES["spec"]))
    with Image.open(os.path.join(base, imf.PALETTE_FILES["spec"])) as strip:
        assert strip.size == (1, 1), "spec strip is not the declared palette"


@needs_imagemagick
def test_no_variant_comes_out_indexed(tmp_path):
    """The end-to-end half of the colour-type rule."""
    src = write_ramp_png(str(tmp_path / "art.png"))
    presets = ["remap-auto", "flat6", "poster6", "denoise-flat"]
    rc = imf.build(src, str(tmp_path), presets, False, "#ffffff", 1, False,
                   spec_palette=("#FF0000",))
    assert rc == 0
    for preset in presets:
        out = os.path.join(str(tmp_path), "art", "%s.png" % preset)
        with Image.open(out) as image:
            assert image.mode == "RGB", (preset, image.mode)
    out = os.path.join(str(tmp_path), "art", "remap-auto.png")
    assert imf.colour_stats(out)["distinct_colours"] <= 6


@needs_imagemagick
def test_remap_without_a_spec_palette_is_skipped_not_guessed(tmp_path):
    """No usable spec.palette -> that variant reports an error, no invention."""
    src = write_png(str(tmp_path / "art.png"), THREE_FLAT)
    rc = imf.build(src, str(tmp_path), ["remap-spec"], False, "#ffffff", 1,
                   False, spec_palette=None)
    assert rc == 1
    with open(os.path.join(str(tmp_path), "art", "filters.json")) as handle:
        payload = json.load(handle)
    assert payload["variants"][0].get("error")


@needs_imagemagick
def test_parallel_matches_sequential(tmp_path):
    """Parallelism is an optimisation; it must not change the result.

    Byte comparison is a legitimate invariant here, not a fragile one:
    ImageMagick's PNG output was measured to be byte- and pixel-identical
    across 12 CONCURRENT identical conversions (one md5, one pixel hash), and
    it writes no timestamps -- only ``dpi`` and ``chromaticity`` chunks.
    """
    src = write_png(str(tmp_path / "art.png"), THREE_FLAT)
    presets = ["identity", "flatten", "flat6"]
    seq_dir = tmp_path / "seq"
    par_dir = tmp_path / "par"
    for target, workers in ((seq_dir, 1), (par_dir, 3)):
        os.makedirs(str(target), exist_ok=True)
        code = imf.build(src, str(target), presets, False, "#ffffff",
                         workers, False)
        if code != 0:
            # Surface WHY. A bare `assert rc == 0` turns a load-induced
            # transient (a worker that could not start under contention) into
            # an unexplainable failure, which is how this test first read.
            with open(os.path.join(str(target), "art", "filters.json")) as fh:
                payload = json.load(fh)
            raise AssertionError(
                "build(workers=%d) returned %d: %s"
                % (workers, code,
                   [(v["preset"], v.get("error")) for v in payload["variants"]
                    if v.get("error")]))
    for name in ("identity.png", "flatten.png", "flat6.png"):
        with open(os.path.join(str(seq_dir), "art", name), "rb") as handle:
            expected = handle.read()
        with open(os.path.join(str(par_dir), "art", name), "rb") as handle:
            actual = handle.read()
        assert expected == actual, name
