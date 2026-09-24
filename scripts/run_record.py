#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_record.py -- stitch a front-half run into one provenance record.

The front half already emits rich per-stage provenance (prep.json with the
source hash, sweep.json with per-candidate VTracer params + SVG hash +
source_sha256, comparison.json with the embedded sweep entry + Layer A/B +
fidelity).  What it does not do is join those into one reconstructable record
per candidate, link the back-half artifacts for the candidates the human chose
to run through the full pipeline, and flag every missing link.

This script is the archive spine: it walks one run (keyed by its traced-dir
stem) and writes ``<out-dir>/<stem>.run.json`` stitching, per candidate:

    source -> prep -> sweep entry -> SVG hash -> Layer A/B -> proof/PDF
           -> Jev sidecar -> human decision

It never picks a winner and never runs the pipeline.  It only reads.

Expected human-decision conventions (both optional; flagged as missing links
when absent):
  - per-candidate ``05_final/<id>.pick.json``:
        {"selected": true|false, "label": "production|style_reference|needs_retrace|discard", "reason": "..."}
  - or a run-level ``04_validated/<stem>.decision.json`` mapping id -> the above.
Expected visual-review convention (optional): ``05_final/<id>.visual_review.md``.

Usage:
    .venv/bin/python scripts/run_record.py --all                     # every run
    .venv/bin/python scripts/run_record.py --stem 00-example         # one run
    .venv/bin/python scripts/run_record.py --traced-dir 02_traced/00-example
    .venv/bin/python scripts/run_record.py --source 00_source/00-example.png
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import front_common as fc  # noqa: E402

SCHEMA = "chopshop-run-record-0.1"

PROJECT = Path(SCRIPT_DIR).parent
SOURCE_DIR = PROJECT / "00_source"
PREPPED_DIR = PROJECT / "01_prepped"
TRACED_DIR = PROJECT / "02_traced"
VALIDATED_DIR = PROJECT / "04_validated"
FINAL_DIR = PROJECT / "05_final"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def discover_stems() -> list[str]:
    """Run keys = traced-dir names under 02_traced/."""
    if not TRACED_DIR.is_dir():
        return []
    return sorted(p.name for p in TRACED_DIR.iterdir() if p.is_dir())


def resolve_stem(args) -> str | None:
    """Resolve a single run stem from --stem/--traced-dir/--source, or None."""
    if args.stem:
        return args.stem
    if args.traced_dir:
        return Path(args.traced_dir).name
    if args.source:
        src = Path(args.source)
        if src.name.endswith(".prepped.png"):
            return src.name[: -len(".prepped.png")]
        return src.stem
    return None


# --------------------------------------------------------------------------
# Per-stage readers (each returns a dict and never raises on a missing file).
# --------------------------------------------------------------------------

def _read_json(path: Path):
    try:
        return fc.load_json(path)
    except (OSError, ValueError):
        return None


def _stamp(path: Path, found: bool):
    return {"path": str(path), "found": found}


def load_prep(stem: str) -> dict:
    path = PREPPED_DIR / f"{stem}.prep.json"
    data = _read_json(path)
    if data is None:
        return _stamp(path, False)
    record = _stamp(path, True)
    record["acceptable"] = data.get("acceptable")
    record["checks"] = data.get("checks")
    record["versions"] = data.get("versions")
    record["input"] = data.get("input")
    return record


def load_source(stem: str, prep: dict) -> dict:
    """Source provenance, preferring prep.json's own input record."""
    prep_input = prep.get("input")
    if isinstance(prep_input, dict) and prep_input.get("file"):
        return {
            "path": prep_input.get("file"),
            "sha256": prep_input.get("sha256"),
            "size_px": prep_input.get("size_px"),
            "found": True,
        }
    # Fall back to globbing 00_source/ for a matching stem.
    for ext in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp"):
        candidate = SOURCE_DIR / f"{stem}{ext}"
        if candidate.is_file():
            return {"path": str(candidate), "sha256": fc.sha256_file(candidate),
                    "size_px": None, "found": True}
    return {"path": None, "sha256": None, "size_px": None, "found": False}


def load_sweep(stem: str) -> dict:
    path = TRACED_DIR / stem / "sweep.json"
    data = _read_json(path)
    if data is None:
        return _stamp(path, False)
    backend = data.get("backend") or {}
    record = _stamp(path, True)
    record["backend"] = backend
    record["deterministic"] = data.get("deterministic")
    record["truncated"] = data.get("truncated")
    record["sweep"] = data.get("sweep")
    record["candidates"] = data.get("candidates") or []
    return record


def load_comparison(stem: str) -> dict:
    path = VALIDATED_DIR / f"{stem}.comparison.json"
    data = _read_json(path)
    if data is None:
        return _stamp(path, False)
    record = _stamp(path, True)
    record["candidate_count"] = data.get("candidate_count")
    record["layers_available"] = data.get("layers_available")
    record["candidates"] = data.get("candidates") or []
    return record


def candidate_back_half(cand_id: str) -> dict:
    manifest_path = FINAL_DIR / f"{cand_id}.manifest.json"
    proof_path = FINAL_DIR / f"{cand_id}.proof.png"
    pdf_path = FINAL_DIR / f"{cand_id}.print.pdf"
    layer_a_path = VALIDATED_DIR / f"{cand_id}.layer_a.txt"
    layer_b_path = FINAL_DIR / f"{cand_id}.layer_b.txt"

    record: dict = {"run": manifest_path.is_file()}

    manifest = _read_json(manifest_path)
    manifest_record = _stamp(manifest_path, manifest is not None)
    if manifest is not None:
        manifest_record["sha256"] = fc.sha256_file(manifest_path)
        manifest_record["passed"] = manifest.get("passed")
        summary = manifest.get("summary") or {}
        manifest_record["hard"] = summary.get("hard")
        manifest_record["advisory"] = summary.get("advisory")
        stats = manifest.get("stats") or {}
        static = stats.get("static") or {}
        manifest_record["stats"] = {
            "tac_exact": stats.get("tac_exact"),
            "rendered_continuous_tone": stats.get("rendered_continuous_tone"),
            "rendered_ink_colors": stats.get("rendered_ink_colors"),
            "rendered_distinct_colors": stats.get("rendered_distinct_colors"),
            "hard_findings": stats.get("hard_findings"),
            "advisory_findings": stats.get("advisory_findings"),
            "node_count_max": static.get("path_nodes_max"),
            "color_count": static.get("color_count"),
        }

    record["manifest"] = manifest_record
    record["proof_png"] = {"path": str(proof_path), "found": proof_path.is_file(),
                           "sha256": fc.sha256_file(proof_path) if proof_path.is_file() else None}
    record["print_pdf"] = {"path": str(pdf_path), "found": pdf_path.is_file(),
                           "sha256": fc.sha256_file(pdf_path) if pdf_path.is_file() else None}
    record["layer_a_txt"] = {"path": str(layer_a_path), "found": layer_a_path.is_file()}
    record["layer_b_txt"] = {"path": str(layer_b_path), "found": layer_b_path.is_file()}
    return record


def candidate_jev(cand_id: str) -> dict:
    path = FINAL_DIR / f"{cand_id}.jev.json"
    data = _read_json(path)
    record = _stamp(path, data is not None)
    if data is not None:
        record["status"] = data.get("status")
        record["schema"] = data.get("schema")
    return record


def candidate_decision(cand_id: str, stem: str) -> dict:
    per_candidate = FINAL_DIR / f"{cand_id}.pick.json"
    run_level = VALIDATED_DIR / f"{stem}.decision.json"

    data = _read_json(per_candidate)
    if data is None:
        run_data = _read_json(run_level)
        if isinstance(run_data, dict):
            data = run_data.get(cand_id)
            if isinstance(data, dict):
                data = dict(data)
    if data is None:
        return {"found": False, "path": None}
    return {"found": True, "path": str(per_candidate if per_candidate.exists()
                                       else run_level),
            "selected": data.get("selected"),
            "label": data.get("label"),
            "reason": data.get("reason")}


def candidate_visual_review(cand_id: str) -> dict:
    path = FINAL_DIR / f"{cand_id}.visual_review.md"
    return {"path": str(path), "found": path.is_file()}


# --------------------------------------------------------------------------
# Stitching.
# --------------------------------------------------------------------------

def _candidate_id(file_name: str) -> str:
    return file_name[: -len(".svg")] if file_name.endswith(".svg") else file_name


def _build_candidates(stem, sweep, comparison) -> list[dict]:
    """Merge sweep + comparison entries per candidate, keyed by file name."""
    sweep_by_file = {c.get("file"): c for c in sweep.get("candidates", [])
                     if isinstance(c, dict) and c.get("file")}
    comp_by_file = {c.get("file"): c for c in comparison.get("candidates", [])
                    if isinstance(c, dict) and c.get("file")}

    files: list[str] = []
    for f in list(comp_by_file) + list(sweep_by_file):
        if f not in files:
            files.append(f)
    if not files:
        # No comparison/sweep records: glob the traced dir directly.
        traced = TRACED_DIR / stem
        files = sorted(p.name for p in traced.glob("candidate_*.svg")
                       if p.is_file())

    candidates = []
    for file_name in files:
        cand_id = _candidate_id(file_name)
        swe = sweep_by_file.get(file_name) or {}
        com = comp_by_file.get(file_name) or {}

        params = swe.get("params") or {}
        svg_path = TRACED_DIR / stem / file_name
        svg_sha = (swe.get("output_sha256")
                   or (fc.sha256_file(svg_path) if svg_path.is_file() else None))

        candidate = {
            "id": cand_id,
            "file": file_name,
            "svg_sha256": svg_sha,
            "file_size": com.get("file_size")
                         or (svg_path.stat().st_size if svg_path.is_file() else None),
            "sweep": {
                "preset": swe.get("preset"),
                "filter_speckle": swe.get("filter_speckle"),
                "hierarchical": swe.get("hierarchical"),
                "use_palette": swe.get("use_palette"),
                "params": params,
                "source_sha256": swe.get("source_sha256"),
            },
        }
        if com:
            candidate["comparison"] = {
                "passed": com.get("passed"),
                "hard": com.get("hard"),
                "advisory": com.get("advisory"),
                "node_count_max": com.get("node_count_max"),
                "node_count_total": com.get("node_count_total"),
                "rendered_ink_colors": com.get("rendered_ink_colors"),
                "declared_colors": com.get("declared_colors"),
                "layer_a": com.get("layer_a"),
                "layer_b": com.get("layer_b"),
                "fidelity": com.get("fidelity"),
            }
        else:
            candidate["comparison"] = None

        candidate["back_half"] = candidate_back_half(cand_id)
        candidate["jev"] = candidate_jev(cand_id)
        candidate["visual_review"] = candidate_visual_review(cand_id)
        candidate["human_decision"] = candidate_decision(cand_id, stem)
        candidates.append(candidate)

    candidates.sort(key=lambda c: c["id"])
    return candidates


def _duplicate_groups(candidates: list[dict]) -> dict:
    """Group candidates by SVG sha256; exact-duplicate groups only."""
    groups: dict[str, list[str]] = {}
    for c in candidates:
        sha = c.get("svg_sha256")
        if sha:
            groups.setdefault(sha, []).append(c["id"])
    return {sha: ids for sha, ids in groups.items() if len(ids) > 1}


def _candidate_links(candidate: dict, *, duplicate_of: str | None):
    """Split a candidate's links into blocking anomalies vs open human steps.

    Blocking anomalies mean a stage genuinely failed or a traced candidate was
    never compared.  Open items are the *normal* state of a candidate the human
    has not chosen / reviewed yet (or an optional step like Jev) -- they are
    surfaced as counts, not as closeout failures.
    """
    missing: list[str] = []
    open_items: list[str] = []
    if not candidate.get("svg_sha256"):
        missing.append("svg hash unknown")
    if candidate.get("comparison") is None:
        missing.append("not compared (Layer A/B + fidelity)")

    back = candidate["back_half"]
    if back["run"]:
        if not back["proof_png"]["found"]:
            missing.append("back half ran but proof PNG missing")
        if not back["print_pdf"]["found"]:
            missing.append("back half ran but print PDF missing")
        if not back["layer_a_txt"]["found"]:
            missing.append("back half ran but Layer A log missing")
    else:
        open_items.append("back half not run (candidate not chosen)")

    if not candidate["jev"]["found"]:
        open_items.append("no Jev sidecar (optional)")
    if not candidate["visual_review"]["found"]:
        open_items.append("no visual review")
    if not candidate["human_decision"]["found"]:
        open_items.append("no human decision")
    if duplicate_of:
        open_items.append(f"sha duplicate of {duplicate_of}")
    return missing, open_items


def build_run(stem: str) -> dict:
    prep = load_prep(stem)
    source = load_source(stem, prep)
    sweep = load_sweep(stem)
    comparison = load_comparison(stem)

    candidates = _build_candidates(stem, sweep, comparison)
    groups = _duplicate_groups(candidates)
    sha_to_first = {c["id"]: c["id"] for c in candidates}
    for sha, ids in groups.items():
        first = ids[0]
        for cid in ids[1:]:
            sha_to_first[cid] = first

    for candidate in candidates:
        duplicate_of = (sha_to_first[candidate["id"]]
                        if sha_to_first[candidate["id"]] != candidate["id"]
                        else None)
        candidate["duplicate_of"] = duplicate_of
        missing, open_items = _candidate_links(candidate, duplicate_of=duplicate_of)
        candidate["missing_links"] = missing
        candidate["open_items"] = open_items

    run_missing = []
    if not source["found"]:
        run_missing.append("source raster not found")
    if not prep["found"]:
        run_missing.append("prep.json missing")
    if not sweep["found"]:
        run_missing.append("sweep.json missing")
    if not comparison["found"]:
        run_missing.append("comparison.json missing")

    return {
        "schema": SCHEMA,
        "generated": _now(),
        "tool": "run_record.py",
        "stem": stem,
        "run": {
            "source": source,
            "prep": prep,
            "sweep": sweep,
            "comparison": comparison,
            "duplicate_groups": groups,
            "missing_links": run_missing,
        },
        "candidates": candidates,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stem", help="run stem (traced-dir name)")
    parser.add_argument("--traced-dir", help="path to a 02_traced/<stem> dir")
    parser.add_argument("--source", help="source raster path; derive the stem")
    parser.add_argument("--all", action="store_true",
                        help="record every discovered run")
    parser.add_argument("--out-dir", default=str(PROJECT / "06_run"),
                        help="output dir (default: 06_run/)")
    args = parser.parse_args(argv)

    if args.all:
        stems = discover_stems()
    else:
        stem = resolve_stem(args)
        stems = [stem] if stem else []

    if not stems:
        print("run_record: no runs found (pass --stem, --source, --traced-dir, or --all)",
              file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for stem in stems:
        run = build_run(stem)
        out_path = out_dir / f"{stem}.run.json"
        fc.write_json(out_path, run)
        print(f"run_record: {out_path} "
              f"({len(run['candidates'])} candidates, "
              f"{len(run['run']['missing_links'])} run-level missing links)")

    if args.all:
        index = {"schema": SCHEMA, "generated": _now(), "tool": "run_record.py",
                 "runs": stems}
        fc.write_json(out_dir / "runs.index.json", index)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
