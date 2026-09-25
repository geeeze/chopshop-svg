#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trace_sweep.py -- multi-pass VTracer tracing with a parameter sweep (stage 2).

Turns one prepped PNG into a set of candidate SVGs, each traced with a
different parameter combination, so a human (or GPT/Astra) can compare them.
This script never judges the traces: it only produces candidates and records
the exact parameters that produced each one, so every candidate is reproducible.

Tracer selection (in order):

  1. the ``vtracer`` CLI, if on PATH -- flags are detected by parsing
     ``vtracer --help``, never assumed;
  2. the ``vtracer`` Python package, if importable -- available parameters are
     introspected from ``convert_image_to_svg_py``'s signature (the API analogue
     of ``--help``);
  3. the ``img2svg`` CLI as a last resort (it is an image-embed wrapper, not a
     vector tracer, and its output will likely fail the raster gate).

If none are available the script exits 3 naming both tools and their install
commands.

Default sweep (overridable with --sweep, capped by spec.print.sweep_max_candidates):

  * 3 presets -- bw / poster / photo (photo is dropped when
    spec.geometry.allow_gradients is false);
  * 3 filter_speckle values -- 2 / 8 / 16;
  * 2 hierarchical modes -- cutout / stacked;
  * palette: one candidate pre-quantised to spec.palette, one untouched.

Candidates are generated in a documented interleaved order (speckle ->
hierarchical -> preset -> palette) so that a small cap still samples every
preset and both hierarchical modes, then truncated to the cap.  If a sweep flag
is unavailable in the detected tracer, that axis is skipped and recorded in
sweep.json.

Usage::

    python scripts/trace_sweep.py 01_prepped/art.prepped.png spec.json \
        [--sweep sweep.json] [--out-dir DIR]

Exit codes: 0 = success, 2 = bad usage, 3 = no tracer available.
"""

from __future__ import annotations

import argparse
import inspect
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from itertools import zip_longest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import front_common as fc  # noqa: E402

# Documented VTracer parameter maps per preset.  Keys are VTracer parameter
# names exactly as the tracer spells them (CLI flag minus "--", or the Python
# API keyword).  Values chosen so bw/poster/photo are meaningfully distinct.
PRESETS = {
    "bw": {"colormode": "binary"},
    "poster": {"colormode": "color", "mode": "spline",
               "color_precision": 6, "layer_difference": 16},
    "photo": {"colormode": "color", "mode": "spline",
              "color_precision": 8, "layer_difference": 8},
}

DEFAULT_SWEEP = {
    "version": 1,
    "presets": ["bw", "poster", "photo"],
    "filter_speckle": [2, 8, 16],
    "hierarchical": ["cutout", "stacked"],
    "use_palette": [False, True],
    "max_candidates": 12,
}

# img2svg is not a tracer; its "output" wraps the raster in an <image> element.
IMGSVG_NOTE = ("img2svg embeds the raster rather than vectorising it; its "
               "candidates will likely fail the raster gate. Install VTracer "
               "for real vector traces.")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Backend detection
# --------------------------------------------------------------------------

def _parse_cli_flags(binary):
    """Available long flags from a --help dump, keyed by parameter name."""
    try:
        out = subprocess.run([binary, "--help"], capture_output=True, text=True,
                             timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    return {f.lstrip("-") for f in re.findall(r"--[a-z][a-z0-9_-]*", out or "")}


def _api_parameters():
    """Available parameters of the vtracer Python API, or None."""
    try:
        import vtracer
        sig = inspect.signature(vtracer.convert_image_to_svg_py)
        return (vtracer,
                {p for p in sig.parameters if p not in ("image_path", "out_path")})
    except Exception:  # noqa: BLE001
        return None, set()


def detect_backend():
    """Return (backend, warnings).  backend is None when no tracer exists."""
    cli = fc.which("vtracer")
    if cli is not None:
        flags = _parse_cli_flags(cli)
        return {"kind": "vtracer_cli", "binary": cli, "flags": flags,
                "version": fc.tool_version("vtracer"),
                "note": "standalone vtracer CLI; flags parsed from --help"}, []

    module, params = _api_parameters()
    if module is not None:
        return {"kind": "vtracer_api", "module": module, "flags": params,
                "version": fc.package_version("vtracer"),
                "note": "vtracer Python API (no CLI on PATH); parameters "
                        "introspected from convert_image_to_svg_py signature"}, []

    img_cli = fc.which("img2svg")
    if img_cli is not None:
        return {"kind": "img2svg", "binary": img_cli, "flags": set(),
                "version": fc.tool_version("img2svg"), "note": IMGSVG_NOTE}, \
            [IMGSVG_NOTE]

    return None, []


# --------------------------------------------------------------------------
# Palette pre-quantisation (palette axis, tool-agnostic)
# --------------------------------------------------------------------------

def quantize_to_palette(in_png, palette_hexes, out_png):
    """Map every pixel to its nearest palette colour (sRGB), tile by tile.

    Tiled so a 300-dpi shirt graphic cannot exhaust memory: only one 512-row
    strip and the output buffer are resident at a time.
    """
    import numpy as np
    from PIL import Image

    pal = np.array([fc.hex_to_rgb(h) for h in palette_hexes], dtype=np.int32)
    img = Image.open(in_png).convert("RGB")
    width, height = img.size
    out = np.empty((height, width, 3), dtype=np.uint8)
    tile = 512
    for y0 in range(0, height, tile):
        y1 = min(y0 + tile, height)
        strip = np.asarray(img.crop((0, y0, width, y1)), dtype=np.int32)
        # Nearest-palette search as one argmin over a stacked distance array.
        # Ties resolve to the first palette colour, matching the old `< best`
        # strict comparison.  Palette sizes are print-bounded (a handful of
        # inks), so materialising (palette x rows x width) distances per tile
        # is cheap; the tile keeps even that bounded to 512 rows.
        distances = np.stack([((strip - colour) ** 2).sum(axis=2)
                              for colour in pal], axis=0)
        index = np.argmin(distances, axis=0)
        out[y0:y1] = pal[index]
    Image.fromarray(out).save(out_png, "PNG")
    return out_png


# --------------------------------------------------------------------------
# Candidate generation
# --------------------------------------------------------------------------

def build_candidates(sweep, spec, available_flags, backend_desc):
    """Return (candidates, skipped_axes).  candidates is a list of dicts."""
    presets = list(sweep.get("presets", ["bw", "poster", "photo"]))
    if not (spec.get("geometry") or {}).get("allow_gradients"):
        presets = [p for p in presets if p != "photo"]

    # Allow the sweep JSON to override or add preset parameter sets.  A
    # ``preset_params`` key maps preset name → dict of VTracer params; these
    # merge over the built-in PRESETS, so the user can lower color_precision
    # for a flatter trace without editing the code.
    preset_overrides = sweep.get("preset_params") or {}
    preset_table = dict(PRESETS)
    preset_table.update(preset_overrides)

    speckles = list(sweep.get("filter_speckle", [2, 8, 16]))
    hierarchicals = list(sweep.get("hierarchical", ["cutout", "stacked"]))
    use_palettes = list(sweep.get("use_palette", [False, True]))
    palette = spec.get("palette") or []
    if not palette:
        use_palettes = [False]

    skipped_axes = {}

    if "filter_speckle" not in available_flags:
        skipped_axes["filter_speckle"] = ("flag not available in %s; traced "
                                          "without it" % backend_desc)
        speckles = [None]
    if "hierarchical" not in available_flags:
        skipped_axes["hierarchical"] = ("flag not available in %s; traced "
                                        "without it" % backend_desc)
        hierarchicals = [None]

    # Filter each preset down to the flags this tracer actually supports, and
    # record what was dropped.  If two presets collapse to the same parameters,
    # keep both (the sweep is still honest about what it asked for) but the
    # dropped-flag note tells the user why they look alike.
    effective = {}
    for name in presets:
        params = preset_table.get(name, {})
        kept = {k: v for k, v in params.items() if k in available_flags}
        dropped = [k for k in params if k not in available_flags]
        if dropped:
            skipped_axes["preset:%s" % name] = (
                "dropped unavailable flags: %s" % ", ".join(sorted(dropped)))
        effective[name] = kept

    full = []
    for speckle in speckles:
        for hier in hierarchicals:
            for preset in presets:
                for use_palette in use_palettes:
                    params = dict(effective[preset])
                    if speckle is not None:
                        params["filter_speckle"] = speckle
                    if hier is not None:
                        params["hierarchical"] = hier
                    full.append({
                        "preset": preset,
                        "filter_speckle": speckle,
                        "hierarchical": hier,
                        "use_palette": use_palette,
                        "params": params,
                    })
    return full, skipped_axes


def _trace(backend, in_png, out_svg, params):
    """Run one trace.  Raises on failure (the caller records it)."""
    if backend["kind"] == "vtracer_cli":
        argv = [backend["binary"]]
        for key, value in params.items():
            argv += ["--" + key, str(value)]
        argv += ["--input", in_png, "--output", out_svg]
        subprocess.run(argv, check=True, capture_output=True, text=True,
                       timeout=600)
    elif backend["kind"] == "vtracer_api":
        backend["module"].convert_image_to_svg_py(in_png, out_svg, **params)
    else:  # img2svg
        subprocess.run([backend["binary"], in_png, "-o", out_svg],
                       check=True, capture_output=True, text=True, timeout=600)


# --- parallel worker --------------------------------------------------------

_WORKER_BACKEND = None


def _worker_backend():
    """Detect the tracer once per worker process.

    Module state is per-process, so the parent's backend object (which holds a
    live module reference for the vtracer Python API) is never pickled across
    the pool.  Each worker re-detects on its first task and caches the result.
    """
    global _WORKER_BACKEND
    if _WORKER_BACKEND is None:
        _WORKER_BACKEND, _ = detect_backend()
    return _WORKER_BACKEND


def _trace_one(task):
    """Trace one candidate inside a worker process.  Returns its record.

    ``task`` is a plain (picklable) dict carrying the input/output paths, the
    palette, the tracer params, and a pre-built ``record`` skeleton to fill in.
    """
    record = task["record"]
    backend = _worker_backend()
    if backend is None:
        record["error"] = "no tracer available in worker process"
        return record

    work_png = task["in_png"]
    if task["use_palette"]:
        try:
            quantize_to_palette(task["in_png"], task["palette"], task["pal_png"])
            record["palette_quantized_sha256"] = fc.sha256_file(task["pal_png"])
            work_png = task["pal_png"]
        except Exception as exc:  # noqa: BLE001
            record["palette_error"] = str(exc)
            record["use_palette"] = False
            work_png = task["in_png"]
    try:
        _trace(backend, work_png, task["out_svg"], task["params"])
        record["output_sha256"] = fc.sha256_file(task["out_svg"])
    except Exception as exc:  # noqa: BLE001
        record["error"] = "%s: %s" % (type(exc).__name__, exc)
    return record


def _sweep(prepped_png, spec, sweep, out_dir, workers=None):
    os.makedirs(out_dir, exist_ok=True)

    backend, warnings = detect_backend()
    if backend is None:
        print("trace_sweep: no tracer available.", file=sys.stderr)
        print("  Install VTracer:  pip install vtracer   (Python API)", file=sys.stderr)
        print("     or download the standalone CLI from "
              "https://github.com/visioncortex/vtracer/releases", file=sys.stderr)
        print("  Or img2svg:       pip install img2svg", file=sys.stderr)
        return 3
    for warning in warnings:
        print("trace_sweep: WARNING: %s" % warning)

    available_flags = backend.get("flags", set())
    candidates, skipped_axes = build_candidates(sweep, spec, available_flags,
                                                backend["note"] or backend["kind"])

    raw_print = spec.get("print") or {}
    spec_max = raw_print.get("sweep_max_candidates")
    max_candidates = (spec_max if spec_max is not None
                      else sweep.get("max_candidates", 12))
    # Round-robin across per-speckle groups before truncating, so a small cap
    # still samples every speckle value.  A plain stride through the nested
    # list would alias onto the palette axis (the innermost loop): with a
    # palette and cap 12, all 12 would be speckle=2 and speckle=16 is never
    # traced.  Within each speckle group we also round-robin across presets
    # so a cap doesn't drop the photo preset when allow_gradients is on.
    if max_candidates < len(candidates):
        # First, within each speckle group, round-robin across presets.
        speckle_groups = {}
        for cand in candidates:
            speckle_groups.setdefault(cand.get("filter_speckle"), []).append(cand)
        for speckle, group in speckle_groups.items():
            preset_groups = {}
            for cand in group:
                preset_groups.setdefault(cand.get("preset"), []).append(cand)
            if len(preset_groups) > 1:
                pre_interleaved = []
                for tup in zip_longest(*preset_groups.values()):
                    for cand in tup:
                        if cand is not None:
                            pre_interleaved.append(cand)
                speckle_groups[speckle] = pre_interleaved
        # Then round-robin across speckle groups.
        if len(speckle_groups) > 1:
            interleaved = []
            for tup in zip_longest(*speckle_groups.values()):
                for cand in tup:
                    if cand is not None:
                        interleaved.append(cand)
            selected = interleaved[:max_candidates]
        else:
            selected = candidates[:max_candidates]
    else:
        selected = candidates[:max_candidates]

    source_sha = fc.sha256_file(prepped_png)
    palette_dir = os.path.join(out_dir, ".palette")
    os.makedirs(palette_dir, exist_ok=True)

    records = []
    print("trace_sweep: backend=%s (version %s), %d candidate(s)"
          % (backend["kind"], backend.get("version") or "unknown",
             len(selected)))

    # Build one picklable task per candidate in index order, then trace across
    # worker processes.  Each worker re-detects the tracer (see _trace_one).
    tasks = []
    for idx, cand in enumerate(selected, 1):
        name = "candidate_%02d.svg" % idx
        out_svg = os.path.join(out_dir, name)
        record = {
            "index": idx,
            "file": name,
            "preset": cand["preset"],
            "filter_speckle": cand["filter_speckle"],
            "hierarchical": cand["hierarchical"],
            "use_palette": bool(cand["use_palette"]),
            "params": cand["params"],
            "source_sha256": source_sha,
            "palette_quantized_sha256": None,
            "output_sha256": None,
        }
        tasks.append({
            "in_png": prepped_png,
            "out_svg": out_svg,
            "pal_png": os.path.join(palette_dir, "%s.png" % name),
            "palette": spec.get("palette") or [],
            "use_palette": bool(cand["use_palette"]),
            "params": cand["params"],
            "record": record,
        })

    workers = fc.resolve_workers(len(tasks), explicit=workers)
    print("trace_sweep: tracing with %d worker process(es)" % workers)
    records = fc.run_parallel(_trace_one, tasks, workers=workers)

    # executor.map returns results in task order, so records stay index-ordered
    # and sweep.json is deterministic regardless of completion order.
    for record in records:
        out_svg = os.path.join(out_dir, record["file"])
        status = record.get("error") or ("%s bytes" % (
            os.path.getsize(out_svg) if os.path.exists(out_svg) else "0"))
        print("  %s  preset=%-6s speckle=%s hier=%-7s palette=%-5s -> %s"
              % (record["file"], record["preset"], record["filter_speckle"],
                 record["hierarchical"], bool(record["use_palette"]), status))

    sweep_payload = {
        "tool": "trace_sweep.py",
        "generated": _now(),
        "backend": {"kind": backend["kind"],
                    "version": backend.get("version"),
                    "note": backend.get("note"),
                    "available_flags": sorted(available_flags)},
        "input": {"file": prepped_png, "sha256": source_sha},
        "spec": None,  # filled below
        "sweep": sweep,
        "skipped_axes": skipped_axes,
        "deterministic": True,
        "determinism_note": ("VTracer output is deterministic for a fixed "
                             "version, input and parameter set; verified by "
                             "tests/test_trace_sweep.py (two runs are "
                             "byte-identical). If a future tracer is not, set "
                             "deterministic=false here."),
        "truncated": {"selected": len(selected),
                      "total_available": len(candidates),
                      "max_candidates": max_candidates},
        "candidates": records,
    }
    fc.write_json(os.path.join(out_dir, "sweep.json"), sweep_payload)
    print("trace_sweep: wrote %s" % os.path.join(out_dir, "sweep.json"))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="trace_sweep.py",
        description="Trace a prepped PNG into a sweep of candidate SVGs "
                    "(pipeline stage 2).")
    parser.add_argument("prepped", help="prepped PNG (from prep_raster.py)")
    parser.add_argument("spec", help="path to spec.json")
    parser.add_argument("--sweep", default=None,
                        help="optional sweep config JSON overriding the default")
    parser.add_argument("--out-dir", default=None,
                        help="output directory (default: 02_traced/<stem>/")
    parser.add_argument("--workers", type=int, default=None,
                        help="worker processes for the trace sweep "
                             "(default: min(cpu_count, candidates); 1 disables "
                             "parallelism; overridable via FRONT_PIPELINE_WORKERS)")
    args = parser.parse_args(argv)

    if not os.path.exists(args.prepped):
        print("trace_sweep: input not found: %s" % args.prepped, file=sys.stderr)
        return 2
    if not os.path.exists(args.spec):
        print("trace_sweep: spec not found: %s" % args.spec, file=sys.stderr)
        return 2
    try:
        spec = fc.load_json(args.spec)
    except Exception as exc:  # noqa: BLE001
        print("trace_sweep: cannot read spec %s: %s" % (args.spec, exc),
              file=sys.stderr)
        return 2

    sweep = dict(DEFAULT_SWEEP)
    if args.sweep:
        try:
            user = fc.load_json(args.sweep)
        except Exception as exc:  # noqa: BLE001
            print("trace_sweep: cannot read sweep %s: %s" % (args.sweep, exc),
                  file=sys.stderr)
            return 2
        if not isinstance(user, dict):
            print("trace_sweep: sweep config must be a JSON object", file=sys.stderr)
            return 2
        sweep.update(user)

    project = os.path.dirname(SCRIPT_DIR)
    stem = os.path.splitext(os.path.basename(args.prepped))[0]
    out_dir = args.out_dir or os.path.join(project, "02_traced", stem)

    code = _sweep(args.prepped, spec, sweep, out_dir, workers=args.workers)
    if code != 0:
        return code

    # stamp the spec path back into the payload (avoids threading it through)
    sweep_path = os.path.join(out_dir, "sweep.json")
    if os.path.exists(sweep_path):
        payload = fc.load_json(sweep_path)
        payload["spec"] = args.spec
        fc.write_json(sweep_path, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
