#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""closeout.py -- print the pass/fail board for a run; never stop at the first failure.

Reads a ``run.json`` produced by ``scripts/run_record.py`` and checks every
candidate against the requirements matrix (``requirements.json``).  Like
CraftBot's Runner, it does NOT fix anything and does NOT pick a winner: it
reports, per candidate and per requirement, whether the run is complete and
where each failure lives (tracer / preflight / proof / review / Jev / human).

Exit codes:
  0  run is closed: every structural link present and every requirement
     resolved (PASS or FAIL, nothing PENDING), or the outstanding requirements
     were explicitly waived with --waive.
  1  run is incomplete: missing structural links or unresolved (PENDING)
     requirements remain.  The board above the exit line says exactly which.
  2  usage / unreadable input.

``--no-gate`` prints the board and always exits 0 (for humans who just want to
read it, not gate a closeout step).

Usage:
    .venv/bin/python scripts/closeout.py 06_run/00-example.run.json
    .venv/bin/python scripts/closeout.py 06_run/00-example.run.json --no-gate
    .venv/bin/python scripts/closeout.py 06_run/00-example.run.json --waive R-01,R-05
"""

from __future__ import annotations

import argparse
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import front_common as fc  # noqa: E402

PROJECT = os.path.dirname(SCRIPT_DIR)
DEFAULT_REQUIREMENTS = os.path.join(PROJECT, "requirements.json")

# Statuses a requirement check can produce.
PASS, FAIL, PENDING, NA = "PASS", "FAIL", "PENDING", "N/A"

_HUMAN_CHECKS = {"visual_review_present", "human_decision_present"}


def _load(path):
    try:
        return fc.load_json(path)
    except (OSError, ValueError) as exc:
        print(f"closeout: cannot read {path}: {exc}", file=sys.stderr)
        return None


def evaluate(req: dict, candidate: dict, run: dict) -> str:
    """Evaluate one requirement against one candidate."""
    chk = req.get("check") or {}
    kind = chk.get("type")
    comp = candidate.get("comparison") or {}
    back = candidate.get("back_half") or {}
    manifest = back.get("manifest") or {}
    stats = manifest.get("stats") or {}

    def comp_status(layer):
        if not comp:
            return NA
        entry = comp.get(layer) or {}
        return entry.get("status")

    if kind == "layer_a_passed":
        st = comp_status("layer_a")
        return PASS if st == "pass" else (FAIL if st else NA)
    if kind == "layer_b_passed":
        st = comp_status("layer_b")
        return PASS if st == "pass" else (FAIL if st else NA)
    if kind == "back_half_passed":
        if not back.get("run"):
            return NA
        return PASS if manifest.get("passed") is True else FAIL
    if kind == "tac_exact":
        if not back.get("run"):
            return NA
        return PASS if stats.get("tac_exact") is True else FAIL
    if kind == "no_continuous_tone":
        if not back.get("run"):
            return NA
        return PASS if stats.get("rendered_continuous_tone") is False else FAIL
    if kind == "rendered_colors_le":
        value = comp.get("rendered_ink_colors")
        if value is None:
            return NA
        return PASS if value <= int(chk["value"]) else FAIL
    if kind == "node_max_le":
        value = comp.get("node_count_max")
        if value is None:
            return NA
        return PASS if value <= int(chk["value"]) else FAIL
    if kind == "vtracer_only":
        backend = (run.get("run", {}).get("sweep", {}) or {}).get("backend") or {}
        return PASS if backend.get("kind") in ("vtracer_api", "vtracer_cli") else FAIL
    if kind == "unique":
        return PASS if not candidate.get("duplicate_of") else FAIL
    if kind == "visual_review_present":
        return PASS if (candidate.get("visual_review") or {}).get("found") else PENDING
    if kind == "human_decision_present":
        return PASS if (candidate.get("human_decision") or {}).get("found") else PENDING
    return NA


def _board_cell(status: str, width: int) -> str:
    return status.ljust(width)


def print_boards(run: dict, requirements: list[dict], waived: set[str]) -> bool:
    candidates = run.get("candidates", [])

    print("=" * 78)
    print(f"CHOPSHOP CLOSEOUT — run: {run.get('stem')}  "
          f"({len(candidates)} candidates)")
    print("=" * 78)

    # --- Per-candidate board ---
    print()
    print("Candidate board (all failures shown, never stopped early):")
    header = ("candidate".ljust(20) + "cmp LA.hard LB.hard inks nodes "
              "backhalf jev visual human")
    print(header)
    print("-" * len(header))
    for c in candidates:
        comp = c.get("comparison") or {}
        back = c.get("back_half") or {}
        la = comp.get("layer_a") or {}
        lb = comp.get("layer_b") or {}
        line = (
            c["id"].ljust(20)
            + ("Y" if comp else "-").ljust(4)
            + str(la.get("hard", "-")).ljust(8)
            + str(lb.get("hard", "-")).ljust(8)
            + str(comp.get("rendered_ink_colors", "-")).ljust(5)
            + str(comp.get("node_count_max", "-")).ljust(6)
            + ("Y" if back.get("run") else "-").ljust(9)
            + ("Y" if c.get("jev", {}).get("found") else "-").ljust(6)
            + ("Y" if c.get("visual_review", {}).get("found") else "-").ljust(7)
            + ("Y" if c.get("human_decision", {}).get("found") else "-")
        )
        print(line)

    # --- Per-requirement board ---
    print()
    print("Requirements board:")
    print(("req".ljust(6) + "".join(c["id"].ljust(14) for c in candidates)))
    print("-" * (6 + 14 * len(candidates)))
    for req in requirements:
        cells = [evaluate(req, c, run) for c in candidates]
        print(req["id"].ljust(6) + "".join(_board_cell(s, 14) for s in cells))
    print()
    for req in requirements:
        print(f"  {req['id']}: {req['requirement']}  [{req.get('category')}]")

    # --- Requirements summary + waivers ---
    print()
    print("Requirements summary:")
    pending_remaining = False
    for req in requirements:
        cells = [evaluate(req, c, run) for c in candidates]
        if req["id"] in waived:
            print(f"  {req['id']}: WAIVED")
            continue
        counts = {PASS: cells.count(PASS), FAIL: cells.count(FAIL),
                  PENDING: cells.count(PENDING), NA: cells.count(NA)}
        if counts[PENDING]:
            pending_remaining = True
        print(f"  {req['id']}: pass={counts[PASS]} fail={counts[FAIL]} "
              f"pending={counts[PENDING]} na={counts[NA]}")

    return pending_remaining


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_json", help="path to a *.run.json")
    parser.add_argument("--requirements", default=DEFAULT_REQUIREMENTS,
                        help="requirements matrix JSON (default: requirements.json)")
    parser.add_argument("--waive", default="",
                        help="comma-separated requirement ids to waive (e.g. R-01,R-05)")
    parser.add_argument("--no-gate", action="store_true",
                        help="print the board and exit 0 regardless of completeness")
    args = parser.parse_args(argv)

    run = _load(args.run_json)
    if run is None:
        return 2
    reqs = _load(args.requirements)
    if reqs is None:
        return 2
    requirements = reqs.get("requirements") or []
    waived = {w.strip() for w in args.waive.split(",") if w.strip()}

    pending_remaining = print_boards(run, requirements, waived)

    # --- Closeout board (run-level spine) ---
    run_meta = run.get("run", {})
    print()
    print("Closeout (run spine):")
    lines = [
        ("source provenance", "source", "found"),
        ("prep record", "prep", "found"),
        ("sweep manifest", "sweep", "found"),
        ("comparison", "comparison", "found"),
    ]
    spine_ok = True
    for label, section, flag in lines:
        ok = bool((run_meta.get(section) or {}).get(flag))
        spine_ok = spine_ok and ok
        print(f"  {label}: {'PASS' if ok else 'FAIL'}")

    candidates = run.get("candidates", [])
    n_back = sum(1 for c in candidates if c["back_half"]["run"])
    n_jev = sum(1 for c in candidates if c["jev"]["found"])
    n_vis = sum(1 for c in candidates if c["visual_review"]["found"])
    n_hum = sum(1 for c in candidates if c["human_decision"]["found"])
    print(f"  back-half runs: {n_back}/{len(candidates)}")
    print(f"  visual reviews: {n_vis}/{len(candidates)}")
    print(f"  Jev annotations: {n_jev}/{len(candidates)}")
    print(f"  human decisions: {n_hum}/{len(candidates)}")

    run_missing = run_meta.get("missing_links") or []
    candidate_missing = sum(len(c.get("missing_links") or []) for c in candidates)
    candidate_open = sum(len(c.get("open_items") or []) for c in candidates)
    if run_missing:
        print("  run-level missing links:")
        for m in run_missing:
            print(f"    - {m}")
    print(f"  candidate blocking missing-link count: {candidate_missing}")
    print(f"  candidate open items (not-yet-chosen/reviewed/optional): {candidate_open}")

    # Production-ready = passed both layers per comparison, human-labelled production.
    n_prod = 0
    for c in candidates:
        comp = c.get("comparison") or {}
        decision = c.get("human_decision") or {}
        if comp.get("passed") and decision.get("label") == "production":
            n_prod += 1
    print(f"  production-ready candidates: {n_prod}")

    complete = (spine_ok and not run_missing and candidate_missing == 0
                and not pending_remaining)
    if args.no_gate:
        print("\ncloseout: --no-gate, board printed (no completeness gate).")
        return 0
    if complete:
        print("\ncloseout: PASS — run is closed.")
        return 0
    print("\ncloseout: INCOMPLETE — see outstanding links/requirements above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
