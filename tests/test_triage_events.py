#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for scripts/triage_events.py (Phase 2 candidate-triage event log).

The two properties worth pinning, because both are silent when they break:

* the event line is exactly the contracted shape -- ``t``/``job``/``svg_sha``/
  ``event``/``pos``/``ranker`` -- with the 1-based RANK (first candidate = 1)
  the candidate was shown at, matching the comparison stage's own display loop
  and the studio's Event.position_of.  A learner keyed on a misnamed field does
  not error, it learns nothing.
* the store is JOB-INDEPENDENT and concurrent-safe.  If events land under the
  per-job ``06_run/<stem>/`` they die with the job they describe; if two workers
  can interleave a line the JSONL becomes unparseable partway through.  Both
  failures are invisible until the data is needed, so both are asserted here.

Everything writes into ``tmp_path`` via ``PIPELINE_LEARNING_DIR`` -- no test
touches the real ``06_run/_learning/``.
"""

import concurrent.futures
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
for path in (ROOT, SCRIPTS):
    if path not in sys.path:
        sys.path.insert(0, path)

import triage_events  # noqa: E402

CONTRACT_FIELDS = ["t", "job", "svg_sha", "event", "pos", "ranker"]


@pytest.fixture
def learning(tmp_path, monkeypatch):
    """Point the learning store at a temp dir for one test."""
    target = tmp_path / "learning"
    monkeypatch.setenv("PIPELINE_LEARNING_DIR", str(target))
    return target


def _read_events(learning_dir):
    path = os.path.join(str(learning_dir), "events.jsonl")
    with open(path, "r", encoding="utf-8") as handle:
        return [line for line in handle.read().splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# 1. Line shape and position                                                   #
# --------------------------------------------------------------------------- #


def test_append_event_writes_one_valid_jsonl_line_with_exact_fields(learning):
    assert triage_events.append_event("job-a", "a" * 64, pos=1) is True

    lines = _read_events(learning)
    assert len(lines) == 1
    line = json.loads(lines[0])
    # Exact field names and order: the JSONL contract is the interface.
    assert list(line.keys()) == CONTRACT_FIELDS
    assert set(line.keys()) == set(CONTRACT_FIELDS)
    assert line["job"] == "job-a"
    assert line["svg_sha"] == "a" * 64
    assert line["event"] == "shown"
    assert line["pos"] == 1
    assert line["ranker"] is None  # no ranker exists yet

    # t is a real ISO-8601 UTC timestamp, not a placeholder.
    stamp = datetime.fromisoformat(line["t"])
    assert stamp.tzinfo is not None
    assert stamp.utcoffset().total_seconds() == 0


def test_record_shown_logs_one_line_per_candidate_with_its_index(learning):
    shas = ["0" * 64, "1" * 64, "2" * 64]
    written = triage_events.record_shown("job-a", shas)
    assert written == 3

    lines = [json.loads(line) for line in _read_events(learning)]
    assert [line["pos"] for line in lines] == [1, 2, 3]
    assert [line["svg_sha"] for line in lines] == shas
    assert all(line["event"] == "shown" for line in lines)


def test_record_shown_resolves_sha_from_candidate_dicts(learning):
    # The comparison stage hands over its records, which name the candidate sha
    # the way sweep.json does (output_sha256).
    records = [{"output_sha256": "f" * 64}, {"file": "candidate_02.svg"}]
    assert triage_events.record_shown("job-a", records) == 2

    lines = [json.loads(line) for line in _read_events(learning)]
    assert lines[0]["svg_sha"] == "f" * 64
    # An unresolvable sha is recorded as null rather than guessed.
    assert lines[1]["svg_sha"] is None


def test_events_land_under_job_independent_dir_not_per_job(learning):
    triage_events.append_event("deletable-job", "b" * 64, pos=0)

    # The log lives at the shared, job-independent path...
    assert os.path.exists(os.path.join(str(learning), "events.jsonl"))
    # ...and nothing was created under a per-job directory that a delete sweeps.
    assert not os.path.exists(os.path.join(str(learning), "deletable-job"))
    assert "deletable-job" not in str(triage_events.events_path())


def test_default_learning_dir_is_shared_and_derived(monkeypatch):
    # With no override the store sits in the repo's 06_run tree, in a directory
    # that is NOT the per-job 06_run/<stem>/.  Derived from the module location,
    # never a hardcoded machine path.
    monkeypatch.delenv("PIPELINE_LEARNING_DIR", raising=False)
    path = triage_events.learning_dir()
    assert path == os.path.join(ROOT, "06_run", "_learning")
    assert os.path.basename(path) == "_learning"
    assert path == os.path.dirname(triage_events.events_path())


# --------------------------------------------------------------------------- #
# 2. Concurrency: appends must never interleave into a corrupt line            #
# --------------------------------------------------------------------------- #


def _assert_uncorrupted(learning_dir, expected_lines):
    lines = _read_events(learning_dir)
    assert len(lines) == expected_lines
    for raw in lines:
        payload = json.loads(raw)  # every line parses
        assert list(payload.keys()) == CONTRACT_FIELDS


def test_concurrent_thread_appends_do_not_corrupt_lines(learning):
    per_thread = 25
    threads = 8

    def worker(index):
        for pos in range(per_thread):
            triage_events.append_event("thread-%d" % index, "%02d" % index,
                                       pos=pos)

    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as pool:
        list(pool.map(worker, range(threads)))

    lines = _read_events(learning)
    _assert_uncorrupted(learning, threads * per_thread)

    # Every thread's positions survived intact: no line was split or lost.
    by_job = {}
    for raw in lines:
        payload = json.loads(raw)
        by_job.setdefault(payload["job"], []).append(payload["pos"])
    assert len(by_job) == threads
    for job, positions in by_job.items():
        assert sorted(positions) == list(range(per_thread)), job


def test_concurrent_process_appends_do_not_corrupt_lines(learning):
    # Cross-PROCESS: the guarantee under test is O_APPEND on a single write, not
    # an in-process lock.  Four real interpreters append at once.
    procs = 4
    per_proc = 40
    script = (
        "import sys, triage_events as te\n"
        "job = sys.argv[1]\n"
        "for pos in range(int(sys.argv[2])):\n"
        "    te.append_event(job, 'c' * 64, pos=pos)\n"
    )
    env = dict(os.environ, PYTHONPATH=SCRIPTS)
    running = [
        subprocess.Popen([sys.executable, "-c", script, "proc-%d" % i,
                          str(per_proc)], env=env)
        for i in range(procs)
    ]
    for proc in running:
        assert proc.wait(timeout=120) == 0

    _assert_uncorrupted(learning, procs * per_proc)


# --------------------------------------------------------------------------- #
# 3. Non-fatal by contract: an unusable learning dir must not raise            #
# --------------------------------------------------------------------------- #


def test_unusable_learning_dir_is_a_no_op(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file, not a directory", encoding="utf-8")
    # A path whose parent is a regular file cannot be created.
    monkeypatch.setenv("PIPELINE_LEARNING_DIR",
                       str(blocker / "learning"))

    assert triage_events.append_event("job", "d" * 64, pos=0) is False
    assert triage_events.record_shown("job", ["d" * 64]) == 0
    assert triage_events.ensure_learning_dir() is None
    assert triage_events.save_learned_buckets({}) is None
    # Reads degrade to the empty default rather than exploding.
    assert triage_events.load_learned_buckets() == \
        triage_events.default_learned_buckets()


# --------------------------------------------------------------------------- #
# 4. Checksum sidecar at creation                                              #
# --------------------------------------------------------------------------- #


def test_checksum_sidecar_records_the_correct_sha256(tmp_path):
    artifact = tmp_path / "candidate_01.svg"
    body = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"
    artifact.write_bytes(body)
    expected = hashlib.sha256(body).hexdigest()

    sidecar = triage_events.stamp_checksum(str(artifact))

    assert sidecar == str(tmp_path / "candidate_01.sha256.json")
    payload = json.load(open(sidecar, encoding="utf-8"))
    assert payload["sha256"] == expected
    assert payload["algo"] == "sha256"
    assert payload["file"] == "candidate_01.svg"
    assert payload["size"] == len(body)
    # The sidecar records provenance, not this machine's path.
    assert str(tmp_path) not in json.dumps(payload)


def test_checksum_sidecar_reuses_a_supplied_digest(tmp_path):
    artifact = tmp_path / "candidate_02.svg"
    artifact.write_text("<svg/>", encoding="utf-8")
    sidecar = triage_events.stamp_checksum(str(artifact), sha="e" * 64)
    assert json.load(open(sidecar, encoding="utf-8"))["sha256"] == "e" * 64


def test_stamp_checksum_missing_artifact_is_not_an_error(tmp_path):
    assert triage_events.stamp_checksum(str(tmp_path / "nope.svg")) is None


def test_svg_file_sha_matches_the_file_bytes(tmp_path):
    artifact = tmp_path / "candidate_03.svg"
    body = b"<svg><path d='M0 0'/></svg>"
    artifact.write_bytes(body)
    assert triage_events.svg_file_sha(str(artifact)) == \
        hashlib.sha256(body).hexdigest()
    assert triage_events.svg_file_sha(str(tmp_path / "gone.svg")) is None


_has_vtracer = importlib.util.find_spec("vtracer") is not None


@pytest.mark.skipif(not _has_vtracer, reason="vtracer not installed")
def test_trace_sweep_stamps_a_checksum_sidecar_per_candidate(tmp_path):
    # The trace stage writes the candidate, so it is the stage that stamps the
    # checksum at creation.  Observed end to end: run one real trace and check
    # each candidate has a sidecar whose digest matches both sweep.json's
    # output_sha256 and the file on disk.
    from PIL import Image

    import trace_sweep

    png = tmp_path / "art.png"
    img = Image.new("RGB", (96, 96), "#ffffff")
    for x in range(16, 64):
        for y in range(16, 64):
            img.putpixel((x, y), (0, 0, 0))
    img.save(str(png), "PNG")

    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps({
        "print_method": "screen_print",
        "max_colors": 6,
        "geometry": {"allow_gradients": False},
        "palette": ["#000000", "#FFFFFF", "#FF0000"],
        "print": {"dpi": 100, "sweep": {"max_candidates": 1}},
    }), encoding="utf-8")

    out_dir = tmp_path / "traced"
    code = trace_sweep.main([str(png), str(spec_path), "--out-dir",
                             str(out_dir), "--workers", "1"])
    assert code == 0

    sweep = json.load(open(out_dir / "sweep.json", encoding="utf-8"))
    assert sweep["candidates"], "no candidate was traced"
    for cand in sweep["candidates"]:
        assert cand.get("output_sha256"), cand
        svg = out_dir / cand["file"]
        sidecar = out_dir / (svg.stem + ".sha256.json")
        assert sidecar.exists(), "no checksum stamped for %s" % cand["file"]
        payload = json.load(open(sidecar, encoding="utf-8"))
        assert payload["sha256"] == cand["output_sha256"]
        assert payload["sha256"] == \
            hashlib.sha256(svg.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- #
# 5. learned_buckets.json survives deletion: location + round trip             #
# --------------------------------------------------------------------------- #


def test_learned_buckets_round_trips(learning):
    empty = triage_events.load_learned_buckets()
    assert empty == triage_events.default_learned_buckets()
    assert empty["buckets"] == {}

    buckets = triage_events.load_learned_buckets()
    buckets["buckets"]["poster|s1|c2|"] = triage_events.empty_bucket_stats()
    buckets["buckets"]["poster|s1|c2|"]["rejected"] = 3
    written = triage_events.save_learned_buckets(buckets)
    assert written == str(learning / "learned_buckets.json")

    assert triage_events.load_learned_buckets() == buckets


def test_learned_buckets_lives_beside_the_event_log(learning):
    assert triage_events.learned_buckets_path() == os.path.join(
        str(learning), "learned_buckets.json")
    assert os.path.dirname(triage_events.learned_buckets_path()) == \
        os.path.dirname(triage_events.events_path())


def test_corrupt_learned_buckets_degrades_to_default(learning):
    learning.mkdir(parents=True, exist_ok=True)
    (learning / "learned_buckets.json").write_text("{ not json",
                                                   encoding="utf-8")
    assert triage_events.load_learned_buckets() == \
        triage_events.default_learned_buckets()


# --------------------------------------------------------------------------- #
# 6. Wire-in: the comparison stage emits the shown events                      #
# --------------------------------------------------------------------------- #

SVG_HEAD = ('<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" '
            'viewBox="0 0 100 100">')
CANDIDATES = {
    "candidate_01.svg":
        SVG_HEAD + '<rect x="0" y="0" width="100" height="100" fill="#ffffff"/>'
        '<rect x="10" y="10" width="40" height="40" fill="#000000"/></svg>',
    "candidate_02.svg":
        SVG_HEAD + '<rect x="0" y="0" width="100" height="100" fill="#ffffff"/>'
        '<rect x="10" y="10" width="40" height="40" fill="#000000"/>'
        '<rect x="60" y="60" width="30" height="30" fill="#ff0000"/></svg>',
}


def test_compare_candidates_writes_shown_events_to_the_learning_store(
        tmp_path, learning):
    import compare_candidates

    traced = tmp_path / "traced" / "art"
    traced.mkdir(parents=True)
    for name, body in CANDIDATES.items():
        (traced / name).write_text(body, encoding="utf-8")
    (traced / "sweep.json").write_text(json.dumps({"candidates": [
        {"file": "candidate_01.svg", "preset": "bw", "filter_speckle": 2,
         "hierarchical": "cutout", "use_palette": False, "params": {}},
        {"file": "candidate_02.svg", "preset": "poster", "filter_speckle": 8,
         "hierarchical": "stacked", "use_palette": False, "params": {}},
    ]}), encoding="utf-8")

    spec = {
        "print_method": "screen_print",
        "max_colors": 6,
        "geometry": {"min_stroke_width_pt": 0, "max_nodes_per_path": 500,
                     "allow_raster_embed": False, "allow_gradients": False,
                     "allow_open_paths": True},
        "palette": ["#000000", "#ffffff", "#ff0000"],
        "print": {"dpi": 72},
    }
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    code = compare_candidates.main([
        str(traced), str(spec_path), "--out-dir", str(tmp_path / "04_validated"),
    ])
    assert code == 0

    events = [json.loads(line) for line in _read_events(learning)]
    assert len(events) == len(CANDIDATES)
    assert [event["event"] for event in events] == ["shown", "shown"]
    # pos is the 1-based rank in the presented order (first candidate = 1).
    assert sorted(event["pos"] for event in events) == [1, 2]
    assert all(event["job"] == "art" for event in events)

    # Each event's svg_sha is the sha256 of the candidate SVG shown.
    shas = set()
    for name in CANDIDATES:
        body = (traced / name).read_bytes()
        shas.add(hashlib.sha256(body).hexdigest())
    assert {event["svg_sha"] for event in events} == shas
