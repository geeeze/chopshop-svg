#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compare_candidates.py -- run every trace candidate through Layers A and B.

Given a ``02_traced/<stem>/`` directory, this runs each candidate SVG through
Layer A (validate_svg.py, source-level) and Layer B (preflight.py, rendered),
then writes a human-readable table and a machine-readable JSON.  It records
metrics and tradeoffs and lays out the sweep parameters for a human to choose
-- with ONE exception.  A candidate is ranked on whether the artwork survived
the trace at all, because "no gate fired" is not the same as "it is the
design": a bw/binary trace of a colour image discards the whole artwork and
still reports hard=0. Measured on the shipped example, all six bw candidates
came back clean with artwork MAE 104.2 against 0.005 for the faithful ones,
and sorting on gates alone ranked them level. See ``_fidelity_verdict``.

For each candidate it records: filename, sweep parameters, Layer A and Layer B
result (pass/fail + findings), declared colour count, rendered ink count, node
count (total and per-path max), file size, and a "distance from spec" summary
(hard gates failed + advisories).  Candidates are sorted by "fewest hard gates
failed, then whether the artwork survived the trace, then fewest advisories,
then lowest artwork MAE" -- the middle term is the exception described above,
and the table footer states the same ordering.  Do not describe this as a
gates-only sort: see ``references/passed-is-not-faithful.md``.

When a source raster can be resolved (from sweep.json's ``input.file`` or the
``--source`` flag), each candidate is ALSO rendered back to a raster with
Inkscape and diffed against the source, producing a pixel-fidelity metric
(mean absolute error over all pixels, over the artwork pixels only, the 95th
percentile, and the fraction within 10 units).  This is reported alongside the
printability gates -- it is the "accuracy" view for the raster->vector goal,
and it never picks a winner either.

Layer availability is detected at import time: if preflight.py is missing only
Layer A runs, if validate_svg.py is missing only Layer B runs, and the absent
layer is marked "not run" in the output.

Usage::

    python scripts/compare_candidates.py 02_traced/<stem> spec.json

Exit codes: 0 = report written (including the zero-candidate case),
2 = bad usage / bad input.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
for p in (ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import front_common as fc  # noqa: E402

# --- optional layer imports -------------------------------------------------
layer_a_available = True
layer_b_available = True
layer_a_error = None
layer_b_error = None
try:
    import validate_svg  # noqa: E402
except Exception as exc:  # noqa: BLE001
    layer_a_available = False
    layer_a_error = str(exc)
try:
    import preflight  # noqa: E402
except Exception as exc:  # noqa: BLE001
    layer_b_available = False
    layer_b_error = str(exc)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_candidates(traced_dir):
    """Return (candidates, sweep_payload).  candidates is a list of
    {file, sweep} where sweep carries the params that produced the file."""
    sweep_payload = None
    sweep_path = os.path.join(traced_dir, "sweep.json")
    if os.path.exists(sweep_path):
        try:
            sweep_payload = fc.load_json(sweep_path)
        except Exception:  # noqa: BLE001
            sweep_payload = None

    sweep_by_file = {}
    if sweep_payload and isinstance(sweep_payload.get("candidates"), list):
        for entry in sweep_payload["candidates"]:
            sweep_by_file[entry.get("file")] = entry

    files = sorted(
        f for f in os.listdir(traced_dir)
        if re.fullmatch(r"candidate_\d+\.svg", f))
    candidates = [{"file": f, "sweep": sweep_by_file.get(f, {})}
                  for f in files]
    return candidates, sweep_payload


def _findings_by_layer(hard, advisory):
    """Split classified findings into per-layer, per-severity buckets."""
    buckets = {}
    for layer in ("source_validation", "render_preflight"):
        buckets[layer] = {"hard": [], "advisory": []}
    for entry in hard:
        buckets.setdefault(entry["layer"], {"hard": [], "advisory": []}) \
            ["hard"].append(entry)
    for entry in advisory:
        buckets.setdefault(entry["layer"], {"hard": [], "advisory": []}) \
            ["advisory"].append(entry)
    return buckets


def run_one(svg_path, spec_path, workdir):
    """Run both layers for one candidate and return its record."""
    record = {
        "file": os.path.basename(svg_path),
        "file_size": os.path.getsize(svg_path) if os.path.exists(svg_path) else None,
        "layer_a": {"status": "not_run", "hard": 0, "advisory": 0,
                    "findings": []},
        "layer_b": {"status": "not_run", "hard": 0, "advisory": 0,
                    "findings": []},
        "declared_colors": None,
        "rendered_ink_colors": None,
        "node_count_total": None,
        "node_count_max": None,
        "hard": 0,
        "advisory": 0,
        "passed": False,
    }

    if layer_b_available:
        try:
            failures, notes, stats, _w, options = preflight.preflight(
                svg_path, spec_path, workdir=workdir)
            hard, advisory = preflight.classify_findings(failures, options)
            buckets = _findings_by_layer(hard, advisory)

            declared = stats.get("declared_colors") or []
            record["declared_colors"] = len(declared)
            record["rendered_ink_colors"] = stats.get("rendered_ink_colors")
            static = stats.get("static") or {}
            record["node_count_total"] = static.get("path_nodes_total")
            record["node_count_max"] = static.get("path_nodes_max")

            for layer_key in ("source_validation", "render_preflight"):
                bucket = buckets.get(layer_key,
                                     {"hard": [], "advisory": []})
                hard_entries = bucket["hard"]
                adv_entries = bucket["advisory"]
                status = "pass" if not hard_entries else "fail"
                record["layer_%s" % ("a" if layer_key == "source_validation"
                                     else "b")] = {
                    "status": status,
                    "hard": len(hard_entries),
                    "advisory": len(adv_entries),
                    "findings": [_fmt(entry) for entry in
                                 hard_entries + adv_entries],
                }

            # Layer B only truly "ran" if it produced a render.  When the
            # render tools are absent preflight returns before rasterising, so
            # the ink count is missing and a MISSING_TOOL finding explains why.
            if record["rendered_ink_colors"] is None:
                tool_finding = next((f for f in failures
                                     if f[0] == preflight.RULE_TOOL), None)
                record["layer_b"]["status"] = "not_run"
                record["layer_b"]["reason"] = (
                    "render tools unavailable" if tool_finding
                    else "render produced no ink reading")

            record["hard"] = record["layer_a"]["hard"] + record["layer_b"]["hard"]
            record["advisory"] = (record["layer_a"]["advisory"]
                                  + record["layer_b"]["advisory"])
            record["passed"] = (record["layer_a"]["status"] == "pass"
                                and record["layer_b"]["status"] == "pass")
        except Exception as exc:  # noqa: BLE001 - a broken candidate is data
            record["layer_a"]["status"] = "error"
            record["layer_b"]["status"] = "error"
            record["error"] = "%s: %s" % (type(exc).__name__, exc)
    elif layer_a_available:
        try:
            failures, notes, stats = validate_svg.validate(svg_path, spec_path)
            record["declared_colors"] = len(stats.get("colors") or [])
            record["node_count_total"] = stats.get("path_nodes_total")
            record["node_count_max"] = stats.get("path_nodes_max")
            # Without preflight there is no severity model; fail-safe to hard.
            record["layer_a"] = {
                "status": "pass" if not failures else "fail",
                "hard": len(failures),
                "advisory": 0,
                "findings": [{"rule": rule, "detail": detail,
                              "severity": "hard"}
                             for rule, detail in failures],
            }
            record["layer_b"]["status"] = "not_run"
            record["layer_b"]["reason"] = "preflight.py unavailable"
            record["hard"] = record["layer_a"]["hard"]
            record["passed"] = record["layer_a"]["status"] == "pass"
        except Exception as exc:  # noqa: BLE001
            record["layer_a"]["status"] = "error"
            record["error"] = "%s: %s" % (type(exc).__name__, exc)
    else:
        record["layer_a"]["reason"] = "validate_svg.py unavailable"
        record["layer_b"]["reason"] = "preflight.py unavailable"

    return record


def _fmt(entry):
    return {"rule": entry["rule"], "severity": entry["severity"],
            "detail": entry["detail"]}


def _sweep_summary(sweep_entry):
    if not sweep_entry:
        return "-"
    return "variant=%s preset=%s speckle=%s hier=%s palette=%s" % (
        sweep_entry.get("variant", "source"),
        sweep_entry.get("preset", "-"),
        sweep_entry.get("filter_speckle", "-"),
        sweep_entry.get("hierarchical", "-"),
        sweep_entry.get("use_palette", "-"))


def _status_mark(status):
    if status == "pass":
        return "PASS"
    if status == "fail":
        return "FAIL"
    if status == "error":
        return "ERROR"
    return "not run"


def _resolve_source(sweep_payload, source_flag):
    """Find the source raster to diff against.  The ``--source`` flag wins,
    else sweep.json's ``input.file`` (the prepped PNG the tracer was fed)."""
    if source_flag and os.path.exists(source_flag):
        return source_flag
    if sweep_payload:
        inp = sweep_payload.get("input") or {}
        path = inp.get("file")
        if path and os.path.exists(path):
            return path
    return None


def _candidate_source(sweep_entry, default_source):
    """The raster a candidate should be diffed against for fidelity.

    A pitch-shift candidate (inverse/pitch) was traced from a *derived* raster,
    not the prepped source, so diffing it against the original source would
    report a misleadingly large MAE.  When the sweep record names the variant's
    own raster (``variant_input``), diff against that instead.
    """
    vi = (sweep_entry or {}).get("variant_input")
    if vi and os.path.exists(vi):
        return vi
    return default_source


def _fidelity(svg_path, source_png, workdir, timeout=300):
    """Render a candidate back to a raster and diff it against the source.

    Returns a dict.  ``measured`` is True only when a real diff was computed.
    MAE is over all pixels (dominated by large flat areas), ``mae_art`` is over
    the artwork pixels only (source pixels that deviate from the modal colour,
    i.e. the actual subject rather than the background).
    """
    try:
        import numpy as np
        from PIL import Image

        inkscape = fc.which("inkscape")
        if inkscape is None:
            return {"measured": False, "note": "inkscape not installed"}
        src_img = Image.open(source_png).convert("RGB")
        width, height = src_img.size
        out_png = os.path.join(workdir,
                               os.path.basename(svg_path) + ".fidelity.png")
        subprocess.run(
            [inkscape, "--export-type=png",
             "--export-width=%d" % width, "--export-height=%d" % height,
             "--export-filename=" + out_png, svg_path],
            check=True, capture_output=True, text=True, timeout=timeout)
        if not os.path.exists(out_png):
            return {"measured": False, "note": "render produced no PNG"}

        src = np.asarray(src_img, dtype=np.int16)
        rend = np.asarray(Image.open(out_png).convert("RGB"), dtype=np.int16)
        if rend.shape != src.shape:
            return {"measured": False,
                    "note": "render size %s != source %s"
                    % (rend.shape, src.shape)}

        diff = np.abs(rend - src)
        mae = float(diff.mean())
        p95 = float(np.percentile(diff, 95))
        within10 = float((diff.max(axis=2) < 10).mean())

        # Artwork-only MAE: pixels where the source differs from its modal
        # (most common) colour, i.e. the subject rather than the background.
        flat = src.reshape(-1, 3)
        uniq, counts = np.unique(flat, axis=0, return_counts=True)
        modal = uniq[int(np.argmax(counts))]
        art_mask = np.abs(src - modal).max(axis=2) > 20
        mae_art = float(diff[art_mask].mean()) if art_mask.any() else None

        return {"measured": True,
                "mae": round(mae, 3),
                "p95": round(p95, 3),
                "within10": round(within10, 4),
                "mae_art": (None if mae_art is None else round(mae_art, 3)),
                "source": source_png}
    except Exception as exc:  # noqa: BLE001 - a failed render is data, not fatal
        return {"measured": False, "note": "%s: %s" % (type(exc).__name__, exc)}


# How far a candidate may drift from its source on the ARTWORK before it is
# called a fidelity failure. Measured on the shipped example: the six
# colour-preserving candidates read mae_art 0.005, and the six bw/binary ones
# read 104.214 -- a factor of ~20,000. Anything in between is not a gap in the
# measurements, it is a different job, so the threshold sits low and the
# comparison is meant to be a cliff rather than a curve.
MAE_ART_FAIL = 8.0
# A candidate that renders fewer distinct inks than the source declared has
# discarded colour, whatever the gate says. On the example the bw preset
# declares 1 colour for a 5-colour source and still passes both layers.
MIN_COLOUR_RETENTION = 0.5


def _fidelity_verdict(record):
    """Did this candidate keep the artwork? Returns (verdict, reason).

    This exists because "passed" only means no GATE fired. A bw/binary trace of
    a colour image discards the entire design and can still come back clean:
    on the shipped example all six bw candidates report passed=True, hard=0,
    advisory=0 -- while mae_art is 104.2 against 0.005 for the colour ones.
    Sorted purely on gates, they rank level with the faithful candidates and
    the table shows nothing that distinguishes them.

    This is deliberately NOT a gate. Fidelity is advisory: a deliberately
    simplified single-colour job is legitimate, and a hard failure here would
    reject it. What it must not do is be invisible.
    """
    fid = record.get("fidelity") or {}
    mae_art = fid.get("mae_art")
    declared = record.get("declared_colors")
    rendered = record.get("rendered_ink_colors")

    if mae_art is not None and mae_art > MAE_ART_FAIL:
        return "artwork_lost", (
            "artwork MAE %.1f exceeds %.1f -- the trace does not reproduce the "
            "design; check colormode (binary/bw discards colour)"
            % (mae_art, MAE_ART_FAIL))

    if declared and rendered is not None:
        # rendered_ink_colors counts what the render produced, which includes
        # the background, so only a large shortfall is meaningful.
        if rendered < declared * MIN_COLOUR_RETENTION:
            return "colour_dropped", (
                "render kept %s of %s declared colours -- some of the design "
                "was dropped" % (rendered, declared))

    if mae_art is not None and mae_art > 1.0:
        return "drift", "artwork MAE %.2f -- small but measurable drift" % mae_art
    return "faithful", "artwork MAE %s" % ("n/a" if mae_art is None else mae_art)


def _fidelity_cell(fid):
    """Compact "MAE all / MAE art" string for the table."""
    if not fid or not fid.get("measured"):
        return "-"
    mae = fid.get("mae")
    mae_art = fid.get("mae_art")
    all_s = "%.2f" % mae if mae is not None else "-"
    art_s = "%.2f" % mae_art if mae_art is not None else "-"
    return "%s / %s" % (all_s, art_s)


def _build_markdown(traced_dir, spec_path, candidates, layers):
    lines = []
    lines.append("# Candidate comparison: %s" % os.path.basename(traced_dir))
    lines.append("")
    lines.append("- spec: `%s`" % spec_path)
    lines.append("- candidates: %d" % len(candidates))
    lines.append("- layer A (source validation): %s"
                 % ("available" if layers["a"] else "NOT AVAILABLE"))
    lines.append("- layer B (render preflight): %s"
                 % ("available" if layers["b"] else "NOT AVAILABLE"))
    lines.append("- sorted by: fewest hard gates failed, then whether the "
                 "artwork survived the trace, then fewest advisories")
    lines.append("")
    lines.append("| # | file | sweep | LayerA | LayerB | declared | inks | "
                 "nodes (total/max) | size | MAE all/art | hard | adv | "
                 "fidelity |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for idx, cand in enumerate(candidates, 1):
        nodes = "-"
        if cand["node_count_total"] is not None:
            nodes = "%s/%s" % (cand["node_count_total"],
                               cand["node_count_max"])
        lines.append("| %d | %s | %s | %s | %s | %s | %s | %s | %s | %s | %d | %d | %s |"
                     % (idx, cand["file"],
                        _sweep_summary(cand.get("sweep")),
                        _status_mark(cand["layer_a"]["status"]),
                        _status_mark(cand["layer_b"]["status"]),
                        cand["declared_colors"] if cand["declared_colors"]
                        is not None else "-",
                        cand["rendered_ink_colors"] if cand["rendered_ink_colors"]
                        is not None else "-",
                        nodes,
                        cand["file_size"] if cand["file_size"] is not None
                        else "-",
                        _fidelity_cell(cand.get("fidelity")),
                        cand["hard"], cand["advisory"],
                        cand.get("fidelity_verdict") or "-"))

    # Fidelity ranking (the "accuracy" view).  Only when a source was diffed.
    measured = [c for c in candidates
                if (c.get("fidelity") or {}).get("measured")]
    if measured:
        ranked = sorted(measured,
                        key=lambda c: c["fidelity"].get(
                            "mae_art", c["fidelity"].get("mae", 1e9)))
        lines.append("")
        lines.append("## By pixel fidelity (accuracy view)")
        lines.append("")
        lines.append("Rendered back to the source resolution and diffed against "
                     "the source raster. Lower MAE = closer to the original. "
                     "`MAE art` ignores the background and measures the subject "
                     "only.")
        lines.append("")
        lines.append("A candidate can pass every gate and still not be the "
                     "artwork: a `bw`/binary trace of a colour image discards "
                     "the design entirely and comes back with hard=0. The "
                     "verdict column above is that check, and the main table is "
                     "sorted on it. It is advisory, not a gate -- a "
                     "single-colour job is a legitimate ask.")
        lines.append("")
        lines.append("| rank | file | MAE all | MAE art | P95 | within-10 |")
        lines.append("|---|---|---|---|---|---|")
        for rank, c in enumerate(ranked, 1):
            fid = c["fidelity"]
            lines.append("| %d | %s | %.3f | %s | %.3f | %.1f%% |"
                         % (rank, c["file"], fid["mae"],
                            ("%.3f" % fid["mae_art"])
                            if fid["mae_art"] is not None else "-",
                            fid["p95"], fid["within10"] * 100))

    # findings shared by every candidate (usually the uniform ones, e.g. a
    # dimension mismatch inherited from the tracer)
    lines.append("")
    lines.append("## Per-candidate findings")
    lines.append("")
    for cand in candidates:
        lines.append("### %s" % cand["file"])
        if cand.get("error"):
            lines.append("- **error**: %s" % cand["error"])
        for layer_label, key in (("Layer A", "layer_a"), ("Layer B", "layer_b")):
            layer = cand[key]
            if layer.get("status") == "not_run":
                lines.append("- %s: not run%s" % (
                    layer_label,
                    " (%s)" % layer["reason"] if layer.get("reason") else ""))
                continue
            if not layer["findings"]:
                lines.append("- %s: pass, no findings" % layer_label)
            else:
                lines.append("- %s: %s (%d hard, %d advisory)"
                             % (layer_label, layer["status"], layer["hard"],
                                layer["advisory"]))
                for finding in layer["findings"]:
                    lines.append("  - [%s] %s: %s" % (
                        finding["severity"], finding["rule"],
                        finding["detail"]))
        lines.append("")
    return "\n".join(lines)


def _compare_one(task):
    """Run both layers + fidelity for one candidate, in its own workdir.

    ``preflight.preflight`` writes fixed filenames into its workdir and unlinks
    the whole directory, so every candidate must get its own workdir or parallel
    workers would clobber each other.  Returns ``{"record": ..., "fidelity": ...}``.
    """
    os.makedirs(task["workdir"], exist_ok=True)
    record = run_one(task["svg_path"], task["spec_path"], task["workdir"])
    fidelity = (_fidelity(task["svg_path"], task["source_png"], task["workdir"])
                if task["source_png"] else
                {"measured": False, "note": "no source raster"})
    return {"record": record, "fidelity": fidelity}


def _compare(traced_dir, spec_path, out_dir=None, source_flag=None, workers=None):
    stem = os.path.basename(os.path.normpath(traced_dir))
    out_dir = out_dir or os.path.join(ROOT, "04_validated")
    os.makedirs(out_dir, exist_ok=True)

    candidates, sweep_payload = load_candidates(traced_dir)

    layers = {"a": layer_a_available, "b": layer_b_available}
    if not layer_a_available:
        print("compare_candidates: WARNING: Layer A unavailable: %s"
              % layer_a_error)
    if not layer_b_available:
        print("compare_candidates: WARNING: Layer B unavailable: %s"
              % layer_b_error)

    if not candidates:
        message = "no candidates found in %s" % traced_dir
        payload = {
            "tool": "compare_candidates.py",
            "generated": _now(),
            "traced_dir": traced_dir,
            "spec": spec_path,
            "candidate_count": 0,
            "note": message,
            "layers_available": layers,
            "candidates": [],
        }
        fc.write_json(os.path.join(out_dir, "%s.comparison.json" % stem),
                      payload)
        with open(os.path.join(out_dir, "%s.comparison.md" % stem), "w",
                  encoding="utf-8") as handle:
            handle.write("# Candidate comparison: %s\n\n%s\n"
                         % (stem, message))
        print("compare_candidates: %s (wrote empty comparison)" % message)
        return 0

    workdir = os.path.join(out_dir, "%s.work" % stem)
    os.makedirs(workdir, exist_ok=True)

    source_png = _resolve_source(sweep_payload, source_flag)
    if source_png:
        print("compare_candidates: fidelity source: %s" % source_png)
    else:
        print("compare_candidates: no source raster resolved; fidelity not "
              "measured (pass --source, or run via trace_sweep so sweep.json "
              "records the input)")

    # One task per candidate, each with its own workdir so parallel workers
    # never share preflight's fixed-name temp files.
    tasks = []
    for cand in candidates:
        svg_path = os.path.join(traced_dir, cand["file"])
        cw = os.path.join(workdir, os.path.splitext(cand["file"])[0])
        cand_source = _candidate_source(cand.get("sweep"), source_png)
        tasks.append({"svg_path": svg_path, "spec_path": spec_path,
                      "workdir": cw, "source_png": cand_source})

    workers_n = fc.resolve_workers(len(tasks), explicit=workers)
    print("compare_candidates: %d candidate(s), %d worker process(es)"
          % (len(tasks), workers_n))
    results = fc.run_parallel(_compare_one, tasks, workers=workers_n)

    records = []
    for cand, result in zip(candidates, results):
        record = result["record"]
        record["sweep"] = cand["sweep"]
        record["fidelity"] = result["fidelity"]
        records.append(record)
        fid = record["fidelity"]
        fid_str = ("mae=%.2f art=%.2f" % (fid["mae"], fid["mae_art"])
                   if fid.get("measured") and fid.get("mae_art") is not None
                   else ("mae=%.2f" % fid["mae"] if fid.get("measured")
                         else "n/a"))
        print("compare_candidates: %s  layerA=%s layerB=%s hard=%d adv=%d "
              "fidelity(%s)"
              % (cand["file"], record["layer_a"]["status"],
                 record["layer_b"]["status"], record["hard"],
                 record["advisory"], fid_str))

    # Attach the fidelity verdict before sorting, so a candidate that discarded
    # the artwork cannot rank level with one that reproduced it.
    for record in records:
        verdict, reason = _fidelity_verdict(record)
        record["fidelity_verdict"] = verdict
        record["fidelity_reason"] = reason
        fid = record.get("fidelity") or {}
        if verdict == "artwork_lost":
            # Surfaced as an advisory so it reaches the UI, but the gates are
            # untouched: a single-colour job is a legitimate ask.
            record["advisory"] = record.get("advisory", 0) + 1
            record.setdefault("layer_b", {}).setdefault("findings", []).append({
                "rule": "FIDELITY_ARTWORK_LOST",
                "severity": "advisory",
                "layer": "layer_b",
                "detail": reason,
            })
            if "layer_b" in record:
                record["layer_b"]["advisory"] = record["layer_b"].get("advisory", 0) + 1

    # Gates first, then: did it keep the artwork, then how faithful. Without the
    # middle term the bw candidates -- which pass every gate while throwing the
    # design away -- sort alongside the real ones.
    #
    # The ranks count DOWN, because the sort is ascending and the best candidate
    # has to come first. Ranking "artwork_lost" as 0 put the six candidates that
    # discarded the design at the top of the table, which is the exact opposite
    # of the fix. Caught by test_faithful_candidates_sort_above_artwork_lost.
    _FID_RANK = {"faithful": 0, "drift": 1,
                 "colour_dropped": 2, "artwork_lost": 3}
    records.sort(key=lambda c: (c["hard"], _FID_RANK.get(
        c.get("fidelity_verdict"), 2), c["advisory"],
        (c.get("fidelity") or {}).get("mae_art") or 0.0))

    common_rules = None
    for record in records:
        rules = {f["rule"] for layer_key in ("layer_a", "layer_b")
                 for f in record[layer_key]["findings"]}
        common_rules = rules if common_rules is None else (common_rules & rules)
    common_rules = sorted(common_rules or [])

    payload = {
        "tool": "compare_candidates.py",
        "generated": _now(),
        "traced_dir": traced_dir,
        "spec": spec_path,
        "candidate_count": len(records),
        "layers_available": layers,
        "common_findings": common_rules,
        "sort": ("fewest hard gates failed, then whether the artwork survived "
                 "(faithful < drift < colour_dropped < artwork_lost), then "
                 "fewest advisories, then lowest artwork MAE"),
        "fidelity_verdicts": sorted({r.get("fidelity_verdict")
                                     for r in records}),
        "candidates": records,
    }
    fc.write_json(os.path.join(out_dir, "%s.comparison.json" % stem), payload)

    md = _build_markdown(traced_dir, spec_path, records, layers)
    md += "\n\n## Common to all candidates\n\n%s\n" % (
        ", ".join(common_rules) if common_rules else "(none)")
    with open(os.path.join(out_dir, "%s.comparison.md" % stem), "w",
              encoding="utf-8") as handle:
        handle.write(md)

    print("compare_candidates: wrote %s"
          % os.path.join(out_dir, "%s.comparison.json" % stem))
    print("compare_candidates: wrote %s"
          % os.path.join(out_dir, "%s.comparison.md" % stem))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="compare_candidates.py",
        description="Run trace candidates through Layers A and B and produce "
                    "a comparison report (pipeline stage 4 comparison).")
    parser.add_argument("traced_dir", help="directory of candidate SVGs "
                                           "(02_traced/<stem>/")
    parser.add_argument("spec", help="path to spec.json")
    parser.add_argument("--out-dir", default=None,
                        help="output directory (default: <project>/04_validated)")
    parser.add_argument("--source", default=None,
                        help="source raster to diff candidates against for the "
                             "fidelity metric (default: sweep.json input.file)")
    parser.add_argument("--workers", type=int, default=None,
                        help="worker processes for the comparison "
                             "(default: min(cpu_count, candidates); 1 disables "
                             "parallelism; overridable via FRONT_PIPELINE_WORKERS)")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.traced_dir):
        print("compare_candidates: not a directory: %s" % args.traced_dir,
              file=sys.stderr)
        return 2
    if not os.path.exists(args.spec):
        print("compare_candidates: spec not found: %s" % args.spec,
              file=sys.stderr)
        return 2

    return _compare(args.traced_dir, args.spec, args.out_dir, args.source,
                    workers=args.workers)


if __name__ == "__main__":
    sys.exit(main())
