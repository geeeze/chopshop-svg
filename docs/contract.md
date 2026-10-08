# chopshop-svg — Interface Contract

T-shirt print pipeline: raster -> candidate vector traces -> print-readiness check.

## Stages (deliberately separate)

- **Trace** (`front_pipeline.sh`): `prep_raster.py` -> `trace_sweep.py` -> `compare_candidates.py`.
  Raster (PNG/JPG/TIFF) -> VTracer candidate SVGs + comparison report (printability gates + pixel-fidelity).
- **Print check** (`pipeline.sh`): two layers, both run, both reported.
  - Layer A (`validate_svg.py`) — source-level, fast, deterministic.
  - Layer B (`preflight.py`) — rendered, measured, slower.
  Job passes only if neither layer raises a HARD finding; advisories reported, do not fail.
  Plus `snap_colors.py`.

## Boundaries

- No LLM ever generates or edits SVG paths — tracing is VTracer only.
- Contract + tests updated in the same commit as any schema/shape change.
