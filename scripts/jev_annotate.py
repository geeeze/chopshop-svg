#!/usr/bin/env python3
"""Annotate a chopshop preflight manifest with a Jev decision sidecar.

OPTIONAL ADD-ON.  This is not part of the deterministic pipeline and it is
never run by default: it calls an external decision service that needs an API
key, so it is opt-in only.  `pipeline.sh`, `compare_candidates.py`, and the
other stages do not invoke it.

It is deliberately additive: it does not modify the manifest produced by
preflight.py and it does not choose a winning candidate.  It asks four narrow,
advisory questions and writes a separate `<stem>.jev.json` sidecar.

Usage:
    python3 scripts/jev_annotate.py 05_final/artwork.manifest.json --dry-run
    python3 scripts/jev_annotate.py manifest.json --optional   # skip w/o a key
    TYPESAFE_API_KEY=... python3 scripts/jev_annotate.py manifest.json

The live call uses the official TypeSafe endpoint and reads TYPESAFE_API_KEY
from the environment. The API request contains only the compact projection
created by project_manifest(); it never sends the full manifest or file paths
that are not useful to the decision.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
ANNOTATION_SCHEMA = "chopshop-jev-annotation-0.1"

# These are intentionally narrow, atomic questions. They are advisory labels,
# not a replacement for the deterministic gates or human visual review.
QUESTIONS: dict[str, dict[str, Any]] = {
    "dominant_issue": {
        "type": "choice",
        "instructions": (
            "Which single issue is the most useful next engineering action for "
            "this candidate, based only on the supplied measurements?"
        ),
        "criteria": {
            "color_budget": "Rendered or declared colour count is the main blocker.",
            "palette": "The candidate's colours do not match the declared palette.",
            "node_complexity": "Path node count or geometric complexity is the main blocker.",
            "stroke_geometry": "Stroke width or open/invalid geometry is the main blocker.",
            "raster_or_font": "Embedded raster resolution or font embedding is the main blocker.",
            "coverage_or_color_space": "Ink coverage or colour-space evidence is the main blocker.",
            "render_or_pdf": "Rendering or PDF evidence is missing or failed.",
            "no_hard_issue": "No hard issue is present in the supplied measurements.",
            "unclear": "The supplied measurements do not identify one dominant issue.",
        },
    },
    "next_action": {
        "type": "choice",
        "instructions": "What should a human or a deterministic script inspect next?",
        "criteria": {
            "reduce_color_complexity": "Reduce or restructure the number of colours or tonal steps.",
            "simplify_paths": "Reduce path nodes or split/rebuild complex geometry.",
            "snap_or_rework_palette": "Snap colours or redesign the palette against the job contract.",
            "fix_geometry": "Fix strokes, open paths, embedded assets, or invalid SVG geometry.",
            "inspect_proof_visually": "Inspect the rendered proof because metrics alone are insufficient.",
            "inspect_pdf_evidence": "Inspect PDF colour-space, coverage, font, or render evidence.",
            "keep_as_style_reference": "Keep this as a style/reference candidate, not a production file.",
            "no_action": "No further action is indicated by the supplied evidence.",
            "unclear": "The evidence is insufficient to recommend one next action.",
        },
    },
    "evidence_quality": {
        "type": "score",
        "instructions": "How complete and decision-useful is the supplied preflight evidence?",
        "criteria": [
            "Incomplete: rendering or manifest evidence is missing or failed.",
            "Limited: some metrics exist, but important claims are estimates or absent.",
            "Useful: source and render metrics are present, with known limitations.",
            "Strong: source, render, PDF, and toolchain evidence are all present and coherent.",
        ],
    },
    "human_visual_review": {
        "type": "noul",
        "instructions": (
            "Should a human inspect the proof visually before this candidate is used "
            "for a production decision?"
        ),
        "criteria": {
            "true": "The metrics cannot establish visual quality, style coherence, or print appearance by themselves.",
            "false": "The supplied evidence is sufficient without visual inspection for the intended decision.",
        },
    },
}


def _short_path(value: Any) -> Any:
    """Remove machine-specific path prefixes while retaining useful names."""
    if not isinstance(value, str):
        return value
    if "/" in value or "\\" in value:
        return Path(value).name
    return value


def _clip(value: Any, limit: int = 240) -> Any:
    """Bound a long string so a single finding cannot blow up the Jev payload.

    The real preflight manifest embeds whole colour lists inside a finding's
    ``detail`` text; the decision needs the gist, not thousands of hex values.
    """
    if not isinstance(value, str):
        return value
    return value if len(value) <= limit else value[:limit] + "\u2026"


def _pick(mapping: dict[str, Any], *keys: str) -> dict[str, Any]:
    """Copy selected keys when present, preserving zero and false values."""
    return {key: mapping[key] for key in keys if key in mapping}


def _compact_finding(finding: Any) -> dict[str, Any]:
    if not isinstance(finding, dict):
        return {"detail": _clip(str(finding))}
    result: dict[str, Any] = {}
    # The real preflight manifest calls the human-readable text ``detail``;
    # ``message``/``note`` are kept as aliases for older or external producers.
    for key in ("detail", "message", "note"):
        if key in finding:
            result["detail"] = _clip(finding[key])
            break
    for key in (
        "rule", "code", "severity", "layer", "value", "actual",
        "limit", "expected", "path", "field", "kind",
    ):
        if key in finding:
            result[key] = _short_path(finding[key]) if key in {"path"} else finding[key]
    if not result:
        # Keep an unknown finding inspectable but bounded.
        result = {str(k): v for k, v in list(finding.items())[:8]}
    return result


def _compact_stats(stats: Any) -> dict[str, Any]:
    if not isinstance(stats, dict):
        return {}
    # These are the measurements useful for decisions, matched to the real
    # preflight manifest. Large colour/font/image lists are intentionally
    # excluded (the counts suffice); node/geometry figures live in
    # ``stats.static`` and are lifted out below.
    allowed = {
        "input_kind", "dpi",
        "hard_findings", "advisory_findings",
        "rendered_distinct_colors", "rendered_ink_colors", "rendered_screens",
        "rendered_colored_inks", "rendered_continuous_tone",
        "rendered_diffuse_tone", "rendered_diffuse_area_percent",
        "rendered_largest_ramp_percent", "rendered_significant_colors",
        "rendered_white_screen",
        "ink_tac_max_percent", "ink_tac_mean_percent",
        "ink_tac_area_over_limit_percent", "ink_tac_limit_percent",
        "tac_exact", "tac_is_estimate", "ink_plate_means_percent",
        "pdf_pages", "pdf_page_size", "pdf_color_spaces",
        "pdf_images", "pdf_fonts", "proof_pixels",
        "proof_path", "pdf_path", "render_error", "pdf_error",
    }
    result: dict[str, Any] = {}
    for key in allowed:
        if key not in stats:
            continue
        value = stats[key]
        if key in {"proof_path", "pdf_path"}:
            value = _short_path(value)
        elif key in {"pdf_images", "pdf_fonts"} and isinstance(value, list):
            # Preserve counts and a tiny signal, not full metadata.
            value = {"count": len(value)}
        result[key] = value
    # Node/geometry and colour-conformance figures live under stats.static in
    # the real manifest; lift the decision-relevant ones to the top level.
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
    stats = manifest.get("stats") if isinstance(manifest.get("stats"), dict) else {}
    findings = manifest.get("findings") if isinstance(manifest.get("findings"), list) else []

    input_projection = _pick(source, "kind")
    if "file" in source:
        input_projection["file"] = _short_path(source["file"])
    if "spec" in source:
        input_projection["spec"] = _short_path(source["spec"])

    summary_projection = _pick(
        summary,
        "hard", "advisory", "by_layer", "print_method", "gradient_handling",
        "require_cmyk", "tac_exact",
    )

    return {
        "manifest_schema": "preflight-manifest",
        "passed": bool(manifest.get("passed")),
        "input": input_projection,
        "summary": summary_projection,
        "findings": [_compact_finding(item) for item in findings],
        "stats": _compact_stats(stats),
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
    """Make one non-retried JSON request to Jev."""
    body = json.dumps(request, separators=(",", ":")).encode("utf-8")
    http_request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "chopshop-svg-jev-annotator/0.1",
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
