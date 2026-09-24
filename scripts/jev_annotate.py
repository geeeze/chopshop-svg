#!/usr/bin/env python3
"""Annotate a chopshop preflight manifest with a Jev decision sidecar.

OPTIONAL ADD-ON.  This is not part of the deterministic pipeline and it is
never run by default: it calls an external decision service that needs an API
key, so it is opt-in only.  `pipeline.sh`, `compare_candidates.py`, and the
other stages do not invoke it.

It is deliberately additive: it does not modify the manifest produced by
preflight.py and it does not choose a winning candidate.  It asks four narrow,
advisory questions, folds the answers into a thresholded verdict, and writes a
separate `<stem>.jev.json` sidecar.

Usage:
    python3 scripts/jev_annotate.py 05_final/artwork.manifest.json --dry-run
    python3 scripts/jev_annotate.py manifest.json --optional   # skip w/o a key
    TYPESAFE_API_KEY=... python3 scripts/jev_annotate.py manifest.json

The live call uses the official TypeSafe endpoint and reads TYPESAFE_API_KEY
from the environment. The API request contains only the compact projection
created by project_manifest(); it never sends the full manifest, and long
colour lists are collapsed to a gist before they leave the machine.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
ANNOTATION_SCHEMA = "chopshop-jev-annotation-0.4"
DEFAULT_CONFIDENCE_FLOOR = 0.6

# Long runs of hex codes appear verbatim inside preflight finding details and
# inside declared_colors. They are meaningless to a decision and would blow the
# token budget, so they are collapsed before the state is sent.
_HEX = re.compile(r"#[0-9a-fA-F]{6}\b")
_DETAIL_LIMIT = 400
_NOTE_LIMIT = 400
_TOP_INKS_LIMIT = 12

# These are intentionally narrow, atomic questions. Each asks one bounded
# judgment; the surrounding code decides what to do with it. No question asks
# Jev to rank or select a candidate.
QUESTIONS: dict[str, dict[str, Any]] = {
    "failure_mode": {
        "type": "choice",
        "instructions": (
            "Based only on the supplied preflight measurements and finding "
            "details, which failure signature best describes this candidate?"
        ),
        "criteria": {
            "screen_explosion": "The rendered or declared artwork needs far more distinct colours or screens than the contract allows.",
            "continuous_tone": "A smooth tonal transition or photographic ramp was traced, which flat-colour printing cannot reproduce.",
            "geometry_overload": "Paths carry far more nodes than the limit, making the file slow to RIP and hard to hand-edit.",
            "palette_mismatch": "Declared colours fall outside the contract palette.",
            "render_or_tool_failure": "The proof or PDF could not be produced, or a tool was missing.",
            "no_hard_failure": "No hard finding is present in the supplied measurements.",
            "unclear": "The supplied measurements do not identify one clear failure signature.",
        },
    },
    "print_route": {
        "type": "choice",
        "instructions": "Which production route best fits this artwork as measured?",
        "criteria": {
            "screen_spot_low": "Spot-colour screen printing within the contract colour budget.",
            "screen_spot_multi": "Spot-colour screen printing, but above the contract budget and needing more screens or a redesign.",
            "process_halftone": "Four-colour process or simulated-process printing is required because tone must be reproduced.",
            "dtg_or_dtf": "Direct-to-garment or direct-to-film fits better than screen printing for this artwork.",
            "unsuitable": "No route reproduces this artwork as measured without rework.",
            "unclear": "The measurements do not determine a production route.",
        },
    },
    "tonal_structure": {
        "type": "score",
        "instructions": (
            "How faithfully did the trace preserve the source's tonal structure, "
            "judging only from the measured colour count, ramp and coverage numbers?"
        ),
        "criteria": [
            "Broken: tone collapsed into noise or non-printing blends.",
            "Flat: tonal range reduced to a few hard steps with visible banding.",
            "Retained: a coherent tonal read survives in flat colours.",
            "Engraving-grade: dense, ordered tonal structure suitable as a deliberate screen/engraving look.",
        ],
    },
    "evidence_is_actionable": {
        "type": "noul",
        "instructions": (
            "Do the supplied measurements contain enough reliable evidence to act "
            "on a production decision, given the stated limitations?"
        ),
        "criteria": {
            "true": "The source, render and PDF evidence are present and their stated limitations do not block a production decision.",
            "false": "A required measurement is missing, failed, or is an estimate whose limitation blocks a production decision.",
        },
    },
}


def _scrub(text: str, limit: int) -> str:
    """Collapse long hex runs and truncate long free text."""
    if not isinstance(text, str):
        return str(text)
    matches = _HEX.findall(text)
    if len(matches) > 8:
        kept = ", ".join(matches[:6])
        text = _HEX.sub("", text)
        text = re.sub(r"[,\s]{2,}", " ", text).strip()
        text = f"{text} [colours: {kept} … {len(matches) - 6} more omitted]"
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _short_path(value: Any) -> Any:
    """Remove machine-specific path prefixes while retaining useful names."""
    if not isinstance(value, str):
        return value
    if "/" in value or "\\" in value:
        return Path(value).name
    return value


def _compact_finding(finding: Any) -> dict[str, Any]:
    """Keep the classifier fields and a scrubbed human-readable detail.

    The real preflight manifest calls the human-readable text ``detail``;
    ``message``/``note`` are kept as aliases for older or external producers.
    """
    if not isinstance(finding, dict):
        return {"detail": _scrub(str(finding), _DETAIL_LIMIT)}
    result: dict[str, Any] = {}
    for key in ("detail", "message", "note"):
        if key in finding:
            result["detail"] = _scrub(str(finding[key]), _DETAIL_LIMIT)
            break
    for key in (
        "rule", "code", "severity", "layer", "value", "actual",
        "limit", "expected", "path", "field", "kind",
    ):
        if key in finding:
            result[key] = _short_path(finding[key]) if key == "path" else finding[key]
    if not result:
        # Keep an unknown finding inspectable but bounded.
        result = {str(k): v for k, v in list(finding.items())[:8]}
    return result


def _compact_top_inks(value: Any) -> Any:
    if isinstance(value, list):
        return value[:_TOP_INKS_LIMIT]
    return value


def _compact_stats(stats: Any) -> dict[str, Any]:
    """Project the real preflight stats object down to decision-useful scalars.

    Large colour lists are intentionally excluded (the counts suffice); node
    and geometry figures live in ``stats.static`` and are lifted out below.
    """
    if not isinstance(stats, dict):
        return {}
    scalars = [
        "input_kind", "dpi", "proof_pixels",
        "hard_findings", "advisory_findings",
        "rendered_distinct_colors", "rendered_significant_colors",
        "rendered_ink_colors", "rendered_colored_inks", "rendered_screens",
        "rendered_white_screen", "rendered_white_treated_as",
        "rendered_continuous_tone", "rendered_ramp_colors",
        "rendered_largest_ramp_percent", "rendered_diffuse_tone",
        "rendered_diffuse_colors", "rendered_diffuse_area_percent",
        "rendered_blends_absorbed", "rendered_area_threshold_percent",
        "rendered_merge_tolerance", "rendered_blend_tolerance",
        "ink_tac_max_percent", "ink_tac_mean_percent",
        "ink_tac_area_over_limit_percent", "ink_tac_limit_percent",
        "ink_profile_used", "tac_exact", "tac_is_estimate",
        "white_counted_as_ink", "dark_garment_underbase",
        "pdf_pages", "pdf_page_size", "pdf_color_spaces",
        "ink_plate_means_percent", "render_error", "pdf_error",
    ]
    result: dict[str, Any] = {k: stats[k] for k in scalars if k in stats}
    if "pdf_images" in stats and isinstance(stats["pdf_images"], list):
        result["pdf_images"] = {"count": len(stats["pdf_images"])}
    if "pdf_fonts" in stats and isinstance(stats["pdf_fonts"], list):
        fonts = stats["pdf_fonts"]
        result["pdf_fonts"] = {
            "count": len(fonts),
            "embedded": sum(1 for f in fonts if isinstance(f, dict) and f.get("embedded")),
        }
    if "pdf_image_masks" in stats and isinstance(stats["pdf_image_masks"], list):
        result["pdf_image_masks"] = {"count": len(stats["pdf_image_masks"])}
    if "rendered_top_inks" in stats:
        result["rendered_top_inks"] = _compact_top_inks(stats["rendered_top_inks"])
    if "proof_path" in stats:
        result["proof_path"] = _short_path(stats["proof_path"])
    if "pdf_path" in stats:
        result["pdf_path"] = _short_path(stats["pdf_path"])
    # Node/geometry and colour-conformance figures live under stats.static in
    # the real manifest; lift the decision-relevant ones to the top level. The
    # ``static`` dict itself is never copied -- it can contain a full
    # off-palette colour list (thousands of hexes).
    static = stats.get("static")
    if isinstance(static, dict):
        for key in ("paths", "path_nodes_total", "path_nodes_max",
                    "paths_over_node_limit", "color_count",
                    "palette_conformance"):
            if key in static:
                result[key] = static[key]
    return result


def project_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Project a full preflight manifest into a compact Jev state object."""
    source = manifest.get("input") if isinstance(manifest.get("input"), dict) else {}
    summary = manifest.get("summary") if isinstance(manifest.get("summary"), dict) else {}
    findings = manifest.get("findings") if isinstance(manifest.get("findings"), list) else []
    notes = manifest.get("notes") if isinstance(manifest.get("notes"), list) else []

    input_projection = {k: source[k] for k in ("kind",) if k in source}
    if "file" in source:
        input_projection["file"] = _short_path(source["file"])
    if "spec" in source:
        input_projection["spec"] = _short_path(source["spec"])

    summary_projection = {
        k: summary[k] for k in (
            "hard", "advisory", "by_layer", "print_method",
            "gradient_handling", "require_cmyk", "tac_exact",
        ) if k in summary
    }

    return {
        "manifest_schema": "preflight-manifest",
        "passed": bool(manifest.get("passed")),
        "input": input_projection,
        "summary": summary_projection,
        "findings": [_compact_finding(item) for item in findings],
        "stats": _compact_stats(manifest.get("stats")),
        "notes": [_scrub(n, _NOTE_LIMIT) for n in notes],
    }


def build_request(state: dict[str, Any], model: str = DEFAULT_MODEL) -> dict[str, Any]:
    """Build the documented TypeSafe System One request."""
    return {"model": model, "state": state, "questions": copy.deepcopy(QUESTIONS)}


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")


def call_jev(
    request: dict[str, Any],
    *,
    api_key: str,
    endpoint: str = DEFAULT_ENDPOINT,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Make one non-retried JSON request to Jev.

    No automatic retry: a timeout or a truncated response may already have been
    charged, and a blind retry could double-bill or mask an unknown outcome.
    """
    body = json.dumps(request, separators=(",", ":")).encode("utf-8")
    http_request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "chopshop-svg-jev-annotator/0.4",
        },
    )
    try:
        with urllib.request.urlopen(http_request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"Jev HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Jev connection failed: {exc.reason}") from exc
    try:
        result = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("Jev returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Jev returned a non-object JSON response")
    return result


def validate_response(response: dict[str, Any]) -> dict[str, Any]:
    """Validate the response envelope without pretending semantic correctness."""
    answers = response.get("answers")
    if not isinstance(answers, dict):
        raise RuntimeError("Jev response has no answers object")
    missing = sorted(set(QUESTIONS) - set(answers))
    if missing:
        raise RuntimeError("Jev response is missing answers: " + ", ".join(missing))
    return answers


def _fraction_label(score: Any, legend: Any) -> Any:
    """Map a possibly-fractional score onto the nearest described level."""
    if not isinstance(legend, dict) or score is None:
        return None
    try:
        index = int(round(float(score)))
    except (TypeError, ValueError):
        return None
    return legend.get(str(index)) or legend.get(index)


def compose_verdict(answers: dict[str, Any], *, confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR) -> dict[str, Any]:
    """Turn the raw typed answers into a compact, thresholded verdict.

    The verdict states what Jev judged and how sure it was; it never ranks
    candidates and never overrides the deterministic gates. Any choice answer
    below the confidence floor is surfaced in low_confidence instead of being
    reported as a settled fact.
    """
    verdict = {
        "headline": None,
        "print_route": None,
        "tonal_label": None,
        "tonal_score": None,
        "evidence_actionable": None,
        "low_confidence": [],
        "confidence_floor": confidence_floor,
    }

    failure = answers.get("failure_mode") or {}
    if failure.get("choice"):
        verdict["headline"] = failure["choice"]
        if failure.get("confidence", 1.0) < confidence_floor:
            verdict["low_confidence"].append("failure_mode")

    route = answers.get("print_route") or {}
    if route.get("choice"):
        verdict["print_route"] = route["choice"]
        if route.get("confidence", 1.0) < confidence_floor:
            verdict["low_confidence"].append("print_route")

    tonal = answers.get("tonal_structure") or {}
    if tonal.get("score") is not None:
        verdict["tonal_score"] = round(float(tonal["score"]), 2)
        verdict["tonal_label"] = _fraction_label(tonal["score"], tonal.get("legend"))
        if tonal.get("confidence", 1.0) < confidence_floor:
            verdict["low_confidence"].append("tonal_structure")

    evidence = answers.get("evidence_is_actionable") or {}
    if evidence.get("noul") is not None:
        # A noul has no separate confidence field; the probability is the answer.
        verdict["evidence_actionable"] = bool(evidence["noul"] >= 0.5)

    return verdict


def make_annotation(
    manifest_path: Path,
    state: dict[str, Any],
    request: dict[str, Any],
    response: dict[str, Any],
) -> dict[str, Any]:
    answers = validate_response(response)
    return {
        "schema": ANNOTATION_SCHEMA,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_manifest": manifest_path.name,
        "model": response.get("model", request.get("model")),
        "status": "ok",
        "state": state,
        "verdict": compose_verdict(answers),
        "questions": request["questions"],
        "answers": answers,
        "usage": response.get("usage"),
    }


def make_skipped_annotation(manifest_path: Path, state: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "schema": ANNOTATION_SCHEMA,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_manifest": manifest_path.name,
        "model": DEFAULT_MODEL,
        "status": "skipped",
        "reason": reason,
        "state": state,
        "questions": QUESTIONS,
    }


def default_output(manifest_path: Path) -> Path:
    name = manifest_path.name
    if name.endswith(".manifest.json"):
        name = name[: -len(".manifest.json")] + ".jev.json"
    else:
        name = manifest_path.stem + ".jev.json"
    return manifest_path.with_name(name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="preflight manifest JSON")
    parser.add_argument("--output", type=Path, help="sidecar path; defaults to <stem>.jev.json")
    parser.add_argument("--endpoint", default=os.getenv("TYPESAFE_ENDPOINT", DEFAULT_ENDPOINT))
    parser.add_argument("--model", default=os.getenv("TYPESAFE_MODEL", DEFAULT_MODEL))
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--api-key", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", help="write the request envelope without calling Jev")
    parser.add_argument("--optional", action="store_true", help="write status=skipped and exit 0 when no key or API call is available")
    args = parser.parse_args(argv)

    try:
        manifest = _read_json(args.manifest)
        state = project_manifest(manifest)
        request = build_request(state, args.model)
        output = args.output or default_output(args.manifest)

        if args.dry_run:
            _write_json(output, {
                "schema": ANNOTATION_SCHEMA,
                "status": "dry_run",
                "source_manifest": args.manifest.name,
                "model": args.model,
                "request": request,
            })
            print(f"Jev dry-run request: {output}")
            return 0

        api_key = args.api_key or os.getenv("TYPESAFE_API_KEY")
        if not api_key:
            reason = "TYPESAFE_API_KEY is not set"
            if args.optional:
                _write_json(output, make_skipped_annotation(args.manifest, state, reason))
                print(f"Jev skipped: {output}")
                return 0
            print(f"error: {reason}; use --dry-run or --optional", file=sys.stderr)
            return 2

        try:
            response = call_jev(request, api_key=api_key, endpoint=args.endpoint, timeout=args.timeout)
            annotation = make_annotation(args.manifest, state, request, response)
        except RuntimeError as exc:
            if not args.optional:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            annotation = make_skipped_annotation(args.manifest, state, str(exc))
            annotation["endpoint"] = args.endpoint
        _write_json(output, annotation)
        print(f"Jev annotation: {output}")
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
