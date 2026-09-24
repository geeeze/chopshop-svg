---
name: shirt-print-front-half
description: Use when tracing a raster into candidate SVGs for print.
---

# Shirt-print pipeline: front half (raster → traced candidates)

The project `/home/monop/davesnothere` is a two-half T-shirt print pipeline.
The BACK half (`validate_svg.py` Layer A, `preflight.py` Layer B,
`snap_colors.py`, `pipeline.sh`) checks an SVG for print readiness. The FRONT
half answers what comes before: given a raster, which vector traces are worth
feeding to the back half.

It NEVER picks a winner. Trace quality is a visual call for the human (or
GPT/Astra). It produces candidates + metrics only.

## Workflow

```bash
cd /home/monop/davesnothere
./front_pipeline.sh artwork.png                 # prep -> sweep -> compare
./front_pipeline.sh artwork.png --sweep s.json  # custom sweep
./front_pipeline.sh artwork.png --skip-prep     # reuse an existing prep
SPEC=other.json ./front_pipeline.sh art.png     # different spec
```

Stages (each script is also runnable alone):
1. `scripts/prep_raster.py <png> <spec>` → `01_prepped/<stem>.prepped.png` + `.prep.json`
2. `scripts/trace_sweep.py <prepped.png> <spec>` → `02_traced/<stem>/candidate_NN.svg` + `sweep.json`
3. `scripts/compare_candidates.py <traced_dir> <spec>` → `04_validated/<stem>.comparison.md` + `.json`
4. human reads `.comparison.md`, picks a candidate, runs `./pipeline.sh` on it.

## Semi-interactive loop (--loop)

`front_pipeline.sh --loop` appends a pick loop: after compare it hands off to
`scripts/pick_finish.py`, which prints a menu (preset, speckle, hier, palette,
MAE-all/MAE-art, Layer A/B, hard/adv), waits for the user to pick up to 3
candidates (space-separated numbers, or `q` to quit), runs `pipeline.sh` on each
pick (full Layer A+B + proof + print.pdf + manifest), then loops back to the
menu. Max 3 picks per iteration; re-tracing with new params = re-run
front_pipeline.sh. Parse logic is in `pick_finish.parse_picks` (pure, unit-tested);
the loop itself is `interactive_loop` (reads stdin, not unit-tested).

## Fidelity (accuracy) metric — the user's actual goal

`compare_candidates.py` also measures PIXEL FIDELITY: it renders each candidate
back to a raster with Inkscape (at the source's dimensions) and diffs it against
the source raster. Source is auto-resolved from `sweep.json`'s `input.file`
(the prepped PNG) or the `--source` flag. Per candidate it records
`fidelity: {measured, mae, p95, within10, mae_art}`.

- `mae` = mean abs error over ALL pixels. On dark-art-on-dark-background images
  this is dominated by the background and is misleadingly low.
- `mae_art` = MAE over ARTWORK pixels only (source pixels deviating >20 from the
  modal colour). THIS is the number that matters for raster→vector accuracy.
- The report keeps the printability sort (hard, advisory) but adds a separate
  "By pixel fidelity" ranking (sorted by mae_art). Neither picks a winner.

Measured on a real 1080² dark-art image (1472 unique colours, 92% near-black):
`bw` = silhouette, mae_art≈49 (bad); `poster` no-palette = mae_art≈9.6 (best);
`poster` palette-quantized = mae_art≈45. Conclusion: the palette axis and the
SPEC'S DEFAULT PALETTE hurt fidelity unless the palette matches the artwork's
real colours. The trace's true colours are visible in the PALETTE advisory —
read it before trusting the palette axis.

## Prep is CHECK-FIRST (do not modify by default)

`prep_raster.py` defaults to CHECK mode: it reports acceptability checks and
copies the input through byte-identical (sha256 unchanged). It only modifies
with an explicit `--fix` flag (front_pipeline.sh passes `--fix` through).
This is the user's explicit preference — prep should check boxes, not mangle
the source. An early version had a real squash bug: `img.resize(target)` forced
a landscape source into a portrait spec's pixel box, distorting the art. Two
rules now guard it:
- NEVER resize to the spec's exact pixel box; upscale by `scale = min(tw/w, th/h)`
  (fit within, aspect preserved) and only when `scale > 1` (source smaller than
  target).
- Report aspect mismatch as a `fail` check ("rotate, crop, or change
  spec.dimensions") rather than silently stretching.

Acceptability checks (sidecar `checks[]`, each `{check, status, detail}`,
status ∈ pass/warn/fail/info): format, color_mode, alpha, aspect_ratio,
physical_size, color_count, background. `acceptable = no check is fail`.

## Size/orientation is NOT prescribed

The default spec.json carries NO `dimensions` block, and prep/compare must not
invent one. With no `dimensions`, validate_svg.py's `load_spec` leaves it None
and the DIMENSIONS hard gate is skipped (`size_check = "skipped: spec has no
dimensions block"`), and prep's geometry check reports the source's own
px-size/aspect as `info` (never pass/fail). Add `dimensions: {width_mm,
height_mm}` back only for a job with a fixed print size. The user was explicit:
do not prescribe size/orientation — a landscape source against a portrait spec
is a data mismatch to REPORT, never a distortion to 'fix'.

Exit codes of `front_pipeline.sh`: 0 = a candidate passed both gates, 1 = none
passed, 2 = a step failed. `trace_sweep.py` exits 3 when no tracer exists.
`compare_candidates.py` exits 0 (cleanly) on zero candidates.

## Tracer: VTracer

VTracer ships as either a CLI or a Python package. The PyPI wheel (`pip install
vtracer`) is **API-only** — no `vtracer` binary on PATH, and its
`convert_image_to_svg_py` signature lacks `gradient_step`. The standalone CLI
(from https://github.com/visioncortex/vtracer/releases) has `--help`.

`trace_sweep.py` probes in order: (1) `vtracer` CLI, parsing `--help` for flags
— never assume flag names; (2) `vtracer` Python API, introspecting
`convert_image_to_svg_py`'s signature as the `--help` analogue; (3) `img2svg`
(last resort — it embeds the raster, does NOT vectorise).

VTracer output is deterministic: same input + same params = byte-identical SVG.
It emits `width`/`height` in image PIXELS, so the Layer A `DIMENSIONS` gate
flags candidates uniformly; fix `width`/`height`/`viewBox` to the ordered
physical size on the chosen candidate before the back half.

## Prep helpers (all optional, degrade gracefully)

- `rembg` (`pip install 'rembg[cpu]'` — module + onnxruntime; rembg 2.x has no
  CLI binary, so our prep uses the Python module) — background removal. Skip via
  `spec.print.assume_opaque_bg: true`.
- `realesrgan-ncnn-vulkan` (GitHub release binary) — upscale. Falls back to
  Pillow LANCZOS.
- `pngquant` (`apt-get install pngquant`) — quantise to `spec.print.prep_colors`.
  `null` disables.

Missing binaries: skip the step, log a warning, record it in the sidecar —
NEVER crash the run. CMYK TIFF → convert to sRGB first. Very large inputs:
downscale-then-upscale (bounded memory).

## Spec keys the front half owns (all under `print`, optional)

```json
"print": {
  "assume_opaque_bg": false,   // skip bg removal
  "prep_colors": 16,           // pngquant budget; null = off
  "background_hex": "#ffffff", // flatten alpha onto this
  "sweep_max_candidates": 12   // cap candidates
}
```

## Parallelism

`trace_sweep.py` and `compare_candidates.py` both fan per-candidate work out
across a `ProcessPoolExecutor` (default workers = `min(cpu_count, candidates)`,
override with `--workers N` or env `FRONT_PIPELINE_WORKERS`; `--workers 1` =
sequential). Shared helper: `front_common.run_parallel(fn, tasks, workers)` —
falls back to a sequential map if the pool can't start, so parallelism can never
break a stage. Output stays deterministic: `executor.map` returns in task order.

Two concurrency pitfalls, both fixed in `front_common`/`compare_candidates`:
- **Inkscape aborts under concurrency** (`Gio::DBus::Error`, exit -6): concurrent
  instances race to register a GApplication on the session bus. The pool's
  `initializer` sets `DBUS_SESSION_BUS_ADDRESS=disabled:` so render tools skip
  D-Bus (headless export doesn't need it). Symptom in the wild: a candidate gets
  `RENDER_ERROR: 'inkscape' exited -6` ONLY when run in parallel.
- **`preflight.preflight` unlinks its whole workdir** and writes fixed names
  (`flattened.pdf`, `<stem>.print.pdf`), so parallel workers must each get their
  OWN workdir or they clobber each other. `compare_candidates` gives every
  candidate a sub-workdir `<out>/<stem>.work/<candidate>/`.

Locked in by `test_parallel_matches_sequential` in both test files.

## Conventions / pitfalls

- Log to stdout in the same `--- stage N ---` banner style as `pipeline.sh`;
  version-stamp every tool call in the JSON (mirror `preflight.tool_versions()`).
- Every script idempotent: rerunning produces the same result, no corruption.
- Tests use synthetic Pillow fixtures only; never touch `00_source/` (that's
  the back half's batch).
- `.venv/bin/python` is the interpreter (`$PROJECT/.venv`); lxml, svgpathtools,
  Pillow, numpy, pytest already installed there.
- `compare_candidates.py` runs BOTH layers by importing `preflight` + calling
  `preflight.preflight()` (which internally calls `validate_svg.validate()`), so
  declared colours / node counts come from `stats["declared_colors"]` and
  `stats["static"]["path_nodes_total"/"path_nodes_max"]`. If a render tool is
  missing, `rendered_ink_colors` is None and a MISSING_TOOL finding marks Layer
  B "not run".
- Sort candidates by (hard, advisory) ascending ONLY — never rank or judge.
- The full pipeline (`pipeline.sh`) is run by the human on the CHOSEN candidate;
  `front_pipeline.sh` never calls it.
- rembg may be installed module-only (`pip install rembg`, no `[cli]` extra): no
  `rembg` binary, but `import rembg` works. `prep_raster._run_rembg` falls back
  to the module's `remove()` when the CLI is absent, so a module-only install is
  used rather than reported missing.
- A JPEG/TIFF/WEBP source must be re-encoded to a genuine PNG in prep (check
  mode) even though it is otherwise copied through unchanged: VTracer's Rust
  loader sniffs the EXTENSION, not the bytes, and aborts on JPEG bytes under a
  `.png` name. PNG sources stay byte-identical; non-PNG are decode→re-save PNG
  (container only, pixels untouched).
