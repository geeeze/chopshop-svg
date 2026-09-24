#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for scripts/pick_finish.py -- the pure menu/parse logic.

Only the non-interactive pieces are exercised: menu formatting and pick-line
parsing.  The interactive loop and pipeline.sh invocation are integration
behaviour, not unit-tested here.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
for p in (ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import pick_finish  # noqa: E402


def _candidate(file, preset="bw", speckle=2, hier="cutout", pal=False,
               hard=0, advisory=0, a_status="pass", b_status="pass",
               mae=1.0, mae_art=5.0):
    return {
        "file": file,
        "sweep": {"preset": preset, "filter_speckle": speckle,
                  "hierarchical": hier, "use_palette": pal},
        "fidelity": {"measured": True, "mae": mae, "mae_art": mae_art},
        "layer_a": {"status": a_status},
        "layer_b": {"status": b_status},
        "hard": hard, "advisory": advisory,
    }


def test_format_menu_lists_every_candidate():
    cands = [_candidate("candidate_01.svg"),
             _candidate("candidate_02.svg", preset="poster", pal=True,
                        hard=2)]
    lines = pick_finish.format_menu(cands)
    text = "\n".join(lines)
    assert "candidate_01.svg" in text
    assert "candidate_02.svg" in text
    assert "poster" in text
    assert "MAE-all" in text and "MAE-art" in text


def test_parse_picks_valid():
    kind, picks = pick_finish.parse_picks("1 3", 12)
    assert kind == "ok"
    assert picks == [1, 3]


def test_parse_picks_dedupe_and_order():
    kind, picks = pick_finish.parse_picks("3, 1, 3", 12)
    assert kind == "ok"
    assert picks == [3, 1]


def test_parse_picks_quit():
    kind, picks = pick_finish.parse_picks("q", 12)
    assert kind == "quit"
    assert picks == []
    kind, _ = pick_finish.parse_picks("done", 12)
    assert kind == "quit"


def test_parse_picks_rejects_out_of_range():
    kind, picks = pick_finish.parse_picks("13", 12)
    assert kind == "invalid"
    assert picks == []


def test_parse_picks_rejects_non_number():
    kind, picks = pick_finish.parse_picks("abc", 12)
    assert kind == "invalid"


def test_parse_picks_rejects_empty():
    kind, picks = pick_finish.parse_picks("", 12)
    assert kind == "invalid"
