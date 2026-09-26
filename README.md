# chopshop-svg · v0.1.0

Turn a raster image into print-ready vector art — automatically.

**chopshop-svg** is a T-shirt print pipeline. You give it a picture (a logo, a
scan, a photo, an AI-generated image); it produces a set of traced vector
candidates, measures each one against the rules a real screen-printer cares
about (colour count, node weight, stroke thickness, coverage), and shows you the
trade-offs. **It never picks a winner** — tracing quality is a visual judgment,
so a human chooses from the candidates and metrics the pipeline lays out.

The project is two halves that you use in sequence:

1. **Front half** — raster → vector candidates. `front_pipeline.sh` runs prep →
   trace sweep → comparison. You look at the report, pick a candidate by eye.
2. **Back half** — vector → print check. `pipeline.sh` runs the chosen candidate
   through two independent gates (source-level *Layer A*, rendered *Layer B*)
   and produces a proof image, a print PDF, and a JSON manifest.

---

## Quick start

```bash
cd chopshop-svg

# 1. create a virtual environment and install the Python dependencies
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. make sure the system tools are present (see "System tools" below)
command -v inkscape gs qpdf

# 3. trace a raster into candidates and get a comparison report
./front_pipeline.sh artwork.png

# 4. read the report, choose a candidate, then validate it for print
./pipeline.sh 02_traced/artwork/candidate_04.svg
```

Try it on the shipped example, which is known to pass both layers:

```bash
./front_pipeline.sh 00_source/00-example.png
./pipeline.sh 02_traced/00-example/candidate_10.svg   # -> PIPELINE PASSED
```

`00_source/00-example.png` is generated, not hand-drawn — rebuild it with
`.venv/bin/python scripts/make_example_source.py`. It is deliberately flat and
inside `spec.json`'s 6-colour palette, because **tonal art cannot pass a
6-colour gate**: the previous example was an engraved floral with 245 608
distinct colours (256 of them cover only 34% of the pixels), and the only
candidates that cleared it did so by discarding the artwork down to one black
silhouette. It now traces to 5 colours at `mae_art` 0.005 with zero hard gates
and zero advisories. The old image is kept as
`00_source/00-example-tonal-reference.png` — it is a useful negative example,
and the numbers quoted throughout `scripts/NODE_REDUCTION.md` were measured on
it.

That's the whole workflow. The `--loop` flag turns step 3–4 semi-interactive:
after the comparison it prints a menu, you pick up to 3 candidates, and it runs
the back half on each in turn:

```bash
./front_pipeline.sh artwork.png --loop
```

### Two side tools

Neither is part of the workflow above; both are run by hand.

**`scripts/tune_sweep.py`** — a batch bench for finding a good trace
configuration. It re-traces ONE raster under many parameter sets, measures
every cell on the same axes, and writes a report you compare yourself:

```bash
.venv/bin/python scripts/tune_sweep.py 01_prepped/art.prepped.png \
    --spec spec.json --preset baseline,nodewise,coarse,flat4 \
    --fidelity --contact-sheet --with-node-reduce
# → 08_tune/art/tune.md, tune.json, contact-sheet.png
```

It ranks by pixel fidelity as a **measurement order and never recommends a
cell** — the same law the pipeline follows. Two findings worth knowing before
you sweep: `layer_difference` (colour count) and `splice_threshold` (worst
single path's nodes) are **orthogonal**, so one-axis sweeps mislead; and
`layer_difference` is **not monotone** on node count, so coarser can be worse.

**`scripts/node_reduce.py`** — the remedy for `geometry_overload`, a trace that
passes every colour and ink rule but has one path with too many nodes for the
cutter. It targets only the over-gate paths, because simplifying a whole file
measurably makes the worst path worse:

```bash
.venv/bin/python scripts/node_reduce.py 02_traced/art/candidate_11.svg \
    --out reduced.svg --max-nodes 500 --verify
```

Clearing the node budget is **not** the same as the artwork surviving.
`--verify` renders before and after and reports ink drift and the largest
background-coloured blob, which is the shared-boundary gap signature. Measured
on real candidates: one clears at 965 → 391 with a 2px gap, another at
2240 → 461 but with a 16px gap — gate cleared, art eroded. Read
`scripts/NODE_REDUCTION.md` before trusting it.

### What the comparison report tells you

`04_validated/artwork.comparison.md` is the report. For every candidate it lists:

- the exact tracing parameters that produced it (reproducible),
- **Layer A / Layer B** pass-or-fail plus the specific findings,
- declared colours, rendered ink count, node count (total and heaviest path), file size,
- a **pixel-fidelity** score (how close the trace is to the original raster),
- a summary of hard gates failed and advisories raised.

Candidates are sorted by *fewest hard failures, then fewest advisories* — and
nothing more. The pixel-fidelity ranking is shown separately as a second lens.
Pick what looks right for your job.

---

## System tools (not Python — install via your OS)

| Tool | Needed by | Status |
|---|---|---|
| Inkscape | render the SVG to a proof PNG + fidelity diff (back + front half) | **required** |
| Ghostscript (`gs`) | PDF colour separation in Layer B | **required** |
| qpdf | PDF inspection in Layer B | **required** |
| VTracer | raster → vector tracing (front half) | in `requirements.txt` |
| pngquant | optional colour quantisation in prep | optional — skipped if absent |
| rembg | optional background removal in prep | optional — skipped if absent |
| realesrgan-ncnn-vulkan | optional higher-quality upscale in prep | optional — skipped if absent |
| SVGO (Node.js) | optional SVG cleanup (see `scripts/svgo_print.yml`) | optional — needs a Node runtime |

Debian/Ubuntu one-liner for the required tools:

```bash
sudo apt-get install inkscape ghostscript qpdf poppler-utils potrace
```

### Optional tools (install only if you want that prep step)

Each of these is *skippable* — if absent, the step logs a warning and is
recorded in the sidecar, and the pipeline still runs. Install commands verified
against the current releases:

```bash
# colour quantisation in prep (--fix), to spec.print.prep_colors
sudo apt-get install pngquant              # Debian/Ubuntu
#   or: brew install pngquant              # macOS

# background removal in prep (--fix).  rembg 2.x ships NO CLI binary, and its
# ML backend (onnxruntime) lives in the [cpu] extra, not [cli] — so install the
# cpu extra and the pipeline drives it through the Python module:
.venv/bin/pip install 'rembg[cpu]'

# higher-quality upscale in prep (--fix); falls back to Pillow LANCZOS if absent
#   download a release binary from https://github.com/xinntao/Real-ESRGAN
#   (the "realesrgan-ncnn-vulkan" asset) and put it on your PATH
```

Everything optional degrades gracefully: if a tool is missing, that step is
skipped, a warning is logged, and the skip is recorded in the JSON sidecar —
the pipeline still produces candidates and metrics. The one hard requirement on
the front half is a tracer (VTracer, via `requirements.txt`).

---

## The job contract (`spec.json`)

The whole pipeline is driven by one JSON file, `spec.json` (start from
`spec.example.json`). It is the single statement of what "printable" means for
a job: the colour budget, the geometry limits, the tolerances, and the
front-half prep/trace behaviour. Every gate in Layer A and Layer B reads from
it; any key you omit falls back to its default below.

### Top-level keys

| Key | Type | Default | Meaning |
|---|---|---|---|
| `print_method` | string | `""` | e.g. `screen_print`, `dtg`, `vinyl`. Drives a few derived defaults (e.g. open-path tolerance). |
| `max_colors` | int | *(none)* | Colour budget — Layer A hard gate (declared colours must not exceed it). |
| `palette` | string[] | *(none)* | The expected palette as `#rrggbb`; Layer A checks the artwork's colours against it. Also the front half's palette axis. |
| `require_cmyk` | bool | `false` | Require genuine CMYK source (a PDF/ICC reading) rather than an RGB-derived estimate. |
| `icc_profile_path` | string | `null` | ICC profile for colour separation; falls back to a bundled press profile when absent or missing. |
| `ink_limit_percent` | number | `300` | Total area coverage (TAC) limit, percent. |
| `dimensions` | object | *(absent)* | `{width_mm, height_mm}` — physical size. **Absent = the size gate is skipped** (no size/orientation prescription); present = real gate. |
| `geometry` | object | — | See below. |
| `validation` | object | — | See below. |
| `print` | object | — | See below (most knobs live here). |

### `geometry`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `min_stroke_width_pt` | number | *(none)* | Minimum stroke width; thinner strokes are a hard finding. |
| `max_nodes_per_path` | int | *(none)* | Node-count limit per path (trace weight / RIP load). |
| `allow_raster_embed` | bool | `false` | Whether `<image>` raster embeds are permitted (default bans them). |
| `allow_gradients` | bool | `false` | Whether gradients / continuous tone are allowed. Also gates the front half's `photo` preset. |
| `allow_open_paths` | bool | `true` | Whether open (unclosed) paths are acceptable. |
| `gradient_handling` | string | `""` | How gradients are treated: `vector_halftone`, `embedded_raster`, etc. |
| `halftone_handling` | string | `""` | How halftones are treated. |

### `validation`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `run_preflight` | bool | `true` | Whether `pipeline.sh` runs Layer B (render preflight) at all. |
| `flag_on_any_failure` | bool | `true` | Any hard finding fails the job. |
| `auto_retry_limit` | int | `3` | Retry budget (used by the back-half runner). |

### `print`

The render-preflight tolerances and the front-half prep/trace defaults.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `dpi` | number | `300` | Render/measurement DPI. |
| `min_image_ppi` | number | `300` | Minimum effective PPI for placed bitmaps. |
| `ink_area_threshold_percent` | number | `0.05` | A colour must cover this share of the sheet to count as an ink (drops anti-alias fringe). |
| `ink_merge_tolerance` | number | `20` | Colours closer than this (Euclidean sRGB, max 441) merge into one ink. |
| `ramp_min_members` | int | `6` | A merged ink built from ≥ this many colours is treated as a gradient/ramp. |
| `count_white_as_ink` | bool | `false` | Count pure white as an ink (default excludes it, assuming white substrate). |
| `dark_garment_underbase` | bool | `false` | Dark-garment underbase: implies `count_white_as_ink` (white = the underbase screen). |
| `require_embedded_fonts` | bool | `false` | Font substitution is advisory unless this is set. |
| `assume_opaque_bg` | bool | `false` | *Front half* — skip background removal in prep. |
| `prep_colors` | int \| null | `16` | *Front half* — pngquant colour budget in prep; `null` disables quantisation. |
| `background_hex` | string | `"#ffffff"` | *Front half* — colour to flatten alpha onto before tracing. |
| `sweep_max_candidates` | int | `12` | *Front half* — cap on the number of traced candidates. |
| `invert` | bool \| object | `true` | *Front half* — also write a colour-inverted **twin** of the prepped raster (`<stem>.prepped.inverse.png`), in both check and fix mode. `{"mode": "negative"}` inverts chroma and **preserves alpha** (use it for a transparent matte); the default `photometric` inverts every band and therefore flattens a transparent background to opaque. The twin is a derived artefact and may be absent; its presence is always recorded in the `.prep.json` sidecar under `inverse`. A stale twin from an earlier run is deleted when this is turned off, since the prep dir is globbed. |

### Notes

- **Size/orientation is not prescribed by default.** With no `dimensions` block,
  the size gate is skipped and prep reports the source's own dimensions as
  information. Add `dimensions` only for a job with a fixed print size.
- **`spec.json` is the active contract; `spec.example.json` is the annotated
  starting point.** Copy the example and edit, or merge a fragment over it —
  the loaders merge over defaults, so a minimal spec is fine.
- The front half owns the four `print.*` keys marked *Front half* above; the
  rest belong to the back half. See `scripts/FRONT_HALF.md` for the front-half
  specifics.

---

## Layout

```
chopshop-svg/
├── front_pipeline.sh          # FRONT half orchestrator: prep → sweep → compare
├── pipeline.sh                # BACK half: validate + preflight a chosen SVG
├── validate_svg.py            # Layer A — source-level SVG checks
├── preflight.py               # Layer B — render + colour/ink/coverage checks
├── spec.json                  # the active job contract (see spec.example.json)
├── requirements.txt           # Python dependencies (required + optional)
├── scripts/
│   ├── prep_raster.py         # front: check/normalise a raster (--fix to modify)
│   ├── trace_sweep.py         # front: multi-pass VTracer candidate sweep
│   ├── compare_candidates.py  # front: run every candidate through A + B
│   ├── pick_finish.py         # front: --loop menu driver
│   ├── snap_colors.py         # snap a trace's colours to a palette
│   ├── palette_variants.py    # aux: re-colour a finished trace onto named palettes
│   ├── palettes.json          # palette library for palette_variants.py
│   ├── run_batch.py           # batch-run the back half over a folder
│   ├── run_record.py          # provenance spine: stitch a run into 06_run/<stem>.run.json
│   ├── closeout.py            # pass/fail board per candidate vs requirements.json
│   ├── FRONT_HALF.md          # front-half documentation
│   ├── NODE_REDUCTION.md      # node-reduction sourcing research (no tool adopted)
├── requirements.json          # requirements matrix (R-NN | requirement | source | check)
├── tests/                     # pytest suite (synthetic fixtures only)
├── 00_source/                 # example raster/SVG batch for the back half
└── 01_prepped/ 02_traced/ 04_validated/ 05_final/ 06_run/ 07_palettes/   # generated (gitignored)
```

---

## Run record + closeout (the archive spine)

`scripts/run_record.py` stitches one front-half run into a single provenance
record, and `scripts/closeout.py` prints the pass/fail board against
`requirements.json`. Neither runs the pipeline, neither picks a winner.

```bash
.venv/bin/python scripts/run_record.py --all                # every run under 02_traced/
.venv/bin/python scripts/run_record.py --stem 00-example    # one run -> 06_run/00-example.run.json
.venv/bin/python scripts/closeout.py 06_run/00-example.run.json            # board; exit 1 if incomplete
.venv/bin/python scripts/closeout.py 06_run/00-example.run.json --no-gate   # just read the board
```

`run_record.py` joins, per candidate: source hash → prep → sweep entry →
SVG hash → Layer A/B → proof/PDF → human decision, flagging
every missing link. `closeout.py` then checks each candidate against the
requirements matrix and never stops at the first failure.

The matrix in `requirements.json` separates three things that are otherwise
easy to conflate: **hard production requirements** (Layer A/B, colour budget,
continuous tone, exact TAC), **style objectives** (recognizability, engraving
character — these need a human/visual review), and **bookkeeping** (VTracer
only, no SHA duplicates). A candidate can fail production yet still be worth
keeping as a style reference.

The human decision + visual review are recorded by convention (both optional,
both flagged as missing links until they exist):

- `05_final/<candidate>.pick.json` — `{"selected": true, "label": "production|style_reference|needs_retrace|discard", "reason": "..."}`
- `05_final/<candidate>.visual_review.md` — the visual-inspection report

---

## Palette variations (`scripts/palette_variants.py`)

The Chopshop-Aided-Design layer. Takes a finished trace — a validated proof, a
snapped candidate, or the SVG a `05_final/` manifest was built from — and
re-colours it onto each palette in `scripts/palettes.json`. **The CAD structure
is never touched**: no path, node, `viewBox` or dimension changes, only
`fill` / `stroke` / `stop-color`. Same geometry, new colour vectors.

```bash
.venv/bin/python scripts/palette_variants.py --list
.venv/bin/python scripts/palette_variants.py 05_final/art.svg
.venv/bin/python scripts/palette_variants.py --from-final 00-example --preflight
.venv/bin/python scripts/palette_variants.py art.svg --only cool-luxe --map '#c1440e=#9caf88'
```

Output lands in `07_palettes/<stem>/<palette-id>/` (the variant SVG plus a
`mapping.json`) with a `report.json` / `report.md` board for the whole run.

### How it maps colours

Three sources of truth, highest priority first:

1. **Explicit pin** — `map` in the palette entry, or `--map src=dst` on the
   command line. This is the production path.
2. **Background anchor** — the largest-area source colour takes the palette's
   declared `background`.
3. **Strategy** — `--strategy area` (default: rank the remaining source colours
   by rendered area against the remaining palette entries) or `nearest` (closest
   colour; the same semantics `snap_colors.py` uses).

Weights come from a single Inkscape render, reused for every palette. Without
Inkscape the weighting degrades to element counts and says so in the report.

> **The heuristic cannot read your intent.** No colour-space distance knows that
> *this* red is the blossom (so it should become plum) while *that* green is
> foliage (so it should become sage). The `area` and `nearest` passes produce a
> plausible starting point, not a finished design. Every run writes
> `mapping.json` showing exactly what it chose, so pinning the result is a
> one-line edit.

`--preflight` is optional and off by default, so the layer adds no cost to the
standard flow. It is a separate layer that *calls* the pipeline — never the
other way round — and it never picks a winner: the report orders by
print-readiness gates (hard, then advisories), never by which variant looks
best.

### The library (`scripts/palettes.json`)

Six starting palettes — `red-cream`, `blue-charcoal`, `turquoise-black`,
`cool-luxe`, `warm-sunny`, `pop-playful`. Each entry is a `background`, a
`colours` list and an optional `map`. Adding your own is plain JSON; a colour
that is not a real `#rrggbb` is rejected rather than written into the artwork.

---

## Tests

```bash
.venv/bin/python -m pytest tests/
.venv/bin/pyflakes scripts/*.py validate_svg.py preflight.py  # lint
```

246 tests pass. The suite uses synthetic images generated in-test (Pillow),
never the real artwork, so it runs anywhere without the `00_source/` batch.
Tests that need system tools (inkscape, gs, qpdf, poppler-utils) skip
cleanly when those are absent — a fresh checkout without tools won't look
broken.

---

## More reading

- `OVERVIEW.md` — the design and the reasoning behind the two-layer gate.
- `HOWTO-print-check.md` — deeper walkthrough of the print-check concepts.
- `scripts/FRONT_HALF.md` — the front half in detail (sweep, fidelity metric,
  parallelism).
- `scripts/NODE_REDUCTION.md` — why no external tool was adopted for
  `geometry_overload`: byte optimizers do not reduce nodes, svg-simplifier
  crashes on 40% of real paths, Inkscape's default threshold costs 18% of the
  ink, and every tool ignores the shared boundaries between adjacent colour
  regions. Includes the measured trade-off curves.
- `AGENTS.md` — handoff notes for an AI agent (GPT/Hermes/etc.) picking this up.
