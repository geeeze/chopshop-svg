# Front half: raster → traced candidates → comparison

The back half of this pipeline (`validate_svg.py`, `preflight.py`,
`snap_colors.py`, `pipeline.sh`) takes an **SVG** and checks it for print
readiness. The front half answers the question that comes *before* that: given
a **raster** image, which vector traces are worth feeding to the back half?

It does **not** pick a winner. Whether a trace looks right is a visual judgment.
The front half produces a set of candidates plus honest metrics, and a human (or
GPT/Astra) chooses. Then you run the back half on the chosen SVG.

```
    raster (PNG/JPG/TIFF)
            │
    ┌───────▼────────┐   scripts/prep_raster.py
    │ 01_prepped/    │   normalise: background removal, upscale to target DPI,
    │ <stem>.prepped.png  quantise, flatten alpha  (+ <stem>.prep.json sidecar)
    └───────┬────────┘
            │
    ┌───────▼────────┐   scripts/trace_sweep.py
    │ 02_traced/     │   VTracer sweep -> candidate_01..NN.svg  (+ sweep.json)
    │ <stem>/        │
    └───────┬────────┘
            │
    ┌───────▼────────┐   scripts/compare_candidates.py
    │ 04_validated/  │   every candidate through Layer A + Layer B
    │ <stem>.comparison.md / .json   (no winner chosen)
    └───────┬────────┘
            │
    (human picks a candidate)
            │
    ┌───────▼────────┐
    │ ./pipeline.sh  │   the BACK half: validate + preflight the chosen SVG
    └────────────────┘
```

`front_pipeline.sh` chains the first three stages. It **never** calls
`pipeline.sh`; the two halves stay separate so you always make the choice in
between.

**Prep is check-first.** By default `prep_raster.py` (and therefore
`front_pipeline.sh`) does *not* modify your image — it reports whether the file
checks the boxes for tracing and copies it through unchanged. The transforms
(background removal, upscale, quantise, flatten) run only when you pass `--fix`.
This exists because an early version silently *squashed* a landscape source into
a portrait spec's pixel box; now an aspect mismatch is reported, never "fixed"
by stretching.

## The scripts

| Script | What it does |
|---|---|
| `prep_raster.py` | Check a raster for tracing suitability (default) or, with `--fix`, normalise it. Default mode reports the acceptability checks (format, colour mode, alpha, aspect ratio vs the spec, physical size at DPI, colour count, background uniformity) and copies the file through **unchanged**. `--fix` applies the optional transforms, preserving aspect ratio. Outputs `01_prepped/<stem>.prepped.png` + a `.prep.json` sidecar. |
| `trace_sweep.py` | Trace the prepped PNG with VTracer across a parameter sweep (preset × filter_speckle × hierarchical × palette), capped by `spec.print.sweep_max_candidates`. Outputs `02_traced/<stem>/candidate_NN.svg` + `sweep.json`. |
| `compare_candidates.py` | Run every candidate through Layer A and Layer B, then write `04_validated/<stem>.comparison.md` (table) and `.comparison.json` (machine-readable). Sorts by fewest hard gates failed, then fewest advisories. Nothing more. Also, when a source raster is resolvable, renders each candidate back with Inkscape and diffes it against the source for a **pixel-fidelity metric** (see below). |
| `pick_finish.py` | Semi-interactive tail: print a menu of candidates, wait for the user to pick up to 3, run `pipeline.sh` on each, loop. Driven by `front_pipeline.sh --loop`. |
| `front_pipeline.sh` | Orchestrator: prep → sweep → compare, then reports the outcome (or, with `--loop`, hands off to `pick_finish.py`). Exit 0 = a candidate passed both gates, 1 = none passed, 2 = a step failed. |

Shared helpers live in `front_common.py` (sha256, tool-version stamping, the
default `print.*` values).

## Installing the dependencies

Everything degrades gracefully when a tool is missing: the step is skipped, a
warning is logged, and the skip is recorded in the JSON output. Only one thing
is required — a tracer.

### Required: a tracer (VTracer)

```bash
# Python API (what this project's tests use):
.venv/bin/pip install vtracer

# or the standalone CLI (has --help, which trace_sweep.py parses):
#   download a release binary from https://github.com/visioncortex/vtracer/releases
#   and put `vtracer` on your PATH
```

If VTracer is absent, `trace_sweep.py` falls back to `img2svg`
(`pip install img2svg`) and, if *that* is absent too, exits 3 naming both tools
and their install commands. Note: the PyPI `img2svg` embeds the raster rather
than vectorising it, so its candidates will likely fail the raster gate — it is
a last resort, not a peer of VTracer.

### Optional: prep helpers

```bash
# background removal (step a)
.venv/bin/pip install 'rembg[cpu]'   # module + onnxruntime; rembg 2.x has no CLI

# higher-quality upscaling (step b) — falls back to Pillow LANCZOS
#   install realesrgan-ncnn-vulkan from https://github.com/xinntao/Real-ESRGAN
#   (ncnn Vulkan builds) and put the binary on PATH

# colour quantisation (step c)
sudo apt-get install pngquant        # Debian/Ubuntu
# or: brew install pngquant          # macOS
```

Pillow, numpy, lxml and svgpathtools are already in the project's `.venv`.

## The sweep, in detail

The default sweep (override with `--sweep sweep.json`) is:

- **presets** — `bw` (binary), `poster` (limited-colour), `photo` (highest
  colour precision). `photo` is dropped unless `spec.geometry.allow_gradients`
  is true, because a photo-like trace implies gradients a spot-colour job
  cannot print.
- **filter_speckle** — 2 / 8 / 16 (noise/speckle removal).
- **hierarchical** — `cutout` / `stacked` (how overlapping colour layers are
  composed).
- **palette** — one candidate is pre-quantised to `spec.palette` (nearest colour
  in sRGB, done with Pillow/numpy, not a VTracer flag), one is left untouched.

Candidates are generated in a documented interleaved order (speckle →
hierarchical → preset → palette) so a small cap still samples every preset and
both hierarchical modes, then truncated to `spec.print.sweep_max_candidates`
(default 12).

VTracer's flags are **not** assumed: the CLI path parses `vtracer --help`, and
the Python-API path introspects `convert_image_to_svg_py`'s signature. If a
sweep flag is unavailable in the detected tracer, that axis is skipped and the
skip is recorded in `sweep.json`. Every candidate is reproducible: the same
input + the same sweep config produces byte-identical SVGs (verified by
`tests/test_trace_sweep.py`), and `sweep.json` records the exact parameters,
the source's sha256, and the tracer's version for each candidate.

## The spec fields the front half owns

All under `print`, all optional:

| Field | Default | Meaning |
|---|---|---|
| `assume_opaque_bg` | `false` | Skip background removal; assume the raster has no transparent background. |
| `prep_colors` | `16` | pngquant colour budget; `null` disables quantisation. |
| `background_hex` | `"#ffffff"` | Colour to flatten any alpha channel onto before tracing. |
| `sweep_max_candidates` | `12` | Cap on the number of traced candidates. |

**Size and orientation are not prescribed.** The default `spec.json` carries no
`dimensions` block, so the Layer A `DIMENSIONS` gate is skipped and prep reports
the source's own pixel size and aspect ratio as *information*, never a pass/fail.
Add a `dimensions: {width_mm, height_mm}` block only for a job with a fixed
print size, and it becomes a real gate again — the two layers already honour it.

## The workflow

```bash
cd /path/to/chopshop-svg

# one command, front half only (prep checks + copies through unchanged):
./front_pipeline.sh my-artwork.png

# if the prep checks say the image needs it, apply the transforms too:
./front_pipeline.sh my-artwork.png --fix

# semi-interactive: after compare, pick up to 3 candidates and run
# pipeline.sh (the back half) on each, in a loop
./front_pipeline.sh my-artwork.png --loop

# read the report, choose a candidate by eye:
#   04_validated/my-artwork.comparison.md

# then run the BACK half on the chosen candidate:
./pipeline.sh 02_traced/my-artwork/candidate_04.svg
```

Or stage by stage:

```bash
.venv/bin/python scripts/prep_raster.py my-artwork.png spec.json          # check
.venv/bin/python scripts/prep_raster.py my-artwork.png spec.json --fix    # modify
.venv/bin/python scripts/trace_sweep.py 01_prepped/my-artwork.prepped.png spec.json
.venv/bin/python scripts/compare_candidates.py 02_traced/my-artwork spec.json
```

Run the tests with `.venv/bin/python -m pytest tests/`.

## The fidelity metric (raster → vector accuracy)

`compare_candidates.py` reports two independent views. The main table is the
**printability** view (Layer A/B gates, colour budget). When a source raster is
resolvable — automatically from `sweep.json`'s `input.file`, or explicitly via
`--source` — it also renders each candidate back to a raster with Inkscape and
diffs it against the source, giving a **pixel-fidelity** view:

| metric | meaning |
|---|---|
| `MAE all` | mean absolute error over every pixel |
| `MAE art` | MAE over artwork pixels only (ignores the background) — the number to watch for dark-art-on-dark-background |
| `P95` | 95th-percentile error |
| `within-10` | fraction of pixels within 10/255 of the source |

Lower is closer to the original. The report keeps the printability sort as its
main ordering, then adds a "By pixel fidelity" ranking sorted by `MAE art`.
Neither view picks a winner — they are two lenses on the same candidates.

Two things the metric surfaces that the gates cannot:

- The **palette axis hurts fidelity** unless `spec.palette` matches the
  artwork's actual colours (the trace's true colours are listed in the `PALETTE`
  advisory).
- On a photo-like source, the `poster` preset is usually the fidelity winner;
  the `bw` preset is a silhouette and scores badly on `MAE art` even though it
  "passes" the gates.

## Parallelism

`trace_sweep.py` and `compare_candidates.py` both run their per-candidate work
across worker processes (a `ProcessPoolExecutor`), so the sweep and the Layer
A/B + fidelity renders scale with the core count instead of tracing one candidate
at a time. Each candidate is independent, and every candidate — including the
preflight render — gets its own workdir, so workers never share temp files.

- Default worker count: `min(cpu_count, candidate_count)`.
- Override per invocation with `--workers N` (both scripts).
- Override globally with the `FRONT_PIPELINE_WORKERS` environment variable.
- `--workers 1` disables parallelism (the sequential fallback).
- Output is byte-identical whether you run with 1 worker or many: the pool maps
  results back in task order, so `sweep.json` and the comparison stay
  deterministic regardless of completion order (locked in by
  `test_parallel_matches_sequential` in both test files).

Two notes on the mechanism, both handled for you:

- **Inkscape crashes under concurrency** (`Gio::DBus::Error`, exit -6): several
  instances racing to register a GApplication on the session bus at startup.
  The worker initialiser points `DBUS_SESSION_BUS_ADDRESS` at `disabled:` so the
  render tools skip D-Bus entirely. Headless export does not need it.
- **`preflight.py` writes fixed-name temp files and unlinks its workdir**, so a
  shared workdir would be clobbered by concurrent candidates. `compare_candidates`
  gives every candidate its own sub-workdir under `<out>/<stem>.work/`.

Observed speedup (24-core box, VTracer Python API): a 12-candidate sweep went
~3.4s → ~0.9s on a 1500×1500 source; the win grows with source size and colour
complexity, where tracing (and the Inkscape fidelity render) dominates.

## Known caveats

- **VTracer emits pixel-sized SVGs.** It writes `width`/`height` in image
  pixels rather than physical millimetres. With no `dimensions` block in the
  spec (the default), this is harmless — the size gate is skipped. If a job
  does order a size, set `width`/`height`/`viewBox` on the chosen candidate to
  that physical size before the back half.
- **The colour trace includes the background.** VTracer's `color` mode traces
  the whole image, so a background shows up as a near-background fill and can
  push the declared/rendered colour count up. The `bw` preset and the
  background-removal + flatten steps exist to counter this.
- **`prep_colors`/`rembg`/`realesrgan` are best-effort.** Missing binaries are
  skipped and recorded; the pipeline still produces candidates and metrics.
