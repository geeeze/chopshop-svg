#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prep_expand.py -- the expanded prep stage: the raster-side benches that
front_pipeline.sh does not run, driven from the job spec.

front_pipeline.sh is prep -> trace sweep -> compare. Three side tools sit
outside it, all run by hand:

  * ``scripts/im_filters.py``  ("chopshop-im") -- raster filters BEFORE the
    tracer. This is the lever for node count: VTracer traces literal pixel
    boundaries, so grain becomes hundreds of corrective nodes. Prepares a raster
    it was handed; this decides what to hand it.
  * ``scripts/tune_sweep.py``  -- a batch bench over trace settings
    (``splice_threshold``, ``length_threshold``, ``color_precision``,
    ``layer_difference``), measured on one raster, ranked by pixel fidelity.
  * ``scripts/node_reduce.py`` -- the remedy for ``geometry_overload``. Needs
    traced candidates, so it cannot run before the sweep.

This script is the one integration point for all three, driven by
``spec.print.prep_expand``. One key, one home: the studio's Prep card writes it
and this reads it, so there is no second source of truth for what ran.

TWO PHASES, because the three tools do not run at the same time::

    prep_expand.py --phase prep       <raster> <spec> --out-dir DIR   # filters + tune
    prep_expand.py --phase post-trace <stem>   <spec> --out-dir DIR   # node_reduce

The runner calls the first after prep and the second after the sweep, and the
artifacts land in ``--out-dir`` so they can be copied into the job directory
where the studio's archive and delete buttons already reach.

SPEC (all keys optional; omit the block and the stage is a no-op)::

    "print": {
      "prep_expand": {
        "filters":     {"presets": ["kuwahara", "flat6"], "contact_sheet": true},
        "tune":        {"presets": ["baseline", "nodewise"], "max_cells": 12,
                        "contact_sheet": true, "with_node_reduce": false},
        "node_reduce": {"max_nodes": 500, "tolerance": 0.5,
                        "tolerance_cap": 8.0, "verify": true}
      }
    }

``node_reduce`` WRITES NEW CANDIDATES AND KEEPS THE ORIGINALS. A reduced copy
is written as the next free ``candidate_NN.svg`` in the traced dir, because
``compare_candidates.py`` discovers candidates with
``re.fullmatch(r"candidate_\\d+\\.svg", f)`` and anything else is never measured.
Its provenance is appended to ``sweep.json`` so the comparison report can say
which original it came from.

IT NEVER PICKS A WINNER, and neither do the tools it drives. Ranking is a
measurement order. A reduced candidate that clears the node gate can still have
lost the artwork -- ``--verify`` reports the ink LOST and GAINED against the
pre-reduction render, plus the largest background-coloured blob, which is the
shared-boundary gap signature that means "gate cleared, art eroded". Read them
together: the blob is the number that says the art survived, not the gate.

Exit codes: 0 = the phase ran (even if a tool was missing and skipped),
1 = bad usage or I/O error.
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)

import front_common as fc  # noqa: E402

TOOL = "prep_expand"
VERSION = "0.1.0"

# The candidate name compare_candidates.py will actually pick up. Anything else
# is invisible to the report, so reduced copies have to use this spelling.
CANDIDATE_RE = re.compile(r"candidate_(\d+)\.svg$")

# A bench that takes a long time and produces little is worse than no bench; the
# default caps keep an unattended job bounded.
DEFAULT_MAX_CELLS = 12
DEFAULT_MAX_NODES = 500


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _py() -> str:
    """The project venv interpreter, falling back to this one.

    front_pipeline.sh hard-requires .venv/bin/python; the benches import
    vtracer/svgpathtools/numpy, so running them under a bare python3 would fail
    on import rather than on the work.
    """
    venv = os.path.join(PROJECT, ".venv", "bin", "python")
    return venv if os.path.exists(venv) else sys.executable


def _run(command: list[str], log: list[str]) -> int:
    """Run one bench, echoing its output into the job log.

    Never raises on a non-zero exit: a missing tool (im_filters exits 3 with no
    ImageMagick) is a skip to record, not a reason to fail the whole job. The
    caller decides what a given code means.
    """
    log.append("$ " + " ".join(command))
    proc = subprocess.run(command, cwd=PROJECT, capture_output=True, text=True)
    for stream in (proc.stdout, proc.stderr):
        for line in (stream or "").splitlines():
            if line.strip():
                log.append("  " + line.rstrip())
    return proc.returncode


def _spec_block(spec: dict, key: str) -> dict | None:
    block = (spec.get("print") or {}).get("prep_expand") or {}
    value = block.get(key)
    if value in (None, False):
        return None
    if value is True:
        return {}
    return value if isinstance(value, dict) else None


def _csv(value, default: str) -> str:
    """Accept a list or a comma string, because both spellings are natural JSON."""
    if value in (None, "", []):
        return default
    if isinstance(value, (list, tuple)):
        return ",".join(str(v).strip() for v in value if str(v).strip())
    return str(value)


# ------------------------------------------------------------------ filters
def run_filters(spec: dict, raster: str, out_dir: str, log: list[str]) -> dict:
    cfg = _spec_block(spec, "filters")
    if cfg is None:
        return {"status": "off"}

    target = os.path.join(out_dir, "filters")
    command = [_py(), os.path.join(SCRIPT_DIR, "im_filters.py"), raster,
               "--outdir", target,
               "--presets", _csv(cfg.get("presets"), "kuwahara,flat6")]
    if cfg.get("contact_sheet", True):
        command.append("--contact-sheet")
    if cfg.get("keep_alpha"):
        command.append("--keep-alpha")
    if cfg.get("spec") is not None:
        command += ["--spec", str(cfg["spec"])]

    code = _run(command, log)
    if code == 3:
        # House convention: no engine on PATH exits 3 and never substitutes a
        # Python filter for the named one. Record it and carry on.
        return {"status": "skipped", "reason": "imagemagick not on PATH"}
    if code != 0:
        return {"status": "failed", "exit": code}
    # im_filters nests one level under --outdir by the source's stem
    # (<outdir>/<stem>/filters.json). Reading the report straight out of
    # --outdir found nothing and reported both paths as absent, which is how a
    # correct run looked like a failure.
    produced = _filters_dir(target)
    if produced is None:
        return {"status": "failed", "reported": [], "exit": 0,
                "reason": "im_filters wrote no filters.json under %s" % _rel(target)}
    return {"status": "ok", "out_dir": _rel(produced),
            "report": _rel(os.path.join(produced, "filters.json")),
            "markdown": _exists(os.path.join(produced, "filters.md")),
            "contact_sheet": _exists(os.path.join(produced, "contact-sheet.png"))}


def _filters_dir(base: str) -> str | None:
    """Where im_filters actually wrote: --outdir, or the stem dir under it."""
    if os.path.isfile(os.path.join(base, "filters.json")):
        return base
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            sub = os.path.join(base, name)
            if os.path.isfile(os.path.join(sub, "filters.json")):
                return sub
    return None


# --------------------------------------------------------------------- tune
def run_tune(spec: dict, raster: str, spec_path: str, out_dir: str,
             log: list[str]) -> dict:
    cfg = _spec_block(spec, "tune")
    if cfg is None:
        return {"status": "off"}

    target = os.path.join(out_dir, "tune")
    os.makedirs(target, exist_ok=True)
    json_path = os.path.join(target, "tune.json")
    command = [_py(), os.path.join(SCRIPT_DIR, "tune_sweep.py"), raster,
               "--spec", spec_path,
               "--out-dir", target,
               "--json", json_path,
               "--preset", _csv(cfg.get("presets"), "baseline,flat,nodewise"),
               "--max-cells", str(int(cfg.get("max_cells") or DEFAULT_MAX_CELLS))]
    if cfg.get("filter_speckle") is not None:
        command += ["--filter-speckle", _csv(cfg["filter_speckle"], "")]
    if cfg.get("hierarchical") is not None:
        command += ["--hierarchical", _csv(cfg["hierarchical"], "")]
    if cfg.get("with_node_reduce"):
        command.append("--with-node-reduce")
    if cfg.get("no_fidelity"):
        command.append("--no-fidelity")
    if cfg.get("contact_sheet", True):
        command.append("--contact-sheet")

    code = _run(command, log)
    if code != 0:
        return {"status": "failed", "exit": code}
    cleared = None
    cells = None
    if os.path.isfile(json_path):
        try:
            payload = fc.load_json(json_path)
            cells = payload.get("cells_total")
            cleared = sum(1 for c in payload.get("cells", [])
                          if c.get("gate_cleared"))
        except Exception:  # noqa: BLE001 - a broken report is data, not a crash
            pass
    return {"status": "ok", "out_dir": _rel(target),
            "report": _rel(os.path.join(target, "tune.json")),
            "markdown": _exists(os.path.join(target, "tune.md")),
            "contact_sheet": _exists(os.path.join(target, "contact-sheet.png")),
            "cells": cells, "cells_clearing_gate": cleared}


# -------------------------------------------------------------- node_reduce
def _max_nodes(traced_dir: str, name: str) -> int | None:
    """Max nodes in one path, by node_reduce's own counting rule.

    Imported lazily: node_reduce needs lxml and svgpathtools, which the venv
    has and a bare python3 may not. A failure to import degrades to "unknown"
    and the candidate is handed to the tool anyway.
    """
    try:
        import node_reduce as nr
        from lxml import etree
    except Exception:  # noqa: BLE001
        return None
    try:
        root = etree.parse(os.path.join(traced_dir, name)).getroot()
        counts = nr.node_counts(root)
        return max(counts) if counts else 0
    except Exception:  # noqa: BLE001
        return None


def _next_candidate_index(traced_dir: str) -> int:
    highest = 0
    for name in os.listdir(traced_dir) if os.path.isdir(traced_dir) else []:
        match = CANDIDATE_RE.search(name)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def _node_gate(spec: dict, cfg: dict) -> int:
    if cfg.get("max_nodes"):
        return int(cfg["max_nodes"])
    return int((spec.get("geometry") or {}).get("max_nodes_per_path")
               or DEFAULT_MAX_NODES)


def run_node_reduce(spec: dict, stem: str, spec_path: str, out_dir: str,
                    traced_root: str, log: list[str]) -> dict:
    """Write a reduced COPY of every over-gate candidate, originals untouched.

    Only over-gate candidates are targeted, because node_reduce's own research
    (scripts/NODE_REDUCTION.md) found that simplifying a whole file makes the
    worst path worse.
    """
    cfg = _spec_block(spec, "node_reduce")
    if cfg is None:
        return {"status": "off"}

    traced_dir = os.path.join(traced_root, stem)
    if not os.path.isdir(traced_dir):
        return {"status": "skipped", "reason": f"no traced dir at {_rel(traced_dir)}"}

    gate = _node_gate(spec, cfg)
    target = os.path.join(out_dir, "node_reduce")
    os.makedirs(target, exist_ok=True)

    # The gate lives in the spec, but node_reduce's own default is 500; passing
    # it explicitly is what keeps the bench and the pipeline's constraint equal.
    script = os.path.join(SCRIPT_DIR, "node_reduce.py")
    originals = sorted(n for n in os.listdir(traced_dir) if CANDIDATE_RE.search(n))
    if not originals:
        return {"status": "skipped", "reason": "no candidates to reduce"}

    # A preflight read of the node counts, so a candidate that is already inside
    # the gate is not even started. The count has to be node_reduce's OWN rule
    # (segments per path): counting command letters in the `d` attribute gives a
    # different number -- 102 against the tool's 96 on a real candidate -- and a
    # wrong number in a report is worse than no number.
    over = []
    for name in originals:
        nodes = _max_nodes(traced_dir, name)
        if nodes is None or nodes > gate:
            # Unknown counts are passed through rather than skipped: node_reduce
            # declines a candidate that is inside the gate by itself
            # ("nothing_over_gate") and writes nothing, so the cost is one
            # skipped run and the result is still correct.
            over.append((name, nodes))

    if not over:
        fc.write_json(os.path.join(target, "node-reduce.json"),
                      {"tool": TOOL, "generated": _now(), "gate_max_nodes": gate,
                       "candidates": len(originals), "over_gate": 0,
                       "reduced": [], "detail": "every candidate is already inside the gate"})
        return {"status": "ok", "out_dir": _rel(target), "over_gate": 0, "reduced": []}

    results, next_index = [], _next_candidate_index(traced_dir)
    for name, nodes in over:
        source = os.path.join(traced_dir, name)
        out_name = "candidate_%02d.svg" % next_index
        out_path = os.path.join(traced_dir, out_name)
        json_path = os.path.join(target, "%s.reduce.json" % os.path.splitext(name)[0])
        command = [_py(), script, source, "--out", out_path,
                   "--max-nodes", str(gate),
                   "--json", json_path]
        if cfg.get("tolerance") is not None:
            command += ["--tolerance", str(cfg["tolerance"])]
        if cfg.get("tolerance_cap") is not None:
            command += ["--tolerance-cap", str(cfg["tolerance_cap"])]
        if cfg.get("verify", True):
            command.append("--verify")

        code = _run(command, log)
        record = {"from": name, "to": out_name, "nodes_before": nodes,
                  "exit": code, "gate_max_nodes": gate}
        if os.path.isfile(json_path):
            try:
                payload = fc.load_json(json_path)
                record["nodes_after"] = payload.get("path_nodes_max_after")
                record["status"] = payload.get("status")
                record["gate_cleared"] = payload.get("gate_cleared")
                # The number that says whether the ART survived, as opposed to
                # whether the gate cleared. Kept next to each other on purpose.
                verify = payload.get("verify") or {}
                # The real fields: verify() reports ink LOST and GAINED against
                # the pre-reduction render, plus the largest background-coloured
                # blob. There is no single "ink drift" number.
                record["ink_lost_percent"] = verify.get("ink_lost_percent")
                record["ink_gained_percent"] = verify.get("ink_gained_percent")
                record["largest_gap_blob_px"] = verify.get("largest_gap_blob_px")
            except Exception:  # noqa: BLE001
                record["status"] = "report_unreadable"
        if code == 0 and os.path.isfile(out_path):
            # Only a written file becomes a candidate; a rejected run must not
            # leave a half-reduced SVG in the traced dir for compare to measure.
            next_index += 1
            _record_provenance(traced_dir, out_name, name, gate, record)
        else:
            record["status"] = record.get("status") or "not_written"
            if os.path.isfile(out_path):
                os.unlink(out_path)
        results.append(record)

    summary = {
        "tool": TOOL, "version": VERSION, "generated": _now(),
        "spec": _rel(spec_path), "gate_max_nodes": gate,
        "candidates": len(originals), "over_gate": len(over),
        "reduced": results,
        "note": ("reduced copies are extra candidates; the originals are "
                 "untouched. gate_cleared and largest_gap_blob_px are reported "
                 "side by side on purpose -- a cleared gate with a large gap "
                 "blob means the artwork was eroded, not improved."),
    }
    fc.write_json(os.path.join(target, "node-reduce.json"), summary)
    return {"status": "ok", "out_dir": _rel(target),
            "over_gate": len(over),
            "written": [r["to"] for r in results if r.get("gate_cleared") is not None
                        and os.path.isfile(os.path.join(traced_dir, r["to"]))],
            "detail": results}


def _record_provenance(traced_dir: str, out_name: str, from_name: str,
                       gate: int, record: dict) -> None:
    """Append the reduced copy to sweep.json so the comparison can place it.

    Without this the new candidate appears in the report with no parameters at
    all, and a reader cannot tell a traced candidate from a post-processed one.
    """
    path = os.path.join(traced_dir, "sweep.json")
    if not os.path.isfile(path):
        return
    try:
        payload = fc.load_json(path)
    except Exception:  # noqa: BLE001
        return
    entries = payload.get("candidates")
    if not isinstance(entries, list):
        return
    entries.append({
        "file": out_name,
        "preset": "node_reduce",
        "params": {},
        "reduced_from": from_name,
        "stage": "node_reduce",
        "nodes_before": record.get("nodes_before"),
        "gate_max_nodes": gate,
        "gate_cleared": record.get("gate_cleared"),
        "ink_lost_percent": record.get("ink_lost_percent"),
        "ink_gained_percent": record.get("ink_gained_percent"),
        "largest_gap_blob_px": record.get("largest_gap_blob_px"),
    })
    payload["candidates"] = entries
    fc.write_json(path, payload)


# ------------------------------------------------------------------ helpers
def _rel(path: str) -> str:
    """Project-relative when inside the project, absolute otherwise.

    A bare os.path.relpath turns an out-of-project output dir into
    ``../../../.hermes/cache/...``, which is unreadable in a report and cannot
    be used to locate the file again.
    """
    absolute = os.path.abspath(path)
    try:
        relative = os.path.relpath(absolute, PROJECT)
    except ValueError:
        return absolute
    return absolute if relative.startswith(os.pardir) else relative


def _exists(path: str) -> str | None:
    return _rel(path) if os.path.isfile(path) else None


# One report for both phases. Each phase writes its own section, and the
# second one to run MERGES rather than overwrites: the phases cannot see each
# other's work (the prep phase runs before the sweep, node_reduce after it), so
# a plain write from the second lost the first's section entirely -- the report
# came out claiming filters and tune never ran, with their files sitting next to
# it on disk.
SECTIONS = ("filters", "tune", "node_reduce")


def _merge_report(out_dir: str, summary: dict) -> dict:
    path = os.path.join(out_dir, "prep-expand.json")
    merged: dict = {}
    if os.path.isfile(path):
        try:
            merged = fc.load_json(path) or {}
        except Exception:  # noqa: BLE001 - an unreadable report is not fatal
            merged = {}

    for key in SECTIONS:
        if summary.get(key) is not None:
            merged[key] = summary[key]
    # Per-phase bookkeeping, kept side by side: which raster the prep phase saw,
    # which stem node_reduce worked on.
    for key in ("raster", "stem"):
        if summary.get(key):
            merged[key] = summary[key]
    phases = sorted(set((merged.get("phases") or []) + [summary["phase"]]))
    merged.update({"tool": TOOL, "version": VERSION, "phase": summary["phase"],
                   "phases": phases, "generated": summary["generated"],
                   "spec": summary.get("spec") or merged.get("spec")})
    merged["steps_run"] = [k for k in SECTIONS
                           if (merged.get(k) or {}).get("status") not in ("off", None)]
    fc.write_json(path, merged)
    return merged


def _write_markdown(out_dir: str, summary: dict) -> str:
    lines = ["# prep-expand", "",
             "Generated %s by %s %s. Phases: %s." % (
                 summary["generated"], TOOL, VERSION,
                 ", ".join(summary.get("phases") or [summary.get("phase", "")])),
             "", "This is a MEASUREMENT ORDER, never a recommendation: no tool "
             "here picks a candidate, and neither does this report.", ""]
    for key in ("filters", "tune", "node_reduce"):
        phase = summary.get(key)
        if not phase or phase.get("status") in ("off", None):
            continue
        lines.append("## %s" % key)
        lines.append("")
        lines.append("- status: `%s`" % phase.get("status"))
        for field in ("out_dir", "report", "markdown", "contact_sheet",
                      "cells", "cells_clearing_gate", "over_gate", "written",
                      "reason", "reported"):
            if phase.get(field) is not None:
                lines.append("- %s: `%s`" % (field, phase[field]))
        if key == "node_reduce" and phase.get("detail"):
            lines += ["", "| from | to | nodes before | after | gate cleared | "
                          "ink lost % | ink gained % | largest gap blob px |",
                      "| --- | --- | --- | --- | --- | --- | --- | --- |"]
            for item in phase["detail"]:
                lines.append("| %s | %s | %s | %s | %s | %s | %s | %s |" % (
                    item.get("from"), item.get("to"), item.get("nodes_before"),
                    item.get("nodes_after"), item.get("gate_cleared"),
                    item.get("ink_lost_percent"), item.get("ink_gained_percent"),
                    item.get("largest_gap_blob_px")))
            lines += ["", "A cleared gate with a large gap blob is art erosion, "
                          "not a rescue. Read the two columns together."]
        lines.append("")
    path = os.path.join(out_dir, "prep-expand.md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the expanded prep benches from spec.print.prep_expand.")
    parser.add_argument("--phase", choices=("prep", "post-trace"), required=True)
    parser.add_argument("target",
                        help="prep: the prepped raster. post-trace: the stem.")
    parser.add_argument("spec", help="the job spec (spec.json)")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--traced-root", default=os.path.join(PROJECT, "02_traced"))
    parser.add_argument("--json", dest="json_path", default=None)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.spec):
        print("prep_expand: spec not found: %s" % args.spec, file=sys.stderr)
        return 1
    spec = fc.load_json(args.spec)
    os.makedirs(args.out_dir, exist_ok=True)
    log: list[str] = []
    summary = {"tool": TOOL, "version": VERSION, "generated": _now(),
               "phase": args.phase, "spec": _rel(args.spec)}

    if args.phase == "prep":
        if not os.path.isfile(args.target):
            print("prep_expand: raster not found: %s" % args.target, file=sys.stderr)
            return 1
        summary["raster"] = _rel(args.target)
        summary["filters"] = run_filters(spec, args.target, args.out_dir, log)
        summary["tune"] = run_tune(spec, args.target, args.spec, args.out_dir, log)
    else:
        summary["stem"] = args.target
        summary["node_reduce"] = run_node_reduce(
            spec, args.target, args.spec, args.out_dir, args.traced_root, log)

    if not args.quiet:
        for line in log:
            print(line, flush=True)

    ran = [k for k in ("filters", "tune", "node_reduce")
           if summary.get(k) and summary[k].get("status") not in ("off", None)]
    summary["steps_run"] = ran
    if not ran:
        # Nothing asked for: say so rather than writing an empty report that
        # looks like work happened.
        print("prep_expand: spec.print.prep_expand asks for nothing; no-op")
        return 0

    merged = _merge_report(args.out_dir, summary)
    if args.json_path:
        fc.write_json(args.json_path, merged)
    _write_markdown(args.out_dir, merged)
    print("prep_expand: %s phase wrote %s" % (args.phase, _rel(args.out_dir)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
