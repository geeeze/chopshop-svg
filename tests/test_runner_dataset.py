#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_runner_dataset.py -- the runner half of the proof-variant dataset stage.

Two things are pinned here, and both are about the OPERATOR's experience of an
opt-in stage:

1. **Opt-in means opt-in.**  A spec without `validation.variants` produces no
   dataset directory, no flat copies and no `dataset` key -- a job that did not
   ask must not look like a job whose stage broke.
2. **The proof is the deliverable.**  A dataset that could not be derived is
   recorded as a NOTE on the manifest; the proof, the print PDF and the manifest
   are untouched and the validation still settles to done.

The HTTP tests then exercise the two new routes for real (a ThreadingHTTPServer
on an ephemeral port against a synthetic job directory): the four-segment
`/files/<job>/<stem>.dataset/<name>` form, including its containment gate, and
`/jobs/<job>/dataset`, which mirrors the job-archive contract.

Fixtures are synthetic and built in `tmp_path`; nothing here runs pipeline.sh,
needs Inkscape, or touches the real 00_source/ batch.

Run with:  python3 -m pytest tests/test_runner_dataset.py -v
"""

import importlib.util
import json
import os
import shutil
import sys
import threading
import time
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "runner.py"
_module_spec = importlib.util.spec_from_file_location(
    "chopshop_runner_dataset", MODULE_PATH)
runner = importlib.util.module_from_spec(_module_spec)
_module_spec.loader.exec_module(runner)

if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import proof_variants as pv  # noqa: E402

from PIL import Image  # noqa: E402

CANDIDATE = "candidate_02.svg"
STEM = "candidate_02"
SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="40mm" height="40mm"
     viewBox="0 0 40 40">
  <rect id="ground" width="40" height="40" fill="#ffffff"/>
  <path id="motif" d="M2,2 L12,6 L4,18 Z" fill="#c1440e"/>
  <circle id="bud" cx="32" cy="32" r="5" fill="#6a8a3f"/>
</svg>
"""

SCREENS = ["#c1440e", "#6a8a3f"]

_MISSING = object()


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

def make_png(size=(40, 40)):
    image = Image.new("RGB", size, "#ffffff")
    pixels = image.load()
    for y in range(6):
        for x in range(10):
            pixels[x, y] = (193, 68, 14)
    for y in range(40 - 6, 40):
        for x in range(40 - 6, 40):
            pixels[x, y] = (106, 138, 63)
    import io
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def make_spec(variants=_MISSING):
    spec = {
        "print_method": "screen_print",
        "palette": ["#FFFFFF"] + SCREENS,
        "validation": {"run_preflight": True},
    }
    if variants is not _MISSING:
        spec["validation"]["variants"] = variants
    return spec


def print_check_manifest():
    """What the print check's manifest looks like on disk (only the parts read)."""
    return {
        "tool": "preflight.py (v4.0 two-layer pipeline)",
        "generated": "2026-01-01T00:00:00+00:00",
        "passed": True,
        "substrate": {"colour": "#FFFFFF", "palette": ["#FFFFFF"] + SCREENS,
                      "screens": list(SCREENS)},
        "stats": {"dpi": 300},
        "answers": None,
        "notes": [],
    }


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Synthetic project tree + jobs root, wired into the runner's globals."""
    project = tmp_path / "project"
    (project / "05_final").mkdir(parents=True)
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    monkeypatch.setattr(runner, "PROJECT", project)
    monkeypatch.setattr(runner, "JOBS_ROOT", jobs)
    monkeypatch.setattr(runner, "JOBS", {})
    monkeypatch.setattr(runner, "VALIDATIONS", {})
    monkeypatch.setattr(runner, "TOKEN", "")
    return project, jobs


@pytest.fixture
def endpoint(workdir):
    """A real HTTP listener around the runner's handler."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), runner.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def get(port, path):
    """(status, headers, body) for one GET; the path is sent verbatim."""
    connection = HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return (response.status,
                {key.lower(): value for key, value in response.getheaders()},
                response.read())
    finally:
        connection.close()


def make_job(jobs, *, variants=_MISSING, candidate=CANDIDATE):
    """A job directory that already passed the front half."""
    job_dir = jobs / "jobAAA"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "input" / "artwork.png").write_bytes(make_png())
    spec = make_spec(variants if variants is not _MISSING else _MISSING) \
        if variants is not _MISSING else make_spec()
    (job_dir / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    (job_dir / candidate).write_text(SVG, encoding="utf-8")
    job = {"id": "jobAAA", "name": "artwork", "stem": "artwork",
           "dir": str(job_dir), "container_input_path": "", "spec": spec,
           "state": "done", "log": [], "candidates": [{"file": candidate}],
           "comparison": None, "prep_summary": None, "error": None,
           "cancel": False, "proc": None}
    runner.JOBS["jobAAA"] = job
    return job


def run_back(job, monkeypatch, project, candidate=CANDIDATE, screens=None):
    """Drive run_back against a stand-in pipeline.sh.

    The stand-in writes exactly the three artifacts the real shell script leaves
    in 05_final, so run_back's copy/manifest path is exercised for real without
    a container, Inkscape or a rebuild.  ``screens`` overrides the palette the
    stand-in manifest declares (the palette the dataset stage cycles).
    """
    def fake_run_logged(item, command, env):
        stem = Path(command[-1]).stem
        out = project / "05_final"
        out.mkdir(parents=True, exist_ok=True)
        body = print_check_manifest()
        if screens is not None:
            body["substrate"]["screens"] = list(screens)
            body["substrate"]["palette"] = list(screens)
        (out / ("%s.manifest.json" % stem)).write_text(
            json.dumps(body), encoding="utf-8")
        (out / ("%s.proof.png" % stem)).write_bytes(make_png())
        (out / ("%s.print.pdf" % stem)).write_bytes(b"%PDF-1.4 stand-in\n")
        return 0

    monkeypatch.setattr(runner, "_run_logged", fake_run_logged)
    validation = {"id": "valAAA", "job_id": job["id"],
                  "candidate_file": candidate, "state": "queued",
                  "manifest": None, "log": [], "error": None}
    runner.VALIDATIONS["valAAA"] = validation
    runner.run_back(validation)
    return validation


# --------------------------------------------------------------------------
# Reading the request out of the spec
# --------------------------------------------------------------------------

def test_variants_request_defaults_off():
    assert runner.variants_request({}) == (False, None)
    assert runner.variants_request({"validation": {}}) == (False, None)
    assert runner.variants_request(make_spec(False)) == (False, None)
    assert runner.variants_request(make_spec({})) == (False, None)
    # An object that does not say enabled=true asks for nothing yet.
    assert runner.variants_request(
        make_spec({"transforms": ["flip-h"]})) == (False, ["flip-h"])


def test_variants_request_true_means_the_whole_vocabulary():
    assert runner.variants_request(make_spec(True)) == (True, None)


def test_variants_request_reads_a_subset():
    assert runner.variants_request(
        make_spec({"enabled": True, "transforms": ["invert", "flip-h"]})) \
        == (True, ["invert", "flip-h"])


def test_variants_request_refuses_a_shape_it_cannot_read():
    with pytest.raises(ValueError) as caught:
        runner.variants_request(make_spec(["flip-h"]))
    assert "validation.variants" in str(caught.value)


# --------------------------------------------------------------------------
# The stage itself
# --------------------------------------------------------------------------

def test_no_variants_key_means_nothing_new_at_all(workdir, monkeypatch):
    project, jobs = workdir
    job = make_job(jobs)
    validation = run_back(job, monkeypatch, project)
    job_dir = Path(job["dir"])

    assert validation["state"] == "done"
    assert "dataset" not in validation["manifest"]
    assert not any(p.name.endswith(".dataset") for p in job_dir.iterdir())
    assert not [p for p in job_dir.iterdir() if ".dataset" in p.name]


def test_variants_false_means_nothing_new_at_all(workdir, monkeypatch):
    project, jobs = workdir
    job = make_job(jobs, variants=False)
    validation = run_back(job, monkeypatch, project)
    assert "dataset" not in validation["manifest"]
    assert not [p for p in Path(job["dir"]).iterdir() if ".dataset" in p.name]


def test_the_opt_in_builds_the_package_and_the_manifest_key(workdir, monkeypatch):
    project, jobs = workdir
    job = make_job(jobs, variants=True)
    validation = run_back(job, monkeypatch, project)
    job_dir = Path(job["dir"])

    assert validation["state"] == "done"
    dataset = validation["manifest"]["dataset"]
    assert list(dataset) == ["dir", "manifest", "sheet", "package", "count",
                             "transforms"]
    assert dataset["dir"] == "%s.dataset" % STEM
    assert dataset["manifest"] == "%s.dataset.json" % STEM
    assert dataset["sheet"] == "%s.dataset-contact-sheet.png" % STEM
    assert dataset["package"] == "%s.dataset.7z" % STEM
    assert dataset["count"] == len(pv.TRANSFORMS)
    assert dataset["transforms"] == list(pv.TRANSFORMS)

    out = job_dir / dataset["dir"]
    assert (out / "dataset.json").is_file()
    assert (out / "dataset.md").is_file()
    for name in pv.TRANSFORMS:
        assert (out / "raster" / ("%s.png" % name)).is_file()
        assert (out / "vector" / ("%s.svg" % name)).is_file()

    # The three flat names sit at the job root so artifact_path resolves them.
    assert (job_dir / dataset["manifest"]).is_file()
    assert (job_dir / ("%s.dataset.md" % STEM)).is_file()
    with Image.open(job_dir / dataset["sheet"]) as sheet:
        assert sheet.format == "PNG"
    assert (job_dir / ("%s.proof.png" % STEM)).is_file()
    assert any("dataset:" in line for line in validation["log"])


def test_the_opt_in_honours_a_transform_subset(workdir, monkeypatch):
    project, jobs = workdir
    job = make_job(jobs, variants={"enabled": True,
                                   "transforms": ["invert", "flip-h"]})
    validation = run_back(job, monkeypatch, project)
    dataset = validation["manifest"]["dataset"]
    assert dataset["transforms"] == ["flip-h", "invert"]
    assert dataset["count"] == 2
    raster = sorted(p.name for p in
                    (Path(job["dir"]) / dataset["dir"] / "raster").iterdir())
    assert raster == ["flip-h.png", "invert.png"]


def test_a_stage_failure_is_a_note_and_leaves_the_proof_intact(workdir,
                                                              monkeypatch):
    project, jobs = workdir
    job = make_job(jobs, variants={"enabled": True, "transforms": ["sideways"]})
    job_dir = Path(job["dir"])
    on_disk = (job_dir / ("%s.manifest.json" % STEM)).read_bytes() \
        if (job_dir / ("%s.manifest.json" % STEM)).is_file() else None
    proof_before = make_png()

    validation = run_back(job, monkeypatch, project)

    assert validation["state"] == "done", "the proof still succeeded"
    manifest = validation["manifest"]
    assert "dataset" not in manifest
    assert manifest["proof"] == "%s.proof.png" % STEM
    assert manifest["print_pdf"] == "%s.print.pdf" % STEM
    assert any("dataset stage failed" in note for note in manifest["notes"])
    note = [note for note in manifest["notes"]
            if "dataset stage failed" in note][0]
    for name in pv.TRANSFORMS:
        assert name in note, "the refusal must quote the vocabulary"
    assert any("dataset stage failed" in line for line in validation["log"])

    # Nothing was written beside the proof, and the proof itself is untouched.
    assert not [p for p in job_dir.iterdir() if ".dataset" in p.name]
    assert (job_dir / ("%s.proof.png" % STEM)).read_bytes() == proof_before
    if on_disk is not None:
        assert (job_dir / ("%s.manifest.json" % STEM)).read_bytes() == on_disk


def test_a_missing_proof_is_also_only_a_note(workdir, monkeypatch):
    """The stage reads the proof; a job directory without one is a note, not a
    failed validation (the proof copy in run_back is what the studio shows)."""
    project, jobs = workdir
    job = make_job(jobs, variants=True)
    monkeypatch.setattr(runner, "build_proof_dataset",
                        lambda *a, **k: (_ for _ in ()).throw(
                            FileNotFoundError("no proof at all")))
    validation = run_back(job, monkeypatch, project)
    assert validation["state"] == "done"
    assert "dataset" not in validation["manifest"]
    assert any("FileNotFoundError" in note for note in validation["manifest"]["notes"])


def test_a_palette_cycle_that_cannot_cycle_is_a_note_not_a_failed_proof(
        workdir, monkeypatch):
    """The stage's own refusal (an unreachable permutation) is a note too.

    This is the raise path the duplicate guard uses: the dataset is not built,
    the reason lands on the manifest, and the PROOF is untouched -- a dataset
    that could not be derived must never turn a good proof into a failed job.
    """
    project, jobs = workdir
    job = make_job(jobs, variants={"enabled": True, "transforms": ["palette-cycle"]})
    validation = run_back(job, monkeypatch, project,
                          screens=["#ffffff", "#ffffff"])

    assert validation["state"] == "done"
    assert "dataset" not in validation["manifest"]
    notes = [note for note in validation["manifest"]["notes"]
             if "dataset stage failed" in note]
    assert notes and "nothing to cycle" in notes[0], notes
    assert validation["manifest"]["proof"] == "%s.proof.png" % STEM
    assert not [p for p in Path(job["dir"]).iterdir() if ".dataset" in p.name]


def test_single_ink_art_still_gets_the_other_eleven_variants(workdir, monkeypatch):
    """The measured defect through the runner: 11 of 12, not `None`.

    A one-colour screen set is the operational form of the real failure (a
    candidate whose whole colour set is ONE ink): `palette-cycle` cannot apply to
    it, and that must cost that one variant instead of the whole dataset.  The
    block reports what was EMITTED, and the skip is named in the log and in
    dataset.json -- 11 of 12 with a note, never 12 claimed and 11 held.
    """
    project, jobs = workdir
    job = make_job(jobs, variants=True)
    validation = run_back(job, monkeypatch, project, screens=["#000000"])

    assert validation["state"] == "done"
    dataset = validation["manifest"]["dataset"]
    assert dataset["count"] == len(pv.TRANSFORMS) - 1 == 11
    assert dataset["transforms"] == [name for name in pv.TRANSFORMS
                                     if name != "palette-cycle"]

    out = Path(job["dir"]) / dataset["dir"]
    assert sorted(p.name for p in (out / "raster").iterdir()) \
        == sorted("%s.png" % name for name in dataset["transforms"])
    assert sorted(p.name for p in (out / "vector").iterdir()) \
        == sorted("%s.svg" % name for name in dataset["transforms"])
    assert not (out / "raster" / "palette-cycle.png").exists()

    payload = json.loads((out / "dataset.json").read_text(encoding="utf-8"))
    assert any("palette-cycle: not applicable" in note
               for note in payload["notes"]), payload["notes"]
    assert any("palette-cycle not applicable" in line
               for line in validation["log"]), validation["log"]


def test_every_transform_inapplicable_means_no_dataset_key_at_all(
        workdir, monkeypatch):
    """Nothing could be derived is a NOTE on a done validation, and no key.

    An empty `.dataset/` would be listed by the archive route as a real dataset;
    the stage leaves nothing behind and the operator is told why.
    """
    project, jobs = workdir
    job = make_job(jobs, variants={"enabled": True,
                                   "transforms": ["palette-cycle"]})
    validation = run_back(job, monkeypatch, project, screens=["#000000"])

    assert validation["state"] == "done"
    manifest = validation["manifest"]
    assert "dataset" not in manifest
    assert any("not applicable" in note for note in manifest["notes"]), manifest
    assert manifest["proof"] == "%s.proof.png" % STEM
    assert not [p for p in Path(job["dir"]).iterdir() if ".dataset" in p.name]


# --------------------------------------------------------------------------
# dataset_stems / dataset_file
# --------------------------------------------------------------------------

def test_dataset_stems_lists_what_is_on_disk(tmp_path):
    job_dir = tmp_path / "job"
    (job_dir / "candidate_02.dataset").mkdir(parents=True)
    (job_dir / "candidate_05.dataset").mkdir(parents=True)
    (job_dir / "candidate_02.svg").write_text("<svg/>", encoding="utf-8")
    assert runner.dataset_stems({"dir": str(job_dir)}) == ["candidate_02",
                                                           "candidate_05"]
    assert runner.dataset_stems({"dir": str(tmp_path / "gone")}) == []


def test_dataset_file_accepts_a_nested_variant(tmp_path):
    dataset = tmp_path / "candidate_02.dataset"
    (dataset / "raster").mkdir(parents=True)
    (dataset / "raster" / "flip-h.png").write_bytes(b"png")
    resolved = runner.dataset_file(dataset, "raster/flip-h.png")
    assert resolved == (dataset / "raster" / "flip-h.png").resolve()


@pytest.mark.parametrize("relative", [
    "", "..", "../secret.txt", "../../secret.txt", "raster/../../secret.txt",
    "/etc/passwd", "raster\\..\\..\\secret.txt", "raster/./x.png",
    "raster//x.png", "a\x00b.png",
])
def test_dataset_file_refuses_anything_that_escapes(tmp_path, relative):
    with pytest.raises(ValueError):
        runner.dataset_file(tmp_path / "candidate_02.dataset", relative)


def test_dataset_file_refuses_a_symlink_out_of_the_tree(tmp_path):
    dataset = tmp_path / "candidate_02.dataset"
    (dataset / "raster").mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("not for publishing", encoding="utf-8")
    os.symlink(secret, dataset / "raster" / "leak.txt")
    with pytest.raises(ValueError):
        runner.dataset_file(dataset, "raster/leak.txt")


# --------------------------------------------------------------------------
# GET /files/<job>/<stem>.dataset/<name> -- the four-segment form
# --------------------------------------------------------------------------

def test_the_nested_file_route_serves_a_variant(workdir, monkeypatch, endpoint):
    project, jobs = workdir
    job = make_job(jobs, variants=True)
    run_back(job, monkeypatch, project)

    status, headers, body = get(
        endpoint, "/files/jobAAA/%s.dataset/raster/rot90.png" % STEM)
    assert status == 200
    assert headers["content-type"] == "image/png"
    assert headers["content-disposition"].startswith("inline")
    import io as _io
    with Image.open(_io.BytesIO(body)) as image:
        assert image.size == (40, 40), "the proof's own pixel dimensions"


def test_the_nested_file_route_serves_a_vector(workdir, monkeypatch, endpoint):
    project, jobs = workdir
    job = make_job(jobs, variants=True)
    run_back(job, monkeypatch, project)

    status, headers, body = get(
        endpoint, "/files/jobAAA/%s.dataset/vector/flip-h.svg" % STEM)
    assert status == 200
    assert headers["content-type"] == "image/svg+xml"
    assert b'transform="translate(40 0) scale(-1 1)"' in body


def test_the_nested_file_route_serves_the_dataset_manifest(workdir, monkeypatch,
                                                           endpoint):
    project, jobs = workdir
    job = make_job(jobs, variants=True)
    run_back(job, monkeypatch, project)

    status, _headers, body = get(
        endpoint, "/files/jobAAA/%s.dataset/dataset.json" % STEM)
    assert status == 200
    assert json.loads(body.decode("utf-8"))["count"] == len(pv.TRANSFORMS)


def test_the_flat_names_still_go_through_the_three_segment_route(workdir,
                                                                 monkeypatch,
                                                                 endpoint):
    project, jobs = workdir
    job = make_job(jobs, variants=True)
    run_back(job, monkeypatch, project)

    status, _headers, body = get(
        endpoint, "/files/jobAAA/%s.dataset.json" % STEM)
    assert status == 200
    assert json.loads(body.decode("utf-8"))["dir"] == "%s.dataset" % STEM

    status, headers, _body = get(
        endpoint, "/files/jobAAA/%s.dataset-contact-sheet.png" % STEM)
    assert status == 200
    assert headers["content-type"] == "image/png"


def test_the_nested_file_route_refuses_traversal(workdir, monkeypatch, endpoint):
    project, jobs = workdir
    job = make_job(jobs, variants=True)
    run_back(job, monkeypatch, project)
    secret = jobs / "secret.txt"
    secret.write_text("not for publishing", encoding="utf-8")

    for path in ("/files/jobAAA/%s.dataset/../../secret.txt" % STEM,
                 "/files/jobAAA/%s.dataset/raster/../../../secret.txt" % STEM,
                 "/files/jobAAA/%s.dataset/..%%2f..%%2fsecret.txt" % STEM):
        status, _headers, body = get(endpoint, path)
        assert status in (400, 404), (path, status)
        assert b"not for publishing" not in body, path
    assert secret.read_text(encoding="utf-8") == "not for publishing"


def test_the_nested_file_route_404s_an_unknown_dataset_or_job(workdir,
                                                              monkeypatch,
                                                              endpoint):
    project, jobs = workdir
    job = make_job(jobs, variants=True)
    run_back(job, monkeypatch, project)

    status, _headers, _body = get(
        endpoint, "/files/jobAAA/candidate_99.dataset/raster/flip-h.png")
    assert status == 404
    status, _headers, _body = get(
        endpoint, "/files/nosuchjob/%s.dataset/raster/flip-h.png" % STEM)
    assert status == 404
    status, _headers, _body = get(
        endpoint, "/files/jobAAA/%s.dataset/raster/nope.png" % STEM)
    assert status == 404


# --------------------------------------------------------------------------
# GET /jobs/<job>/dataset -- the on-demand archive
# --------------------------------------------------------------------------

def test_the_dataset_archive_is_409_unless_the_job_is_done(workdir, monkeypatch,
                                                           endpoint):
    project, jobs = workdir
    job = make_job(jobs, variants=True)
    run_back(job, monkeypatch, project)
    job["state"] = "running"

    status, _headers, body = get(endpoint, "/jobs/jobAAA/dataset")
    assert status == 409
    assert b"job not done" in body


def test_the_dataset_archive_is_404_when_there_is_no_dataset(workdir,
                                                             monkeypatch,
                                                             endpoint):
    project, jobs = workdir
    job = make_job(jobs)                       # opted out: no dataset at all
    run_back(job, monkeypatch, project)

    status, _headers, body = get(endpoint, "/jobs/jobAAA/dataset")
    assert status == 404
    assert b"no dataset" in body


def test_the_dataset_archive_404s_an_unknown_candidate(workdir, monkeypatch,
                                                       endpoint):
    project, jobs = workdir
    job = make_job(jobs, variants=True)
    run_back(job, monkeypatch, project)

    status, _headers, _body = get(
        endpoint, "/jobs/jobAAA/dataset?candidate=candidate_99.svg")
    assert status == 404


def test_the_dataset_archive_is_400_when_the_candidate_is_ambiguous(workdir,
                                                                    monkeypatch,
                                                                    endpoint):
    project, jobs = workdir
    job = make_job(jobs, variants=True)
    run_back(job, monkeypatch, project)
    # A second validation's dataset for the same job: the caller must say which.
    other = Path(job["dir"]) / "candidate_05.dataset"
    (other / "raster").mkdir(parents=True)

    status, _headers, body = get(endpoint, "/jobs/jobAAA/dataset")
    assert status == 400
    assert b"candidate is required" in body

    status, _headers, _body = get(
        endpoint, "/jobs/jobAAA/dataset?candidate=candidate_05.svg")
    # 7z IS present on this host, and the route really does build the package:
    # measured, it answers 200 with a ~19 MB archive containing 26 files. This
    # used to read `assert status in (200, 500)`, which accepted a built package
    # OR a failed build -- it could not detect a broken archive path in either
    # direction, and this is the only test covering the route whose whole job is
    # to produce the package the feature exists for. The tolerance was there for
    # hosts without 7z; state that case instead of covering both with one blur.
    if shutil.which("7z") or shutil.which("7za") or shutil.which("7zr"):
        assert status == 200, "7z is present, so the package must actually build"
    else:
        assert status == 500, \
            "no 7z on this host: the route must SAY so rather than pretending"
    status, _headers, _body = get(
        endpoint, "/jobs/jobAAA/dataset?candidate=../../etc/passwd")
    assert status == 400


def test_the_dataset_archive_streams_and_unlinks_its_temporary_file(
        workdir, monkeypatch, endpoint):
    project, jobs = workdir
    job = make_job(jobs, variants=True)
    run_back(job, monkeypatch, project)
    monkeypatch.setattr(runner, "free_bytes", lambda path: 8 * 1024 ** 3)

    def fake_archive(dataset_dir, out_path):
        out_path.write_bytes(b"7z-ish bytes")
        return out_path

    monkeypatch.setattr(runner, "build_dataset_archive", fake_archive)

    status, headers, body = get(endpoint, "/jobs/jobAAA/dataset")
    assert status == 200
    assert headers["content-type"] == "application/x-7z-compressed"
    assert headers["content-disposition"] == \
        'attachment; filename="%s.dataset.7z"' % STEM
    assert body == b"7z-ish bytes"
    # The unlink happens SERVER-side, in a finally after serve_file returns --
    # and the client can have read the whole body and returned before that
    # thread reaches the unlink, so the instant after the read is a race. This
    # is not theoretical: it passed 6/6 in isolation on both the host and the
    # image's interpreter, then failed exactly here under the load of the full
    # suite in the image build (the Dockerfile runs pytest, so a flaky assertion
    # makes the image unbuildable). Poll with a bounded deadline instead.
    temp = jobs / ("%s.dataset.7z" % STEM)
    deadline = time.monotonic() + 5
    while temp.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not temp.exists(), \
        "the temporary archive is unlinked in a finally"


def test_the_dataset_archive_is_500_when_7z_is_missing(workdir, monkeypatch,
                                                       endpoint):
    project, jobs = workdir
    job = make_job(jobs, variants=True)
    run_back(job, monkeypatch, project)
    monkeypatch.setattr(runner, "free_bytes", lambda path: 8 * 1024 ** 3)
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)

    status, _headers, body = get(endpoint, "/jobs/jobAAA/dataset")
    assert status == 500
    assert b"7z unavailable" in body


# ------------------------------------------- published artifact names -------
#
# The name-stamping path, driven for real through the synthetic project tree:
# _copy_front_outputs publishes the candidates a job's directory serves and
# run_back publishes the proof. Both directions matter -- the studio stores the
# published candidate name and posts it BACK to /validations, so run_back has to
# map it to the pipeline's own spelling before the print check runs.

NAMED_CANDIDATE = "jobby-the-boat-candidate-02.svg"


def named_job(jobs, project, *, candidate=NAMED_CANDIDATE):
    """A job that passed the front half under a NAMED run."""
    job_dir = jobs / "jobNAMED"
    (job_dir / "input").mkdir(parents=True)
    (job_dir / "input" / "artwork.png").write_bytes(make_png())
    spec = make_spec()
    (job_dir / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    (job_dir / candidate).write_text(SVG, encoding="utf-8")
    job = {"id": "jobNAMED", "name": "jobby the boat", "name_slug": "jobby-the-boat",
           "stem": "artwork", "dir": str(job_dir), "container_input_path": "",
           "spec": spec, "state": "done", "log": [],
           "candidates": [{"file": candidate}], "comparison": None,
           "prep_summary": None, "error": None, "cancel": False, "proc": None}
    runner.JOBS["jobNAMED"] = job
    return job


def test_front_outputs_are_published_under_the_job_name(workdir):
    project, jobs = workdir
    job_dir = jobs / "jobNAMED"
    job_dir.mkdir()
    traced = project / "02_traced" / "artwork"
    traced.mkdir(parents=True)
    for name in ("candidate_02.svg", "candidate_05.svg"):
        (traced / name).write_text(SVG, encoding="utf-8")
    (project / "04_validated").mkdir(parents=True)
    (project / "04_validated" / "artwork.comparison.json").write_text(
        json.dumps({"candidates": [{"file": "candidate_02.svg"},
                                   {"file": "candidate_05.svg"}]}), encoding="utf-8")
    job = {"id": "jobNAMED", "name": "jobby the boat", "name_slug": "jobby-the-boat",
           "stem": "artwork", "dir": str(job_dir), "spec": {}, "state": "running",
           "log": [], "candidates": [], "comparison": None, "prep_summary": None,
           "error": None, "cancel": False, "proc": None}

    candidates = runner._copy_front_outputs(job, "artwork")

    assert [c["file"] for c in candidates] == ["jobby-the-boat-candidate-02.svg",
                                               "jobby-the-boat-candidate-05.svg"]
    for name in ("jobby-the-boat-candidate-02.svg", "jobby-the-boat-candidate-05.svg"):
        assert (job_dir / name).is_file(), "the copy the file route serves"
    assert "candidate_02.svg" not in [p.name for p in job_dir.iterdir()], \
        "the pipeline's own name is not ALSO published -- one name per artifact"

    # The comparison the caller reads (run_front re-reads this file) says the
    # same thing the entries do, or the studio would store a name it cannot fetch.
    served = json.loads((job_dir / "comparison.json").read_text(encoding="utf-8"))
    assert [c["file"] for c in served["candidates"]] == \
        [c["file"] for c in candidates]


def test_run_back_publishes_the_proof_under_the_job_name(workdir, monkeypatch):
    project, jobs = workdir
    job = named_job(jobs, project)
    job_dir = Path(job["dir"])

    validation = run_back(job, monkeypatch, project, candidate=NAMED_CANDIDATE)

    assert validation["state"] == "done"
    assert validation["manifest"]["proof"] == "jobby-the-boat-proof-02.png"
    assert validation["manifest"]["print_pdf"] == "jobby-the-boat-proof-02.pdf"
    for name in ("jobby-the-boat-proof-02.png", "jobby-the-boat-proof-02.pdf",
                 "jobby-the-boat-proof-02.manifest.json"):
        assert (job_dir / name).is_file(), name
    on_disk = json.loads((job_dir / "jobby-the-boat-proof-02.manifest.json")
                         .read_text(encoding="utf-8"))
    assert on_disk["proof"] == "jobby-the-boat-proof-02.png", \
        "the manifest names the file that was actually published"

    # The pipeline never sees the job's name: the print check ran on its own
    # candidate_NN.svg, which is the half AGENTS.md freezes.
    assert (project / "02_traced" / "artwork" / "candidate_02.svg").is_file()
    assert not (project / "02_traced" / "artwork" / NAMED_CANDIDATE).exists()


def test_a_named_job_still_serves_through_the_file_route(workdir, monkeypatch):
    """The published bytes are reachable by the name the studio holds -- the
    whole point of the rename, and the one thing a unit test on the mapper
    cannot show."""
    project, jobs = workdir
    job = named_job(jobs, project)
    job_dir = Path(job["dir"])
    run_back(job, monkeypatch, project, candidate=NAMED_CANDIDATE)

    for name in (NAMED_CANDIDATE, "jobby-the-boat-proof-02.png",
                 "jobby-the-boat-proof-02.pdf"):
        assert runner.artifact_path(project, stem=job["stem"], name=name,
                                    job_dir=job_dir) == job_dir / name
