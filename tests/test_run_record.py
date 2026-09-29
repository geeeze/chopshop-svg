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


def make_run(tmp_path, *, duplicate=False, scoped=False,
             scoped_label="style_reference", stem="demo"):
    """Build a synthetic run and point run_record's constants at it.

    ``scoped`` also writes the artwork-scoped artefact set that pipeline.sh
    publishes under ``05_final/<stem>/`` (and ``04_validated/<stem>.<cand>.*``),
    with content that differs from the flat set so a test can tell which name the
    record actually read.  Without it the layout is the pre-fix flat-only one,
    which the fallback path still has to read.
    """
    root = tmp_path / "proj"
    rr.SOURCE_DIR = root / "00_source"
    rr.PREPPED_DIR = root / "01_prepped"
    rr.TRACED_DIR = root / "02_traced"
    rr.VALIDATED_DIR = root / "04_validated"
    rr.FINAL_DIR = root / "05_final"

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

    # The artwork-scoped set pipeline.sh now publishes NEXT TO those flat names.
    # Content deliberately differs per artwork, so a cross-artwork read shows up
    # as a wrong hash / wrong label rather than as a silently plausible value.
    if scoped:
        scoped_dir = rr.FINAL_DIR / stem
        scoped_dir.mkdir(parents=True, exist_ok=True)
        scoped_manifest = dict(manifest)
        scoped_manifest["notes"] = [f"scoped back half for {stem}"]
        fc.write_json(scoped_dir / "candidate_01.manifest.json", scoped_manifest)
        _write(scoped_dir / "candidate_01.proof.png", f"scoped proof {stem}")
        _write(scoped_dir / "candidate_01.print.pdf", f"%PDF scoped {stem}")
        _write(scoped_dir / "candidate_01.layer_b.txt", "scoped layer b")
        _write(scoped_dir / "candidate_01.visual_review.md", "# scoped review")
        fc.write_json(scoped_dir / "candidate_01.pick.json",
                      {"selected": True, "label": scoped_label,
                       "reason": f"scoped pick for {stem}"})
        _write(rr.VALIDATED_DIR / f"{stem}.candidate_01.layer_a.txt",
               f"scoped layer a {stem}")

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


# ---------------------------------------------------------------------------
# Artefact identity: the artwork-scoped name is this run's; the flat name is
# shared by every artwork with that candidate file stem, so it is a flagged
# fallback, never a silent read.
# ---------------------------------------------------------------------------

def test_artwork_scoped_artefacts_are_preferred(tmp_path):
    """(1) When both names exist, the artwork-scoped set is what is recorded."""
    _, stem, _ = make_run(tmp_path, scoped=True)
    run = rr.build_run(stem)
    c01 = next(c for c in run["candidates"] if c["id"] == "candidate_01")
    back = c01["back_half"]
    scoped_dir = rr.FINAL_DIR / stem

    assert back["run"] is True
    assert back["manifest"]["path"] == str(scoped_dir / "candidate_01.manifest.json")
    assert back["manifest"]["path_scope"] == "artwork"
    # Hash identity: the record read THIS artwork's file, not the flat sibling.
    assert back["manifest"]["sha256"] == fc.sha256_file(
        scoped_dir / "candidate_01.manifest.json")
    assert back["manifest"]["sha256"] != fc.sha256_file(
        rr.FINAL_DIR / "candidate_01.manifest.json")
    assert back["proof_png"]["path_scope"] == "artwork"
    assert back["proof_png"]["sha256"] == fc.sha256_file(
        scoped_dir / "candidate_01.proof.png")
    assert back["print_pdf"]["path_scope"] == "artwork"
    assert back["layer_b_txt"]["path_scope"] == "artwork"
    # Layer A's scoped name is a stem-prefixed sibling in 04_validated/, not a
    # subdirectory, so the orphan sweep still keys it to the artwork.
    assert back["layer_a_txt"]["path"] == str(
        rr.VALIDATED_DIR / f"{stem}.candidate_01.layer_a.txt")
    assert back["layer_a_txt"]["path_scope"] == "artwork"

    # The human's pick and the visual review follow the same preference.
    assert c01["human_decision"]["label"] == "style_reference"  # scoped, not flat "production"
    assert c01["human_decision"]["path_scope"] == "artwork"
    assert c01["visual_review"]["path_scope"] == "artwork"
    assert c01["anomalies"] == []   # nothing fell back, so nothing to flag


def test_flat_artefacts_are_read_but_flagged_not_attributable(tmp_path):
    """(2) Flat-only (pre-fix) layout: readable, but marked as a shared name."""
    _, stem, _ = make_run(tmp_path)      # flat names only
    run = rr.build_run(stem)
    c01 = next(c for c in run["candidates"] if c["id"] == "candidate_01")
    c02 = next(c for c in run["candidates"] if c["id"] == "candidate_02")
    back = c01["back_half"]

    assert back["run"] is True
    assert back["manifest"]["path"] == str(rr.FINAL_DIR / "candidate_01.manifest.json")
    assert back["manifest"]["path_scope"] == "flat-shared"
    assert back["layer_a_txt"]["path_scope"] == "flat-shared"
    assert back["proof_png"]["path_scope"] == "flat-shared"
    assert c01["human_decision"]["path_scope"] == "flat-shared"
    assert c01["visual_review"]["path_scope"] == "flat-shared"

    notes = " | ".join(c01["anomalies"])
    assert "not attributable" in notes
    assert "candidate_01.manifest.json" in notes
    assert c01["missing_links"] == []   # readable data, not a failed stage

    # A candidate with no back half at all is "absent", and gets no anomaly noise.
    assert c02["back_half"]["manifest"]["path_scope"] == "absent"
    assert c02["back_half"]["run"] is False
    assert c02["anomalies"] == []


def test_shared_candidate_name_does_not_cross_artworks(tmp_path):
    """(3) Two artworks with the same candidate_01 must not read each other."""
    make_run(tmp_path, scoped=True, stem="art_a", scoped_label="style_reference")
    # art_b runs second: its flat names overwrite art_a's flat names, which is
    # exactly the shared-state hazard the scoped set removes.
    make_run(tmp_path, scoped=True, stem="art_b", scoped_label="needs_retrace")

    run_a = rr.build_run("art_a")
    run_b = rr.build_run("art_b")
    a01 = next(c for c in run_a["candidates"] if c["id"] == "candidate_01")
    b01 = next(c for c in run_b["candidates"] if c["id"] == "candidate_01")

    a_manifest = rr.FINAL_DIR / "art_a" / "candidate_01.manifest.json"
    b_manifest = rr.FINAL_DIR / "art_b" / "candidate_01.manifest.json"
    assert a01["back_half"]["manifest"]["sha256"] == fc.sha256_file(a_manifest)
    assert b01["back_half"]["manifest"]["sha256"] == fc.sha256_file(b_manifest)
    assert a01["back_half"]["manifest"]["sha256"] != b01["back_half"]["manifest"]["sha256"]

    # The picks differ per artwork; reading the flat pick would hand art_a art_b's.
    assert a01["human_decision"]["label"] == "style_reference"
    assert b01["human_decision"]["label"] == "needs_retrace"
    flat_pick = json.loads((rr.FINAL_DIR / "candidate_01.pick.json").read_text())
    assert flat_pick["label"] == "production"      # the shared flat file's own label
    assert a01["human_decision"]["label"] != flat_pick["label"]
    assert a01["human_decision"]["path"] == str(
        rr.FINAL_DIR / "art_a" / "candidate_01.pick.json")
    # Proofs are per-artwork too.
    assert a01["back_half"]["proof_png"]["sha256"] == fc.sha256_file(
        rr.FINAL_DIR / "art_a" / "candidate_01.proof.png")
    assert a01["back_half"]["layer_a_txt"]["path"].endswith(
        "art_a.candidate_01.layer_a.txt")


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
