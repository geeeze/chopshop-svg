#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for scripts/prep_raster.py (front-half stage 1).

Synthetic fixtures only (Pillow); nothing here touches 00_source/.

The contract under test is check-first: the default mode reports acceptability
and copies the input through UNCHANGED; --fix applies transforms (aspect-
preserving) only when asked.
"""

import json
import os
import sys

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
for p in (ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import prep_raster  # noqa: E402
import front_common as fc  # noqa: E402


def make_spec(tmp_path, width_mm=300, height_mm=400, dpi=100, **overrides):
    spec = {
        "print_method": "screen_print",
        "max_colors": 6,
        "dimensions": {"width_mm": width_mm, "height_mm": height_mm},
        "geometry": {"allow_gradients": False},
        "palette": ["#000000", "#FFFFFF", "#FF0000"],
        "print": {
            "dpi": dpi,
            "prep_colors": 16,
            "background_hex": "#ffffff",
            "assume_opaque_bg": False,
            "sweep_max_candidates": 12,
        },
    }
    spec["print"].update(overrides)
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return str(path)


def make_raster(path, size=(200, 200), mode="RGB"):
    img = Image.new(mode, size, "#ffffff")
    for x in range(20, 70):
        for y in range(20, 70):
            img.putpixel((x, y), (0, 0, 0))
    for x in range(100, 160):
        for y in range(100, 160):
            img.putpixel((x, y), (255, 0, 0))
    img.save(path, "PNG")
    return str(path)


def _run(tmp_path, input_path, spec_path, fix=False):
    out_dir = tmp_path / "out"
    argv = [input_path, spec_path, "--out-dir", str(out_dir)]
    if fix:
        argv.append("--fix")
    code = prep_raster.main(argv)
    return code, str(out_dir)


def _load_sidecar(out_dir, stem="art"):
    return json.load(open(os.path.join(out_dir, "%s.prep.json" % stem),
                          encoding="utf-8"))


def test_check_mode_copies_through_unchanged(tmp_path):
    src = make_raster(tmp_path / "art.png")
    spec = make_spec(tmp_path)
    code, out_dir = _run(tmp_path, src, spec)

    assert code == 0
    out_png = os.path.join(out_dir, "art.prepped.png")
    assert os.path.exists(out_png)

    # byte-identical to the input: prep did NOT modify anything
    assert fc.sha256_file(out_png) == fc.sha256_file(src)
    assert Image.open(out_png).mode == Image.open(src).mode

    sidecar = _load_sidecar(out_dir)
    assert sidecar["mode"] == "check"
    assert sidecar["output"]["unchanged"] is True
    assert isinstance(sidecar["checks"], list)
    assert any(c["check"] == "color_mode" for c in sidecar["checks"])


def test_check_mode_reports_aspect_mismatch(tmp_path):
    # 200x100 is landscape (2.0); the spec orders 300x400mm portrait (0.75).
    img = Image.new("RGB", (200, 100), "#ffffff")
    src = tmp_path / "art.png"
    img.save(src, "PNG")
    spec = make_spec(tmp_path)
    code, out_dir = _run(tmp_path, str(src), spec)

    assert code == 0
    sidecar = _load_sidecar(out_dir)
    aspect = next(c for c in sidecar["checks"] if c["check"] == "aspect_ratio")
    assert aspect["status"] == "fail"
    assert sidecar["acceptable"] is False


def test_check_mode_reports_solid_background(tmp_path):
    src = make_raster(tmp_path / "art.png")
    spec = make_spec(tmp_path)
    code, out_dir = _run(tmp_path, src, spec)

    assert code == 0
    sidecar = _load_sidecar(out_dir)
    bg = next(c for c in sidecar["checks"] if c["check"] == "background")
    assert bg["status"] == "pass"  # white border is uniform


def test_vector_input_is_copied_through(tmp_path):
    src = tmp_path / "art.svg"
    src.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    spec = make_spec(tmp_path)
    code, out_dir = _run(tmp_path, str(src), spec)

    assert code == 0
    dst = os.path.join(out_dir, "art.svg")
    assert os.path.exists(dst)
    assert dst != str(src)
    assert open(dst, encoding="utf-8").read() == src.read_text(encoding="utf-8")
    assert _load_sidecar(out_dir)["already_vector"] is True


def test_fix_mode_preserves_aspect_ratio(tmp_path, monkeypatch):
    # Force the optional binaries absent so --fix exercises the Pillow path.
    real_which = fc.which

    def fake_which(name):
        if name in ("rembg", "realesrgan-ncnn-vulkan", "pngquant"):
            return None
        return real_which(name)

    monkeypatch.setattr(prep_raster.fc, "which", fake_which)

    # 100x50 landscape (2.0), dpi 20 -> target (236, 315) for 300x400mm.
    img = Image.new("RGB", (100, 50), "#ffffff")
    src = tmp_path / "art.png"
    img.save(src, "PNG")
    spec = make_spec(tmp_path, dpi=20)
    code, out_dir = _run(tmp_path, str(src), spec, fix=True)

    assert code == 0
    out = Image.open(os.path.join(out_dir, "art.prepped.png"))
    # upscaled to fit WITHIN the target without stretching: aspect stays 2.0
    assert out.size == (236, 118)
    assert abs(out.size[0] / out.size[1] - 2.0) < 1e-6

    sidecar = _load_sidecar(out_dir)
    assert sidecar["mode"] == "fix"
    up = sidecar["steps"]["upscale"]
    assert up["ran"] is True
    assert up["aspect_preserved"] is True


def test_missing_optional_tools_do_not_crash_check_mode(tmp_path, monkeypatch):
    real_which = fc.which

    def fake_which(name):
        if name in ("rembg", "realesrgan-ncnn-vulkan", "pngquant"):
            return None
        return real_which(name)

    monkeypatch.setattr(prep_raster.fc, "which", fake_which)

    src = make_raster(tmp_path / "art.png")
    spec = make_spec(tmp_path)
    code, out_dir = _run(tmp_path, src, spec)

    assert code == 0
    assert os.path.exists(os.path.join(out_dir, "art.prepped.png"))
    sidecar = _load_sidecar(out_dir)
    assert sidecar["versions"]["rembg"] is None
    assert sidecar["versions"]["pngquant"] is None


def test_missing_input_is_usage_error(tmp_path):
    spec = make_spec(tmp_path)
    code, _ = _run(tmp_path, str(tmp_path / "nope.png"), spec)
    assert code == 2


def test_no_dimensions_is_non_prescriptive(tmp_path):
    # With no dimensions block, geometry is informational, never a fail, and
    # the file is judged acceptable on its own terms.
    spec = {
        "print_method": "screen_print",
        "max_colors": 6,
        "geometry": {"allow_gradients": False},
        "palette": ["#000000", "#FFFFFF", "#FF0000"],
        "print": {"dpi": 100},
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    img = Image.new("RGB", (200, 100), "#ffffff")
    src = tmp_path / "art.png"
    img.save(src, "PNG")

    code, out_dir = _run(tmp_path, str(src), str(spec_path))
    assert code == 0

    sidecar = _load_sidecar(out_dir)
    geom = next(c for c in sidecar["checks"] if c["check"] == "geometry")
    assert geom["status"] == "info"
    assert "200x100px" in geom["detail"]
    assert sidecar["acceptable"] is True

