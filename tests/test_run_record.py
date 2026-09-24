#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for scripts/run_record.py and scripts/closeout.py.

Synthetic in-test fixtures only (never the real 00_source/ batch): we build a
minimal run layout in a tmp dir, monkeypatch the scripts' directory constants
to point at it, and assert the provenance stitching + closeout board behave.
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
for p in (ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import run_record as rr  # noqa: E402
import closeout as co  # noqa: E402
import front_common as fc  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture: a synthetic run laid out exactly like the real artifact dirs.
# ---------------------------------------------------------------------------

def _write(path, content=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode())


def make_run(tmp_path, *, duplicate=False):
    """Build a synthetic run and point run_record's constants at it."""
    root = tmp_path / "proj"
    rr.SOURCE_DIR = root / "00_source"
    rr.PREPPED_DIR = root / "01_prepped"
    rr.TRACED_DIR = root / "02_traced"
    rr.VALIDATED_DIR = root / "04_validated"
    rr.FINAL_DIR = root / "05_final"

    stem = "demo"
    for d in (rr.SOURCE_DIR, rr.PREPPED_DIR, rr.TRACED_DIR / stem,
              rr.VALIDATED_DIR, rr.FINAL_DIR):
        d.mkdir(parents=True, exist_ok=True)
    source = rr.SOURCE_DIR / "demo.png"
    _write(source, b"source-png-bytes")
    source_sha = fc.sha256_file(source)

    # prep
    prep = {
        "acceptable": True,
        "input": {"file": "00_source/demo.png", "sha256": source_sha,
                  "size_px": [100, 100]},
        "checks": [{"check": "format", "status": "pass"}],
        "versions": {"pillow": "12.0"},
    }
    fc.write_json(rr.PREPPED_DIR / "demo.prep.json", prep)

    # two candidate SVGs; candidate_02 is a SHA duplicate of candidate_01
    svg1 = rr.TRACED_DIR / stem / "candidate_01.svg"
    svg2 = rr.TRACED_DIR / stem / "candidate_02.svg"
    _write(svg1, b"<svg>one</svg>")
    _write(svg2, b"<svg>one</svg>" if duplicate else b"<svg>two</svg>")
    sha1 = fc.sha256_file(svg1)
    sha2 = fc.sha256_file(svg2)

    sweep = {
        "backend": {"kind": "vtracer_api", "version": "0.6.15"},
        "deterministic": True, "truncated": False,
        "candidates": [
            {"file": "candidate_01.svg", "output_sha256": sha1,
             "preset": "poster", "filter_speckle": 4, "hierarchical": "stacked",
             "use_palette": False, "source_sha256": source_sha,
             "params": {"colormode": "color", "color_precision": 6}},
            {"file": "candidate_02.svg", "output_sha256": sha2,
             "preset": "bw", "filter_speckle": 8, "hierarchical": "cutout",
             "use_palette": False, "source_sha256": source_sha,
             "params": {"colormode": "binary"}},
        ],
    }
    fc.write_json(rr.TRACED_DIR / stem / "sweep.json", sweep)

    comparison = {
        "candidate_count": 2,
        "layers_available": {"a": True, "b": True},
        "candidates": [
            {"file": "candidate_01.svg", "passed": True, "hard": 0, "advisory": 1,
             "rendered_ink_colors": 4, "node_count_max": 200, "node_count_total": 900,
             "file_size": 12,
             "layer_a": {"status": "pass", "hard": 0, "advisory": 1, "findings": []},
             "layer_b": {"status": "pass", "hard": 0, "advisory": 0, "findings": []},
             "fidelity": {"measured": True, "mae": 5.0, "mae_art": 6.0},
             "sweep": {"file": "candidate_01.svg", "output_sha256": sha1,
                       "preset": "poster", "params": {"colormode": "color"}}},
            {"file": "candidate_02.svg", "passed": False, "hard": 1, "advisory": 0,
             "rendered_ink_colors": 2, "node_count_max": 501, "node_count_total": 1000,
             "file_size": 12,
             "layer_a": {"status": "pass", "hard": 0, "advisory": 0, "findings": []},
             "layer_b": {"status": "fail", "hard": 1, "advisory": 0, "findings": []},
             "fidelity": {"measured": True, "mae": 50.0, "mae_art": 52.0},
             "sweep": {"file": "candidate_02.svg", "output_sha256": sha2,
                       "preset": "bw", "params": {"colormode": "binary"}}},
        ],
    }
    fc.write_json(rr.VALIDATED_DIR / "demo.comparison.json", comparison)

    # back half for candidate_01 only
    manifest = {
        "passed": True,
        "summary": {"hard": 0, "advisory": 1},
        "stats": {"tac_exact": True, "rendered_continuous_tone": False,
                  "rendered_ink_colors": 4, "rendered_distinct_colors": 4,
                  "hard_findings": 0, "advisory_findings": 1,
                  "static": {"path_nodes_max": 200, "color_count": 4}},
    }
    fc.write_json(rr.FINAL_DIR / "candidate_01.manifest.json", manifest)
    _write(rr.FINAL_DIR / "candidate_01.proof.png", b"proof")
    _write(rr.FINAL_DIR / "candidate_01.print.pdf", b"%PDF")
    _write(rr.FINAL_DIR / "candidate_01.layer_b.txt", "layer b")
    _write(rr.VALIDATED_DIR / "candidate_01.layer_a.txt", "layer a")
    fc.write_json(rr.FINAL_DIR / "candidate_01.pick.json",
                  {"selected": True, "label": "production", "reason": "clean"})
    _write(rr.FINAL_DIR / "candidate_01.visual_review.md", "# review")

    return root, stem, source_sha


# ---------------------------------------------------------------------------
# run_record tests
# ---------------------------------------------------------------------------

def test_build_run_stitches_source_and_sweep(tmp_path):
    _, stem, source_sha = make_run(tmp_path)
    run = rr.build_run(stem)
    assert run["run"]["source"]["sha256"] == source_sha
    assert run["run"]["sweep"]["backend"]["kind"] == "vtracer_api"
    assert run["run"]["missing_links"] == []


def test_build_run_candidates_carry_back_half_and_review(tmp_path):
    _, stem, _ = make_run(tmp_path)
    run = rr.build_run(stem)
    c01 = next(c for c in run["candidates"] if c["id"] == "candidate_01")
    c02 = next(c for c in run["candidates"] if c["id"] == "candidate_02")

    assert c01["back_half"]["run"] is True
    assert c01["back_half"]["manifest"]["stats"]["tac_exact"] is True
    assert c01["human_decision"]["label"] == "production"
    assert c01["visual_review"]["found"] is True
    assert c01["comparison"]["passed"] is True

    assert c02["back_half"]["run"] is False
    assert "no human decision" in c02["open_items"]
    assert any("back half not run" in m for m in c02["open_items"])
    assert c02["missing_links"] == []  # not chosen is an open item, not an anomaly


def test_build_run_flags_sha_duplicates(tmp_path):
    _, stem, _ = make_run(tmp_path, duplicate=True)
    run = rr.build_run(stem)
    c02 = next(c for c in run["candidates"] if c["id"] == "candidate_02")
    assert c02["duplicate_of"] == "candidate_01"
    assert any("sha duplicate" in m for m in c02["open_items"])
    assert run["run"]["duplicate_groups"]


def test_main_writes_run_json(tmp_path):
    root, stem, _ = make_run(tmp_path)
    out = root / "06_run"
    code = rr.main(["--stem", stem, "--out-dir", str(out)])
    assert code == 0
    written = out / "demo.run.json"
    assert written.exists()
    data = json.loads(written.read_text())
    assert data["stem"] == stem
    assert len(data["candidates"]) == 2


def test_main_no_runs_exits_2(tmp_path):
    rr.TRACED_DIR = tmp_path / "empty_traced"
    code = rr.main(["--all", "--out-dir", str(tmp_path / "out")])
    assert code == 2


# ---------------------------------------------------------------------------
# closeout tests
# ---------------------------------------------------------------------------

def _requirements(tmp_path):
    path = tmp_path / "requirements.json"
    fc.write_json(path, {
        "requirements": [
            {"id": "R-01", "requirement": "recognizable", "source": "brief",
             "category": "style", "check": {"type": "visual_review_present"}},
            {"id": "R-03", "requirement": "colour budget", "source": "spec",
             "category": "production",
             "check": {"type": "rendered_colors_le", "value": 6}},
            {"id": "R-07", "requirement": "vtracer only", "source": "constraint",
             "category": "bookkeeping", "check": {"type": "vtracer_only"}},
            {"id": "R-08", "requirement": "not duplicate", "source": "sweep",
             "category": "bookkeeping", "check": {"type": "unique"}},
        ]
    })
    return str(path)


def test_closeout_evaluate_check_types(tmp_path):
    _, stem, _ = make_run(tmp_path)
    run = rr.build_run(stem)
    c01 = next(c for c in run["candidates"] if c["id"] == "candidate_01")
    c02 = next(c for c in run["candidates"] if c["id"] == "candidate_02")

    vtracer_req = {"check": {"type": "vtracer_only"}}
    assert co.evaluate(vtracer_req, c01, run) == co.PASS

    colors_req = {"check": {"type": "rendered_colors_le", "value": 6}}
    assert co.evaluate(colors_req, c01, run) == co.PASS   # 4 <= 6

    visual_req = {"check": {"type": "visual_review_present"}}
    assert co.evaluate(visual_req, c01, run) == co.PASS   # review exists
    assert co.evaluate(visual_req, c02, run) == co.PENDING  # missing -> pending

    tac_req = {"check": {"type": "tac_exact"}}
    assert co.evaluate(tac_req, c01, run) == co.PASS      # tac_exact True
    assert co.evaluate(tac_req, c02, run) == co.NA        # not run back half


def test_closeout_complete_run_exits_0(tmp_path, capsys):
    root, stem, _ = make_run(tmp_path)
    run_path = root / "06_run" / "demo.run.json"
    run_path.parent.mkdir(parents=True, exist_ok=True)
    fc.write_json(run_path, rr.build_run(stem))
    reqs = _requirements(tmp_path)

    # Waive the visual/human style checks so the run is closed.
    code = co.main([str(run_path), "--requirements", reqs, "--waive", "R-01"])
    out = capsys.readouterr().out
    assert "Candidate board" in out
    assert "production-ready candidates: 1" in out
    assert code == 0


def test_closeout_pending_visual_exits_1(tmp_path, capsys):
    root, stem, _ = make_run(tmp_path)
    run_path = root / "06_run" / "demo.run.json"
    run_path.parent.mkdir(parents=True, exist_ok=True)
    fc.write_json(run_path, rr.build_run(stem))
    reqs = _requirements(tmp_path)

    # candidate_02 has no visual review -> R-01 PENDING, so incomplete.
    code = co.main([str(run_path), "--requirements", reqs])
    assert code == 1
    out = capsys.readouterr().out
    assert "INCOMPLETE" in out


def test_closeout_no_gate_exits_0(tmp_path):
    root, stem, _ = make_run(tmp_path)
    run_path = root / "06_run" / "demo.run.json"
    run_path.parent.mkdir(parents=True, exist_ok=True)
    fc.write_json(run_path, rr.build_run(stem))
    reqs = _requirements(tmp_path)
    code = co.main([str(run_path), "--requirements", reqs, "--no-gate"])
    assert code == 0
