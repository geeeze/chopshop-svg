#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the colour-inverted source twin in scripts/prep_raster.py.

Synthetic fixtures only. Nothing here touches 00_source/ or 01_prepped/.

The contracts under test:

  1. ADDITIVE ONLY. The twin must never alter the normal prepped output --
     the pipeline traces that file, so a regression here silently changes
     every trace. The prep contract is also check-first: without --fix the
     prepped file must stay byte-identical to the input.
  2. OPT-IN AND OVERRIDABLE. Off by default in the spec; --invert/--no-invert
     override it without touching the spec file on disk.
  3. EXACT. `photometric` mode must satisfy out == 255 - in per channel.
  4. TRANSPARENCY-SAFE. `negative` must preserve a transparent matte; the
     naive invert would turn a transparent background opaque.
  5. NEVER CRASHES. Exotic modes (P, CMYK, L, 1, 16-bit) must produce a
     recorded outcome, not a traceback.
  6. SIDE-COMPATIBLE. The new top-level `inverse` key must not break the
     consumers that read .prep.json.
"""

import json
import os
import subprocess
import sys

import pytest
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
for p in (ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import prep_raster  # noqa: E402
import front_common as fc  # noqa: E402


def rgb_image(size=(16, 16), colour=(200, 100, 50)):
    return Image.new("RGB", size, colour)


def make_spec(tmp_path, invert=None, **print_over):
    pr = {"assume_opaque_bg": True, "prep_colors": None}
    if invert is not None:
        pr["invert"] = invert
    pr.update(print_over)
    spec = {
        "print_method": "screen_print",
        "max_colors": 6,
        "geometry": {"allow_gradients": False},
        "palette": ["#000000", "#FFFFFF"],
        "print": pr,
    }
    path = tmp_path / "spec.json"
    fc.write_json(str(path), spec)
    return str(path)


# ------------------------------------------------------------------ _invert

def test_photometric_invert_is_exact():
    img = rgb_image(colour=(200, 100, 50))
    out = prep_raster._invert(img, "photometric")
    assert out.mode == "RGB"
    assert out.getpixel((0, 0)) == (55, 155, 205)   # 255 - each channel


def test_photometric_default_mode():
    assert prep_raster._invert(rgb_image()).mode == "RGB"


def test_negative_preserves_transparent_matte():
    img = Image.new("RGBA", (4, 4), (255, 0, 0, 0))     # fully transparent red
    out = prep_raster._invert(img, "negative")
    assert out.mode == "RGBA", "alpha band must survive"
    assert out.getpixel((0, 0))[3] == 0, "transparent must stay transparent"
    assert out.getpixel((0, 0))[:3] == (0, 255, 255)   # chroma inverted


def test_negative_preserves_opaque_alpha():
    img = Image.new("RGBA", (4, 4), (255, 0, 0, 255))
    out = prep_raster._invert(img, "negative")
    assert out.getpixel((0, 0)) == (0, 255, 255, 255)


def test_la_mode_does_not_crash():
    img = Image.new("LA", (4, 4), (128, 64))
    out = prep_raster._invert(img, "negative")
    assert out.mode == "LA"
    assert out.getpixel((0, 0))[1] == 64, "alpha must be untouched"


def test_bad_mode_raises_value_error():
    with pytest.raises(ValueError):
        prep_raster._invert(rgb_image(), "sideways")


@pytest.mark.parametrize("mode,size", [
    ("P", (8, 8)), ("L", (8, 8)), ("1", (8, 8)), ("CMYK", (8, 8)),
    ("I;16", (8, 8)), ("RGB", (8, 8)), ("RGBA", (8, 8)),
])
def test_exotic_modes_do_not_crash(mode, size):
    img = Image.new(mode, size)
    out = prep_raster._invert(img, "photometric")
    assert out is not None and out.size == size


# ------------------------------------------------------------------ twin

def test_twin_not_written_when_not_requested(tmp_path):
    spec = make_spec(tmp_path, invert=False)
    src = tmp_path / "art.png"
    rgb_image((32, 32)).save(src)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    result = prep_raster._write_inverse(Image.open(src), str(out_dir),
                                        "art", fc.load_json(spec))
    assert result["ran"] is False
    assert not (out_dir / "art.prepped.inverse.png").exists()


def test_twin_written_when_spec_enables_it(tmp_path):
    spec = make_spec(tmp_path, invert=True)
    src = tmp_path / "art.png"
    rgb_image((32, 32), (10, 20, 30)).save(src)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    result = prep_raster._write_inverse(Image.open(src), str(out_dir),
                                        "art", fc.load_json(spec))
    assert result["ran"] is True
    path = out_dir / "art.prepped.inverse.png"
    assert path.is_file()
    assert result["sha256"] == fc.sha256_file(str(path))
    assert result["mode"] == "photometric"
    assert Image.open(path).getpixel((0, 0)) == (245, 235, 225)


def test_twin_accepts_a_dict_setting(tmp_path):
    spec = make_spec(tmp_path, invert={"mode": "negative"})
    src = tmp_path / "art.png"
    Image.new("RGBA", (8, 8), (255, 0, 0, 0)).save(src)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    result = prep_raster._write_inverse(Image.open(src), str(out_dir),
                                        "art", fc.load_json(spec))
    assert result["ran"] is True
    assert result["mode"] == "negative"
    assert Image.open(out_dir / "art.prepped.inverse.png").getpixel((0, 0))[3] == 0


def test_twin_size_matches_source(tmp_path):
    spec = make_spec(tmp_path, invert=True)
    src = tmp_path / "art.png"
    rgb_image((40, 24)).save(src)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    prep_raster._write_inverse(Image.open(src), str(out_dir), "art",
                               fc.load_json(spec))
    assert Image.open(out_dir / "art.prepped.inverse.png").size == (40, 24)


def test_twin_reports_bad_mode_without_crashing(tmp_path):
    spec = make_spec(tmp_path, invert={"mode": "nonsense"})
    src = tmp_path / "art.png"
    rgb_image().save(src)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    result = prep_raster._write_inverse(Image.open(src), str(out_dir),
                                        "art", fc.load_json(spec))
    assert result["ran"] is False
    assert "error" in result


# ------------------------------------------------------------------ driver

def test_check_mode_leaves_prepped_byte_identical_and_adds_twin(tmp_path):
    """The twin is additive: the traced file must be byte-identical."""
    spec = make_spec(tmp_path, invert=True)
    src = tmp_path / "art.png"
    rgb_image((32, 32), (77, 88, 99)).save(src)
    before = fc.sha256_file(str(src))
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    assert prep_raster._prep(str(src), fc.load_json(spec), str(out_dir),
                             fix=False) == 0
    prepped = out_dir / "art.prepped.png"
    assert fc.sha256_file(str(prepped)) == before, "check mode must copy through"
    assert (out_dir / "art.prepped.inverse.png").is_file()

    sidecar = json.loads((out_dir / "art.prep.json").read_text())
    assert sidecar["inverse"]["ran"] is True
    assert sidecar["output"]["sha256"] == before


def test_sidecar_records_a_skipped_twin(tmp_path):
    spec = make_spec(tmp_path, invert=False)
    src = tmp_path / "art.png"
    rgb_image((16, 16)).save(src)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    prep_raster._prep(str(src), fc.load_json(spec), str(out_dir), fix=False)
    sidecar = json.loads((out_dir / "art.prep.json").read_text())
    assert "inverse" in sidecar, "the key must exist even when skipped"
    assert sidecar["inverse"]["ran"] is False


def test_vector_input_records_that_there_is_nothing_to_invert(tmp_path):
    spec = make_spec(tmp_path, invert=True)
    src = tmp_path / "art.svg"
    src.write_text('<svg xmlns="http://www.w3.org/2000/svg" '
                   'viewBox="0 0 10 10"><rect width="10" height="10"/></svg>')
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    assert prep_raster._prep(str(src), fc.load_json(spec), str(out_dir),
                             fix=False) == 0
    sidecar = json.loads((out_dir / "art.prep.json").read_text())
    assert sidecar["already_vector"] is True
    assert sidecar["inverse"]["ran"] is False
    assert "vector" in sidecar["inverse"]["skipped"]


def test_run_record_and_closeout_tolerate_the_new_key(tmp_path):
    """The sidecar gained a top-level key; its consumers must not break."""
    spec = make_spec(tmp_path, invert=True)
    src = tmp_path / "art.png"
    rgb_image((16, 16)).save(src)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    prep_raster._prep(str(src), fc.load_json(spec), str(out_dir), fix=False)
    # These are the shapes the consumers read.
    sidecar = json.loads((out_dir / "art.prep.json").read_text())
    assert "output" in sidecar and "checks" in sidecar
    assert sidecar["inverse"]["ran"] is True
    # run_record reads output.file / output.sha256; both must be unchanged.
    assert os.path.isfile(sidecar["output"]["file"])


# ------------------------------------------------------------------ CLI

def _run_cli(args, cwd=None):
    return subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "prep_raster.py")] + args,
        capture_output=True, text=True, timeout=180, cwd=cwd or ROOT)


def test_cli_invert_flag(tmp_path):
    spec = make_spec(tmp_path, invert=False)
    src = tmp_path / "art.png"
    rgb_image((16, 16), (10, 10, 10)).save(src)
    out_dir = tmp_path / "out"
    r = _run_cli([str(src), spec, "--out-dir", str(out_dir), "--invert"])
    assert r.returncode == 0, r.stderr
    assert (out_dir / "art.prepped.inverse.png").is_file()
    assert "inverse twin" in r.stdout


def test_cli_no_invert_overrides_the_spec(tmp_path):
    spec = make_spec(tmp_path, invert=True)
    src = tmp_path / "art.png"
    rgb_image((16, 16)).save(src)
    out_dir = tmp_path / "out"
    r = _run_cli([str(src), spec, "--out-dir", str(out_dir), "--no-invert"])
    assert r.returncode == 0, r.stderr
    assert not (out_dir / "art.prepped.inverse.png").exists()


def test_cli_does_not_mutate_the_spec_file(tmp_path):
    spec = make_spec(tmp_path, invert=False)
    before = open(spec).read()
    src = tmp_path / "art.png"
    rgb_image((16, 16)).save(src)
    _run_cli([str(src), spec, "--out-dir", str(tmp_path / "o"), "--invert"])
    assert open(spec).read() == before, "the spec on disk must be untouched"


def test_cli_invert_mode_negative(tmp_path):
    spec = make_spec(tmp_path, invert=False)
    src = tmp_path / "art.png"
    Image.new("RGBA", (8, 8), (255, 0, 0, 0)).save(src)
    out_dir = tmp_path / "out"
    r = _run_cli([str(src), spec, "--out-dir", str(out_dir),
                  "--invert", "--invert-mode", "negative"])
    assert r.returncode == 0, r.stderr
    sidecar = json.loads((out_dir / "art.prep.json").read_text())
    assert sidecar["inverse"]["mode"] == "negative"


def test_cli_invert_mode_alone_does_not_enable_the_twin(tmp_path):
    """--invert-mode must not turn the feature on by itself."""
    spec = make_spec(tmp_path, invert=False)
    src = tmp_path / "art.png"
    rgb_image((16, 16)).save(src)
    out_dir = tmp_path / "out"
    r = _run_cli([str(src), spec, "--out-dir", str(out_dir),
                  "--invert-mode", "negative"])
    assert r.returncode == 0, r.stderr
    assert not (out_dir / "art.prepped.inverse.png").exists()


def test_cli_help_documents_the_flags():
    r = _run_cli(["--help"])
    assert r.returncode == 0
    for flag in ("--invert", "--no-invert", "--invert-mode"):
        assert flag in r.stdout


# ------------------------------------------------------------------ idempotence

def test_twin_is_idempotent(tmp_path):
    spec = make_spec(tmp_path, invert=True)
    src = tmp_path / "art.png"
    rgb_image((24, 24), (5, 90, 200)).save(src)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    a = prep_raster._write_inverse(Image.open(src), str(out_dir), "art",
                                   fc.load_json(spec))
    b = prep_raster._write_inverse(Image.open(src), str(out_dir), "art",
                                   fc.load_json(spec))
    assert a["sha256"] == b["sha256"]


def test_twin_is_invertible_back_to_the_original(tmp_path):
    """sanity: inverting the twin must return the source exactly"""
    spec = make_spec(tmp_path, invert=True)
    src = tmp_path / "art.png"
    rgb_image((16, 16), (33, 66, 99)).save(src)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    prep_raster._write_inverse(Image.open(src), str(out_dir), "art",
                               fc.load_json(spec))
    back = prep_raster._invert(Image.open(out_dir / "art.prepped.inverse.png"),
                               "photometric")
    assert back.getpixel((0, 0)) == (33, 66, 99)


# ------------------------------------------------------------------ bad specs

@pytest.mark.parametrize("print_value", [None, "nope", 7, []])
def test_non_dict_print_block_does_not_crash_the_cli(tmp_path, print_value):
    """`"print": null` must not raise.

    `spec.setdefault("print", {})` returns the existing None instead of
    replacing it, so the following item assignment raised TypeError and the
    script died with a traceback (rc=1) rather than its documented rc=2.
    """
    spec = {"print_method": "screen_print", "palette": ["#000000"],
            "print": print_value}
    spec_path = tmp_path / "spec.json"
    fc.write_json(str(spec_path), spec)
    src = tmp_path / "art.png"
    rgb_image((8, 8)).save(src)
    r = _run_cli([str(src), str(spec_path), "--out-dir", str(tmp_path / "o"),
                  "--invert"])
    assert r.returncode == 0, "traceback: %s" % r.stderr[-400:]
    assert "Traceback" not in r.stderr


def test_missing_print_block_is_created_for_the_cli_override(tmp_path):
    spec = {"print_method": "screen_print", "palette": ["#000000"]}
    spec_path = tmp_path / "spec.json"
    fc.write_json(str(spec_path), spec)
    src = tmp_path / "art.png"
    rgb_image((8, 8)).save(src)
    r = _run_cli([str(src), str(spec_path), "--out-dir", str(tmp_path / "o"),
                  "--invert"])
    assert r.returncode == 0, r.stderr[-400:]
    assert (tmp_path / "o" / "art.prepped.inverse.png").is_file()


# ------------------------------------------------- regressions (review found)

def test_bare_invert_does_not_clobber_a_specs_negative_mode(tmp_path):
    """`--invert` must not silently switch a spec's `negative` to photometric.

    `--invert-mode` defaulted to "photometric", so a bare `--invert` wrote that
    default over a spec saying {"mode": "negative"} -- flattening exactly the
    transparent matte the user had chosen `negative` to protect.
    """
    spec = {"print_method": "screen_print", "palette": ["#000000"],
            "print": {"invert": {"mode": "negative"}}}
    spec_path = tmp_path / "spec.json"
    fc.write_json(str(spec_path), spec)
    src = tmp_path / "art.png"
    Image.new("RGBA", (8, 8), (200, 30, 40, 0)).save(src)   # transparent
    out_dir = tmp_path / "o"
    r = _run_cli([str(src), str(spec_path), "--out-dir", str(out_dir),
                  "--invert"])
    assert r.returncode == 0, r.stderr[-400:]
    sidecar = json.loads((out_dir / "art.prep.json").read_text())
    assert sidecar["inverse"]["mode"] == "negative"
    twin = Image.open(out_dir / "art.prepped.inverse.png")
    assert twin.mode == "RGBA"
    assert twin.getpixel((0, 0))[3] == 0, "the matte must survive"


def test_bare_invert_still_defaults_to_photometric_for_a_plain_spec(tmp_path):
    spec = {"print_method": "screen_print", "palette": ["#000000"],
            "print": {"invert": True}}
    spec_path = tmp_path / "spec.json"
    fc.write_json(str(spec_path), spec)
    src = tmp_path / "art.png"
    rgb_image((8, 8)).save(src)
    out_dir = tmp_path / "o"
    _run_cli([str(src), str(spec_path), "--out-dir", str(out_dir), "--invert"])
    sidecar = json.loads((out_dir / "art.prep.json").read_text())
    assert sidecar["inverse"]["mode"] == "photometric"


def test_explicit_invert_mode_still_overrides_the_spec(tmp_path):
    spec = {"print_method": "screen_print", "palette": ["#000000"],
            "print": {"invert": {"mode": "negative"}}}
    spec_path = tmp_path / "spec.json"
    fc.write_json(str(spec_path), spec)
    src = tmp_path / "art.png"
    rgb_image((8, 8)).save(src)
    out_dir = tmp_path / "o"
    r = _run_cli([str(src), str(spec_path), "--out-dir", str(out_dir),
                  "--invert", "--invert-mode", "photometric"])
    assert r.returncode == 0, r.stderr[-400:]
    sidecar = json.loads((out_dir / "art.prep.json").read_text())
    assert sidecar["inverse"]["mode"] == "photometric"


def test_palette_image_with_transparency_keeps_its_matte(tmp_path):
    """A P-mode PNG carries transparency in a tRNS index, not an alpha band.

    It fell through to the plain convert("RGB") path, destroying the matte
    while the sidecar still reported mode "negative" -- the one thing that mode
    promises not to do.
    """
    pal = Image.new("P", (8, 8), 0)
    pal.putpalette([200, 30, 40] + [0, 0, 0] * 255)
    pal.info["transparency"] = 0
    out = prep_raster._invert(pal, "negative")
    assert out.mode == "RGBA", "a transparent palette image must keep alpha"
    assert out.getpixel((0, 0))[3] == 0


def test_photometric_documented_as_flattening_alpha(tmp_path):
    """Guard the honest statement: photometric inverts EVERY channel."""
    img = Image.new("RGBA", (4, 4), (200, 30, 40, 0))
    out = prep_raster._invert(img, "photometric")
    assert out.mode == "RGB", "photometric drops the alpha band by design"


def test_a_stale_twin_is_removed_when_invert_is_off(tmp_path):
    """Idempotency: a twin from an earlier run must not outlive the setting.

    `trace_sweep.py` and `run_record.py` glob 01_prepped/, so a stale file
    would be picked up as though the current run had produced it.
    """
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    stale = out_dir / "art.prepped.inverse.png"
    Image.new("RGB", (4, 4)).save(stale)
    result = prep_raster._write_inverse(
        Image.new("RGB", (4, 4)), str(out_dir), "art",
        {"print": {"invert": False}})
    assert result["ran"] is False
    assert not stale.exists(), "the stale twin survived"
    assert result.get("stale_file_removed") == str(stale)
