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
import subprocess
import threading
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT = Path(os.environ.get("PIPELINE_ROOT", "/app"))
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


def slug(name: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return value or "artwork"


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
    traced = root / "02_traced" / stem / name
    if traced.is_file():
        return traced
    final = root / "05_final" / name
    return final if final.is_file() else None


def _append_log(job: dict, line: str) -> None:
    with STATE_LOCK:
        job["log"].append(line.rstrip())


def _run_logged(job: dict, command: list[str], env: dict[str, str]) -> int:
    process = subprocess.Popen(
        command, cwd=PROJECT, env={**os.environ, **env}, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        _append_log(job, line)
    return process.wait()


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


def run_front(job: dict) -> None:
    with PIPELINE_LOCK:
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
        code = _run_logged(job, [str(PROJECT / "front_pipeline.sh"), str(input_path)],
                           {"SPEC": str(spec_path), "FRONT_PIPELINE_WORKERS": "1"})
        prep = PROJECT / "01_prepped" / f"{stem}.prep.json"
        prep_summary = json.loads(prep.read_text(encoding="utf-8")) if prep.is_file() else None
        candidates = _copy_front_outputs(job, stem) if code in (0, 1) else []
        if not candidates:
            raise RuntimeError(f"front pipeline exited {code} without candidates")
        with STATE_LOCK:
            job.update(stem=stem, candidates=candidates, comparison=json.loads(
                (Path(job["dir"]) / "comparison.json").read_text(encoding="utf-8")),
                prep_summary=prep_summary, state="done")


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
        with STATE_LOCK:
            validation.update(manifest=manifest, state="done")


def guarded(fn, item: dict) -> None:
    try:
        fn(item)
    except Exception as exc:
        with STATE_LOCK:
            item.update(state="error", error=f"{type(exc).__name__}: {exc}")
        traceback.print_exc()


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

    def serve_file(self, path: Path, download: str):
        if not path.is_file():
            self.json_response(404, {"error": "not found"})
            return
        content_types = {".svg": "image/svg+xml", ".png": "image/png",
                         ".pdf": "application/pdf", ".json": "application/json"}
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_types.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'inline; filename="{download}"')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self.authorized():
            self.json_response(401, {"error": "unauthorized"})
            return
        parts = [part for part in self.path.split("/") if part]
        if self.path == "/health":
            self.json_response(200, {"ok": True, "host": "geenet-docker", "mode": "real",
                                     "tools": {"inkscape": shutil.which("inkscape") is not None,
                                               "gs": shutil.which("gs") is not None}})
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
                    "candidate_count": len(job.get("candidates", [])),
                    "comparison": job.get("comparison"), "error": job.get("error")})
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
                   "prep_summary": None, "error": None}
            with STATE_LOCK:
                JOBS[job_id] = job
            threading.Thread(target=guarded, args=(run_front, job), daemon=True).start()
            self.json_response(202, {"id": job_id, "stem": job["stem"]})
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
        self.json_response(404, {"error": "unknown route"})


def main() -> None:
    JOBS_ROOT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST_BIND, PORT), Handler)
    print(f"real runner on {HOST_BIND}:{PORT}, root={JOBS_ROOT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
