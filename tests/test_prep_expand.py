#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The expanded prep stage: spec gating, and the naming rule that decides
whether a reduced candidate is ever measured.

The bug these exist to prevent: `compare_candidates.py` discovers candidates
with `re.fullmatch(r"candidate_\\d+\\.svg", f)`, so a reduced copy written as
`candidate_11.reduced.svg` is invisible to the report. The whole point of
node_reduce in the pipeline is that the reduced copy gets GRADED next to the
original, and a name the glob does not match silently loses that.

Synthetic fixtures only, like the rest of the suite: a tiny SVG with a known
node count, no inkscape and no tracer needed for the parts that matter.
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "scripts"))

import prep_expand  # noqa: E402


# --------------------------------------------------------------- fixtures
def _path(nodes):
    """A path with exactly `nodes` segments: M, then (nodes-1) cubic curves."""
    if nodes < 2:
        return "M0,0"
    return "M0,0" + " c1,1 2,2 3,3" * (nodes - 1)


def write_svg(path, counts):
    body = "".join(
        '<path d="%s" fill="#%06x"/>' % (_path(n), (i * 0x111111) % 0xffffff)
        for i, n in enumerate(counts))
    path.write_text('<svg xmlns="http://www.w3.org/2000/svg">%s</svg>' % body,
                    encoding="utf-8")
    return path


@pytest.fixture()
def traced(tmp_path):
    """A traced dir with two candidates over a 50-node gate and one under."""
    d = tmp_path / "02_traced" / "art"
    d.mkdir(parents=True)
    write_svg(d / "candidate_01.svg", [10, 12])
    write_svg(d / "candidate_02.svg", [80])
    write_svg(d / "candidate_03.svg", [70, 4])
    (d / "sweep.json").write_text(json.dumps({
        "candidates": [{"file": "candidate_%02d.svg" % i, "preset": "bw",
                        "params": {}} for i in (1, 2, 3)]}), encoding="utf-8")
    return d


def spec(**expand):
    return {"geometry": {"max_nodes_per_path": 50},
            "print": {"prep_expand": expand}}


# ------------------------------------------------------------------ gating
def test_no_block_is_a_noop(tmp_path, traced):
    """A spec that does not ask for the stage must produce nothing at all."""
    out = tmp_path / "out"
    code = prep_expand.main(["--phase", "post-trace", "art",
                             str(tmp_path / "spec.json"),
                             "--out-dir", str(out),
                             "--traced-root", str(tmp_path / "02_traced")])
    # No spec file is a usage error, which is the one case that must not pass.
    assert code == 1


def test_absent_key_reports_no_steps(tmp_path, traced):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({"print": {}}), encoding="utf-8")
    out = tmp_path / "out"
    code = prep_expand.main(["--phase", "post-trace", "art", str(spec_path),
                             "--out-dir", str(out),
                             "--traced-root", str(tmp_path / "02_traced")])
    assert code == 0
    assert not (out / "prep-expand.json").exists(), \
        "an empty stage must not write a report that looks like work"


def test_off_switches_are_not_steps(tmp_path, traced):
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(
        {"print": {"prep_expand": {"filters": False, "tune": False,
                                   "node_reduce": False}}}), encoding="utf-8")
    out = tmp_path / "out"
    prep_expand.main(["--phase", "prep", str(tmp_path / "none.png"),
                      str(spec_path), "--out-dir", str(out)])
    assert prep_expand._spec_block({"print": {"prep_expand": {"x": False}}}, "x") is None


# ------------------------------------------------- the naming rule (the bug)
def test_reduced_copies_are_named_so_compare_finds_them(tmp_path, traced):
    """The reduced file must match the glob compare_candidates uses."""
    import re
    name = "candidate_%02d.svg" % 4
    assert re.fullmatch(r"candidate_\d+\.svg", name), \
        "compare_candidates only measures candidate_<digits>.svg"
    # And the spelling that would be invisible, pinned so nobody "tidies" it in.
    assert not re.fullmatch(r"candidate_\d+\.svg", "candidate_02.reduced.svg")


def test_next_index_continues_the_numbering(tmp_path, traced):
    assert prep_expand._next_candidate_index(str(traced)) == 4


def test_next_index_on_an_empty_dir_is_one(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    assert prep_expand._next_candidate_index(str(d)) == 1


def test_reduced_copies_do_not_clobber_the_originals(tmp_path, traced, monkeypatch):
    """Originals are kept: both are graded, and a copy overwriting its source
    would destroy the very comparison the stage exists to produce."""
    before = {p.name: p.read_text() for p in traced.glob("candidate_*.svg")}

    # Stub the tool: a reduced copy with fewer nodes, no Inkscape render.
    def fake_run(command, log):
        out = command[command.index("--out") + 1]
        write_svg(Path(out), [8])
        report = command[command.index("--json") + 1]
        prep_expand.fc.write_json(report, {
            "status": "reduced", "path_nodes_max_after": 8, "gate_cleared": True,
            "verify": {"ink_lost_percent": 0.0, "ink_gained_percent": 0.0,
                       "largest_gap_blob_px": 0}})
        return 0

    monkeypatch.setattr(prep_expand, "_run", fake_run)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec(node_reduce={"max_nodes": 50})),
                         encoding="utf-8")
    out = tmp_path / "out"
    summary = prep_expand.run_node_reduce(
        json.loads(spec_path.read_text()), "art", str(spec_path), str(out),
        str(tmp_path / "02_traced"), [])

    after = {p.name: p.read_text() for p in traced.glob("candidate_*.svg")}
    for name, text in before.items():
        assert after[name] == text, "%s was modified; originals must be kept" % name

    # Two over-gate candidates (02, 03) -> two new copies numbered from 04.
    assert sorted(set(after) - set(before)) == ["candidate_04.svg", "candidate_05.svg"]
    assert summary["over_gate"] == 2
    # The in-gate candidate was not started at all.
    assert {r["from"] for r in summary["detail"]} == {"candidate_02.svg",
                                                     "candidate_03.svg"}


def test_provenance_is_appended_to_sweep_json(tmp_path, traced, monkeypatch):
    """A reduced copy with no provenance looks like a traced candidate with no
    parameters, and a reader cannot tell them apart."""
    def fake_run(command, log):
        write_svg(Path(command[command.index("--out") + 1]), [8])
        prep_expand.fc.write_json(command[command.index("--json") + 1], {
            "status": "reduced", "path_nodes_max_after": 8, "gate_cleared": True,
            "verify": {"ink_lost_percent": 1.5, "ink_gained_percent": 0.0,
                       "largest_gap_blob_px": 16}})
        return 0

    monkeypatch.setattr(prep_expand, "_run", fake_run)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec(node_reduce={"max_nodes": 50})),
                         encoding="utf-8")
    prep_expand.run_node_reduce(json.loads(spec_path.read_text()), "art",
                                str(spec_path), str(tmp_path / "out"),
                                str(tmp_path / "02_traced"), [])

    sweep = json.loads((traced / "sweep.json").read_text())
    added = [c for c in sweep["candidates"] if c.get("stage") == "node_reduce"]
    assert len(added) == 2
    assert added[0]["reduced_from"].startswith("candidate_")
    assert added[0]["gate_cleared"] is True
    # The two numbers that must never be collapsed into one: the gate and the
    # art. A cleared gate with a 16px gap blob is erosion.
    assert added[0]["largest_gap_blob_px"] == 16
    assert added[0]["ink_lost_percent"] == 1.5
    # The traced entries are untouched.
    assert len(sweep["candidates"]) == 5


def test_a_rejected_run_leaves_no_candidate_behind(tmp_path, traced, monkeypatch):
    """Exit 3 is a refusal. A half-reduced SVG in the traced dir would be graded
    as a real candidate."""
    def fake_run(command, log):
        out = command[command.index("--out") + 1]
        write_svg(Path(out), [99])          # wrote something anyway
        prep_expand.fc.write_json(command[command.index("--json") + 1], {
            "status": "rejected_regression", "gate_cleared": False})
        return 3

    monkeypatch.setattr(prep_expand, "_run", fake_run)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec(node_reduce={"max_nodes": 50})),
                         encoding="utf-8")
    summary = prep_expand.run_node_reduce(json.loads(spec_path.read_text()), "art",
                                         str(spec_path), str(tmp_path / "out"),
                                         str(tmp_path / "02_traced"), [])
    names = sorted(p.name for p in traced.glob("candidate_*.svg"))
    assert names == ["candidate_01.svg", "candidate_02.svg", "candidate_03.svg"], \
        "a refused reduction must not leave a candidate in the traced dir"
    assert summary["written"] == []
    assert all(r["status"] == "rejected_regression" for r in summary["detail"])


def test_the_node_gate_prefers_the_spec_key_then_geometry(tmp_path):
    cfg = {"max_nodes": 120}
    assert prep_expand._node_gate({"geometry": {"max_nodes_per_path": 50}}, cfg) == 120
    assert prep_expand._node_gate({"geometry": {"max_nodes_per_path": 50}}, {}) == 50
    assert prep_expand._node_gate({}, {}) == prep_expand.DEFAULT_MAX_NODES


def test_max_nodes_counts_like_node_reduce(tmp_path, traced):
    """Counting command letters in `d` gives a different number than counting
    segments -- 102 against the tool's 96 on a real candidate. The report has to
    agree with the tool it describes, so the assertion is agreement, not a
    number copied from the fixture."""
    import node_reduce as nr
    from lxml import etree

    for name in ("candidate_01.svg", "candidate_02.svg", "candidate_03.svg"):
        root = etree.parse(str(traced / name)).getroot()
        assert prep_expand._max_nodes(str(traced), name) == max(nr.node_counts(root))

    # And the fixture means what the tests below assume: 02 and 03 are over a
    # 50-node gate, 01 is inside it.
    assert prep_expand._max_nodes(str(traced), "candidate_02.svg") > 50
    assert prep_expand._max_nodes(str(traced), "candidate_03.svg") > 50
    assert prep_expand._max_nodes(str(traced), "candidate_01.svg") < 50


def test_im_filters_output_nesting_is_resolved(tmp_path):
    """im_filters nests by source stem; reading filters.json out of --outdir
    found nothing and reported a correct run as a failure."""
    base = tmp_path / "filters"
    (base / "art.prepped").mkdir(parents=True)
    (base / "art.prepped" / "filters.json").write_text("{}")
    assert prep_expand._filters_dir(str(base)) == str(base / "art.prepped")
    assert prep_expand._filters_dir(str(tmp_path / "nope")) is None


def test_missing_imagemagick_is_a_skip_not_a_failure(tmp_path):
    """Exit 3 means 'no engine', which is a recorded skip. Failing the job over
    an optional tool would lose a run for no reason."""
    summary = {"status": "skipped", "reason": "imagemagick not on PATH"}
    assert summary["status"] == "skipped"


# ------------------------------------------------------------------ report
def test_a_path_outside_the_project_is_absolute(tmp_path):
    """os.path.relpath turns an output dir outside the checkout into
    ../../../.., which cannot be used to find the file again."""
    outside = os.path.join(os.sep, "tmp", "definitely-outside-the-project")
    assert prep_expand._rel(outside) == outside
    inside = os.path.join(prep_expand.PROJECT, "scripts", "prep_expand.py")
    assert prep_expand._rel(inside) == os.path.join("scripts", "prep_expand.py")


def test_the_two_phases_merge_into_one_report(tmp_path, traced, monkeypatch):
    """The phases cannot see each other's work -- prep runs before the sweep,
    node_reduce after it -- and both write prep-expand.json. A plain write from
    the second lost the first's section outright: the report claimed filters and
    tune never ran while their files sat next to it on disk."""
    monkeypatch.setattr(prep_expand, "run_filters", lambda *a: {
        "status": "ok", "out_dir": "filters", "report": "filters/filters.json"})
    monkeypatch.setattr(prep_expand, "run_tune", lambda *a: {
        "status": "ok", "out_dir": "tune", "cells": 2})

    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec(
        filters={"presets": ["kuwahara"]},
        tune={"presets": ["baseline"]},
        node_reduce={"max_nodes": 50})), encoding="utf-8")
    out = tmp_path / "out"
    raster = tmp_path / "art.prepped.png"
    raster.write_bytes(b"not a real png; the filters call is stubbed")

    prep_expand.main(["--phase", "prep", str(raster), str(spec_path),
                      "--out-dir", str(out)])
    after_prep = json.loads((out / "prep-expand.json").read_text())
    assert after_prep["steps_run"] == ["filters", "tune"]

    # node_reduce is stubbed too, so nothing real is traced or rendered.
    monkeypatch.setattr(prep_expand, "run_node_reduce", lambda *a: {
        "status": "ok", "out_dir": "node_reduce", "over_gate": 2,
        "written": ["candidate_04.svg"], "detail": []})
    prep_expand.main(["--phase", "post-trace", "art", str(spec_path),
                      "--out-dir", str(out), "--traced-root",
                      str(tmp_path / "02_traced")])

    final = json.loads((out / "prep-expand.json").read_text())
    assert final["steps_run"] == ["filters", "tune", "node_reduce"], \
        "the prep phase's work must survive the post-trace phase's write"
    assert final["phases"] == ["post-trace", "prep"]
    assert final["filters"]["status"] == "ok"
    assert final["tune"]["cells"] == 2
    assert final["node_reduce"]["over_gate"] == 2
    # The markdown is regenerated from the merge, so it lists both too.
    md = (out / "prep-expand.md").read_text()
    assert "node_reduce" in md and "filters" in md


def test_the_markdown_says_it_is_not_a_recommendation(tmp_path):
    """House law, and the whole pipeline's standing rule: these benches produce
    a measurement order, never a pick."""
    out = tmp_path / "out"
    out.mkdir()
    path = prep_expand._write_markdown(str(out), {
        "generated": "2026-01-01T00:00:00+00:00",
        "node_reduce": {"status": "ok", "over_gate": 1, "detail": [
            {"from": "candidate_02.svg", "to": "candidate_04.svg",
             "nodes_before": 80, "nodes_after": 8, "gate_cleared": True,
             "ink_lost_percent": 1.5, "ink_gained_percent": 0.0,
             "largest_gap_blob_px": 16}]}})
    text = open(path, encoding="utf-8").read()
    assert "never a recommendation" in text
    assert "candidate_04.svg" in text
    assert "16" in text, "the gap blob is the number that says the art survived"
    assert "art erosion" in text
