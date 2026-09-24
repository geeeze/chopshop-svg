#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for scripts/compare_candidates.py (front-half stage 3).

Three synthetic candidates with known declared-colour and node counts, plus the
zero-candidate case.  Layer A metrics (declared colours, node counts) are
deterministic, so those are what we assert precisely.
"""

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
for p in (ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import compare_candidates  # noqa: E402


def make_spec(tmp_path):
    spec = {
        "print_method": "screen_print",
        "max_colors": 6,
        "geometry": {
            "min_stroke_width_pt": 0,
            "max_nodes_per_path": 500,
            "allow_raster_embed": False,
            "allow_gradients": False,
            "allow_open_paths": True,
            "gradient_handling": "vector_halftone",
            "halftone_handling": "vector_halftone",
        },
        "palette": ["#000000", "#ffffff", "#ff0000"],
        "print": {"dpi": 72},
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    return str(path)


SVG_HEAD = '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">'

CANDIDATE_SVGS = {
    # 2 declared colours, no paths
    "candidate_01.svg":
        SVG_HEAD
        + '<rect x="0" y="0" width="100" height="100" fill="#ffffff"/>'
        + '<rect x="10" y="10" width="40" height="40" fill="#000000"/>'
        + "</svg>",
    # 3 declared colours, no paths
    "candidate_02.svg":
        SVG_HEAD
        + '<rect x="0" y="0" width="100" height="100" fill="#ffffff"/>'
        + '<rect x="10" y="10" width="40" height="40" fill="#000000"/>'
        + '<rect x="60" y="60" width="30" height="30" fill="#ff0000"/>'
        + "</svg>",
    # 2 declared colours, two paths: 4 + 3 = 7 segments, max 4
    "candidate_03.svg":
        SVG_HEAD
        + '<rect x="0" y="0" width="100" height="100" fill="#ffffff"/>'
        + '<path d="M0 0 L10 0 L10 10 L0 10 Z" fill="#000000"/>'
        + '<path d="M20 20 L30 20 L30 25 Z" fill="#000000"/>'
        + "</svg>",
}

SWEEP = {
    "candidates": [
        {"file": "candidate_01.svg", "preset": "bw", "filter_speckle": 2,
         "hierarchical": "cutout", "use_palette": False,
         "params": {"colormode": "binary", "filter_speckle": 2,
                    "hierarchical": "cutout"}},
        {"file": "candidate_02.svg", "preset": "poster", "filter_speckle": 8,
         "hierarchical": "stacked", "use_palette": True,
         "params": {"colormode": "color", "mode": "spline",
                    "filter_speckle": 8, "hierarchical": "stacked"}},
        {"file": "candidate_03.svg", "preset": "bw", "filter_speckle": 16,
         "hierarchical": "cutout", "use_palette": False,
         "params": {"colormode": "binary", "filter_speckle": 16,
                    "hierarchical": "cutout"}},
    ]
}


def _make_traced_dir(tmp_path, with_sweep=True):
    traced = tmp_path / "traced" / "art"
    traced.mkdir(parents=True)
    for name, body in CANDIDATE_SVGS.items():
        (traced / name).write_text(body, encoding="utf-8")
    if with_sweep:
        (traced / "sweep.json").write_text(json.dumps(SWEEP), encoding="utf-8")
    return str(traced)


def _run(tmp_path, traced_dir, spec_path, workers=None):
    out_dir = tmp_path / "04_validated"
    argv = [traced_dir, spec_path, "--out-dir", str(out_dir)]
    if workers is not None:
        argv += ["--workers", str(workers)]
    code = compare_candidates.main(argv)
    return code, str(out_dir)


def test_comparison_reports_correct_counts(tmp_path):
    traced = _make_traced_dir(tmp_path)
    spec = make_spec(tmp_path)
    code, out_dir = _run(tmp_path, traced, spec)

    assert code == 0
    payload = json.load(open(os.path.join(out_dir, "art.comparison.json"),
                             encoding="utf-8"))
    candidates = payload["candidates"]
    assert len(candidates) == 3

    files = [c["file"] for c in candidates]
    assert files == ["candidate_01.svg", "candidate_02.svg",
                     "candidate_03.svg"]

    by_file = {c["file"]: c for c in candidates}
    assert by_file["candidate_01.svg"]["declared_colors"] == 2
    assert by_file["candidate_02.svg"]["declared_colors"] == 3
    assert by_file["candidate_03.svg"]["declared_colors"] == 2

    assert by_file["candidate_01.svg"]["node_count_total"] == 0
    assert by_file["candidate_03.svg"]["node_count_total"] == 7
    assert by_file["candidate_03.svg"]["node_count_max"] == 4

    # every candidate carries its sweep parameters and a file size
    for c in candidates:
        assert "sweep" in c
        assert c["sweep"]["preset"]
        assert c["file_size"] > 0


def test_markdown_table_is_written(tmp_path):
    traced = _make_traced_dir(tmp_path)
    spec = make_spec(tmp_path)
    code, out_dir = _run(tmp_path, traced, spec)

    assert code == 0
    md_path = os.path.join(out_dir, "art.comparison.md")
    assert os.path.exists(md_path)
    md = open(md_path, encoding="utf-8").read()
    assert "candidate_01.svg" in md
    assert "LayerA" in md and "LayerB" in md


def test_zero_candidates_exits_cleanly(tmp_path):
    traced = tmp_path / "traced" / "empty"
    traced.mkdir(parents=True)
    spec = make_spec(tmp_path)
    code, out_dir = _run(tmp_path, str(traced), spec)

    assert code == 0
    payload = json.load(open(os.path.join(out_dir, "empty.comparison.json"),
                             encoding="utf-8"))
    assert payload["candidate_count"] == 0
    assert payload["candidates"] == []


def test_sorted_by_hard_then_advisory(tmp_path):
    # A 4-colour candidate (over a 3-colour budget) must sort after the
    # clean 2-colour ones, even though all have the same advisory count.
    traced = tmp_path / "traced" / "art"
    traced.mkdir(parents=True)
    (traced / "candidate_01.svg").write_text(
        SVG_HEAD
        + '<rect x="0" y="0" width="100" height="100" fill="#ffffff"/>'
        + '<rect x="10" y="10" width="40" height="40" fill="#000000"/>'
        + "</svg>", encoding="utf-8")
    (traced / "candidate_02.svg").write_text(
        SVG_HEAD
        + '<rect x="0" y="0" width="100" height="100" fill="#ffffff"/>'
        + '<rect x="10" y="10" width="40" height="40" fill="#000000"/>'
        + '<rect x="20" y="20" width="30" height="30" fill="#ff0000"/>'
        + '<rect x="30" y="30" width="20" height="20" fill="#0000ff"/>'
        + "</svg>", encoding="utf-8")
    (traced / "sweep.json").write_text(json.dumps({
        "candidates": [
            {"file": "candidate_01.svg", "preset": "bw",
             "filter_speckle": 2, "hierarchical": "cutout",
             "use_palette": False, "params": {}},
            {"file": "candidate_02.svg", "preset": "bw",
             "filter_speckle": 2, "hierarchical": "cutout",
             "use_palette": False, "params": {}},
        ]}), encoding="utf-8")

    spec = make_spec(tmp_path)
    spec_obj = json.load(open(spec, encoding="utf-8"))
    spec_obj["max_colors"] = 3
    spec_obj["palette"] = ["#000000", "#ffffff", "#ff0000", "#0000ff"]
    spec = str(tmp_path / "spec2.json")
    json.dump(spec_obj, open(spec, "w", encoding="utf-8"))

    code, out_dir = _run(tmp_path, str(traced), spec)
    assert code == 0
    payload = json.load(open(os.path.join(out_dir, "art.comparison.json"),
                             encoding="utf-8"))
    candidates = payload["candidates"]
    assert candidates[0]["file"] == "candidate_01.svg"
    assert candidates[0]["hard"] < candidates[1]["hard"]


def test_fidelity_metric_measures(tmp_path):
    # A white-on-white trace should diff to ~0 against a white source.  The
    # sweep.json points input.file at the source so it is auto-resolved.
    from PIL import Image
    src = tmp_path / "src.png"
    Image.new("RGB", (40, 40), "#ffffff").save(src)

    traced = tmp_path / "traced" / "art"
    traced.mkdir(parents=True)
    (traced / "candidate_01.svg").write_text(
        SVG_HEAD.replace('width="100" height="100" viewBox="0 0 100 100"',
                         'width="40" height="40" viewBox="0 0 40 40"')
        + '<rect width="40" height="40" fill="#ffffff"/></svg>',
        encoding="utf-8")
    (traced / "sweep.json").write_text(json.dumps({
        "input": {"file": str(src), "sha256": "x"},
        "candidates": [{"file": "candidate_01.svg", "preset": "bw",
                        "filter_speckle": 2, "hierarchical": "cutout",
                        "use_palette": False, "params": {}}],
    }), encoding="utf-8")

    spec = make_spec(tmp_path)
    code, out_dir = _run(tmp_path, str(traced), spec)
    assert code == 0

    payload = json.load(open(os.path.join(out_dir, "art.comparison.json"),
                             encoding="utf-8"))
    fid = payload["candidates"][0]["fidelity"]
    assert fid["measured"] is True
    assert 0.0 <= fid["mae"] < 2.0


def test_fidelity_not_measured_without_source(tmp_path):
    # No sweep.json input.file and no --source -> fidelity is "not measured",
    # never a crash.
    traced = _make_traced_dir(tmp_path, with_sweep=False)
    spec = make_spec(tmp_path)
    code, out_dir = _run(tmp_path, str(traced), spec)
    assert code == 0

    payload = json.load(open(os.path.join(out_dir, "art.comparison.json"),
                             encoding="utf-8"))
    for c in payload["candidates"]:
        assert c["fidelity"]["measured"] is False


def test_parallel_matches_sequential(tmp_path):
    # Comparison with one worker and with several must produce identical
    # per-candidate results.  Layer B renders via Inkscape, which crashes when
    # concurrent instances race on D-Bus; this test locks in the workaround.
    traced = _make_traced_dir(tmp_path)
    spec = make_spec(tmp_path)
    code_s, dir_s = _run(tmp_path, str(traced), spec, workers=1)
    code_p, dir_p = _run(tmp_path, str(traced), spec, workers=4)
    assert code_s == 0 and code_p == 0

    def _strip(payload):
        payload["generated"] = ""
        for c in payload["candidates"]:
            c.pop("file_size", None)
        return payload

    s = json.load(open(os.path.join(dir_s, "art.comparison.json"),
                       encoding="utf-8"))
    p = json.load(open(os.path.join(dir_p, "art.comparison.json"),
                       encoding="utf-8"))
    assert _strip(s) == _strip(p)
