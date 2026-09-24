#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pick_finish.py -- the semi-interactive tail of the front pipeline.

After ``front_pipeline.sh`` has produced a comparison report, this script takes
over the "choose a candidate" step and turns it into a loop:

  1. print a compact menu of every candidate (sweep params, fidelity, gates),
  2. wait for the user to pick up to 3 candidates,
  3. run the BACK half (``pipeline.sh``) on each pick -- full Layer A + Layer B,
     proof.png, print.pdf, manifest.json,
  4. loop back to the menu so the user can pick more, or quit.

It never judges the traces and never picks a winner: the human does.  The only
loop is "pick -> validate -> pick again".  Re-tracing with new parameters means
re-running ``front_pipeline.sh``.

Usage::

    python scripts/pick_finish.py 02_traced/<stem> [spec.json]

Or let the orchestrator drive it::

    ./front_pipeline.sh artwork.png --loop

Exit codes: 0 = clean exit (user quit), 2 = bad usage.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
for p in (ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import front_common as fc  # noqa: E402

MAX_PICKS = 3
QUIT_TOKENS = {"q", "quit", "exit", "done"}


def find_comparison(traced_dir):
    """Path to the comparison JSON for this traced dir, or None."""
    stem = os.path.basename(os.path.normpath(traced_dir))
    candidate = os.path.join(ROOT, "04_validated", "%s.comparison.json" % stem)
    if os.path.exists(candidate):
        return candidate
    return None


def load_candidates(traced_dir):
    """(candidates, comparison_path).  candidates is [] when no report yet."""
    comp = find_comparison(traced_dir)
    if comp is None:
        return [], None
    try:
        payload = fc.load_json(comp)
        return payload.get("candidates", []), comp
    except Exception:  # noqa: BLE001
        return [], comp


def _fid(cand):
    fid = cand.get("fidelity") or {}
    if not fid.get("measured"):
        return "-", "-"
    mae = fid.get("mae")
    mae_art = fid.get("mae_art")
    return ("%.2f" % mae if mae is not None else "-",
            "%.2f" % mae_art if mae_art is not None else "-")


def _status(layer):
    s = (layer or {}).get("status")
    if s == "pass":
        return "PASS"
    if s == "fail":
        return "FAIL"
    if s == "error":
        return "ERR"
    return "n/a"


def format_menu(candidates):
    """Return the menu text (list of lines) for the given candidates."""
    lines = []
    lines.append("Candidates (pick up to %d, space-separated, or 'q' to quit):"
                 % MAX_PICKS)
    header = ("  %-3s %-17s %-7s %-3s %-7s %-4s %-7s %-7s %-5s %-5s %-4s %-3s"
              % ("#", "file", "preset", "spk", "hier", "pal",
                 "MAE-all", "MAE-art", "A", "B", "hard", "adv"))
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))
    for idx, cand in enumerate(candidates, 1):
        sweep = cand.get("sweep") or {}
        mae_all, mae_art = _fid(cand)
        lines.append(
            "  %-3d %-17s %-7s %-3s %-7s %-4s %-7s %-7s %-5s %-5s %-4d %-3d"
            % (idx, cand.get("file"),
               sweep.get("preset", "-"),
               str(sweep.get("filter_speckle", "-")),
               sweep.get("hierarchical", "-"),
               "y" if sweep.get("use_palette") else "-",
               mae_all, mae_art,
               _status(cand.get("layer_a")), _status(cand.get("layer_b")),
               cand.get("hard", 0), cand.get("advisory", 0)))
    return lines


def parse_picks(line, n_candidates):
    """Parse a user line into (kind, picks).  kind is 'quit', 'invalid', or
    'ok'.  picks is a list of 1-based candidate indices."""
    text = (line or "").strip().lower()
    if text in QUIT_TOKENS:
        return "quit", []
    if not text:
        return "invalid", []
    tokens = text.replace(",", " ").split()
    picks = []
    for tok in tokens:
        try:
            idx = int(tok)
        except ValueError:
            return "invalid", []
        if idx < 1 or idx > n_candidates:
            return "invalid", []
        if idx not in picks:
            picks.append(idx)
    if not picks:
        return "invalid", []
    if len(picks) > MAX_PICKS:
        return "invalid", picks[:MAX_PICKS]
    return "ok", picks


def run_back_half(svg_path, spec_path):
    """Run pipeline.sh on one candidate, streaming its output live."""
    pipeline = os.path.join(ROOT, "pipeline.sh")
    env = dict(os.environ)
    env["SPEC"] = spec_path
    print("\n" + "=" * 72)
    print("pipeline.sh %s" % svg_path)
    print("=" * 72)
    return subprocess.run([pipeline, svg_path], env=env).returncode


def interactive_loop(traced_dir, spec_path):
    """The pick -> validate -> pick loop.  Reads stdin, writes to stdout."""
    candidates, comp_path = load_candidates(traced_dir)
    if not candidates:
        print("pick_finish: no candidates in %s" % traced_dir)
        if comp_path is None:
            print("  run ./front_pipeline.sh <raster> first to produce them")
        else:
            print("  the comparison report has no candidate entries: %s"
                  % comp_path)
        return 0

    n = len(candidates)
    print("pick_finish: %d candidates; report: %s" % (n, comp_path))
    print()
    while True:
        for line in format_menu(candidates):
            print(line)
        print()
        try:
            line = input("Pick 1-%d candidate(s), or 'q' to quit: " % MAX_PICKS)
        except EOFError:
            print()
            return 0
        kind, picks = parse_picks(line, n)
        if kind == "quit":
            print("done.")
            return 0
        if kind == "invalid":
            if picks:
                print("too many -- max %d picks (got %d)."
                      % (MAX_PICKS, len(picks)))
            else:
                print("unrecognised -- enter candidate numbers (1-%d) or 'q'."
                      % n)
            continue
        for idx in picks:
            cand = candidates[idx - 1]
            svg_path = os.path.join(traced_dir, cand["file"])
            code = run_back_half(svg_path, spec_path)
            print("pipeline.sh exit: %d" % code)
        print()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="pick_finish.py",
        description="Semi-interactive tail of the front pipeline: pick up to 3 "
                    "candidates and run pipeline.sh on each, in a loop.")
    parser.add_argument("traced_dir", help="directory of candidate SVGs "
                                           "(02_traced/<stem>/")
    parser.add_argument("spec", nargs="?", default=None,
                        help="path to spec.json (default: <project>/spec.json)")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.traced_dir):
        print("pick_finish: not a directory: %s" % args.traced_dir,
              file=sys.stderr)
        return 2

    spec_path = args.spec or os.path.join(ROOT, "spec.json")
    if not os.path.exists(spec_path):
        print("pick_finish: spec not found: %s" % spec_path, file=sys.stderr)
        return 2

    return interactive_loop(args.traced_dir, spec_path)


if __name__ == "__main__":
    sys.exit(main())
