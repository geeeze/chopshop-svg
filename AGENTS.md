# AGENTS.md — handoff for an AI agent

You are working in **chopshop-svg v0.1.0**, a T-shirt print pipeline that turns
a raster into candidate vector traces, then checks a chosen vector for print
readiness. This file is the orientation you need before touching code. Read it
fully; it encodes hard rules, environment facts, and pitfalls that cost real
time when rediscovered.

## What the project does (one paragraph)

Two halves, used in sequence, deliberately kept separate:

- **Front half** (`front_pipeline.sh`) — `prep_raster.py` → `trace_sweep.py` →
  `compare_candidates.py`. Turns a raster (PNG/JPG/TIFF) into a set of VTracer
  candidate SVGs plus a comparison report (printability gates **and** a
  pixel-fidelity metric).
- **Back half** (`pipeline.sh`) — `validate_svg.py` (Layer A, source-level) +
  `preflight.py` (Layer B, rendered), plus `snap_colors.py`. Validates one
  chosen SVG and emits a proof PNG, print PDF, and JSON manifest.

The single most important rule: **the pipeline never picks a winner.** Tracing
quality is a visual judgment for a human (or a model looking at the art). Your
job is candidates + metrics only. Never rank beyond "fewest hard gates failed,
then fewest advisories," and never judge which trace "looks best."

## The human decision point (the pipeline pauses here)

The front half ends with candidates + metrics; the back half validates **one**
chosen SVG. Between them sits the only step a machine must not take: **choosing
which candidate goes to final.** That is a human's visual call.

The pipeline can be driven several ways — interactively from a CLI, operated by
an agent (Hermes / GPT / Claude / etc.), or wired into an external tool/plugin
that calls the scripts directly. The rule is identical regardless of driver:

- **After the front half, pause and wait for the human to choose.** Do not run
  `pipeline.sh` on a candidate, do not "pick the best," do not auto-advance to
  the back half.
- **The only exception is an explicit instruction.** If the operator has already
  named a candidate (e.g. "run candidate_03", "run the back half on all passing
  traces", "just validate 07 and 08"), that is the human's selection given ahead
  of time — proceed on that instruction alone.
- When presenting the choice, show the comparison table (sort by fewest hard
  gates failed, then fewest advisories) alongside the fidelity metric, and stop.
  Never rank by "looks best."

`pick_finish.py` (or `./front_pipeline.sh <raster> --loop`) is the interactive
menu driver for exactly this step: print the candidate menu, run the back half
on each pick, loop for more picks or quit.

## Hard rules (do not violate)

- **Do NOT modify `validate_svg.py`, `preflight.py`, `snap_colors.py`, or
  `pipeline.sh`.** They are the working, tested back half. Read them to learn
  the contracts (function signatures, manifest format, gate IDs); change only
  the front half.
- **Never use an LLM to generate or edit SVG paths.** Tracing is VTracer (or
  img2svg as a last resort) only. If neither is available, exit with a clear
  message naming both and their install commands — never synthesize paths.
- **Prep is check-first.** `prep_raster.py` reports acceptability and copies
  the input through unchanged unless `--fix` is passed. It must never silently
  transform (resize/squash/re-colour) a source.
- **Do not prescribe size/orientation.** The default spec has no `dimensions`
  block; the size gate is skipped, and prep reports the source's own dimensions
  as information. An aspect mismatch is a *finding to report*, never a
  distortion to "fix."
- **Never read, print, or commit secrets** (tokens, passwords, `.env`).
- **Tests use synthetic in-test fixtures only.** Never depend on the real
  `00_source/` batch in a test.

## Environment facts

- **Target platform: Debian 13-family (x86_64).** System tools install via
  `apt`, Python deps via `pip`; see "Deployment / container" below for the
  run-anywhere image.
- **Python 3.13 through a repo-root venv** (`.venv/bin/python`). On PEP 668
  hosts a bare `pip install` is refused, so always go through the venv.
- **Do not assume a Node.js runtime.** `scripts/svgo_print.yml` is an SVGO
  config for hosts that do have one; the pure-Python equivalent is
  `snap_colors.py`, and nothing in the pipeline requires Node.
- **Required system tools:** Inkscape 1.4+, Ghostscript (`gs`), qpdf,
  poppler-utils (`pdfinfo`/`pdfimages`), potrace.
- **Optional prep tools** must stay skippable when absent: pngquant, rembg,
  realesrgan-ncnn-vulkan.
- Tracer: the `vtracer` PyPI wheel (`0.6.15`) is the Python-API-only build — **no
  `vtracer` CLI on PATH, and no `gradient_step` parameter**. The standalone CLI
  has `--help` and more flags; the pip API's available params come from
  introspecting `convert_image_to_svg_py`'s signature.
- Prefer non-privileged paths: run the pipeline as a normal user and avoid any
  step that needs root.

## Contracts and conventions

- **Layer B (`preflight.py`) must be given a per-candidate workdir when run in
  parallel.** `preflight.preflight(svg_path, spec_path, workdir=...)` writes
  fixed names (`flattened.pdf`, `<stem>.print.pdf`) and *unlinks the whole
  workdir*. Concurrent workers sharing one workdir clobber each other.
  `compare_candidates.py` already gives every candidate its own sub-workdir.
- **Inkscape crashes under concurrency** (`Gio::DBus::Error`, exit -6) — several
  instances race to register a GApplication on the session bus. The parallel
  worker initialiser in `scripts/front_common.py` sets
  `DBUS_SESSION_BUS_ADDRESS=disabled:` before any child runs. Keep it.
- **`--export-background-opacity=255` is valid on Inkscape 1.4** (help says
  "0.0 to 1.0, or 1 to 255"). Do not "fix" it to a float.
- **A JPEG/TIFF/WEBP source must be re-encoded to a real PNG in prep** (even in
  check mode): VTracer's Rust loader sniffs the *extension*, not the bytes, and
  aborts on JPEG bytes under a `.png` name. PNG sources stay byte-identical;
  non-PNG are decode→re-save PNG (container only).
- Log to stdout in the same `--- stage N ---` banner style as `pipeline.sh`;
  version-stamp every tool call in output JSON (mirror `preflight.tool_versions()`).
- Every script must be idempotent (rerunning yields the same result, no
  corruption) and graceful when an optional tool is missing (skip + warn +
  record, never crash).
- Candidates sort by `(hard, advisory)` ascending only. Fidelity is a separate
  ranking, never a winner-picker.

## Parallelism

`trace_sweep.py` and `compare_candidates.py` fan per-candidate work across a
`ProcessPoolExecutor` (helper: `front_common.run_parallel`). Default workers =
`min(cpu_count, candidates)`; override with `--workers N` or env
`FRONT_PIPELINE_WORKERS`; `--workers 1` = sequential. Output stays deterministic
(`executor.map` returns in task order). Parallelism is an optimisation with a
sequential fallback — a pool failure must never break a stage.

## Deployment / container (run anywhere)

The full stack (Python venv + Inkscape 1.4 + Ghostscript + qpdf + potrace) is
packaged as a container so it can run on any Docker/podman host with zero host
installs. Files: `Dockerfile` (Debian 13 base, matching the tested deployment),
`docker-compose.yml` (bind-mounts inputs + outputs), `.dockerignore` (keeps the
build context lean).

```bash
docker compose build                                   # once
docker compose run --rm chopshop ./pipeline.sh 00_source/art.svg       # back half
docker compose run --rm chopshop ./front_pipeline.sh 00_source/art.png # front half
```

- The code and venv are **baked into the image**; only `00_source/`, `spec.json`,
  and the `01_prepped/…05_final/` output dirs are bind-mounted, so results land
  on the host.
- `podman compose` / `podman-compose` understand the same file, so the image
  runs under either runtime. With podman, drive it directly
  (`podman build` + `podman run`) if a `docker-compose` binary on the machine
  targets a daemon that is not running.
- Inkscape in the container runs headless with `DBUS_SESSION_BUS_ADDRESS=disabled:`
  (already set by `front_common.py` for parallel workers); give it a writable
  `HOME` (the image sets `HOME=/root`).

## Running tests

```bash
.venv/bin/python -m pytest tests/
.venv/bin/pyflakes scripts/*.py validate_svg.py preflight.py  # lint
```

The suite currently passes (246 tests). If you change the front half, add or
update the matching test in `tests/` — especially any change to tracing,
comparison, or prep behaviour.

## Key files

- `skills/` — the two deep-reference skills exported from the author's Hermes
  install (`shirt-print-front-half` = front half, `svg-print-preflight` = back
  half). Read them before touching the matching code; they hold the pitfalls
  and design decisions this file only summarises. `skills/README.md` explains
  how to re-import them into a Hermes install.
- `scripts/FRONT_HALF.md` — front-half design (sweep, fidelity, parallelism).
- `scripts/palette_variants.py` — auxiliary Chopshop-Aided-Design layer (wired
  into no stage): re-colours a finished trace onto the palettes in
  `scripts/palettes.json`, leaving the geometry untouched (only
  `fill`/`stroke`/`stop-color` change). Runs standalone, from a `05_final/`
  manifest (`--from-final`), and can gate each variant through the back half
  (`--preflight`). An explicit `map` always beats its area/nearest heuristic --
  the heuristic cannot recover intent. Never picks a winner.
- `OVERVIEW.md`, `HOWTO-print-check.md` — design and print-check walkthrough.
- `spec.json` / `spec.example.json` — the job contract (front-half keys are
  under `print`: `assume_opaque_bg`, `prep_colors`, `background_hex`,
  `sweep_max_candidates`).
- `requirements.txt` — required + optional deps, with install notes.
