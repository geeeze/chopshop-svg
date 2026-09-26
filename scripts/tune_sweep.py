#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tune_sweep.py -- batch parameter/mode experiments for the raster->SVG pipeline.

Run a MATRIX of trace settings against one prepped raster, measure every
combination on the same axes, and write one ranked report. This exists so
settings can be compared side by side rather than guessed at one run at a time.

It is deliberately NOT `trace_sweep.py`. That produces printability-gate
candidates and stops at the human pick. This one is an experiment bench:

  * it sweeps the axes that `trace_sweep.py` does not expose
    (`splice_threshold`, `length_threshold`, `color_precision`,
    `layer_difference`, `hierarchical`, `filter_speckle`);
  * it measures PIXEL FIDELITY (MAE over the artwork) and NODE/INK stats for
    every cell, so "fewer nodes" can be weighed against "closer to the
    original";
  * it optionally runs `node_reduce.py` on each result, so the tracer-side and
    post-process-side routes to the same node budget can be compared;
  * it optionally renders each result to a PNG contact sheet, because the
    numbers cannot tell you which trace looks right.

IT NEVER PICKS A WINNER. It reports every cell and stops. Ranking is by a
configurable metric and is a MEASUREMENT ORDER, not a recommendation; the
"pick" column is deliberately absent for the same reason `trace_sweep.py` has
none. A human reads the contact sheet and decides.

MEASURED CONTEXT (this repo, 1024x1024 source, 245 608 source colours)
---------------------------------------------------------------------
The two node levers are ORTHOGONAL, which is the single most useful thing this
bench is for. Neither alone gets a complex trace under the 500-node gate:

  layer_difference  ->  colour and path COUNT.   4 -> 64 cut paths 18 053 ->
                       2 863 and colours 11 658 -> 2 493. Barely moves the
                       WORST single path (1 528 throughout).
  splice_threshold  ->  the WORST single path.  45 -> 150 took max nodes
                       798 -> 588 on one preset, monotonically, for about
                       0.6 MAE.

Combine them and the worst path falls while the file also gets much smaller:

  sp120/lt20/ld16   max 605   total 71 080   fills 3 326   MAE_art 21.77
  sp150/lt20/ld16   max 588   total 68 018   fills 3 326   MAE_art 22.17
  sp150/lt20/ld32   max 601   total 59 033   fills 2 342   MAE_art 22.25
  sp150/lt20/ld64   max 763   total 34 710   fills   962   MAE_art 24.13

Read that last row carefully: layer_difference 64 halves the file again but
pushes max nodes back UP, past the gate. `layer_difference` is not monotone on
max nodes. Any bench that sweeps it one axis at a time will mislead you.

Also measured, and worth knowing before you design a matrix:
  * `max_iterations` is INERT -- identical output at 10, 20 and 50.
  * `path_precision` is INERT -- identical output at 1, 2, 3 and 4.
  * `corner_threshold` at 60 (the default) is already the minimum; raising it
    makes max nodes WORSE (1 528 -> 1 633 at 150). Do not sweep it upward.
  * `color_precision` 2 collapses the whole image to ONE colour and 6 points.
    The useful floor is 3, not 2.
  * Quantising the SOURCE raster first (pngquant / PIL median / maxcov /
    octree, 6-16 colours) does NOT reduce node count. It changes the colour
    count but leaves max nodes where it was. It is a colour tool, not a node
    tool -- do not expect it to help `geometry_overload`.

Usage::

    # the measured sweet spot vs the documented baseline
    python3 scripts/tune_sweep.py 01_prepped/art.prepped.png

    # a custom matrix
    python3 scripts/tune_sweep.py art.png --preset baseline,flat,nodewise \\
        --filter-speckle 4 --hierarchical cutout --splice 45,90,150 \\
        --color-precision 3,4,6 --layer-difference 16,32,64

    # include the post-process route and a contact sheet
    python3 scripts/tune_sweep.py art.png --with-node-reduce --contact-sheet

Exit codes: 0 = ran (even with zero successful cells -- that is data), 1 = bad
usage / unreadable input, 3 = no tracer available.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import re
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, os.path.dirname(SCRIPT_DIR))

import front_common as fc  # noqa: E402

TOOL = "tune_sweep.py"
VERSION = "0.1.0"

# VTracer parameter spellings. Discovered by introspection, never assumed.
API_PARAMS = {"colormode", "hierarchical", "mode", "filter_speckle",
              "color_precision", "layer_difference", "corner_threshold",
              "length_threshold", "max_iterations", "splice_threshold",
              "path_precision"}

CMD_RE = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]")
SVG_NS = "http://www.w3.org/2000/svg"

# A node budget to report against. Read from the spec when available, else this.
DEFAULT_MAX_NODES = 500

# Documented no-op parameters. Reported as skipped axes so nobody re-derives it.
INERT_PARAMS = {
    "max_iterations": "inert: identical output at 10, 20 and 50 (measured)",
    "path_precision": "inert: identical output at 1, 2, 3 and 4 (measured)",
}


def _now():
    """UTC timestamp, same idiom as the other scripts in this package."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- tracer

def detect_tracer():
    """Return (module, available_params, note) or (None, set(), note)."""
    try:
        import inspect
        import vtracer
        sig = inspect.signature(vtracer.convert_image_to_svg_py)
        params = {p for p in sig.parameters
                  if p not in ("image_path", "out_path")}
        return (vtracer, params,
                "vtracer Python API %s (parameters introspected)"
                % fc.package_version("vtracer"))
    except Exception as exc:  # noqa: BLE001
        return None, set(), "no tracer: %s: %s" % (type(exc).__name__, exc)


# ---------------------------------------------------------------- measure

def svg_stats(svg_path):
    """paths, node totals/max, distinct fills, bytes -- from the file itself."""
    from lxml import etree
    root = etree.parse(svg_path).getroot()
    fills = set()
    total = 0
    worst = 0
    paths = 0
    for el in root.iter("{%s}path" % SVG_NS):
        d = el.get("d")
        if not d:
            continue
        paths += 1
        n = len(CMD_RE.findall(d))
        total += n
        worst = max(worst, n)
        f = el.get("fill")
        if f and f != "none":
            fills.add(f.strip().lower())
    try:
        vb = root.get("viewBox") or root.get("width") or ""
    except Exception:  # noqa: BLE001
        vb = ""
    return {"paths": paths, "nodes_total": total, "nodes_max": worst,
            "distinct_fills": len(fills), "viewbox": vb,
            "bytes": os.path.getsize(svg_path)}


def fidelity(svg_path, source_png, workdir, size=1024):
    """Render to raster and diff against the source. Never raises."""
    try:
        import numpy as np
        from PIL import Image
    except Exception as exc:  # noqa: BLE001
        return {"measured": False, "note": str(exc)}
    try:
        os.makedirs(workdir, exist_ok=True)
        png = os.path.join(workdir, "render.png")
        env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS="disabled:")
        r = subprocess.run(
            ["inkscape", svg_path, "--export-filename=%s" % png,
             "--export-width=%d" % size, "--export-height=%d" % size,
             "--export-background=#ffffff", "--export-background-opacity=255"],
            capture_output=True, timeout=300, env=env)
        if not os.path.isfile(png):
            return {"measured": False,
                    "note": "inkscape produced no file: %s"
                            % (r.stderr or b"")[-200:].decode("utf-8", "replace")}
        src = np.asarray(Image.open(source_png).convert("RGB")
                         .resize((size, size), Image.LANCZOS)).astype(np.int16)
        out = np.asarray(Image.open(png).convert("RGB")).astype(np.int16)
        diff = np.abs(src - out)
        flat = src.reshape(-1, 3)
        uniq, counts = np.unique(flat, axis=0, return_counts=True)
        modal = uniq[int(np.argmax(counts))]
        art = np.abs(src - modal).max(axis=2) > 20
        return {
            "measured": True,
            "mae": round(float(diff.mean()), 3),
            "mae_art": round(float(diff[art].mean()), 3) if art.any() else None,
            "p95": round(float(np.percentile(diff, 95)), 3),
            "within10": round(float((diff.max(axis=2) <= 10).mean()), 4),
            "art_fraction": round(float(art.mean()), 4),
            # A full-bleed source has no background, so an ink-vs-white reading
            # is meaningless. Carried so a reader never has to assume.
            "full_bleed": bool(art.mean() > 0.98),
        }
    except Exception as exc:  # noqa: BLE001 - a failed render is data
        return {"measured": False, "note": "%s: %s" % (type(exc).__name__, exc)}


# ---------------------------------------------------------------- matrix

PRESETS = {
    # name          params
    "baseline": {"colormode": "color", "mode": "spline",
                 "color_precision": 6, "layer_difference": 16},
    # Fewer colour clusters at the source. 3 is the useful floor: 2 collapses
    # the entire image to one colour.
    "flat": {"colormode": "color", "mode": "spline",
             "color_precision": 3, "layer_difference": 16},
    "flat4": {"colormode": "color", "mode": "spline",
              "color_precision": 4, "layer_difference": 32},
    # Coarser layering: far fewer paths and colours, but NOT lower max nodes.
    "coarse": {"colormode": "color", "mode": "spline",
               "color_precision": 6, "layer_difference": 64},
    # The node-focused combination, measured to be the best node/fidelity
    # trade in the 1024x1024 probe (max 588, MAE_art 22.17 vs 21.01 baseline).
    "nodewise": {"colormode": "color", "mode": "spline",
                 "color_precision": 6, "layer_difference": 16,
                 "splice_threshold": 150, "length_threshold": 20},
    "nodewise_coarse": {"colormode": "color", "mode": "spline",
                        "color_precision": 6, "layer_difference": 32,
                        "splice_threshold": 150, "length_threshold": 20},
    "binary": {"colormode": "binary"},
    "polygon": {"colormode": "color", "mode": "polygon",
                "color_precision": 6, "layer_difference": 16},
}


def build_matrix(args):
    """Return (cells, skipped_axes). Each cell is a name plus VTracer params."""
    presets = [p.strip() for p in args.preset.split(",") if p.strip()]
    unknown = [p for p in presets if p not in PRESETS]
    if unknown:
        raise SystemExit("tune_sweep: unknown preset(s): %s (known: %s)"
                         % (", ".join(unknown), ", ".join(sorted(PRESETS))))

    def nums(v):
        return [float(x) for x in v.split(",") if x.strip()]

    speckles = [int(x) for x in args.filter_speckle.split(",")] if args.filter_speckle else [None]
    hierarchicals = [h.strip() for h in args.hierarchical.split(",")] if args.hierarchical else ["cutout"]
    splices = nums(args.splice) if args.splice else [None]
    lengths = nums(args.length) if args.length else [None]
    precisions = [int(x) for x in args.color_precision.split(",")] if args.color_precision else [None]
    layers = nums(args.layer_difference) if args.layer_difference else [None]

    cells = []
    for pname, speckle, hier in itertools.product(presets, speckles, hierarchicals):
        base = dict(PRESETS[pname])
        # Explicit CLI axes override the preset's value for that key.
        if speckle is not None:
            base["filter_speckle"] = speckle
        if hier:
            base["hierarchical"] = hier
        for sp, lt, cp, ld in itertools.product(splices, lengths, precisions, layers):
            params = dict(base)
            if sp is not None:
                params["splice_threshold"] = sp
            if lt is not None:
                params["length_threshold"] = lt
            if cp is not None:
                params["color_precision"] = cp
            if ld is not None:
                params["layer_difference"] = ld
            name = "_".join("%s%s" % (k[:4], v) for k, v in sorted(params.items())
                            if k in ("filter_speckle", "splice_threshold",
                                     "color_precision", "layer_difference",
                                     "length_threshold"))
            cells.append({"name": name or pname, "preset": pname, "params": params})

    # De-duplicate: two presets can collapse to the same parameter set.
    seen = {}
    unique = []
    for c in cells:
        key = json.dumps(c["params"], sort_keys=True)
        if key in seen:
            continue
        seen[key] = c["name"]
        unique.append(c)

    if args.max_cells and len(unique) > args.max_cells:
        # Round-robin across presets so a cap still samples every preset.
        by_preset = {}
        for c in unique:
            by_preset.setdefault(c["preset"], []).append(c)
        interleaved = []
        for tup in itertools.zip_longest(*by_preset.values()):
            for c in tup:
                if c is not None:
                    interleaved.append(c)
        unique = interleaved[:args.max_cells]

    skipped = {k: v for k, v in INERT_PARAMS.items()}
    if args.max_cells and len(cells) > args.max_cells:
        skipped["truncation"] = "cap %d of %d cells" % (args.max_cells, len(cells))
    return unique, skipped


# ---------------------------------------------------------------- run

def run_cell(vtracer, cell, source_png, out_dir, spec, max_nodes,
             with_reduce, fidelity_on, size):
    """Trace one cell, measure it, optionally node-reduce it. Never raises."""
    name = cell["name"]
    result = {"name": name, "preset": cell["preset"], "params": cell["params"],
              "errors": []}

    svg = os.path.join(out_dir, "%s.svg" % name)
    t0 = time.time()
    try:
        vtracer.convert_image_to_svg_py(source_png, svg, **cell["params"])
    except Exception as exc:  # noqa: BLE001
        result["errors"].append("trace: %s: %s" % (type(exc).__name__, exc))
        return result
    result["trace_seconds"] = round(time.time() - t0, 2)
    if not os.path.isfile(svg):
        result["errors"].append("trace wrote no file")
        return result

    result.update(svg_stats(svg))
    result["over_gate_paths"] = sum(
        1 for el in _iter_paths(svg) if _node_count(el) > max_nodes)
    result["gate_cleared"] = result["nodes_max"] <= max_nodes

    if with_reduce and not result["gate_cleared"]:
        reduced = os.path.join(out_dir, "%s.reduced.svg" % name)
        try:
            proc = subprocess.run(
                [sys.executable, os.path.join(SCRIPT_DIR, "node_reduce.py"),
                 svg, "--out", reduced, "--max-nodes", str(max_nodes),
                 "--json", os.path.join(out_dir, "%s.reduce.json" % name)],
                capture_output=True, text=True, timeout=900,
                env=dict(os.environ, DBUS_SESSION_BUS_ADDRESS="disabled:"))
            if proc.returncode == 0 and os.path.isfile(reduced):
                after = svg_stats(reduced)
                # STRUCTURAL INVARIANT. A node-count check cannot see a lost
                # `Z` or a welded subpath, and a simplifier that drops closures
                # still "reduces" happily. Compare closure and subpath counts
                # against the input and refuse to record a reduction that
                # changed the shape's structure.
                z_before = _closure_counts(svg)
                z_after = _closure_counts(reduced)
                intact = z_before == z_after
                result["reduced"] = {
                    "file": os.path.basename(reduced),
                    "nodes_max": after["nodes_max"],
                    "nodes_total": after["nodes_total"],
                    "gate_cleared": after["nodes_max"] <= max_nodes,
                    "over_gate_paths": sum(
                        1 for el in _iter_paths(reduced)
                        if _node_count(el) > max_nodes),
                    "bytes": after["bytes"],
                    "structure_intact": intact,
                    "closures_before": z_before[0],
                    "closures_after": z_after[0],
                    "subpaths_before": z_before[1],
                    "subpaths_after": z_after[1],
                }
                if not intact:
                    result["errors"].append(
                        "node_reduce changed structure: closures %d -> %d, "
                        "subpaths %d -> %d (geometry was damaged, not reduced)"
                        % (z_before[0], z_after[0], z_before[1], z_after[1]))
            else:
                result["errors"].append("node_reduce exit %d" % proc.returncode)
        except Exception as exc:  # noqa: BLE001
            result["errors"].append("node_reduce: %s" % exc)

    if fidelity_on:
        result["fidelity"] = fidelity(svg, source_png,
                                      os.path.join(out_dir, ".fid", name),
                                      size=size)
    return result


def _iter_paths(svg_path):
    from lxml import etree
    try:
        root = etree.parse(svg_path).getroot()
    except Exception:  # noqa: BLE001
        return []
    return [el for el in root.iter("{%s}path" % SVG_NS) if el.get("d")]


def _node_count(el):
    return len(CMD_RE.findall(el.get("d") or ""))


def _closure_counts(svg_path):
    """(total `Z` commands, total `M` commands) across every path.

    The structural invariant a node count cannot see. A simplifier that welds
    subpaths together, or drops a `Z`, still reports a lower node count and
    exits 0 -- every reduction bug found in this repo got past node checks and
    only showed up here. Compared as CHARACTER counts, not geometric totals: a
    lost `Z` can be masked by a gain elsewhere in the file.
    """
    closes = moves = 0
    for el in _iter_paths(svg_path):
        d = el.get("d") or ""
        closes += d.upper().count("Z")
        moves += d.count("M") + d.count("m")
    return closes, moves


# ---------------------------------------------------------------- report

def _fmt(v, spec="%.2f"):
    if v is None:
        return "-"
    try:
        return spec % v
    except (TypeError, ValueError):
        return str(v)


def build_report(cells, spec, args, max_nodes, skipped, out_dir, seconds):
    lines = ["# Trace parameter matrix: %s" % os.path.basename(args.source),
             "",
             "- gate: `max_nodes_per_path` = **%d** nodes/path" % max_nodes,
             "- cells: **%d**   traced in %.1fs" % (len(cells), seconds),
             "- spec: `%s`" % (args.spec or "spec.json (defaults)"),
             "- fidelity: %s" % ("render + MAE vs source" if args.fidelity
                                  else "not measured (--fidelity to enable)"),
             ""]
    if skipped:
        lines += ["## Axes deliberately not swept", ""]
        for k, v in sorted(skipped.items()):
            lines.append("- `%s` — %s" % (k, v))
        lines.append("")

    ranked = sorted(cells, key=lambda c: (
        c.get("fidelity", {}).get("mae_art") is None,
        c.get("fidelity", {}).get("mae_art", 1e9),
        c.get("nodes_max", 1e9)))
    ok = [c for c in ranked if not c.get("errors")]

    lines += ["## Results", "",
              "Sorted by **MAE_art** (pixel fidelity over artwork pixels, "
              "ascending). This is a measurement order, not a recommendation — "
              "it does not know which trace looks right.",
              "",
              "| # | cell | preset | paths | nodes max | nodes total | fills | "
              "MAE_art | MAE | gate | over |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, c in enumerate(ok, 1):
        f = c.get("fidelity") or {}
        lines.append("| %d | `%s` | %s | %d | **%d** | %d | %d | %s | %s | %s | %d |"
                     % (i, c["name"], c["preset"], c.get("paths", 0),
                        c.get("nodes_max", 0), c.get("nodes_total", 0),
                        c.get("distinct_fills", 0),
                        _fmt(f.get("mae_art")), _fmt(f.get("mae")),
                        "clear" if c.get("gate_cleared") else "OVER",
                        c.get("over_gate_paths", 0)))
    failed = [c for c in cells if c.get("errors")]
    if failed:
        lines += ["", "## Cells that did not complete", ""]
        for c in failed:
            lines.append("- `%s`: %s" % (c["name"], "; ".join(c["errors"])))
    if args.with_node_reduce:
        lines += ["", "## Node reduction applied (tracer-free route)", "",
                  "The `reduced` figures are the post-processed file; the main "
                  "table shows the raw trace so the two routes can be compared "
                  "on equal terms.", "",
                  "| cell | raw max | reduced max | raw total | reduced total "
                  "| raw bytes | reduced bytes | clears | structure |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for c in ok:
            r = c.get("reduced")
            if not r:
                continue
            lines.append("| `%s` | %d | %s | %d | %d | %d | %d | %s | %s |"
                         % (c["name"], c.get("nodes_max", 0),
                            _fmt(r["nodes_max"], "%d"), c.get("nodes_total", 0),
                            r["nodes_total"], c.get("bytes", 0), r["bytes"],
                            "yes" if r["gate_cleared"] else "no",
                            "intact" if r.get("structure_intact", True)
                            else "**DAMAGED**"))
        lines += ["", "`structure` compares `Z` (closure) and `M` (subpath) "
                  "counts against the input. A reduction that changes them "
                  "altered the geometry rather than simplifying it, and is "
                  "flagged regardless of what the node count did."]
    lines += ["", "---", "",
              "`node_reduce.py --verify` on a chosen cell is the pass condition, "
              "not the node count: clearing the gate says nothing about whether "
              "the artwork survived. See scripts/NODE_REDUCTION.md.", ""]
    return "\n".join(lines)


def contact_sheet(cells, out_dir, sheet, cols=4, tile=300):
    """Tile each successful cell's render into one PNG for visual comparison."""
    try:
        from PIL import Image, ImageDraw
    except Exception as exc:  # noqa: BLE001
        print("tune_sweep: contact sheet skipped (%s)" % exc)
        return None
    tiles = []
    for c in cells:
        if c.get("errors"):
            continue
        p = os.path.join(out_dir, ".fid", c["name"], "render.png")
        if not os.path.isfile(p):
            continue
        try:
            im = Image.open(p).convert("RGB").resize((tile, tile), Image.LANCZOS)
        except Exception:  # noqa: BLE001
            continue
        canvas = Image.new("RGB", (tile, tile + 26), "#101018")
        canvas.paste(im, (0, 0))
        d = ImageDraw.Draw(canvas)
        label = c["name"]
        if len(label) > 30:
            label = label[:29] + "…"
        d.text((5, tile + 6), label, fill="#c8c8d8")
        d.text((5, tile + 15),
               "max %d  fills %d  MAE %s" % (
                   c.get("nodes_max", 0), c.get("distinct_fills", 0),
                   _fmt((c.get("fidelity") or {}).get("mae_art"))),
               fill="#8a8aa0")
        tiles.append(canvas)
    if not tiles:
        print("tune_sweep: no renders to tile; run with --fidelity first")
        return None
    rows = (len(tiles) + cols - 1) // cols
    sh = Image.new("RGB", (cols * tile, rows * (tile + 26)), "#08080c")
    for i, t in enumerate(tiles):
        sh.paste(t, ((i % cols) * tile, (i // cols) * (tile + 26)))
    sh.save(sheet)
    return sheet


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(
        prog=TOOL,
        description="Batch trace-parameter/mode experiments with measured "
                    "fidelity and node stats. Never picks a winner.")
    ap.add_argument("source", help="prepped PNG (or any raster VTracer reads)")
    ap.add_argument("--spec", default=None,
                    help="spec.json, to read max_nodes_per_path from")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--preset", default="baseline,flat,nodewise",
                    help="comma-separated presets: %s (default: %%(default)s)"
                         % ",".join(sorted(PRESETS)))
    ap.add_argument("--filter-speckle", default=None, help="e.g. 2,8,16")
    ap.add_argument("--hierarchical", default=None, help="cutout,stacked")
    ap.add_argument("--splice", default=None,
                    help="splice_threshold values: the max-node lever")
    ap.add_argument("--length", default=None, help="length_threshold values")
    ap.add_argument("--color-precision", default=None,
                    help="colour clusters; 3 is the useful floor, 2 collapses")
    ap.add_argument("--layer-difference", default=None,
                    help="layering coarseness: the colour/path-COUNT lever, "
                         "NOT the max-node lever")
    ap.add_argument("--max-cells", type=int, default=24,
                    help="cap on cells, round-robin across presets (0 = no cap)")
    ap.add_argument("--fidelity", action="store_true", default=True,
                    help="render and diff each cell (default on)")
    ap.add_argument("--no-fidelity", dest="fidelity", action="store_false",
                    help="skip rendering (much faster, no MAE columns)")
    ap.add_argument("--with-node-reduce", action="store_true",
                    help="also run node_reduce.py on cells over the gate")
    ap.add_argument("--contact-sheet", action="store_true",
                    help="tile the renders into one PNG for visual comparison")
    ap.add_argument("--render-size", type=int, default=1024)
    ap.add_argument("--json", dest="json_path", default=None)
    args = ap.parse_args(argv)

    if not os.path.isfile(args.source):
        print("tune_sweep: source not found: %s" % args.source, file=sys.stderr)
        return 1

    vtracer, available, note = detect_tracer()
    if vtracer is None:
        print("tune_sweep: %s" % note, file=sys.stderr)
        print("  pip install vtracer", file=sys.stderr)
        return 3

    max_nodes = DEFAULT_MAX_NODES
    if args.spec and os.path.isfile(args.spec):
        try:
            spec = fc.load_json(args.spec)
            max_nodes = int((spec.get("geometry") or {}).get(
                "max_nodes_per_path") or DEFAULT_MAX_NODES)
        except Exception as exc:  # noqa: BLE001
            print("tune_sweep: cannot read spec (%s); using default gate %d"
                  % (exc, max_nodes))

    cells, skipped = build_matrix(args)

    # Drop any parameter this tracer build does not expose, and say so.
    for c in cells:
        dropped = sorted(set(c["params"]) - available)
        if dropped:
            c["params"] = {k: v for k, v in c["params"].items() if k in available}
            skipped.setdefault("unsupported", "dropped: %s" % ", ".join(dropped))

    stem = os.path.splitext(os.path.basename(args.source))[0]
    out_dir = args.out_dir or os.path.join(
        os.path.dirname(SCRIPT_DIR), "08_tune", stem)
    os.makedirs(out_dir, exist_ok=True)

    print("--- %s %s ---" % (TOOL, VERSION))
    print("source : %s" % args.source)
    print("tracer : %s" % note)
    print("gate   : %d nodes/path" % max_nodes)
    print("cells  : %d%s" % (len(cells),
                             "  (capped)" if args.max_cells else ""))
    for k, v in sorted(skipped.items()):
        print("  skip %-16s %s" % (k, v))

    t0 = time.time()
    results = [run_cell(vtracer, c, args.source, out_dir, None, max_nodes,
                        args.with_node_reduce, args.fidelity, args.render_size)
               for c in cells]
    seconds = time.time() - t0

    payload = {
        "tool": TOOL, "version": VERSION, "generated": _now(),
        "source": {"file": args.source, "sha256": fc.sha256_file(args.source)},
        "tracer": note, "available_params": sorted(available),
        "gate_max_nodes": max_nodes,
        "argv": vars(args),
        "skipped_axes": skipped,
        "cells_total": len(cells), "seconds": round(seconds, 2),
        "cells": results,
    }
    json_path = args.json_path or os.path.join(out_dir, "tune.json")
    fc.write_json(json_path, payload)

    md = build_report(results, None, args, max_nodes, skipped, out_dir, seconds)
    md_path = os.path.join(out_dir, "tune.md")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(md)

    if args.contact_sheet:
        sheet = contact_sheet(results, out_dir,
                              os.path.join(out_dir, "contact-sheet.png"))
        if sheet:
            print("contact sheet: %s" % sheet)

    print("elapsed : %.1fs" % seconds)
    print("report  : %s" % md_path)
    print("json    : %s" % json_path)
    ok = [c for c in results if not c.get("errors")]
    cleared = [c for c in ok if c.get("gate_cleared")]
    line = ("cells   : %d ok, %d cleared the %d-node gate as traced, %d errored"
            % (len(ok), len(cleared), max_nodes, len(results) - len(ok)))
    if args.with_node_reduce:
        # Report the post-processed outcome separately: a cell can fail the gate
        # as traced and still clear it after node_reduce. Collapsing the two into
        # one number would hide exactly the result this bench exists to find.
        helped = [c for c in ok
                  if not c.get("gate_cleared") and (c.get("reduced") or {}).get("gate_cleared")]
        line += ("\n          %d of %d over-gate cells cleared AFTER node_reduce "
                 "(the raw trace is what the table above reports)"
                 % (len(helped), len(ok) - len(cleared)))
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
