#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_batch.py -- run the whole pipeline over every SVG in 00_source/.

This is the v4.0 section 8.2 exercise: a diverse batch (flat logo, complex
illustration, photographic, halftone-heavy, fine line art) pushed through both
layers, so the cases where the two gates DISAGREE can be recorded. Those
disagreements are the interesting ones: a file that passes one layer and fails
the other is exactly the gap either layer would leave if used alone.

Deliberately shells out to pipeline.sh rather than importing the modules, so the
orchestration itself is exercised.

Usage:  python3 scripts/run_batch.py [--dpi N]
Writes: 04_validated/batch_results.json
"""

import argparse
import json
import os
import subprocess
import sys

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(PROJECT, "00_source")
FINAL = os.path.join(PROJECT, "05_final")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dpi", type=int, default=None,
                    help="override the spec dpi (useful to keep a batch quick)")
    args = ap.parse_args()

    files = sorted(f for f in os.listdir(SOURCE) if f.endswith(".svg"))
    if not files:
        print("no SVGs in 00_source/", file=sys.stderr)
        return 2

    results = []
    for name in files:
        stem = os.path.splitext(name)[0]
        cmd = [os.path.join(PROJECT, "pipeline.sh"), os.path.join(SOURCE, name)]
        if args.dpi:
            cmd.append("--dpi=%d" % args.dpi)
        print("=" * 72)
        print("%s" % name)
        print("=" * 72, flush=True)

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        sys.stdout.write(proc.stdout)
        if proc.returncode not in (0, 1):
            sys.stdout.write(proc.stderr)

        manifest_path = os.path.join(FINAL, "%s.manifest.json" % stem)
        entry = {"file": name, "pipeline_exit": proc.returncode}
        if os.path.exists(manifest_path):
            with open(manifest_path) as fh:
                manifest = json.load(fh)
            findings = manifest.get("findings", [])
            entry["passed"] = manifest.get("passed")
            entry["summary"] = manifest.get("summary")
            entry["hard"] = sorted({f["rule"] for f in findings
                                    if f["severity"] == "hard"})
            entry["advisory"] = sorted({f["rule"] for f in findings
                                        if f["severity"] == "advisory"})
            entry["layer_a_hard"] = sorted({f["rule"] for f in findings
                                            if f["severity"] == "hard"
                                            and f["layer"] == "source_validation"})
            entry["layer_b_hard"] = sorted({f["rule"] for f in findings
                                            if f["severity"] == "hard"
                                            and f["layer"] == "render_preflight"})
            stats = manifest.get("stats", {})
            entry["declared_colors"] = stats.get("declared_colors")
            entry["rendered_inks"] = stats.get("rendered_ink_colors")
            entry["continuous_tone"] = stats.get("rendered_continuous_tone")
            entry["tac_max"] = stats.get("ink_tac_max_percent")
            entry["tac_exact"] = stats.get("tac_exact")
            entry["size_mm"] = stats.get("size_mm")
            entry["mm_per_unit"] = stats.get("mm_per_user_unit")
            entry["proof"] = os.path.join(FINAL, "%s.proof.png" % stem)
        else:
            entry["error"] = "no manifest produced"
        results.append(entry)

    out = os.path.join(PROJECT, "04_validated", "batch_results.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fh:
        json.dump(results, fh, indent=2)

    print()
    print("=" * 72)
    print("BATCH SUMMARY")
    print("=" * 72)
    print("%-32s %6s %6s %5s %8s %6s  %s"
          % ("file", "layerA", "layerB", "inks", "declared", "cont", "verdict"))
    for entry in results:
        if entry.get("error"):
            print("%-32s  %s" % (entry["file"], entry["error"]))
            continue
        layer_a = "FAIL" if entry["layer_a_hard"] else "pass"
        layer_b = "FAIL" if entry["layer_b_hard"] else "pass"
        disagree = layer_a != layer_b
        print("%-32s %6s %6s %5s %8s %6s  %s"
              % (entry["file"], layer_a, layer_b,
                 entry.get("rendered_inks"), entry.get("declared_colors"),
                 "yes" if entry.get("continuous_tone") else "no",
                 "<-- LAYERS DISAGREE" if disagree else ""))
    print()
    print("wrote %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
