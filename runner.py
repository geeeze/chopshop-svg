#!/usr/bin/env python3
"""HTTP runner for chopshop-svg.

Executes the real front_pipeline.sh and pipeline.sh inside this image. Job
state is in memory; uploads and per-job artifacts live under bind-mounted
/data directories so Rails can share source files with the container.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

PROJECT = Path(os.environ.get("PIPELINE_ROOT", "/app"))
# The repo root, resolved from this file — not PIPELINE_ROOT — so the recolour
# seam below can import scripts/palette_variants whether the checkout is the
# container (/app) or a host checkout the test suite imports this module from.
HERE = Path(__file__).resolve().parent
JOBS_ROOT = Path(os.environ.get("RUNNER_ROOT", "/data/jobs"))
HOST_UPLOAD_ROOT = Path(os.environ.get("HOST_UPLOAD_ROOT", "/data/uploads"))
CONTAINER_UPLOAD_ROOT = Path(os.environ.get("CONTAINER_UPLOAD_ROOT", "/data/uploads"))
HOST_BIND = os.environ.get("RUNNER_BIND", "0.0.0.0")
PORT = int(os.environ.get("RUNNER_PORT", "8787"))
TOKEN = os.environ.get("RUNNER_TOKEN", "")
PIPELINE_LOCK = threading.Lock()
STATE_LOCK = threading.Lock()
JOBS: dict[str, dict] = {}
VALIDATIONS: dict[str, dict] = {}
MIN_ARCHIVE_FREE = 1 * 1024 ** 3  # refuse to build a 7z archive below 1 GiB free


class Cancelled(Exception):
    """Raised by a worker to signal an operator-cancelled job."""


class JevUnavailable(Exception):
    """Raised when the optional Jev add-on cannot be called at all.

    Distinct from a failed call: this is "the add-on is not configured here",
    which the HTTP layer reports as 503 with the reason, never as a 404 that
    would read as a missing route.
    """


# ---------- Jev (TypeSafe System One) — optional add-on -----------------------
#
# The runner proxies the studio's POST /validations/:id/jev to the chopshop-jev
# annotator. It deliberately does NOT reimplement the question set, the
# projection or the verdict: a second copy would drift from the tested one, and
# the annotator already owns the endpoint/key resolution and the typing rules.
# The script path and key are configuration, not facts about this machine (this
# repo is public) — see docker-compose.yml for the env names.

JEV_DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
JEV_TIMEOUT = float(os.environ.get("JEV_TIMEOUT", "180"))


def jev_unavailable_reason() -> str | None:
    """Return why Jev cannot be called, or None when it can.

    Order matters for diagnosis: the operator who pressed "Ask Jev" gets the
    first missing piece, not a generic failure.
    """
    annotator = os.environ.get("JEV_ANNOTATOR", "")
    if not annotator:
        return "JEV_ANNOTATOR is not set on the runner"
    if not Path(annotator).is_file():
        return f"Jev annotator not found at {annotator}"
    if not (os.environ.get("JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY")):
        return "JEV_API_KEY is not set on the runner"
    return None


def run_jev(manifest_path: Path) -> dict:
    """Ask Jev the four advisory questions about one manifest.

    Returns the studio-facing envelope {model, endpoint, latency_ms, answers,
    verdict}. The annotator writes its sidecar into a temp dir — the job
    directory belongs to the pipeline (and is root-owned on a bind mount), so
    it is not ours to write in.
    """
    reason = jev_unavailable_reason()
    if reason:
        raise JevUnavailable(reason)
    annotator = os.environ["JEV_ANNOTATOR"]
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "jev.json"
        started = time.monotonic()
        try:
            proc = subprocess.run(
                [sys.executable, annotator, str(manifest_path), "--output", str(output)],
                capture_output=True, text=True, timeout=JEV_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            raise JevUnavailable(f"Jev annotator timed out after {JEV_TIMEOUT:.0f}s") from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        if proc.returncode != 0 or not output.is_file():
            lines = [ln for ln in (proc.stderr or proc.stdout or "").splitlines() if ln.strip()]
            detail = lines[-1] if lines else f"annotator exited {proc.returncode}"
            raise JevUnavailable(f"Jev annotator failed: {detail}")
        try:
            sidecar = json.loads(output.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise JevUnavailable(f"Jev annotator wrote invalid JSON: {exc}") from exc
    if sidecar.get("status") != "ok":
        detail = sidecar.get("reason") or "no reason given"
        raise JevUnavailable(f"Jev annotator status {sidecar.get('status')}: {detail}")
    return {
        "model": sidecar.get("model"),
        "endpoint": os.environ.get("JEV_ENDPOINT") or JEV_DEFAULT_ENDPOINT,
        "latency_ms": latency_ms,
        "answers": sidecar.get("answers") or {},
        "verdict": sidecar.get("verdict"),
    }


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def build_archive(job_dir: Path, name: str) -> Path | None:
    """7z the whole job directory (contents under one folder). Returns the
    archive path, or None when 7z is missing or fails."""
    seven = shutil.which("7z") or shutil.which("7za") or shutil.which("7zr")
    if not seven:
        return None
    out = job_dir.parent / f"{name}.7z"
    cmd = [seven, "a", "-t7z", "-mx=5", "-y", "-bd", str(out), str(job_dir)]
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=300)
    if proc.returncode != 0 or not out.is_file():
        return None
    return out


def slug(name: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return value or "artwork"


def health_payload() -> dict:
    """The body of GET /health.

    `host` is a LABEL, read from the environment, not a literal in this file.
    That matters because this repo is public: a hardcoded machine name in a
    tracked file is published to anyone who reads it, and the studio only
    needs something to show in its runner dropdown. Set
    CHOPSHOP_RUNNER_LABEL to override; otherwise report this machine's name.
    """
    return {
        "ok": True,
        "host": os.environ.get("CHOPSHOP_RUNNER_LABEL") or socket.gethostname(),
        "mode": "real",
        "tools": {
            "inkscape": shutil.which("inkscape") is not None,
            "gs": shutil.which("gs") is not None,
            # Optional add-on readiness, reported so an unset key is visible
            # BEFORE the operator presses "Ask Jev" (the studio proxies this
            # through RunnerClient.health).
            "jev": jev_unavailable_reason() is None,
        },
    }


def container_input_path(input_path: str, *, host_upload_root: Path = HOST_UPLOAD_ROOT,
                         container_upload_root: Path = CONTAINER_UPLOAD_ROOT) -> str:
    source = Path(input_path).resolve()
    host_root = Path(host_upload_root).resolve()
    container_root = Path(container_upload_root)
    try:
        relative = source.relative_to(host_root)
    except ValueError as exc:
        raise ValueError(f"input is outside shared upload directory: {source}") from exc
    return str(container_root / relative)


def normalize_input(source: Path, destination: Path, *, max_dimension: int) -> dict:
    from PIL import Image

    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        original_width, original_height = image.size
        scale = min(1.0, max_dimension / max(original_width, original_height))
        width = max(1, round(original_width * scale))
        height = max(1, round(original_height * scale))
        if scale == 1.0:
            image.save(destination)
        else:
            image.resize((width, height), Image.Resampling.LANCZOS).save(destination)
    return {"original_width": original_width, "original_height": original_height,
            "original_pixels": original_width * original_height,
            "width": width, "height": height, "downscaled": scale < 1.0}


def comparison_path(root: Path, stem: str) -> Path:
    return root / "04_validated" / f"{stem}.comparison.json"


def artifact_path(root: Path, *, stem: str, name: str, job_dir: Path) -> Path | None:
    local = job_dir / name
    if local.is_file():
        return local
    # Source raster: Rails requests ``source.png``, but the original upload keeps
    # its own name/extension under ``input/``. Serve it when no canonical source
    # file exists at the job root (older jobs predate one). NOTE: this fallback
    # is for the PLAIN source only — ``source.inverse.png`` must never resolve to
    # the un-inverted upload, or the inverse tile shows the source as its own
    # "twin". An absent inverse twin should 404 so the studio tile self-hides.
    if name == "source.png":
        input_dir = job_dir / "input"
        if input_dir.is_dir():
            for candidate in sorted(p for p in input_dir.iterdir() if p.is_file()):
                if candidate.suffix.lower() in (".png", ".jpg", ".jpeg",
                                                ".tif", ".tiff", ".webp"):
                    return candidate
    traced = root / "02_traced" / stem / name
    if traced.is_file():
        return traced
    final = root / "05_final" / name
    if final.is_file():
        return final
    # Source previews live in 01_prepped under their own naming scheme
    # (<stem>.prepped.png / <stem>.prepped.inverse.png) while the studio asks
    # for the stable names source.png / source.inverse.png. Resolve them here so
    # a job whose runner predates _publish_source_previews still serves both.
    if name in ("source.png", "source.inverse.png") and stem:
        suffix = ".prepped.png" if name == "source.png" else ".prepped.inverse.png"
        prepped = root / "01_prepped" / f"{stem}{suffix}"
        if prepped.is_file():
            return prepped
    return None


def _append_log(job: dict, line: str) -> None:
    with STATE_LOCK:
        job["log"].append(line.rstrip())


def _run_logged(job: dict, command: list[str], env: dict[str, str]) -> int:
    process = subprocess.Popen(
        command, cwd=PROJECT, env={**os.environ, **env}, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1,
    )
    assert process.stdout is not None
    job["proc"] = process
    try:
        for line in process.stdout:
            _append_log(job, line)
            if job.get("cancel") and process.poll() is None:
                process.terminate()
    finally:
        code = process.wait()
        job["proc"] = None
    return code


def _copy_front_outputs(job: dict, stem: str) -> list[dict]:
    report = comparison_path(PROJECT, stem)
    if not report.is_file():
        raise RuntimeError(f"front pipeline produced no comparison report: {report}")
    data = json.loads(report.read_text(encoding="utf-8"))
    job_dir = Path(job["dir"])
    shutil.copy2(report, job_dir / "comparison.json")
    candidates = []
    for entry in data.get("candidates", []):
        name = entry.get("file", "")
        source = PROJECT / "02_traced" / stem / name
        if source.is_file():
            shutil.copy2(source, job_dir / name)
        candidates.append(entry)
    return candidates


def _publish_source_previews(job: dict, stem: str) -> dict:
    """Publish the prepped source and its colour-inverted twin for the studio.

    The studio's source preview fetches two stable names, `source.png` and
    `source.inverse.png`. Publishing them here (rather than expecting the
    caller to know the upload's filename) is what lets the job page show a
    source/inverse pair without knowing anything about the stem.

    The inverse twin is produced by prep_raster.py when `print.invert` is set,
    so its absence is NORMAL, not an error: the studio's image tag self-hides
    on a 404 and the page degrades to the plain source chip. Only the original
    is required.
    """
    job_dir = Path(job["dir"])
    published = {"source.png": False, "source.inverse.png": False}
    original = PROJECT / "01_prepped" / f"{stem}.prepped.png"
    if original.is_file():
        shutil.copy2(original, job_dir / "source.png")
        published["source.png"] = True
    twin = PROJECT / "01_prepped" / f"{stem}.prepped.inverse.png"
    if twin.is_file():
        shutil.copy2(twin, job_dir / "source.inverse.png")
        published["source.inverse.png"] = True
    return published


# Flat names for the expanded-prep artifacts. They are copied to the job root
# because ``artifact_path`` resolves ``job_dir / name`` and nothing deeper, so a
# nested path would archive fine but never be individually servable. The two
# contact sheets would collide under one name, hence the prefixes.
PREP_EXPAND_ARTIFACTS = (
    ("prep-expand.json", "prep-expand.json"),
    ("prep-expand.md", "prep-expand.md"),
    ("tune/tune.json", "tune.json"),
    ("tune/tune.md", "tune.md"),
    ("tune/contact-sheet.png", "tune-contact-sheet.png"),
    ("node_reduce/node-reduce.json", "node-reduce.json"),
)


def _publish_prep_expand(job: dict, stem: str) -> dict:
    """Copy the expanded-prep reports to the job root and summarise them.

    Absent artifacts are simply not listed: the stage is optional and a job that
    did not ask for it must not look like a job whose stage broke.
    """
    source = Path(job["dir"]) / "prep_expand"
    publish = job.get("dir") and Path(job["dir"])
    artifacts: dict[str, str] = {}
    if not source.is_dir():
        return {"steps": [], "artifacts": artifacts, "notes": []}

    pairs = list(PREP_EXPAND_ARTIFACTS)
    # im_filters nests one level by source stem (filters/<stem>/filters.json).
    for nested in sorted(source.glob("filters/*/filters.json")):
        pairs.append((str(nested.relative_to(source)), "filters.json"))
        md = nested.with_name("filters.md")
        if md.is_file():
            pairs.append((str(md.relative_to(source)), "filters.md"))
        sheet = nested.with_name("contact-sheet.png")
        if sheet.is_file():
            pairs.append((str(sheet.relative_to(source)), "filters-contact-sheet.png"))

    for relative, flat in pairs:
        candidate = source / relative
        if candidate.is_file():
            shutil.copy2(candidate, publish / flat)
            artifacts[flat] = relative

    summary = None
    report = source / "prep-expand.json"
    if report.is_file():
        try:
            summary = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            summary = None

    notes: list[str] = []
    steps = list((summary or {}).get("steps_run") or [])
    node = (summary or {}).get("node_reduce") or {}
    if node.get("status") == "ok" and node.get("over_gate"):
        notes.append("node_reduce wrote %d reduced copies of over-gate "
                     "candidates; originals kept" % len(node.get("written") or []))
    for key, label in (("filters", "filters"), ("tune", "tune")):
        phase = (summary or {}).get(key) or {}
        if phase.get("status") == "skipped":
            notes.append("%s skipped: %s" % (label, phase.get("reason")))
    return {"steps": steps, "artifacts": artifacts, "notes": notes,
            "report": json.loads(report.read_text(encoding="utf-8"))
            if report.is_file() else None}


def run_front(job: dict) -> None:
    with PIPELINE_LOCK:
        if job.get("cancel"):
            raise Cancelled()
        job["state"] = "running"
        source = Path(job["container_input_path"])
        normalized = normalize_input(source, Path(job["dir"]) / "input" / source.name,
                                     max_dimension=int(os.environ.get("MAX_INPUT_DIMENSION", "1500")))
        input_path = Path(job["dir"]) / "input" / source.name
        stem = input_path.stem
        spec_path = Path(job["dir"]) / "spec.json"
        spec_path.write_text(json.dumps(job["spec"], indent=2), encoding="utf-8")
        if normalized["downscaled"]:
            _append_log(job, "input normalized for this runner: "
                             f"{normalized['original_width']}x{normalized['original_height']} -> "
                             f"{normalized['width']}x{normalized['height']}")
        _append_log(job, f"front pipeline: {input_path}")
        # spec.print.prep_expand benches (filters, tune, node_reduce) write here
        # rather than to the shared 08_tune/09_filters tree, so their output
        # lands inside the job directory: the archive endpoint 7-Zips that whole
        # directory and DELETE rmtree's it, and a per-job artifact left in the
        # project tree would survive the delete and leak into the next job.
        expand_dir = Path(job["dir"]) / "prep_expand"
        code = _run_logged(job, [str(PROJECT / "front_pipeline.sh"), str(input_path)],
                           {"SPEC": str(spec_path), "FRONT_PIPELINE_WORKERS": "1",
                            "PREP_EXPAND_OUT": str(expand_dir)})
        if job.get("cancel"):
            raise Cancelled()
        prep = PROJECT / "01_prepped" / f"{stem}.prep.json"
        prep_summary = json.loads(prep.read_text(encoding="utf-8")) if prep.is_file() else None
        previews = _publish_source_previews(job, stem)
        expand = _publish_prep_expand(job, stem)
        candidates = _copy_front_outputs(job, stem) if code in (0, 1) else []
        if not candidates:
            raise RuntimeError(f"front pipeline exited {code} without candidates")
        if previews["source.inverse.png"]:
            _append_log(job, "source previews: source.png + source.inverse.png")
        else:
            _append_log(job, "source previews: source.png (no inverse twin; "
                             "set spec.print.invert to produce one)")
        if expand.get("steps"):
            _append_log(job, "prep expand: %s -> %s" % (
                ", ".join(expand["steps"]), ", ".join(sorted(expand["artifacts"]))))
            for note in expand.get("notes", []):
                _append_log(job, "prep expand: " + note)
        with STATE_LOCK:
            job.update(stem=stem, candidates=candidates, comparison=json.loads(
                (Path(job["dir"]) / "comparison.json").read_text(encoding="utf-8")),
                prep_summary=prep_summary, source_previews=previews,
                prep_expand=expand, state="done")


# ------------------------------------------------------- proof dataset seam --
#
# The opt-in tail of the print check: "similar copies of the selected proof"
# (mirrored / rotated / colour-shifted) emitted as a raster+vector PAIR each,
# plus a manifest and a contact sheet, packaged so an operator can see what an
# augmented training set for this pipeline would look like.
#
# The stage lives in scripts/proof_variants.py and is invoked HERE rather than
# from pipeline.sh: AGENTS.md freezes pipeline.sh (and validate_svg.py /
# preflight.py / snap_colors.py) as the tested print check, so the wiring point
# is the runner, after the shell script has already produced the proof.  The
# stage is additive -- it reads the proof, the selected SVG and the manifest and
# writes only into its own `<stem>.dataset/` directory and three flat copies.
#
# Imported lazily, exactly like the recolour seam below, so starting the runner
# never pulls in PIL/lxml.
# dataset-seam

DATASET_SUFFIX = ".dataset"


def _proof_variants():
    import sys as _sys
    _scripts = HERE / "scripts"
    if str(_scripts) not in _sys.path:
        _sys.path.insert(0, str(_scripts))
    import proof_variants  # noqa: F401  (imports PIL + lxml + palette_variants)
    return proof_variants


def variants_request(spec: dict) -> tuple:
    """(enabled, transforms) for ``validation.variants`` in the spec.

    Absent, false or an object without ``enabled`` is the DEFAULT and produces
    nothing new: a job that did not ask for the stage must not look like a job
    whose stage broke.  ``true`` means the whole vocabulary; an object
    ``{"enabled": true, "transforms": [...]}`` selects a subset.  Any other
    shape is a caller error and is raised as one.
    """
    raw = (spec.get("validation") or {}).get("variants")
    if raw is True:
        return True, None
    if raw is None or raw is False:
        return False, None
    if isinstance(raw, dict):
        return bool(raw.get("enabled")), raw.get("transforms")
    raise ValueError("validation.variants must be true/false or an object "
                     "{\"enabled\": bool, \"transforms\": [...]}, got %r" % (raw,))


def dataset_stems(job: dict) -> list:
    """Candidate stems that HAVE a dataset on disk for this job, sorted.

    Read off the filesystem, not out of VALIDATIONS: validations are in-memory
    only, so after a runner restart the datasets are still on the bind mount and
    their archives must still be built.
    """
    root = Path(job["dir"])
    if not root.is_dir():
        return []
    return sorted(entry.name[: -len(DATASET_SUFFIX)] for entry in root.iterdir()
                  if entry.is_dir() and entry.name.endswith(DATASET_SUFFIX))


def dataset_file(dataset_dir: Path, relative: str) -> Path:
    """Resolve one file inside a job's ``<stem>.dataset``; ValueError if it escapes.

    Containment is checked on the RESOLVED path, so a ``..`` segment, an
    absolute name, a backslash or a symlink pointing out of the tree is refused
    rather than normalised into something that looks valid.  This nested form
    needs its own gate: the flat ``/files/`` route's ``[\\w.\\-]+`` name check
    cannot see a second path segment, and the dataset is the one place a request
    contains a slash.
    """
    if not relative or "\\" in relative or "\x00" in relative:
        raise ValueError("invalid dataset file name")
    segments = relative.split("/")
    if any(segment in ("", ".", "..") for segment in segments):
        raise ValueError("invalid dataset file name")
    root = dataset_dir.resolve()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("path escapes the dataset directory")
    return candidate


def build_dataset_archive(dataset_dir: Path, out_path: Path) -> Path | None:
    """7z one dataset directory; returns the archive path or None.

    The same optional-tool rule as build_archive: no 7z on PATH (or a failure)
    returns None so the route can answer 500, never a half-written download.
    """
    seven = shutil.which("7z") or shutil.which("7za") or shutil.which("7zr")
    if not seven:
        return None
    cmd = [seven, "a", "-t7z", "-mx=5", "-y", "-bd", str(out_path),
           str(dataset_dir)]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not out_path.is_file():
        return None
    return out_path


def build_proof_dataset(job: dict, validation: dict, manifest: dict,
                        candidate: str, spec_path: Path):
    """Run the opt-in dataset stage; return the manifest's ``dataset`` value.

    None means the spec did not ask for it, which is the default.  Anything else
    raises, and run_back records that as a NOTE on the manifest instead of
    failing the validation: the proof is the deliverable and it already
    succeeded by the time this runs.
    """
    spec: dict = {}
    if spec_path.is_file():
        try:
            loaded = json.loads(spec_path.read_text(encoding="utf-8"))
            spec = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            spec = {}
    enabled, requested = variants_request(spec)
    if not enabled:
        return None

    pv = _proof_variants()
    plan = pv.variant_plan(requested)          # refuses an unknown name outright
    if not plan:
        _append_log(validation, "dataset: variants are enabled but no transforms "
                                "were selected; nothing derived")
        return None

    job_dir = Path(job["dir"])
    candidate_stem = Path(candidate).stem
    out_dir = job_dir / f"{candidate_stem}{DATASET_SUFFIX}"
    # Provenance points at the manifest ON DISK (run_back has already copied
    # it): a reader of dataset.json can open the file it was derived from. The
    # in-memory dict is the fallback for a job directory without one.
    manifest_file = job_dir / f"{candidate_stem}.manifest.json"
    description = pv.build_dataset(
        job_dir / f"{candidate_stem}.proof.png", job_dir / candidate, out_dir,
        transforms=plan,
        manifest=manifest_file if manifest_file.is_file() else manifest,
        stem=candidate_stem)

    # The three FLAT names sit at the job root so the existing artifact_path
    # resolution serves them without a nested route; the variant files under
    # raster/ and vector/ are served by the four-segment /files/ route.
    shutil.copy2(out_dir / "dataset.json",
                 job_dir / f"{candidate_stem}{DATASET_SUFFIX}.json")
    shutil.copy2(out_dir / "dataset.md",
                 job_dir / f"{candidate_stem}{DATASET_SUFFIX}.md")
    pv.contact_sheet(
        [(out_dir / "raster" / entry["raster"]["name"], entry["transform"])
         for entry in description["files"]],
        job_dir / description["sheet"])

    _append_log(validation, "dataset: %d variants (%s) -> %s"
                % (description["count"], ", ".join(description["transforms"]),
                   description["dir"]))
    # Honest reporting, the operator's half: the dataset block below says what
    # WAS emitted, and a requested transform that cannot apply to this artwork is
    # named here with its reason (the same note is in dataset.json/dataset.md).
    # Its whole point is that 11 out of 12 must not read like a stage that lost
    # one -- this is the only line in the runner that this required.
    for entry in description.get("skipped") or []:
        _append_log(validation, "dataset: %s not applicable to this artwork: %s"
                    % (entry["transform"], entry["reason"]))
    return {
        "dir": description["dir"],
        "manifest": description["manifest"],
        "sheet": description["sheet"],
        "package": description["package"],
        "count": description["count"],
        "transforms": description["transforms"],
    }


def run_back(validation: dict) -> None:
    job = JOBS[validation["job_id"]]
    with PIPELINE_LOCK:
        validation["state"] = "running"
        candidate = validation["candidate_file"]
        stem = job["stem"]
        candidate_dir = PROJECT / "02_traced" / stem
        candidate_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(job["dir"]) / candidate, candidate_dir / candidate)
        spec_path = Path(job["dir"]) / "spec.json"
        _append_log(validation, f"pipeline.sh: {stem}/{candidate}")
        code = _run_logged(validation, [str(PROJECT / "pipeline.sh"), str(candidate_dir / candidate)],
                           {"SPEC": str(spec_path)})
        candidate_stem = Path(candidate).stem
        output_dir = PROJECT / "05_final"
        manifest_path = output_dir / f"{candidate_stem}.manifest.json"
        if not manifest_path.is_file():
            raise RuntimeError(f"back pipeline exited {code} without manifest")
        job_dir = Path(job["dir"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        proof_name = f"{candidate_stem}.proof.png"
        pdf_name = f"{candidate_stem}.print.pdf"
        shutil.copy2(manifest_path, job_dir / f"{candidate_stem}.manifest.json")
        shutil.copy2(output_dir / proof_name, job_dir / proof_name)
        shutil.copy2(output_dir / pdf_name, job_dir / pdf_name)
        manifest["proof"] = proof_name
        manifest["print_pdf"] = pdf_name
        # Opt-in dataset stage (spec: validation.variants).  It runs LAST and it
        # is additive: the proof, the print PDF, the manifest and the selected
        # SVG are never edited or replaced by it.  A failure here is recorded as
        # a NOTE on the manifest and the validation still settles to done -- the
        # proof is the deliverable and it already succeeded, so a dataset that
        # could not be derived must not turn a good proof into a failed job.
        # dataset-seam
        try:
            dataset = build_proof_dataset(job, validation, manifest, candidate,
                                          spec_path)
            if dataset:
                manifest["dataset"] = dataset
        except Exception as exc:                      # noqa: BLE001 - report it
            note = "dataset stage failed: %s: %s" % (type(exc).__name__, exc)
            if not isinstance(manifest.get("notes"), list):
                manifest["notes"] = []
            manifest["notes"].append(note)
            _append_log(validation, note)
        with STATE_LOCK:
            validation.update(manifest=manifest, state="done")


def guarded(fn, item: dict) -> None:
    try:
        fn(item)
    except Cancelled:
        with STATE_LOCK:
            item.update(state="cancelled", error="cancelled by operator")
    except Exception as exc:
        with STATE_LOCK:
            item.update(state="error", error=f"{type(exc).__name__}: {exc}")
        traceback.print_exc()


# ----------------------------------------------------------- recolour seam --
#
# UNBUILT SEAM (2026-09-28). The two routes below -- POST /recolour and
# POST /recolour/colours -- are implemented and unit-tested, but NO studio
# consumer calls them: the "cycle colours" preview they were written for was
# never built, so nothing in the product is behind them. Do not read them as a
# live feature, do not document UI behaviour that assumes them, and do not
# remove them without checking the studio side first. Documented as unbuilt in
# docs/runner-http-contract.md and CONTEXT.md.
#
# What the seam does, if you do call it: recolours a candidate SVG without a
# raster round-trip: only fill / stroke / stop-color change, geometry, node
# count and dimensions are untouched. The actual rewrite is palette_variants'
# tested apply_mapping (scripts/palette_variants.py); this module only exposes
# it over HTTP and validates the caller's map. Imported lazily so the runner's
# startup stays light and lxml/PIL are only pulled in when a recolour happens.
# recolour-seam

def _palette_variants():
    import sys as _sys
    _scripts = HERE / "scripts"
    if str(_scripts) not in _sys.path:
        _sys.path.insert(0, str(_scripts))
    import palette_variants  # noqa: F401  (imports snap_colors + validate_svg)
    return palette_variants


def recolour_map(raw: dict) -> dict:
    """Validate an explicit {source: target} recolour map.

    Every key and value must be a plain ``#rrggbb``; anything else is refused
    rather than silently dropped, so a typo cannot reach the artwork (the same
    rule palette_variants' own --map parsing enforces). Raises ValueError on a
    bad entry or an empty map.
    """
    pv = _palette_variants()
    result = {}
    for source, target in (raw or {}).items():
        src = pv.as_hex(source)
        dst = pv.as_hex(target)
        if src is None or dst is None:
            raise ValueError(
                "recolour map needs #rrggbb pairs, got %r -> %r" % (source, target))
        result[src] = dst
    if not result:
        raise ValueError("recolour map is empty")
    return result


def recolour_svg(svg_path: Path, mapping: dict) -> bytes:
    """Rewrite one SVG's paint per mapping; return the recoloured bytes.

    ``mapping`` is assumed already validated by :func:`recolour_map`. Geometry,
    viewBox, width/height and node count are untouched by construction — only
    fill/stroke/stop-color are written back through the CSS cascade.
    """
    pv = _palette_variants()
    import io
    tree, root, styles = pv.parse_svg(str(svg_path))
    pv.apply_mapping(root, styles, mapping)
    buffer = io.BytesIO()
    tree.write(buffer, encoding="utf-8", xml_declaration=True)
    return buffer.getvalue()


def svg_colours(svg_path: Path) -> list:
    """Distinct declared paint colours in one SVG, most-used first.

    Element counts only (instant); rendered-area weights need an Inkscape pass
    and belong to the variant report, not the live picker. Feeds the studio's
    colour-cycling control so it can list "replace THIS colour".
    """
    pv = _palette_variants()
    tree, root, styles = pv.parse_svg(str(svg_path))
    counts = pv.declared_colours(root, styles)
    return [colour for colour, _count
            in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


# ------------------------------------------------------------ compose seam --
#
# The COMPOSITION operation (2026-09-29). POST /compose merges several layers
# into one printable SVG WITHOUT rasterising: each vector layer stays a vector (a
# <g> with a translate+scale transform), each raster layer embeds as an <image>
# data URI, and the merged document is snapped to the composite's own palette
# through snap_colors' cascade-aware logic. The studio holds the layer stack and
# is the consumer of this seam (unlike the recolour seam above, which nothing
# calls); this module only exposes scripts/compose_svg.py over HTTP. The merge
# itself, its validation and its exit codes live in that script -- a second
# implementation here would drift from the one the CLI runs.
#
# Inline only: every layer's src is an SVG string or a data URI, so the route
# never resolves a path and a compose request cannot read the runner's disk.
# compose-seam

def _compose_module():
    import sys as _sys
    _scripts = HERE / "scripts"
    if str(_scripts) not in _sys.path:
        _sys.path.insert(0, str(_scripts))
    import compose_svg  # noqa: F401  (imports snap_colors + validate_svg)
    return compose_svg


def compose_problem(spec) -> str | None:
    """Why this spec cannot even start a compose, or None when it can.

    Only the structural emptiness is checked here: a missing canvas or no layers
    at all is a client error this layer can name precisely, which matters because
    the studio shows the reason. Everything else (an unparseable inline SVG, an
    unknown layer type, a transform the natural size cannot be derived for) is
    compose_svg.SpecError's business, so exactly one place decides what a usable
    spec is.
    """
    if not isinstance(spec, dict):
        return "compose needs a spec object"
    for key in ("width", "height", "layers"):
        value = spec.get(key)
        if value is None or value == "":
            return "spec is missing %s" % key
    if not isinstance(spec["layers"], list):
        return "spec layers must be a list"
    return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        return

    def json_response(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self) -> bool:
        return not TOKEN or self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    def serve_file(self, path: Path, download: str, attachment: bool = False):
        if not path.is_file():
            self.json_response(404, {"error": "not found"})
            return
        content_types = {".svg": "image/svg+xml", ".png": "image/png",
                         ".pdf": "application/pdf", ".json": "application/json",
                         ".7z": "application/x-7z-compressed",
                         # A dataset's notes are text: served as an unknown
                         # binary they are offered as a download or rendered as
                         # nothing. `text/markdown` lets a browser show them,
                         # which is the whole point of shipping a .md beside the
                         # JSON.
                         ".md": "text/markdown; charset=utf-8"}
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_types.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        disposition = "attachment" if attachment else "inline"
        self.send_header("Content-Disposition", f'{disposition}; filename="{download}"')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self.authorized():
            self.json_response(401, {"error": "unauthorized"})
            return
        route, _, query = self.path.partition("?")
        parts = [part for part in route.split("/") if part]
        if route == "/health":
            self.json_response(200, health_payload())
            return
        if len(parts) >= 4 and parts[0] == "files" \
                and parts[2].endswith(DATASET_SUFFIX):
            # A dataset's variant files are one level deeper than the flat
            # /files/ route can name: raster/<transform>.png and
            # vector/<transform>.svg live under <stem>.dataset/, so this route
            # carries a SECOND path segment.  Containment is re-checked on the
            # resolved path (dataset_file) because the flat route's name regex
            # cannot see across that segment, and a dataset is the one artifact
            # whose request may contain a slash.
            job = JOBS.get(parts[1])
            if not job:
                self.json_response(404, {"error": "no such job"})
                return
            stem = parts[2][: -len(DATASET_SUFFIX)]
            if stem not in dataset_stems(job):
                self.json_response(404, {"error": "no such dataset"})
                return
            relative = "/".join(parts[3:])
            try:
                path = dataset_file(Path(job["dir"]) / parts[2], relative)
            except ValueError as exc:
                self.json_response(400, {"error": str(exc)})
                return
            self.serve_file(path, Path(relative).name)
            return
        if len(parts) >= 3 and parts[0] == "files":
            job = JOBS.get(parts[1])
            if not job:
                self.json_response(404, {"error": "no such job"})
                return
            name = "/".join(parts[2:])
            if not re.fullmatch(r"[\w.\-]+", name):
                self.json_response(400, {"error": "invalid file name"})
                return
            path = artifact_path(PROJECT, stem=job.get("stem", ""), name=name,
                                 job_dir=Path(job["dir"]))
            self.serve_file(path or Path("/nonexistent"), name)
            return
        if len(parts) == 2 and parts[0] == "jobs":
            job = JOBS.get(parts[1])
            if not job:
                self.json_response(404, {"error": "no such job"})
                return
            with STATE_LOCK:
                self.json_response(200, {"id": job["id"], "state": job["state"],
                    "log_tail": job["log"][-40:], "prep_summary": job.get("prep_summary"),
                    "prep_expand": job.get("prep_expand"),
                    "candidate_count": len(job.get("candidates", [])),
                    "comparison": job.get("comparison"), "error": job.get("error")})
            return
        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "archive":
            job = JOBS.get(parts[1])
            if not job:
                self.json_response(404, {"error": "no such job"})
                return
            if job["state"] != "done":
                self.json_response(409, {"error": f"job not done ({job['state']})"})
                return
            job_dir = Path(job["dir"])
            if not job_dir.is_dir():
                self.json_response(404, {"error": "job dir missing"})
                return
            if free_bytes(job_dir) < MIN_ARCHIVE_FREE:
                self.json_response(507, {"error": "insufficient disk to archive"})
                return
            archive = build_archive(job_dir, parts[1])
            if archive is None:
                self.json_response(500, {"error": "7z unavailable or failed"})
                return
            try:
                self.serve_file(archive, f"{job.get('stem') or parts[1]}.7z", attachment=True)
            finally:
                archive.unlink(missing_ok=True)
            return
        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "dataset":
            # 7z ONE <candidate_stem>.dataset on demand and stream it; the
            # whole-job route above is unchanged and this one mirrors it
            # exactly: 409 unless the job is done, 507 below the free-disk
            # floor, 500 when 7z is unavailable, and the temporary archive is
            # unlinked in a finally so nothing accumulates beside the job.
            job = JOBS.get(parts[1])
            if not job:
                self.json_response(404, {"error": "no such job"})
                return
            if job["state"] != "done":
                self.json_response(409, {"error": f"job not done ({job['state']})"})
                return
            job_dir = Path(job["dir"])
            if not job_dir.is_dir():
                self.json_response(404, {"error": "job dir missing"})
                return
            stems = dataset_stems(job)
            wanted = (parse_qs(query).get("candidate") or [""])[0]
            if wanted:
                if not re.fullmatch(r"[\w.\-]+", wanted):
                    self.json_response(400, {"error": "invalid candidate name"})
                    return
                stem = Path(wanted).stem
            elif len(stems) == 1:
                # The common case: one validation, one dataset, no need for the
                # caller to name it.  Ambiguity is a 400, not a guess.
                stem = stems[0]
            elif not stems:
                self.json_response(404, {"error": "no dataset for this job"})
                return
            else:
                self.json_response(400, {"error": "candidate is required: this "
                                        "job has %d datasets" % len(stems)})
                return
            if stem not in stems:
                self.json_response(404, {"error": f"no dataset for {stem}"})
                return
            if free_bytes(job_dir) < MIN_ARCHIVE_FREE:
                self.json_response(507, {"error": "insufficient disk to archive"})
                return
            archive = build_dataset_archive(
                job_dir / f"{stem}{DATASET_SUFFIX}",
                job_dir.parent / f"{stem}{DATASET_SUFFIX}.7z")
            if archive is None:
                self.json_response(500, {"error": "7z unavailable or failed"})
                return
            try:
                self.serve_file(archive, f"{stem}{DATASET_SUFFIX}.7z",
                                attachment=True)
            finally:
                archive.unlink(missing_ok=True)
            return
        if len(parts) == 2 and parts[0] == "validations":
            validation = VALIDATIONS.get(parts[1])
            if not validation:
                self.json_response(404, {"error": "no such validation"})
                return
            with STATE_LOCK:
                self.json_response(200, {"id": validation["id"], "state": validation["state"],
                    "manifest": validation.get("manifest"), "log": validation.get("log", []),
                    "error": validation.get("error")})
            return
        self.json_response(404, {"error": "unknown route"})

    def do_POST(self):
        if not self.authorized():
            self.json_response(401, {"error": "unauthorized"})
            return
        parts = [part for part in self.path.split("/") if part]
        body = self.body()
        if self.path == "/jobs":
            try:
                input_path = container_input_path(str(body.get("input_path", "")))
                if not Path(input_path).is_file():
                    raise ValueError(f"input is not readable: {input_path}")
            except ValueError as exc:
                self.json_response(400, {"error": str(exc)})
                return
            job_id = uuid.uuid4().hex[:12]
            name = str(body.get("name") or "artwork")
            job_dir = JOBS_ROOT / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            job = {"id": job_id, "name": name, "stem": slug(name), "dir": str(job_dir),
                   "container_input_path": input_path, "spec": body.get("spec") or {},
                   "state": "queued", "log": [], "candidates": [], "comparison": None,
                   "prep_summary": None, "error": None, "cancel": False, "proc": None}
            with STATE_LOCK:
                JOBS[job_id] = job
            threading.Thread(target=guarded, args=(run_front, job), daemon=True).start()
            self.json_response(202, {"id": job_id, "stem": job["stem"]})
            return
        if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel":
            job = JOBS.get(parts[1])
            if not job:
                self.json_response(404, {"error": "no such job"})
                return
            with STATE_LOCK:
                if job["state"] in ("queued", "running"):
                    job["cancel"] = True
                    proc = job.get("proc")
                    if proc is not None and proc.poll() is None:
                        proc.terminate()
                    self.json_response(200, {"state": "cancelling"})
                elif job["state"] == "cancelled":
                    self.json_response(200, {"state": "cancelled"})
                else:
                    self.json_response(409, {"state": job["state"]})
            return
        if self.path == "/validations":
            job = JOBS.get(body.get("job_id"))
            candidate = str(body.get("candidate_file") or "")
            if not job or job["state"] != "done":
                self.json_response(409, {"error": "job not ready"})
                return
            if not any(item.get("file") == candidate for item in job.get("candidates", [])):
                self.json_response(404, {"error": "no such candidate"})
                return
            validation_id = uuid.uuid4().hex[:12]
            validation = {"id": validation_id, "job_id": job["id"], "candidate_file": candidate,
                          "state": "queued", "manifest": None, "log": [], "error": None}
            with STATE_LOCK:
                VALIDATIONS[validation_id] = validation
            threading.Thread(target=guarded, args=(run_back, validation), daemon=True).start()
            self.json_response(202, {"id": validation_id})
            return
        if len(parts) == 3 and parts[0] == "validations" and parts[2] == "jev":
            # Ask the optional Jev add-on the four advisory questions about this
            # validation's manifest. Blocking (a live call takes ~40s) but this
            # server is threaded, so other requests are unaffected.
            validation = VALIDATIONS.get(parts[1])
            job = None
            candidate_file = None
            if validation:
                job = JOBS.get(validation["job_id"])
                candidate_file = validation["candidate_file"]
                if validation["state"] != "done":
                    self.json_response(409, {"error": f"validation not ready ({validation['state']})"})
                    return
            else:
                # Validations are in memory only, so a runner restart forgets
                # every one of them even though the artifacts are still on the
                # bind mount. The studio already knows which job and candidate
                # it is asking about, so accept that and resolve the manifest
                # from disk instead of 404ing on a route that works fine.
                job = JOBS.get(str(body.get("job_id") or ""))
                candidate_file = str(body.get("candidate_file") or "") or None
                if not job or not candidate_file:
                    self.json_response(404, {"error": "no such validation"})
                    return
            candidate_stem = Path(candidate_file).stem
            manifest_path = Path(job["dir"]) / f"{candidate_stem}.manifest.json" if job else None
            if not manifest_path or not manifest_path.is_file():
                self.json_response(404, {"error": f"manifest not found for {candidate_stem}"})
                return
            try:
                self.json_response(200, run_jev(manifest_path))
            except JevUnavailable as exc:
                # 503 with the reason: the add-on is not configured here. A 404
                # would read as "this runner has no such route", which is what
                # sent the operator hunting for a wiring bug that was not there.
                self.json_response(503, {"error": str(exc)})
            return
        if len(parts) == 1 and parts[0] == "recolour":
            # Recolour one candidate SVG by an explicit {source: target} map and
            # return the bytes inline, so a caller (unbuilt seam: no studio
            # consumer today) can render the vector directly — the preview never
            # rasterises. Geometry is untouched.
            job = JOBS.get(str(body.get("job_id") or ""))
            name = str(body.get("file") or "")
            if not job or not re.fullmatch(r"[\w.\-]+", name):
                self.json_response(400, {"error": "job_id and file are required"})
                return
            path = artifact_path(PROJECT, stem=job.get("stem", ""), name=name,
                                 job_dir=Path(job["dir"]))
            if not path or not path.is_file():
                self.json_response(404, {"error": "no such file"})
                return
            try:
                mapping = recolour_map(body.get("map") or {})
                svg = recolour_svg(path, mapping)
            except ValueError as exc:
                self.json_response(400, {"error": str(exc)})
                return
            except Exception as exc:
                self.json_response(500, {"error": "recolour failed: %s: %s"
                                        % (type(exc).__name__, exc)})
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(svg)))
            self.end_headers()
            self.wfile.write(svg)
            return
        if len(parts) == 2 and parts[0] == "recolour" and parts[1] == "colours":
            # List the distinct declared colours of one SVG, most-used first,
            # so a colour-cycling picker knows what it can replace (unbuilt
            # seam: no studio consumer calls this today).
            job = JOBS.get(str(body.get("job_id") or ""))
            name = str(body.get("file") or "")
            if not job or not re.fullmatch(r"[\w.\-]+", name):
                self.json_response(400, {"error": "job_id and file are required"})
                return
            path = artifact_path(PROJECT, stem=job.get("stem", ""), name=name,
                                 job_dir=Path(job["dir"]))
            if not path or not path.is_file():
                self.json_response(404, {"error": "no such file"})
                return
            try:
                self.json_response(200, {"file": name, "colours": svg_colours(path)})
            except Exception as exc:
                self.json_response(500, {"error": "recolour failed: %s: %s"
                                        % (type(exc).__name__, exc)})
            return
        if len(parts) == 1 and parts[0] == "compose":
            # Composition: the studio sends the whole layer stack inline (SVG
            # strings and raster data-URIs) as {op, spec} and gets ONE merged SVG
            # back, vectors intact -- no raster round-trip, so what the trace
            # stage produced is still what gets printed. Layers are never read
            # from disk: src is inline by contract.
            spec = body.get("spec") if isinstance(body, dict) else None
            problem = compose_problem(spec)
            if problem:
                self.json_response(422, {"error": problem,
                                         "kind": "invalid_spec"})
                return
            compose_svg = _compose_module()
            findings: dict = {}
            try:
                svg = compose_svg.compose(spec, report=findings).encode("utf-8")
            except compose_svg.SpecError as exc:
                # A spec the merge refuses to guess at. 422 with the kind the
                # studio switches on, never a silent partial merge.
                self.json_response(422, {"error": str(exc),
                                         "kind": "invalid_spec"})
                return
            except Exception as exc:
                self.json_response(500, {"error": "compose failed: %s: %s"
                                        % (type(exc).__name__, exc)})
                return
            if findings.get("off_palette") or findings.get("ignored"):
                # Findings, not failures: a colour off the composite's own
                # palette, or a hue the merge could not apply, is logged so it
                # is visible in the runner's log rather than disappearing into
                # the returned file.
                print("compose: %s" % json.dumps(
                    {"off_palette": findings.get("off_palette"),
                     "ignored": findings.get("ignored")}), flush=True)
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(svg)))
            self.end_headers()
            self.wfile.write(svg)
            return
        self.json_response(404, {"error": "unknown route"})

    def do_DELETE(self):
        if not self.authorized():
            self.json_response(401, {"error": "unauthorized"})
            return
        parts = [part for part in self.path.split("/") if part]
        if len(parts) == 2 and parts[0] == "jobs":
            job = JOBS.get(parts[1])
            if not job:
                self.json_response(404, {"error": "no such job"})
                return
            job_dir = Path(job["dir"])
            with STATE_LOCK:
                if job["state"] in ("queued", "running"):
                    job["cancel"] = True
                    proc = job.get("proc")
                    if proc is not None and proc.poll() is None:
                        proc.terminate()
                JOBS.pop(parts[1], None)
            if job_dir.is_dir():
                shutil.rmtree(job_dir, ignore_errors=True)
            self.json_response(200, {"deleted": parts[1]})
            return
        self.json_response(404, {"error": "unknown route"})


def reindex_jobs() -> None:
    """Rebuild the in-memory JOBS index from disk so completed jobs' files
    survive a container restart. State is otherwise memory-only; without this,
    every ``/files/<job_id>/...`` request 404s after the runner restarts even
    though the artifacts are still on the bind mount."""
    for job_dir in sorted(p for p in JOBS_ROOT.iterdir() if p.is_dir()):
        job_id = job_dir.name
        comparison = None
        candidates: list = []
        cmp_path = job_dir / "comparison.json"
        if cmp_path.is_file():
            try:
                comparison = json.loads(cmp_path.read_text(encoding="utf-8"))
                candidates = list(comparison.get("candidates", []))
            except (OSError, json.JSONDecodeError):
                comparison = None
        spec: dict = {}
        spec_path = job_dir / "spec.json"
        if spec_path.is_file():
            try:
                loaded = json.loads(spec_path.read_text(encoding="utf-8"))
                spec = loaded if isinstance(loaded, dict) else {}
            except (OSError, json.JSONDecodeError):
                spec = {}
        stem = ""
        input_dir = job_dir / "input"
        if input_dir.is_dir():
            inputs = sorted(p for p in input_dir.iterdir() if p.is_file())
            if inputs:
                stem = inputs[0].stem
        JOBS[job_id] = {
            "id": job_id, "name": job_id, "stem": stem, "dir": str(job_dir),
            "spec": spec, "state": "done", "log": [], "candidates": candidates,
            "comparison": comparison, "prep_summary": None, "error": None,
            "cancel": False, "proc": None,
        }


def main() -> None:
    JOBS_ROOT.mkdir(parents=True, exist_ok=True)
    reindex_jobs()
    server = ThreadingHTTPServer((HOST_BIND, PORT), Handler)
    print(f"real runner on {HOST_BIND}:{PORT}, root={JOBS_ROOT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
