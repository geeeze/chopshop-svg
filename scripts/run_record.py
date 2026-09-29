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
           -> human decision

It never picks a winner and never runs the pipeline.  It only reads.

Where the back-half artefacts live (pipeline.sh publishes all of these names,
additively -- the flat one is what runner.py reads, the others are what make the
artefact attributable):

    <candidate>.x                         canonical flat name (runner.py reads it)
    <artwork-stem>/<candidate>.x          artwork-scoped -- preferred here
    <artwork-stem>.<candidate>.<RUN_ID>.x run-unique copy of one back-half run

The flat name is NOT attributable: it is one file, shared by every artwork that
ever traced a candidate with that same file stem (``candidate_04``), so a run
record reading it could be reading a different artwork's manifest, proof and
human pick.  This script therefore prefers the artwork-scoped name, falls back to
the flat one only when the scoped one is absent, and when it does, sets
``"path_scope": "flat-shared"`` on that artefact and records an anomaly note, so a
shared file is never read as if it were this run's.

Expected human-decision conventions (both optional; flagged as missing links
when absent).  The artwork-scoped name is preferred, same reason as above:
  - per-candidate ``05_final/<artwork-stem>/<id>.pick.json`` (preferred), with a
    legacy fallback to the shared flat ``05_final/<id>.pick.json`` -- the fallback
    is read, but marked ``"path_scope": "flat-shared"`` and listed under the
    candidate's ``anomalies``:
        {"selected": true|false, "label": "production|style_reference|needs_retrace|discard", "reason": "..."}
  - or a run-level ``04_validated/<stem>.decision.json`` mapping id -> the above.
Expected visual-review convention (optional), same preference order:
``05_final/<artwork-stem>/<id>.visual_review.md`` then ``05_final/<id>.visual_review.md``.

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


# --------------------------------------------------------------------------
# Artefact resolution: prefer the artwork-scoped name, mark every fallback.
# --------------------------------------------------------------------------

#: ``path_scope`` values, in preference order.  "artwork" == the name carries the
#: run's artwork stem and is therefore this run's own artefact; "flat-shared" ==
#: the name is shared by every artwork with that candidate stem, so it is read
#: only for continuity with pre-fix runs and must be flagged; "absent" == no file
#: at either name.
PATH_SCOPE_ARTWORK = "artwork"
PATH_SCOPE_FLAT = "flat-shared"
PATH_SCOPE_ABSENT = "absent"


def _choose_artefact(scoped: Path, flat: Path) -> tuple[Path, str]:
    """Resolve one artefact to (path, path_scope).

    The artwork-scoped name wins; the flat name is a fallback that is reported as
    "flat-shared" so the caller can record that it is not attributable to this
    run.  When neither exists the scoped path is returned, so a missing-file
    message points at the name the pipeline should have written.
    """
    if scoped.is_file():
        return scoped, PATH_SCOPE_ARTWORK
    if flat.is_file():
        return flat, PATH_SCOPE_FLAT
    return scoped, PATH_SCOPE_ABSENT


def _artefact_record(path: Path, scope: str, *, hashed: bool = False) -> dict:
    """``path``/``found`` (plus ``sha256`` for binary artefacts) + provenance scope.

    The extra ``path_scope`` is the additive part of the record shape; everything
    else matches what this record carried before the artwork-scoping fix.
    """
    record = _stamp(path, path.is_file())
    record["path_scope"] = scope
    if hashed:
        record["sha256"] = fc.sha256_file(path) if path.is_file() else None
    return record


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


def candidate_back_half(cand_id: str, artwork_stem: str) -> dict:
    """The back-half artefacts for one candidate of one artwork.

    ``artwork_stem`` is the RUN's stem (the traced-dir name).  The artwork-scoped
    name under 05_final/<artwork_stem>/ is the run's own artefact; the flat
    05_final/<cand_id>.* name is shared by every artwork with that candidate file
    stem, so it is only a fallback and is always marked as such.
    """
    scoped_dir = FINAL_DIR / artwork_stem
    flat_dir = FINAL_DIR
    manifest_path, manifest_scope = _choose_artefact(
        scoped_dir / f"{cand_id}.manifest.json", flat_dir / f"{cand_id}.manifest.json")
    proof_path, proof_scope = _choose_artefact(
        scoped_dir / f"{cand_id}.proof.png", flat_dir / f"{cand_id}.proof.png")
    pdf_path, pdf_scope = _choose_artefact(
        scoped_dir / f"{cand_id}.print.pdf", flat_dir / f"{cand_id}.print.pdf")
    layer_b_path, layer_b_scope = _choose_artefact(
        scoped_dir / f"{cand_id}.layer_b.txt", flat_dir / f"{cand_id}.layer_b.txt")
    # Layer A's artwork-scoped name is a sibling in 04_validated/, not a subdir:
    # pipeline.sh writes <artwork-stem>.<candidate-stem>.layer_a.txt there so the
    # name stays artwork-stem-prefixed (orphan_sweep's classification rule).
    layer_a_path, layer_a_scope = _choose_artefact(
        VALIDATED_DIR / f"{artwork_stem}.{cand_id}.layer_a.txt",
        VALIDATED_DIR / f"{cand_id}.layer_a.txt")

    record: dict = {"run": manifest_path.is_file(),
                    "cand_id": cand_id, "artwork_stem": artwork_stem}

    manifest = _read_json(manifest_path)
    manifest_record = _stamp(manifest_path, manifest is not None)
    manifest_record["path_scope"] = manifest_scope
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
    record["proof_png"] = _artefact_record(proof_path, proof_scope, hashed=True)
    record["print_pdf"] = _artefact_record(pdf_path, pdf_scope, hashed=True)
    record["layer_a_txt"] = _artefact_record(layer_a_path, layer_a_scope)
    record["layer_b_txt"] = _artefact_record(layer_b_path, layer_b_scope)
    return record


def candidate_decision(cand_id: str, artwork_stem: str) -> dict:
    """The human's pick for one candidate: scoped, then flat, then run-level."""
    scoped = FINAL_DIR / artwork_stem / f"{cand_id}.pick.json"
    flat = FINAL_DIR / f"{cand_id}.pick.json"
    run_level = VALIDATED_DIR / f"{artwork_stem}.decision.json"

    data = _read_json(scoped) if scoped.is_file() else None
    scope = PATH_SCOPE_ARTWORK if data is not None else None
    path = scoped
    if data is None:
        data = _read_json(flat)
        if data is not None:
            scope, path = PATH_SCOPE_FLAT, flat
    if data is None:
        # The run-level decision file is already artwork-keyed, so it is equally
        # attributable -- it just holds every candidate's pick in one document.
        run_data = _read_json(run_level)
        if isinstance(run_data, dict):
            data = run_data.get(cand_id)
            if isinstance(data, dict):
                data = dict(data)
                scope, path = PATH_SCOPE_ARTWORK, run_level
    if data is None:
        return {"found": False, "path": None, "path_scope": PATH_SCOPE_ABSENT}
    return {"found": True, "path": str(path), "path_scope": scope,
            "selected": data.get("selected"),
            "label": data.get("label"),
            "reason": data.get("reason")}


def candidate_visual_review(cand_id: str, artwork_stem: str) -> dict:
    """The visual review for one candidate of one artwork (scoped first)."""
    path, scope = _choose_artefact(
        FINAL_DIR / artwork_stem / f"{cand_id}.visual_review.md",
        FINAL_DIR / f"{cand_id}.visual_review.md")
    record = {"path": str(path), "found": path.is_file()}
    record["path_scope"] = scope
    return record


# --------------------------------------------------------------------------
# Stitching.
# --------------------------------------------------------------------------

def _candidate_id(file_name: str) -> str:
    return file_name[: -len(".svg")] if file_name.endswith(".svg") else file_name


def _scope_notes(back: dict, review: dict, decision: dict) -> list[str]:
    """One note per artefact that had to fall back to a shared flat name.

    A flat ``05_final/<candidate>.*`` (or ``04_validated/<candidate>.layer_a.txt``)
    file is written by EVERY artwork whose trace produced that candidate file
    stem, so reading it proves nothing about this run.  The note is what keeps the
    fallback visible instead of silent.  It is an anomaly, not a blocking missing
    link: closeout's gate counts ``missing_links``, and a legacy flat artefact is
    readable data, not a failed stage.
    """
    notes: list[str] = []
    for key in ("manifest", "proof_png", "print_pdf", "layer_a_txt", "layer_b_txt"):
        entry = back.get(key) or {}
        if entry.get("path_scope") == PATH_SCOPE_FLAT:
            notes.append(f"back-half {key} read from the shared flat name "
                         f"'{entry.get('path')}' -- not attributable to this run")
    for label, entry in (("visual review", review), ("human decision", decision)):
        if (entry or {}).get("path_scope") == PATH_SCOPE_FLAT:
            notes.append(f"{label} read from the shared flat name "
                         f"'{(entry or {}).get('path')}' -- not attributable to this run")
    return notes


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

        candidate["back_half"] = candidate_back_half(cand_id, stem)
        candidate["visual_review"] = candidate_visual_review(cand_id, stem)
        candidate["human_decision"] = candidate_decision(cand_id, stem)
        # Artefacts that had to be read from a shared flat name are recorded as
        # anomalies on the candidate, so "this is not provably this run's file"
        # survives into the run record instead of being lost in a path string.
        candidate["anomalies"] = _scope_notes(candidate["back_half"],
                                              candidate["visual_review"],
                                              candidate["human_decision"])
        #label-run-record-artefact-scope
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
    has not chosen / reviewed yet -- they are surfaced as counts, not as
    closeout failures.
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
