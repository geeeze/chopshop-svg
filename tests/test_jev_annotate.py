#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for scripts/jev_annotate.py.

The annotator is an OPTIONAL add-on: it needs TYPESAFE_API_KEY and a network
round-trip to a decision service, so the default suite must run without either.
Everything here exercises the pure projection/validation/verdict functions
against a real-shaped preflight manifest; the network seam (``call_jev``) is
never hit.
"""

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
for p in (ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import jev_annotate as jev  # noqa: E402


# A manifest shaped like the real preflight.py output (findings use ``detail``,
# node counts live under stats.static, colour lists are long strings, and the
# static block carries an off-palette colour list that must never leak).
REAL_SHAPED_MANIFEST = {
    "passed": False,
    "input": {
        "file": "/repo/02_traced/00-example/candidate_03.svg",
        "kind": "svg",
        "spec": "/repo/spec.json",
    },
    "summary": {
        "hard": 3, "advisory": 3, "print_method": "screen_print",
        "require_cmyk": False, "tac_exact": False,
        "by_layer": {
            "source_validation": {"hard": 1, "advisory": 3},
            "render_preflight": {"hard": 2, "advisory": 0},
        },
    },
    "findings": [
        {"rule": "COLOR_COUNT", "severity": "hard", "layer": "source_validation",
         "detail": "Color count 3748 exceeds max 6 (used: " + ", ".join("#%06x" % i for i in range(2000)) + ")"},
        {"rule": "NODE_COUNT", "severity": "advisory", "layer": "source_validation",
         "detail": "Path #1 (no id) has 969 nodes, above the limit of 500"},
    ],
    "notes": [
        "TAC is an ESTIMATE: source is not CMYK, Ghostscript built separations on the fly.",
    ],
    "stats": {
        "input_kind": "svg",
        "dpi": 300,
        "hard_findings": 3,
        "advisory_findings": 3,
        "rendered_distinct_colors": 64110,
        "rendered_ink_colors": 8,
        "rendered_screens": 8,
        "rendered_continuous_tone": True,
        "rendered_top_inks": [{"hex": "#000000", "percent": 41.2}] * 20,
        "ink_tac_max_percent": 256.5,
        "ink_tac_limit_percent": 300.0,
        "tac_exact": False,
        "tac_is_estimate": True,
        "dark_garment_underbase": True,
        "pdf_pages": 1,
        "pdf_images": [],
        "pdf_fonts": [],
        "proof_path": "/repo/.preflight-work/candidate_03.proof.png",
        "pdf_path": "/repo/.preflight-work/candidate_03.print.pdf",
        "static": {
            "paths": 5042,
            "path_nodes_total": 92259,
            "path_nodes_max": 969,
            "paths_over_node_limit": 2,
            "color_count": 3748,
            "palette_conformance": "3748 of 3748 declared colours off palette",
            # The leak risk: a huge colour list that must NOT be copied whole.
            "off_palette_colors": ["#%06x" % i for i in range(3748)],
            "palette": ["#%06x" % i for i in range(6)],
        },
    },
}


def test_project_manifest_matches_real_schema():
    state = jev.project_manifest(REAL_SHAPED_MANIFEST)
    assert state["passed"] is False
    assert state["manifest_schema"] == "preflight-manifest"
    # Input file is shortened to its basename.
    assert state["input"]["file"] == "candidate_03.svg"
    assert state["input"]["kind"] == "svg"
    # summary carries the real keys.
    assert state["summary"]["hard"] == 3
    assert state["summary"]["by_layer"]["render_preflight"]["hard"] == 2
    # notes are scrubbed and projected.
    assert state["notes"][0].startswith("TAC is an ESTIMATE")


def test_finding_text_is_scrubbed_and_bounded():
    state = jev.project_manifest(REAL_SHAPED_MANIFEST)
    findings = state["findings"]
    # The human-readable ``detail`` survives (it is the decision content).
    assert findings[0]["rule"] == "COLOR_COUNT"
    assert findings[0]["detail"].startswith("Color count 3748 exceeds max 6")
    # The giant embedded colour list is collapsed to a gist, not sent verbatim.
    assert len(findings[0]["detail"]) <= 400
    assert "more omitted" in findings[0]["detail"]
    assert findings[1]["detail"] == "Path #1 (no id) has 969 nodes, above the limit of 500"


def test_stats_lift_node_counts_and_drop_lists():
    state = jev.project_manifest(REAL_SHAPED_MANIFEST)
    stats = state["stats"]
    # Node/geometry figures come from stats.static.
    assert stats["paths"] == 5042
    assert stats["path_nodes_total"] == 92259
    assert stats["path_nodes_max"] == 969
    assert stats["color_count"] == 3748
    assert stats["palette_conformance"].startswith("3748 of 3748")
    # Rendered metrics present.
    assert stats["rendered_distinct_colors"] == 64110
    assert stats["rendered_ink_colors"] == 8
    # Lists are reduced to counts, not sent whole.
    assert stats["pdf_images"] == {"count": 0}
    assert stats["pdf_fonts"] == {"count": 0, "embedded": 0}
    # rendered_top_inks is capped.
    assert len(stats["rendered_top_inks"]) <= 12
    # Paths are shortened.
    assert stats["proof_path"] == "candidate_03.proof.png"
    # No giant colour list leaks into the projection (regression: v0.3 copied
    # the whole stats.static dict, including off_palette_colors, verbatim).
    assert "static" not in stats
    assert "off_palette_colors" not in stats
    assert "declared_colors" not in stats


def test_projection_is_compact_vs_manifest():
    state = jev.project_manifest(REAL_SHAPED_MANIFEST)
    assert len(json.dumps(state)) < len(json.dumps(REAL_SHAPED_MANIFEST))


def test_build_request_shapes_the_four_questions():
    state = jev.project_manifest(REAL_SHAPED_MANIFEST)
    request = jev.build_request(state)
    assert request["model"] == "jev-latest"
    assert set(request["questions"]) == {
        "failure_mode", "print_route", "tonal_structure", "evidence_is_actionable"
    }
    assert request["questions"]["tonal_structure"]["type"] == "score"
    assert request["questions"]["evidence_is_actionable"]["type"] == "noul"


def test_validate_response_rejects_missing_answers():
    response = {"model": "jev-1.13.0", "answers": {"failure_mode": {"choice": "screen_explosion"}}}
    with pytest.raises(RuntimeError, match="missing answers"):
        jev.validate_response(response)


def test_compose_verdict_thresholds_confidence():
    answers = {
        "failure_mode": {"type": "choice", "choice": "screen_explosion", "confidence": 0.97},
        "print_route": {"type": "choice", "choice": "process_halftone", "confidence": 0.55},
        "tonal_structure": {"type": "score", "score": 2.1, "confidence": 0.7,
                            "legend": {"0": "Broken", "1": "Flat", "2": "Retained", "3": "Engraving-grade"}},
        "evidence_is_actionable": {"type": "noul", "noul": 0.2},
    }
    verdict = jev.compose_verdict(answers, confidence_floor=0.6)
    assert verdict["headline"] == "screen_explosion"
    assert verdict["tonal_label"] == "Retained"
    assert verdict["tonal_score"] == 2.1
    assert verdict["evidence_actionable"] is False
    # print_route was below the floor -> surfaced, not settled.
    assert "print_route" in verdict["low_confidence"]
    assert "failure_mode" not in verdict["low_confidence"]


def test_make_annotation_ok(tmp_path):
    state = jev.project_manifest(REAL_SHAPED_MANIFEST)
    request = jev.build_request(state)
    response = {
        "model": "jev-1.13.0",
        "answers": {
            "failure_mode": {"type": "choice", "choice": "screen_explosion", "confidence": 0.98},
            "print_route": {"type": "choice", "choice": "process_halftone", "confidence": 0.95},
            "tonal_structure": {"type": "score", "score": 1.2, "confidence": 0.8,
                                "legend": {"0": "Broken", "1": "Flat", "2": "Retained", "3": "Engraving-grade"}},
            "evidence_is_actionable": {"type": "noul", "noul": 0.99},
        },
        "usage": {"input_tokens": 400, "output_tokens": 50},
    }
    manifest_path = tmp_path / "candidate_03.manifest.json"
    annotation = jev.make_annotation(manifest_path, state, request, response)
    assert annotation["status"] == "ok"
    assert annotation["schema"] == "chopshop-jev-annotation-0.4"
    assert annotation["answers"]["failure_mode"]["choice"] == "screen_explosion"
    assert annotation["verdict"]["headline"] == "screen_explosion"
    assert annotation["usage"]["input_tokens"] == 400


def test_optional_without_key_is_non_blocking(tmp_path, monkeypatch):
    manifest_path = tmp_path / "candidate_03.manifest.json"
    jev._write_json(manifest_path, REAL_SHAPED_MANIFEST)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    out = tmp_path / "candidate_03.jev.json"
    assert jev.main([str(manifest_path), "--optional", "--output", str(out)]) == 0
    payload = json.loads(out.read_text())
    assert payload["status"] == "skipped"
    assert "verdict" not in payload
