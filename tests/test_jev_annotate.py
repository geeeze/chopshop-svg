#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for scripts/jev_annotate.py.

The annotator is an OPTIONAL add-on: it needs TYPESAFE_API_KEY and a network
round-trip to a decision service, so the default suite must run without either.
Everything here exercises the pure projection/validation functions against a
real-shaped preflight manifest; the network seam (``call_jev``) is never hit.
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
# node counts live under stats.static, colour lists are long strings).
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
    "stats": {
        "input_kind": "svg",
        "dpi": 300,
        "hard_findings": 3,
        "advisory_findings": 3,
        "rendered_distinct_colors": 64110,
        "rendered_ink_colors": 8,
        "rendered_screens": 8,
        "ink_tac_max_percent": 256.5,
        "ink_tac_limit_percent": 300.0,
        "tac_exact": False,
        "tac_is_estimate": True,
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


def test_finding_text_is_preserved_and_clipped():
    state = jev.project_manifest(REAL_SHAPED_MANIFEST)
    findings = state["findings"]
    # The human-readable ``detail`` survives (it is the decision content).
    assert findings[0]["rule"] == "COLOR_COUNT"
    assert findings[0]["detail"].startswith("Color count 3748 exceeds max 6")
    # The giant embedded colour list is clipped, not sent verbatim.
    assert len(findings[0]["detail"]) <= 244
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
    assert stats["pdf_fonts"] == {"count": 0}
    # Paths are shortened.
    assert stats["proof_path"] == "candidate_03.proof.png"
    # No giant colour list leaks into the projection.
    assert "declared_colors" not in stats
    assert "rendered_ink_hex" not in stats


def test_build_request_shapes_the_four_questions():
    state = jev.project_manifest(REAL_SHAPED_MANIFEST)
    request = jev.build_request(state)
    assert request["model"] == "jev-latest"
    assert set(request["questions"]) == {
        "dominant_issue", "next_action", "evidence_quality", "human_visual_review"
    }
    assert request["questions"]["evidence_quality"]["type"] == "score"
    assert request["questions"]["human_visual_review"]["type"] == "noul"


def test_make_annotation_validates_missing_answers():
    state = jev.project_manifest(REAL_SHAPED_MANIFEST)
    request = jev.build_request(state)
    response = {"model": "jev-1.13.0", "answers": {"dominant_issue": {"choice": "color_budget"}}}
    with pytest.raises(RuntimeError, match="missing answers"):
        jev.validate_response(response)


def test_make_annotation_ok(tmp_path):
    state = jev.project_manifest(REAL_SHAPED_MANIFEST)
    request = jev.build_request(state)
    response = {
        "model": "jev-1.13.0",
        "answers": {
            "dominant_issue": {"type": "choice", "choice": "color_budget", "confidence": 0.98},
            "next_action": {"type": "choice", "choice": "reduce_color_complexity", "confidence": 0.95},
            "evidence_quality": {"type": "score", "score": 1.2, "confidence": 0.8},
            "human_visual_review": {"type": "noul", "noul": 0.99},
        },
        "usage": {"input_tokens": 400, "output_tokens": 50},
    }
    manifest_path = tmp_path / "candidate_03.manifest.json"
    annotation = jev.make_annotation(manifest_path, state, request, response)
    assert annotation["status"] == "ok"
    assert annotation["schema"] == "chopshop-jev-annotation-0.1"
    assert annotation["answers"]["dominant_issue"]["choice"] == "color_budget"
    assert annotation["usage"]["input_tokens"] == 400
