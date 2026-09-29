#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_trace_sweep_ownership.py -- the sweep's ownership of its candidate dir.

``02_traced/<stem>`` is shared per STEM and persists across runs (documented
behaviour: two jobs with the same artwork name continue the same candidate
numbering, and naming stays ``candidate_%02d.svg`` by sweep index).  The bug
these tests pin is the consequence: a rerun with a SMALLER sweep leaves the
previous run's higher-numbered candidates on disk, where the comparison stage
used to grade them as if this run had produced them -- with the old sweep's
parameters at best, or none at all.

Fix, in two halves:
  * ``trace_sweep.prune_stale_candidates`` deletes the ``candidate_\\d+\\.svg``
    files this sweep did not write, and nothing else (tested here); and
  * ``compare_candidates.load_candidates`` grades exactly what ``sweep.json``
    lists, never a directory glob (tested in test_compare_candidates.py).

These live in their own module rather than in test_trace_sweep.py because they
are PURE FILESYSTEM LOGIC -- no tracer, no ImageMagick, no render.  A test that
does not need the engine must not be skipped when the engine is missing, or the
guard on a destructive cleanup would quietly stop running.

Fixtures are synthetic and built in ``tmp_path``; nothing here touches the real
``00_source/`` batch or the repo's output directories.
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
for p in (ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import trace_sweep  # noqa: E402


def _candidate_dir(tmp_path, count=6):
    """A traced dir as a real run leaves it: candidates, sweep.json, the
    per-candidate checksum sidecars, and the two dot-directories."""
    out = tmp_path / "02_traced" / "art"
    out.mkdir(parents=True)
    for index in range(1, count + 1):
        (out / ("candidate_%02d.svg" % index)).write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>',
            encoding="utf-8")
        # triage_events puts the stamp at `<stem>.sha256.json` beside the
        # artifact, i.e. `candidate_01.sha256.json` -- NOT `candidate_01.svg`
        # anything.  Written here so the guard against deleting it is real.
        (out / ("candidate_%02d.sha256.json" % index)).write_text(
            json.dumps({"algo": "sha256", "sha256": "x"}), encoding="utf-8")
    (out / ".variants").mkdir()
    (out / ".variants" / "source.inverse.png").write_bytes(b"\x89PNG fake")
    (out / ".palette").mkdir()
    (out / ".palette" / "candidate_01.svg.png").write_bytes(b"\x89PNG fake")
    (out / "sweep.json").write_text(json.dumps({
        "tool": "trace_sweep.py",
        "candidates": [{"file": "candidate_%02d.svg" % i, "preset": "bw"}
                       for i in range(1, count + 1)],
    }), encoding="utf-8")
    return out


def test_prune_removes_only_candidates_this_sweep_did_not_write(tmp_path):
    out = str(_candidate_dir(tmp_path, count=6))
    removed = trace_sweep.prune_stale_candidates(
        out, ["candidate_%02d.svg" % i for i in range(1, 4)])

    assert removed == ["candidate_04.svg", "candidate_05.svg",
                       "candidate_06.svg"]
    for index in range(1, 4):
        assert os.path.exists(os.path.join(out, "candidate_%02d.svg" % index))
    for index in range(4, 7):
        assert not os.path.exists(
            os.path.join(out, "candidate_%02d.svg" % index))


def test_prune_leaves_everything_it_did_not_write_alone(tmp_path):
    """sweep.json, the sidecars and the dot-dirs must survive the cleanup.

    A cleanup that ate the record it is supposed to agree with would turn a
    gradeable directory into an ungradeable one -- and the sidecars are the
    provenance the comparison stage relies on.
    """
    out = str(_candidate_dir(tmp_path, count=6))
    before = {}
    for name in ("sweep.json", "candidate_01.sha256.json",
                 "candidate_06.sha256.json", ".variants/source.inverse.png",
                 ".palette/candidate_01.svg.png"):
        before[name] = open(os.path.join(out, name), "rb").read()

    trace_sweep.prune_stale_candidates(
        out, ["candidate_%02d.svg" % i for i in range(1, 4)])

    for name, content in before.items():
        path = os.path.join(out, name)
        assert os.path.exists(path), "%s was deleted by the cleanup" % name
        assert open(path, "rb").read() == content, "%s was modified" % name
    assert os.path.isdir(os.path.join(out, ".variants"))
    assert os.path.isdir(os.path.join(out, ".palette"))
    # ...including the sidecar of a candidate that WAS pruned: the record of
    # what a file used to be is not itself a candidate.
    assert os.path.exists(os.path.join(out, "candidate_06.sha256.json"))
    assert not os.path.exists(os.path.join(out, "candidate_06.svg"))


def test_prune_is_idempotent_on_a_rerun_with_the_same_candidates(tmp_path):
    """A rerun that produces the same set must be a NO-OP.

    This is the property that makes the cleanup safe to call on every sweep:
    nothing is deleted unless the set actually shrank or changed.
    """
    out = str(_candidate_dir(tmp_path, count=3))
    keep = ["candidate_%02d.svg" % i for i in range(1, 4)]

    assert trace_sweep.prune_stale_candidates(out, keep) == []
    assert trace_sweep.prune_stale_candidates(out, keep) == []
    for index in range(1, 4):
        assert os.path.exists(os.path.join(out, "candidate_%02d.svg" % index))
    assert os.path.exists(os.path.join(out, "sweep.json"))


def test_prune_ignores_a_directory_named_like_a_candidate(tmp_path):
    """Only files it could have written are removed -- never a directory."""
    out = tmp_path / "02_traced" / "art"
    out.mkdir(parents=True)
    (out / "candidate_01.svg").write_text("x", encoding="utf-8")
    (out / "candidate_02.svg").mkdir()
    (out / "candidate_02.svg" / "keep.txt").write_text("x", encoding="utf-8")

    removed = trace_sweep.prune_stale_candidates(out_dir=str(out),
                                                keep_files=["candidate_01.svg"])

    assert removed == []
    assert (out / "candidate_02.svg").is_dir()
    assert (out / "candidate_02.svg" / "keep.txt").exists()


def test_prune_on_a_missing_directory_is_not_an_error(tmp_path):
    assert trace_sweep.prune_stale_candidates(
        str(tmp_path / "nope"), ["candidate_01.svg"]) == []


def test_prune_never_reaches_outside_the_output_directory(tmp_path):
    """A nested candidate of the same name is not this sweep's to delete."""
    out = tmp_path / "02_traced" / "art"
    out.mkdir(parents=True)
    nested = out / "sub"
    nested.mkdir()
    (nested / "candidate_09.svg").write_text("x", encoding="utf-8")
    (out / "candidate_01.svg").write_text("x", encoding="utf-8")

    assert trace_sweep.prune_stale_candidates(str(out),
                                             ["candidate_01.svg"]) == []
    assert (nested / "candidate_09.svg").exists()
