# chopshop-svg · v0.1.0

Turn a raster image into print-ready vector art — automatically.

**chopshop-svg** is a T-shirt print pipeline. You give it a picture (a logo, a
scan, a photo, an AI-generated image); it produces a set of traced vector
candidates, measures each one against the rules a real screen-printer cares
about (colour count, node weight, stroke thickness, coverage), and shows you the
trade-offs. **It never picks a winner** — tracing quality is a visual judgment,
so a human chooses from the candidates and metrics the pipeline lays out.

The project is two stages that you use in sequence:

1. **Trace stage** — raster → vector candidates. `front_pipeline.sh` runs prep →
   trace sweep → comparison. You look at the report, pick a candidate by eye.
2. **Print check** — the chosen vector → print-ready. `pipeline.sh` runs it
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
the print check on each in turn:

```bash
./front_pipeline.sh artwork.png --loop
```

### Three side tools — plus a fourth bench that nothing runs

None is part of the default workflow above. All three run by hand, **or** from
the spec: set `print.prep_expand` in `spec.json` and `front_pipeline.sh` runs
them at the two points where they belong — `filters` and `tune` after prep (both
work on the raster), `node_reduce` after the sweep (it needs candidates). The
studio's JSON page has the controls, under **Prep — expanded**. Off by default:
a spec without the key behaves exactly as before, and an empty block is a no-op.

**`scripts/palette_variants.py` is not one of them, and no pipeline call reaches
it.** It is a fourth bench and it is **standalone and unwired**: no stage in
`front_pipeline.sh` or `pipeline.sh` runs it, it has no `prep_expand` key and no
spec key of its own, and no pipeline stage calls its command line (see *Palette
variations* below). Unwired as a *stage* is not the same as unused as a
*module*: the proof-variant dataset stage imports its colour-mapping machinery
(`proof_variants.py` calls `as_hex` / `apply_mapping` / `declared_colours`), so
the colour maths has one tested implementation and two callers. The three above
are wired as stages; this one is the exception.

```bash
# the same three, driven by the spec, in the right order
.venv/bin/python scripts/prep_expand.py --phase prep      01_prepped/art.prepped.png spec.json --out-dir out/
.venv/bin/python scripts/prep_expand.py --phase post-trace art spec.json --out-dir out/
# -> out/prep-expand.json, out/prep-expand.md, plus each tool's own report

# when run by the pipeline the artifacts land in 06_run/<stem>/prep_expand
# (PREP_EXPAND_OUT overrides that), and `reduced` copies are extra candidates: the
# originals are never modified, so both are graded in the same comparison.
```

**`scripts/prep_expand.py`** — the integration point for the other two (plus
`im_filters.py` below): one spec key, one report, run in the right order. It
exists so the studio can drive these benches without a second source of truth
for what ran, and so a job's artifacts land inside the job directory rather than
the shared `08_tune`/`09_filters` trees — which is what puts them in the archive
download and removes them on delete.

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

**The graded candidate set is the sweep's own record, not the directory.**
`02_traced/<stem>` is shared per artwork stem and persists across runs, so a
rerun with a smaller cap, a different preset set or a lower `max_candidates` used
to leave the previous run's higher-numbered `candidate_NN.svg` files on disk,
where `compare_candidates.py` graded them as if this sweep had produced them.
`compare_candidates.py` now grades exactly the `candidates` list in
`02_traced/<stem>/sweep.json`. Anything else matching `candidate_\d+\.svg` is
reported as a non-graded **stale** bucket (`stale_candidates`,
`stale_candidate_count` in the comparison JSON and a *Not graded (stale)* section
in the markdown) and is never rendered or measured. When `sweep.json` is absent
or unreadable the old directory glob is used, and `graded_set_source` says which rule
applied. `trace_sweep.py` also deletes the candidates it did not write, after a
successful sweep (recorded as `stale_removed` in `sweep.json`) — but the record
is the authority if the two ever disagree. Candidate naming is unchanged:
`candidate_%02d.svg` by sweep index, and a second job with the same artwork name
continues the same numbering.

---

## System tools (not Python — install via your OS)

| Tool | Needed by | Status |
|---|---|---|
| Inkscape | render the SVG to a proof PNG + fidelity diff (print check + trace stage) | **required** |
| Ghostscript (`gs`) | PDF colour separation in Layer B | **required** |
| qpdf | PDF inspection in Layer B | **required** |
| VTracer | raster → vector tracing (trace stage) | in `requirements.txt` |
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
the trace stage is a tracer (VTracer, via `requirements.txt`).

---

## The job contract (`spec.json`)

The whole pipeline is driven by one JSON file, `spec.json` (start from
`spec.example.json`). It is the single statement of what "printable" means for
a job: the colour budget, the geometry limits, the tolerances, and the
trace-stage prep/trace behaviour. Every gate in Layer A and Layer B reads from
it; any key you omit falls back to its default below.

### Top-level keys

| Key | Type | Default | Meaning |
|---|---|---|---|
| `print_method` | string | `""` | e.g. `screen_print`, `dtg`, `vinyl`. Drives a few derived defaults (e.g. open-path tolerance). |
| `max_colors` | int | *(none)* | Colour budget — Layer A hard gate (declared colours must not exceed it). |
| `palette` | string[] | *(none)* | The expected palette as `#rrggbb`; Layer A checks the artwork's colours against it. Also the trace stage's palette axis. |
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
| `allow_raster_embed` | bool | `false` | Whether raster embeds are permitted (default bans them). Layer A counts every route to a bitmap: `<image>` anywhere (including inside a `<pattern>`), `<feImage>` with a `data:`/file/external href, `<foreignObject>` (it rasterises XHTML), an `<image>`/`<use>` naming an external document or a `data:` URI, and `url(data:image/...)` in a `<style>` block or an inline style. A document-internal `href="#id"` stays vector and is not reported. Layer B's placed-image PPI check still reads the rendered result. |
| `allow_gradients` | bool | `false` | Whether gradients / continuous tone are allowed. Also gates the trace stage's `photo` preset. **When false this is a Layer A hard gate** (`GRADIENT_NOT_ALLOWED`), not only a Layer B measurement: a declared `<linearGradient>`/`<radialGradient>`, or any paint-server reference (`url(...)` in `fill` / `stroke` / `stop-color` / `flood-color` / `lighting-color`, whether written as an attribute, in an inline style or in a `<style>` block), fails the file, naming each offender and what its `url()` resolves to. A `url(...)` that resolves to nothing in this document is reported too — never passed, because it cannot be proven to be a flat colour. Layer B still measures the rendered tone (`CONTINUOUS_TONE`): a declaration is not an ink count. |
| `allow_open_paths` | bool | `true` | Whether open (unclosed) paths are acceptable. |
| `gradient_handling` | string | `""` | How gradients are treated: `vector_halftone`, `embedded_raster`, etc. |
| `halftone_handling` | string | `""` | How halftones are treated. |

### `validation`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `run_preflight` | bool | `true` | Whether `pipeline.sh` runs Layer B (render preflight) at all. |
| `variants` | bool \| object | `false` | *Print check* — the opt-in tail of the print check: derive a deterministic family of **similar copies** of the chosen proof (mirrored, rotated, colour-shifted) and package the raster **and** the vector of each with a dataset manifest and a contact sheet, so an augmented training set for this pipeline can be eyeballed as-is. `false` (the default) writes nothing new, so a job that did not ask cannot be mistaken for one whose stage broke; `true` emits the whole vocabulary (`flip-h`, `flip-v`, `rot180`, `rot90`, `rot270`, `transpose`, `invert`, `hue-90`, `hue-180`, `hue-270`, `channel-swap`, `palette-cycle`); `{"enabled": true, "transforms": [...]}` selects a subset, and an unknown name refuses the stage with the vocabulary quoted rather than being skipped. `rot90`/`rot270` are clockwise quarter turns on both halves; `hue-*` goes through PIL's 8-bit HSV, so a 90° turn is a 64-step offset and the last bit of each channel is approximate. **`palette-cycle` measures itself**: an exact cycle only permutes pixels/fills that equal a palette colour, which on traced continuous-tone art is almost none, so below 0.5% moved it redoes the cycle through PIL's nearest-colour search (a *requantisation* onto the palette) and says so in the notes, or — when the cycle cannot move that artwork at all — is skipped as **not applicable to it** rather than shipping a near-duplicate. No variant may be a silent duplicate: each transform records the share it moved, and one below the floor (a flip of symmetric art, a hue shift of a grey proof) is named in the notes. Nothing is ranked — every copy the artwork admits is emitted, one it cannot admit is skipped and named in the notes (`count` and `transforms` describe the files that were actually written), and a real fault still discards the whole set. The stage is additive, runs after the proof is written, and never fails the job: a fault is recorded as a note on the manifest. |
| `flag_on_any_failure` | bool | `true` | Any hard finding fails the job. |
| `auto_retry_limit` | int | `3` | Retry budget (used by the print-check runner). |

### `print`

The render-preflight tolerances and the trace-stage prep/trace defaults.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `dpi` | number | `300` | Render/measurement DPI. |
| `min_image_ppi` | number | `300` | Minimum effective PPI for placed bitmaps. |
| `ink_area_threshold_percent` | number | `0.05` | A colour must cover this share of the sheet to count as an ink (drops anti-alias fringe). |
| `ink_merge_tolerance` | number | `20` | Colours closer than this (Euclidean sRGB, max 441) merge into one ink. |
| `ramp_min_members` | int | `6` | A merged ink built from ≥ this many colours is treated as a gradient/ramp. |
| `count_white_as_ink` | bool | `false` | Count pure white as an ink (default excludes it, assuming white substrate). |
| `dark_garment_underbase` | bool | `false` | Dark-garment underbase: implies `count_white_as_ink` (white = the underbase screen). |
| `substrate` | hex \| palette name | *unset* | The **fabric** colour: this palette entry is the garment, not a screen, so nothing is laid down for it. Kept transparent by the pitch/inverse stage, excluded from the manifest's `screens` list, and reported as `substrate.is_fabric`. |
| `substrate_index` | int | *unset* | Index into `palette`, as an alternative to naming the colour. |
| `require_embedded_fonts` | bool | `false` | Font substitution is advisory unless this is set. |
| `assume_opaque_bg` | bool | `false` | *Trace stage* — skip background removal in prep. |
| `prep_colors` | int \| null | `16` | *Trace stage* — pngquant colour budget in prep; `null` disables quantisation. |
| `background_hex` | string | `"#ffffff"` | *Trace stage* — colour to flatten alpha onto before tracing. |
| `sweep_max_candidates` | int | `12` | *Trace stage* — cap on the number of traced candidates. |
| `prep_expand` | object | *(absent)* | *Trace stage* — run the expanded prep benches, at the two points they belong. Absent (or an empty block) runs nothing. Sub-keys: `filters` (`presets`, `contact_sheet`, `keep_alpha`, `spec`) and `tune` (`presets`, `max_cells`, `contact_sheet`, `with_node_reduce`, `filter_speckle`, `hierarchical`) run after prep; `node_reduce` (`max_nodes`, `tolerance`, `tolerance_cap`, `verify`) runs after the sweep and writes a reduced **copy** of every over-gate candidate as a fresh `candidate_NN.svg`. `max_nodes` left out falls back to `geometry.max_nodes_per_path`, so the bench and the gate cannot disagree. All of it is measurement, and it never picks a candidate. |
| `invert` | bool \| object | `true` | *Trace stage* — also write a colour-inverted **twin** of the prepped raster (`<stem>.prepped.inverse.png`), in both check and fix mode. `{"mode": "negative"}` inverts chroma and **preserves alpha** (use it for a transparent matte); the default `photometric` inverts every band and therefore flattens a transparent background to opaque. **Caveat:** this is a blind per-channel flip, so on a substrate-dominated source it turns the fabric into a full-bleed ink flood -- the shipped example went from 0.00% to 91.93% of the sheet over the 300% limit. Prefer the `pitch_shift` stage's `inverse` variant, which identifies the substrate (see `substrate` above) and leaves it transparent instead. The twin is a derived artefact and may be absent; its presence is always recorded in the `.prep.json` sidecar under `inverse`. A stale twin from an earlier run is deleted when this is turned off, since the prep dir is globbed. |

### Notes

- **Size/orientation is not prescribed by default.** With no `dimensions` block,
  the size gate is skipped and prep reports the source's own dimensions as
  information. Add `dimensions` only for a job with a fixed print size.
- **`spec.json` is the active contract; `spec.example.json` is the annotated
  starting point.** Copy the example and edit, or merge a fragment over it —
  the loaders merge over defaults, so a minimal spec is fine.
- The trace stage owns the four `print.*` keys marked *Trace stage* above; the
  rest belong to the print check. See `scripts/TRACE_STAGE.md` for the trace-stage
  specifics.
- **Layer A enforces what the spec says, and reports what it cannot prove.**
  The source-level gate reads declarations. What it *can* read it now enforces:
  `allow_gradients` is a hard gate (`GRADIENT_NOT_ALLOWED`), the colour budget
  counts the implicit black of a shape whose cascade never sets `fill` and
  resolves `currentColor` from the inherited `color`, the stroke-width check
  ignores paint that cannot ink (`display:none`, `visibility:hidden`,
  `opacity:0`, `stroke-opacity:0`, geometry inside a `<clipPath>`, and
  definition content nothing references), and `!important` is ranked in the
  cascade. What it cannot read it reports as NOTES, never as failures — a
  `filter` / `mask` / `clip-path` reference (`EFFECT_REFERENCE`), an opacity
  strictly between 0 and 1 (`TRANSLUCENT_PAINT`), and skipped definition
  content (`UNCHECKED_DEFINITION`, with a count, so partial coverage is
  visible). Nothing unmeasurable can fail a job. The honest list of what is
  still out of reach — the rendered result, a gradient's real ink, a blend, a
  font substitution — is the "Known limitations" section of `validate_svg.py`;
  read it before trusting a pass.

---

## Layout

```
chopshop-svg/
├── front_pipeline.sh          # TRACE stage orchestrator: prep → sweep → compare
│                              #   (+ the spec-gated expanded prep benches)
├── pipeline.sh                # PRINT CHECK: validate + preflight a chosen SVG
├── runner.py                  # HTTP runner: POST /jobs, GET /jobs/:id,
│                              #   GET /files/:job_id/<name>, DELETE /jobs/:id,
│                              #   POST /compose (the vector-preserving merge)
│                              #   (see docs/runner-http-contract.md)
├── validate_svg.py            # Layer A — source-level SVG checks
├── preflight.py               # Layer B — render + colour/ink/coverage checks
├── spec.json                  # the active job contract (see spec.example.json)
├── requirements.txt           # Python dependencies (required + optional)
├── Dockerfile / docker-compose.yml  # the container image and the runner service
├── docs/
│   ├── OVERVIEW.md            # why the two gates, and the design reasoning
│   ├── HOWTO-print-check.md   # running and reading the print check
│   ├── runner-http-contract.md # the runner's HTTP surface (promoted from the
│   │                          #   in-repo skill; kept in sync by hand)
│   └── agents/                # engineering-skill config (issue tracker,
│                              #   triage labels, domain notes)
├── CONTEXT.md                 # working state + the canonical test count
├── scripts/
│   ├── prep_raster.py         # front: check/normalise a raster (--fix to modify)
│   ├── prep_expand.py         # front: runs the expanded prep benches from
│   │                          #      spec.print.prep_expand (filters, tune,
│   │                          #      node_reduce), one report for both phases
│   ├── trace_sweep.py         # front: multi-pass VTracer candidate sweep
│   ├── compare_candidates.py  # front: run every candidate through A + B,
│   │                          #      then rank on whether the artwork survived
│   ├── pick_finish.py         # front: --loop menu driver
│   ├── front_common.py        # front: shared helpers (sha256, tool versions,
│   │                          #      the default print.* values)
│   ├── im_filters.py          # bench: named ImageMagick raster passes (chopshop-im)
│   ├── tune_sweep.py          # bench: re-trace one raster under many parameters
│   ├── node_reduce.py         # bench: the geometry_overload remedy (over-gate paths)
│   ├── snap_colors.py         # snap a trace's colours to a palette
│   ├── compose_svg.py         # merge layers into one SVG, vectors intact (the
│   │                          #      composition operation — POST /compose)
│   ├── palette_variants.py    # aux: re-colour a finished trace onto named palettes
│   │                          #      (standalone — nothing in the pipeline calls it)
│   ├── palettes.json          # palette library for palette_variants.py
│   ├── run_batch.py           # batch-run the print check over a folder
│   ├── run_record.py          # provenance spine: stitch a run into 06_run/<stem>.run.json
│   ├── closeout.py            # pass/fail board per candidate vs requirements.json
│   ├── triage.py              # P1 gates + bucket key + canonical form + text sim
│   ├── triage_events.py       # P2 job-independent learning store (events.jsonl)
│   ├── triage_ranker.py       # P4 preference ranker + P5 bucket bandit
│   ├── disk_check.py          # disk-capacity gate called by both shell entry points
│   ├── orphan_sweep.py        # artifacts of deleted jobs (run by a host-local timer)
│   ├── make_example_source.py # rebuild 00_source/00-example.png
│   ├── TRACE_STAGE.md         # trace-stage documentation
│   ├── NODE_REDUCTION.md      # node-reduction sourcing research (no tool adopted)
├── requirements.json          # requirements matrix (R-NN | requirement | source | check)
├── tests/                     # pytest suite (synthetic fixtures only)
├── skills/                    # in-repo agent skill (references/ docs)
├── 00_source/                 # example raster/SVG batch for the print check
└── 01_prepped/ 02_traced/ 03_cleaned/ 04_validated/ 05_final/ 06_run/ 07_palettes/ 08_tune/ 09_filters/   # generated (gitignored)
                              #  05_final/ also holds per-artwork subdirs and
                              #  <artwork>.<candidate>.<RUN_ID>.* run copies
```

---

## Run record + closeout (the archive spine)

`scripts/run_record.py` stitches one trace-stage run into a single provenance
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
both flagged as missing links until they exist). The artwork-scoped name is
preferred; the shared flat name is still read as a fallback:

- `05_final/<artwork>/<candidate>.pick.json` (preferred) or the legacy shared
  `05_final/<candidate>.pick.json` — `{"selected": true, "label": "production|style_reference|needs_retrace|discard", "reason": "..."}`
- `05_final/<artwork>/<candidate>.visual_review.md` (preferred) or the legacy
  shared `05_final/<candidate>.visual_review.md` — the visual-inspection report

`run_record.py` reads the artwork-scoped name first and falls back to the shared
one only when it is absent; when it does, it marks that artefact
`"path_scope": "flat-shared"` and lists it under the candidate's `anomalies`, so a
shared file is never recorded as if it were this run's.

### Artefact naming: every output names its artwork

`pipeline.sh` publishes each artefact under three names, additively. The artwork
stem is the directory the candidate was run from (`02_traced/<artwork>/`, which
is what `runner.py` passes in); a path handed straight to the script
(`00_source/art.svg`) is its own artwork.

| Name | Notes |
|---|---|
| `05_final/<candidate>.{manifest.json,proof.png,print.pdf,layer_b.txt}`, `04_validated/<candidate>.layer_a.txt` | The canonical names — `runner.py`, the studio and `orphan_sweep.py` are built around them. **Shared**: every artwork whose trace produced a `candidate_04` writes the same `05_final/candidate_04.manifest.json` |
| `05_final/<artwork>/<candidate>.{manifest.json,proof.png,print.pdf,layer_b.txt}`, `04_validated/<artwork>.<candidate>.layer_a.txt` | Artwork-scoped — the only names that separate two artworks whose trace produced the same candidate file stem. Preferred by `run_record.py` |
| `05_final/<artwork>.<candidate>.<RUN_ID>.{manifest.json,proof.png,print.pdf}` | Run-unique copy of one back-half run (`RUN_ID` = timestamp + 4 random bytes), attributable to artwork *and* candidate |

Before this, the flat name was shared state: a later artwork's run record could
read an earlier artwork's manifest, proof and human pick, because the only stem a
back-half run knows is the candidate's file name. Layer B's scratch directory is
per (artwork, candidate) now too — `.preflight-work/<artwork>/<candidate>/` — so
two runs of the same candidate stem can no longer race on the same proof, PDF or
`sep*.tif` cleanup. `05_final/` is not a swept column (`orphan_sweep.py`'s
`STEM_DIRS` are `01_prepped/`, `02_traced/`, `04_validated/`), so an artwork
subdirectory there is never classified as an orphan; the scoped Layer A report is
`<artwork>.`-prefixed in `04_validated/`, so it stays attributable to its artwork.

---

## Candidate triage + learning stack (`scripts/triage*.py`)

A five-phase triage layer that sits **on top of** the trace stage: it can gate,
group, order and schedule candidates, and it can learn from what a human does
with them. The whole layer is three stdlib-only modules (no numpy, scipy or
sklearn — it has to run wherever the pipeline runs) and **none of the five phases
picks a winner**: `gate` drops, demotes or passes; `rank` orders; `choose_bucket`
picks which parameter *family* the sweep should try next. Every candidate still
reaches a human, unfiltered.

| Phase | Module | What it is |
|---|---|---|
| P1 — quality gates | `scripts/triage.py` | deterministic `HARD` / `WEAK` rule lists → `DROP` / `DEMOTE` / `PASS`, each hit carrying a rule name |
| P2 — event log | `scripts/triage_events.py` | the job-independent learning store: append-only `events.jsonl`, `learned_buckets.json`, checksum sidecars |
| P3 — text similarity | `scripts/triage.py` | bigram / TF-IDF cosine over prompt, filename and finding text (`intent_groups`) |
| P4 — preference ranker | `scripts/triage_ranker.py` | pairwise logistic fit on what was shown vs what was picked; cold-starts on the hand order |
| P5 — bucket bandit | `scripts/triage_ranker.py` | beta-Bernoulli Thompson sampling over bucket keys: which family to try next |

### P1 — hard and weak gates (`scripts/triage.py`)

`gate(candidate)` returns a verdict with a `disposition` of `DROP` (any single
hard hit), `DEMOTE` (`weak_needed` weak hits) or `PASS`, plus the list of fired
rule **names** and a bucket key — so a decision is explainable from the JSON
alone. Hard rules today are `ARTWORK_LOST`, `SILHOUETTE_LOST`, `GLOW_AREA_HIGH`,
`COLOR_CAP_EXCEEDED` and `GEOMETRY_HARD_FAIL` (a hard geometry finding already
recorded by Layer A/B); weak rules are `SPECKLE_HEAVY`, `COLORS_NEAR_CAP`,
`LOW_LUMA_CONTRAST`, `NODE_OVERLOAD`, `BORDERLINE_FIDELITY`.

**The defaults live in `triage.py`** (`TRIAGE_DEFAULTS`), not in the docs — they
are the canonical values, and the optional `triage` block of `spec.json` is
merged over them key by key. Neither `spec.json` nor `spec.example.json` ships a
`triage` block today, so every job runs on the table below; a spec can retune the
gate without editing the script.

| key | default | gate it drives |
|---|---|---|
| `max_unique_colors` | `8` | hard — more unique colours than this drops the candidate |
| `min_silhouette_iou` | `0.85` | hard — silhouette overlap below this drops |
| `max_glow_area_pct` | `8` | hard — glow area past this share of the sheet drops |
| `max_mae_art` | `8.0` | hard — artwork MAE past this (or the `artwork_lost` fidelity verdict) drops; also the weak ceiling below |
| `borderline_mae_art` | `1.0` | weak — MAE between this and `max_mae_art` is a borderline hit |
| `max_speckles` | `40` | weak — speckle count past this |
| `min_luma_delta` | `12` | weak — luma separation below this |
| `max_nodes_per_path` | `500` | weak — a path over this node budget (mirrors `geometry.max_nodes_per_path`, so the gate and Layer A cannot drift apart) |
| `weak_needed` | `3` | how many weak hits demote a candidate |
| `learn_min_jobs` | `2` | distinct jobs required before anything is fit (P4) |
| `bucket_cap` | `2` | *declared, not yet read by a rule* — reserved for the bucket phases |
| `learn_min_rejections` | `3` | *declared, not yet read by a rule* — reserved for the bucket phases |

Every rule reads the candidate and returns `False` when a reading is *missing*, so
"not measured" and "measured bad" never collapse into the same verdict. The
observations Layer A/B does not record (`speckle_count`, `glow_area_pct`,
`luma_delta`, `silhouette_iou`) arrive under `comparison.metrics`.

The bucket key is `"{preset}|s{speckle_bin}|c{colour_bin}|{prompt_family}"`, with
deliberately coarse bins (`SPECKLE_EDGES` 0/5/20/40, `COLOR_EDGES` 1/3/5/8) whose
widest edge lines up with the corresponding default above. One human rejection of
one candidate can therefore retire every candidate that shares the key.

The same module holds the canonical-form and hash primitives: `canonical_svg`
(attributes sorted, `id`s dropped, path data tokenised and rounded to 2 decimal
places), `svg_sha` and `file_sha256`. They exist so a regenerate-and-reformat
cycle hashes equal — a "have I already seen this file" aid, not a claim of
semantic equivalence (the module docstring lists the caveats: child order is not
sorted, `url(#…)` references dangle, namespaces are preserved as written).

### P2 — the event log (`scripts/triage_events.py`)

The record of what the pipeline SHOWED a human, and of what was learned from it,
has to outlive the job: the artifact sweep deletes `06_run/<stem>/` when a job
goes, so the store lives at a **job-independent** path resolved from the
environment, never hardcoded (this repo is public):

```
PIPELINE_LEARNING_DIR   default: <repo>/06_run/_learning/
```

`06_run/_learning/` is the one subdirectory that deletion does **not** sweep,
which is what makes "survives job deletion" true by construction.

| file | shape |
|---|---|
| `events.jsonl` | append-only, one compact JSON object per line, **one line per candidate shown**; field order `t`, `job`, `svg_sha`, `event`, `pos`, `ranker`; the only event written today is `"shown"` and `ranker` is `null` |
| `learned_buckets.json` | `version: 1`, `bucket_key -> {shown, chosen, rejected}` (the P2 counters) plus the P5 bandit's `successes` / `attempts`, `last_t` and `veto` |
| `<artifact>.sha256.json` | the checksum sidecar `stamp_checksum` writes beside an artifact at creation time |

`pos` is the **1-based** position in the order the candidate was actually
presented in (`enumerate(candidates, 1)`) — the same scale the studio's own action
rows use — so mere exposure is learnable later, not just the pick. Each append is
a single `O_APPEND` write (atomic on POSIX, so concurrent workers cannot
interleave half a line) plus an in-process lock.

Everything here is **non-fatal by contract**: an unwritable learning directory
degrades to "no event recorded", never to a failed stage.

### P3 — text similarity (`triage.py`)

`norm` / `grams` / `cosine` / `text_sim` plus a small pure-Python TF-IDF
(`tfidf_vectors`, `tfidf_query`, `intent_groups`) for **prompts, filenames and
finding text — never for SVG geometry**. Character bigrams (`GRAM_N = 2`), digits
kept on purpose (dropping them would make `filter_speckle=8` and
`filter_speckle=16` score as identical strings), vectors sparse and L2-normalised.
`intent_groups` buckets near-duplicates at `INTENT_THRESHOLD = 0.8` as a
**grouping aid, not a ranker**: any ordering it produces keeps every index
visible, so the uncertain members of a group can still be seen and judged.

### P4 — the learned preference ranker (`triage_ranker.py`)

The training signal is the event log: a candidate that was shown and then starred
/ picked / proofed is the positive label, a candidate shown and never acted on is
the negative. A small **pairwise logistic** scorer is fit so that, for two
candidates shown in the SAME job, the acted-on one scores higher.

- **Cold start.** Until `learn_min_picks` distinct picks (default `COLD_START_PICKS
  = 30`) across `learn_min_jobs` distinct jobs (default `2`) have been seen — and
  at least one comparable pair exists — `rank` falls back to the pipeline's own
  **hand order**: fewest hard gates failed, then fewest advisories, then artwork
  MAE. Both knobs reach the fit through the spec's `triage` block. The model file
  is still written, with `kind: "cold_start"` and the counts that justified the
  fallback, so a reader can see *why* the pipeline is still on the hand order.
- **Features** are readings the pipeline already computes: `hard`, `advisory`,
  `node_count_max`, `mae_art`, `colours`, `speckles`, `bucket_speckle_bin`,
  `bucket_colour_bin`, an intercept, and one one-hot feature per preset it saw.
  Nothing is hand-scaled: the fit standardises with the training means it records.
- **Fit** is deterministic full-batch gradient descent — 300 iterations, learning
  rate 0.5, L2 0.01. Weights are keyed by feature name, in a fixed order.
- **The anti-feedback rule.** A learner trained on what the pipeline showed only
  ever re-presents its own opinion, so `rank` always keeps `n_random_slots`
  (default `1`) non-greedy slots inside the visible window (the first `8`
  positions). The slots are drawn from the 4 × slots candidates nearest the window
  cut, seeded and reproducible. Nothing is dropped, filtered or hidden: the return
  value is a permutation of the same candidates.
- **The split is by JOB, never by candidate** — a job's candidates never straddle
  the fit/holdout boundary, and a model is never fit on the candidates of one job
  alone.
- `score` records its own direction in the model (`higher_is_more_preferred`), so
  no JSON consumer can read it backwards. Model: `learned_ranker.json`,
  `version: 1`, `kind: cold_start | pairwise_logistic`.

```bash
.venv/bin/python scripts/triage_ranker.py --selftest   # deterministic self-check
.venv/bin/python scripts/triage_ranker.py train        # fit + persist; prints kind and reason
.venv/bin/python scripts/triage_ranker.py choose poster|s1|c0| bw|s2|c1|   # one bandit draw
```

### P5 — the bucket bandit (`triage_ranker.py`)

A **beta-Bernoulli Thompson-sampling bandit over bucket keys**, answering one
narrow question: *which parameter family should the sweep try next?* Per bucket it
keeps `successes` / `attempts` and samples
`Beta(1 + successes, 1 + attempts - successes)` with two stdlib `gammavariate`
draws; the +1 prior keeps an untouched bucket fully explorable.

- **`DEFAULT_EPSILON_EXPLORE = 0.1`** — with that probability it ignores the
  sample and takes a uniform random live bucket (`mode: "explore"`).
- **Staleness TTL = 30 days** (`BANDIT_TTL_SECONDS`): a bucket whose last outcome
  is older than that is re-explored from the prior, so a stale preference cannot
  pin the sweep for ever.
- **Veto**: `set_veto` retires a bucket for good (`vetoed_buckets`), and a veto
  list can also be passed per call. Every bucket vetoed or absent ⇒
  `choose_bucket` returns `None`.
- `bucket_decision` is the explainable form: the chosen bucket, the mode
  (`explore` / `exploit` / `none`), the reason, the sampled thetas, and the live /
  vetoed / expired buckets.

It chooses a **parameter family** — never a candidate and never a winner — and the
default draw is OS-seeded (pass a seeded `rng` to reproduce one).

### What is wired, and what is still a seam

| phase | reachable from | pipeline call site |
|---|---|---|
| P1 gates | `triage.gate(...)`, the tests | **none yet** — the comparison stage is the intended caller |
| P2 log | `compare_candidates.py` writes the `shown` events (at the point its ordered list becomes what a human sees); `trace_sweep.py` stamps each `candidate_NN.svg` checksum | **wired** |
| P3 text sim | the `triage.py` functions, the tests | none yet |
| P4 ranker | `triage_ranker.py train` / `rank(...)` | **none yet** — the comparison stage, once the action lines carry metrics |
| P5 bandit | `triage_ranker.py choose` / `choose_bucket(...)` | **none yet** — the sweep's parameter choice is the intended seat; nothing in `trace_sweep.py` changed for it |

The prep / proof / PDF / candidate checksum stamps beyond the traced candidate are
listed as explicit seams in the `triage_events.py` docstring, not wired. So the
honest summary is: a tested, CLI-reachable library sitting beside the pipeline,
with the event log already being written — not a layer that changes which
candidate the pipeline produces or which one a human picks. Tests:
`tests/test_triage.py`, `tests/test_triage_events.py`, `tests/test_triage_ranker.py`.

---

## Housekeeping: the disk gate and the orphan sweep

Two scripts that keep a long-lived install from filling up or drifting. Neither
is part of the artwork flow, and both are wired into the places that matter.

**`scripts/disk_check.py` — the disk-capacity gate.** Measures the filesystem
holding a path (`disk_check.py [path]`, default `.`) with `shutil.disk_usage`:

| free space | result |
|---|---|
| ≥ 2 GiB | exit **0**, silent |
| < 2 GiB | exit **0** + a `disk check: WARNING` line on stderr |
| < 1 GiB | exit **2** + `disk check: FAILED` — the caller must refuse to run |

Both `front_pipeline.sh` and `pipeline.sh` call it on the project root *before any
stage runs*, so a full disk refuses the job up front instead of failing it halfway
through with a half-written sweep on it. The runner enforces the same 1 GiB floor
itself before it builds a job's 7z archive (`MIN_ARCHIVE_FREE`) and answers `507`
rather than archiving.

```bash
.venv/bin/python scripts/disk_check.py .        # exit 2 refuses the run
```

**`scripts/orphan_sweep.py` — the hourly artifact sweep.** A studio job delete
removes the runner's job directory only. Everything the pipeline wrote into the
shared trees is keyed by the **input stem**, not by the job id, so it survives the
delete — as does the studio's own copy of the upload (measured on a live box: 5
orphan stems held ~215 MiB of traces and validation work, plus 14 orphan uploads,
~25 MiB). This script plans those leftovers and deletes only the ones a wrapper
has proved dead.

It is deliberately **host-agnostic** (this repo is public): it never queries
anything. The caller supplies the authoritative live-stem list — one per line or a
JSON list — and the host-local wrapper that knows where the box keeps its job
database produces it. The hourly timer belongs to that wrapper; it is not in this
repo, so nothing here runs on a schedule by itself.

```bash
.venv/bin/python scripts/orphan_sweep.py --live-stems-file live.txt \
    --root . --uploads-dir /data/uploads --grace-hours 24 --json-out plan.json
# dry run by default: it prints the plan and deletes nothing.
# add --apply to delete, --keep <stem> to protect one more stem (repeatable).
```

Windows: `--root` sweeps `01_prepped/`, `02_traced/` and `04_validated/`;
`--uploads-dir` sweeps source copies whose stem is not live; `--runner-root`
(together with `--runner-url`) sweeps runner job directories the runner itself
answers `404` for.

Safety properties, in the script's own order of importance:

- it **refuses to run on an empty live set** — an empty list would mark every
  artifact an orphan, so a failed query aborts the sweep rather than widening it
  (exit 2, nothing deleted);
- deletion needs an explicit `--apply`; without it the run is a dry run;
- anything modified inside `--grace-hours` (default `24`) is kept, so an upload
  just dropped on the new-job form is never swept;
- a stem matches as `name == stem` or `name.startswith(stem + ".")`, so stem `a`
  never matches `ab.svg`, and only a whitelist of suffixes the pipeline actually
  writes is stem-matched — `04_validated/candidate_01.layer_a.txt` is
  candidate-keyed, so it is reported as not-stem-keyed and left alone; the
  artwork-scoped `04_validated/<artwork>.candidate_01.layer_a.txt` IS
  `<artwork>.`-prefixed (so it is attributable), but its suffix is outside the
  whitelist, so it is reported and kept as well;
- symlinks are never followed (a symlinked entry is skipped and reported), so the
  sweep cannot be talked into deleting outside the tree it was given;
- the whole plan is aborted if it exceeds `--max-delete` (default `2000`);
- `00-example` is always kept — it is tracked reference output, and deleting it
  would dirty the working tree.

Exit codes: **0** success · **2** guard refusal (no window given, a bad
`--runner-root`/`--runner-url` pair, an unreadable or empty live-stem list, or a
plan over `--max-delete`) · **3** at least one deletion failed. Tests:
`tests/test_orphan_sweep.py`.

---

## Palette variations (`scripts/palette_variants.py`)

The Chopshop-Aided-Design layer. Takes a finished trace — a validated proof, a
snapped candidate, or the SVG a `05_final/` manifest was built from — and
re-colours it onto each palette in `scripts/palettes.json`. **The CAD structure
is never touched**: no path, node, `viewBox` or dimension changes, only
`fill` / `stroke` / `stop-color`. Same geometry, new colour vectors.

**It is standalone and unwired as a stage.** No stage, no spec key and no
pipeline call reaches this bench's command line: you run it by hand (or from your
own script). Its colour-mapping machinery is another matter — the proof-variant
dataset stage (`scripts/proof_variants.py`) imports `as_hex`, `apply_mapping`
and `declared_colours` from it, so the paint-rewriting code is shared and
tested once. The runner's recolour seam is the one other consumer of that same
machinery, and it is itself unbuilt — see `docs/runner-http-contract.md`.

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

## Composition (`scripts/compose_svg.py` + `POST /compose`) — the vector-preserving merge

The **missing composition operation**. The pipeline traces one raster into SVG
candidates and print-checks one SVG; it could not MERGE several pieces into a
single printable artwork. The obvious fix — rasterise the layers and re-trace the
composite — throws away the vectors the trace stage just produced (re-quantised
colours, doubled node counts, lost text and geometry). This merges instead, and
**keeps every vector layer a vector**.

```bash
# Merge a layer stack into one SVG
.venv/bin/python scripts/compose_svg.py composite.spec.json -o composite.svg
.venv/bin/python scripts/compose_svg.py - < composite.spec.json > composite.svg
```

```json
{
  "width": 3000, "height": 3000,
  "background": "#ffffff",
  "palette": ["#111111", "#ff0000"],
  "layers": [
    {"type": "svg",    "src": "<svg …</svg>",         "x": 0, "y": 0, "w": 300, "h": 300, "opacity": 1.0, "hue": 0},
    {"type": "raster", "src": "data:image/png;base64,…", "x": 0, "y": 0, "w": 300, "h": 300, "opacity": 1.0, "hue": 0}
  ]
}
```

`src` is always INLINE — an SVG document string or a `data:` URI — so compose
never touches the filesystem for layer content.

- **Vector layer** → the layer's inline SVG is parsed and its inner content
  wrapped in `<g transform="translate(x,y) scale(w/nw, h/nh)" opacity="…">`,
  where `(nw, nh)` is the layer's natural size: its `viewBox` when it has one,
  otherwise its `width`/`height`. (A non-zero viewBox origin is offset back into
  place, so a layer drawn around (100, 100) still lands where you asked.) No
  re-tracing, no node reduction, no colour re-sampling.
- **`hue`** → an `feColorMatrix type="hueRotate"` filter in `<defs>`, one unique
  id per layer. A FILTER ELEMENT rather than a CSS `filter:` declaration,
  because a rasteriser (rsvg, an Inkscape export) honours the element and would
  silently ignore the CSS, which looks correct in a preview and wrong on film.
- **Raster layer** → `<image href="data:…" x y width height opacity>`, embedded
  verbatim. `hue` on a raster layer is a documented follow-up: it is reported,
  never silently dropped.
- **`palette`** → the merged document's `fill` / `stroke` / `stop-color` are
  snapped through `snap_colors.py`'s own colour maths (`nearest_palette` /
  `write_property`, driven over the merged tree through the same CSS cascade —
  imported, not re-implemented). A near miss is snapped; a colour further than
  the tolerance is a **finding, not an error** — it is reported and left in
  place, because a palette the composite owns should not be used to silently
  force a nearest colour.

Exit codes: **0** merged (off-palette colours are reported on stderr, not fatal)
· **2** bad usage · **3** invalid spec. Tests: `tests/test_compose_svg.py`.

The runner exposes the same function as `POST /compose` for Chopshop Studio:
`{"op": "compose", "spec": {…}}` in, `200 image/svg+xml` with the merged document
out; an unmergeable spec is `422 {"error": …, "kind": "invalid_spec"}`. See
`docs/runner-http-contract.md`.

---

## Filter bench (`scripts/im_filters.py`) — "chopshop-im"

The front-half auxiliary layer that runs **before** the tracer. `prep_raster.py`
prepares a raster it was handed; this decides what to hand it. It applies one
named ImageMagick pass per variant — raster in, raster out — and every variant
is a candidate **input** to `./front_pipeline.sh`, never a trace.

```bash
.venv/bin/python scripts/im_filters.py --list
.venv/bin/python scripts/im_filters.py 00_source/art.png --contact-sheet
.venv/bin/python scripts/im_filters.py 00_source/art.png --presets kuwahara,flat6
.venv/bin/python scripts/im_filters.py 00_source/art.png --keep-alpha
./front_pipeline.sh 09_filters/art/flat6.png     # feed a variant forward
```

Output lands in `09_filters/<stem>/`: one PNG per preset, a contact sheet
(`--contact-sheet`), and `filters.json` / `filters.md`.

### Why it is a raster lever, not a palette lever

Most "too many nodes / too jagged" complaints are pixel-level noise, not colour
structure: VTracer traces literal pixel boundaries, so grain becomes hundreds of
corrective nodes. Measured in this repo, quantising the *source* does not reduce
node count (max nodes 967 → 912 → 1415 across 6–48 colours). Noise and soft
gradients are the lever; this is where they get pulled.

### The report is a measurement order, never a recommendation

| column | what it measures |
| --- | --- |
| `distinct colours` / `colours to 95%` | what a screen print has to hold |
| `edge energy` | mean &#124;Laplacian&#124;: the high-frequency floor the tracer will chase |
| `dither` | which dither the preset used — see below |

Rows are sorted by `colours to 95%`, then `edge energy`. Whether the **art**
survived a filter is not in the table: compare the PNGs (or the contact sheet).
Exactly like `tune_sweep.py`, it produces cells and metrics and picks nothing.

### Dither is the quietest lever here — and IM's default is the wrong one

ImageMagick dithers **by default**, and the default method is Riemersma, so a
bare `-colors 6` is silently dithering. Two things were measured on IM 7.1.1-43
rather than assumed:

- **Naming the default changes nothing.** `-dither Riemersma -colors 6` and
  `-dither Riemersma -remap pal.png` are *byte-identical* to the same commands
  with no dither flag. A "dithered" preset built on Riemersma reports success
  while emitting the undithered bytes. `tests/test_im_filters.py` pins this.
- **`-dither None` and `FloydSteinberg` really do differ**, and the cost shows up
  in the `edge energy` column: on the same 800×450 test image, `remap-spec`
  measured 2.76 against 53.35 for `remap-spec-dither` — a **19×** increase in
  high-frequency energy, which is precisely what inflates node count. `flat6`
  2.80 → `flat6-dither` 23.18 (8×).

So every reducing preset states its dither explicitly: `-dither None` is the
print reading (hard flat edges, clean separation), `*‑dither` variants use
FloydSteinberg as the photographic reading for comparison.

### Presets

`identity` and `flatten` are the controls. `identity` copies the source
**byte-for-byte** — an earlier version ran a no-op IM pass, and the alpha
compositing inside it moved 36 931 pixels of an 800×450 image (max Δ94), so the
"control" was itself a transform. `flatten` is that alpha flatten on its own, so
its cost is a reading instead of a hidden step inside every other variant.

Noise: `despeckle`, `median3`, `median5`, `kuwahara`, `bilateral` (a bilateral
stand-in via `-selective-blur`, preferred for skin/hair). Colour depth: `flat8`,
`flat6`, `flat6-dither`, `poster6`. Anisotropy: `offset-blend`. Chains:
`denoise-flat`, `denoise-then-edge`. Palette remaps: `remap-spec` /
`remap-auto` (± `-dither`), which `-remap` the image onto a generated palette
strip — the spec's declared palette, or the artwork's own 6-colour reading.

> **`offset-blend` is a directional blur, not noise cancellation.** The
> trace-stage notes propose averaging an offset copy as noise cancellation by
> the dark-frame-subtraction analogy. That analogy does not hold: the two noise
> samples are not independent (the noise is *in* the image, so the shifted copy
> carries it too), so nothing cancels — it convolves. It is still useful for
> anisotropy, but it is a blur, and offsets beyond ~2 px create double edges
> that VTracer traces as *extra* regions, so the preset caps the offset.

### Every variant comes out RGB

`-colors`, `-posterize` **and** `-remap` all write an **indexed** (mode `P`)
PNG by default — measured: `flat6`, `flat8`, `flat6-dither`, `denoise-flat`,
`poster6` and all four remap presets were mode `P`. The bench forces
`-define png:color-type=2`, so the rest of the chain sees one colour type. Only
`identity` is left alone, because it is the byte-exact control.

This is not cosmetic: on an indexed PNG Pillow's `getpixel` returns a palette
**index** — an `int`, not a colour — which made `prep_raster.py`'s
background-uniformity check raise `TypeError` internally and silently degrade to
`background: could not sample`. `prep_raster.py` now promotes a copy before
sampling, so it handles indexed input from any source (pngquant can emit it
too), and `tests/test_prep_raster.py` pins that.

ImageMagick is optional. With no engine on PATH the bench exits **3** with the
install command and never substitutes a Python filter for the named one. Exit
codes: 0 = every variant written, 1 = a variant failed, 2 = bad usage, 3 = no
engine.

### The output is reproducible, and no longer only on ImageMagick 7

The bench's contract is that parallelism is an optimisation which must not change
the result, and `test_parallel_matches_sequential` compares a `--workers 1` build
against a `--workers 3` build **byte for byte**. ImageMagick stamps every PNG it
writes with `date:create`, `date:modify` and `date:timestamp` tEXt chunks, so two
runs of the same preset a second apart were never byte-identical even with
identical pixels.

`-define png:exclude-chunk=date,time` removes them on ImageMagick 7 (measured:
7.1.1-43). It is **inert on 6.x** — measured on 6.9.12, all three chunks survive
it — so the flake came back on any host with ImageMagick 6, and the test only
passed when both builds happened to fall inside the same second. The bench now
also runs a documented post-pass (`strip_png_time_chunks`) that drops exactly
those three `tEXt` chunks from each non-`identity` output and rewrites the file
atomically, so the property holds on either major version. Every other chunk —
including `iCCP`/`sRGB`/`gAMA` profiles — and the pixel data are copied through
byte for byte and in order; `-strip` is deliberately **not** used because it also
removes profiles, which matters for print colour. `identity` stays a byte copy of
its source, control first.

## Tests

```bash
.venv/bin/python -m pytest tests/
.venv/bin/pyflakes scripts/*.py validate_svg.py preflight.py  # lint
```

The suite count is deliberately not repeated here: **`CONTEXT.md` is its single
canonical home**. The suite uses synthetic images generated in-test (Pillow),
never the real artwork, so it runs anywhere without the `00_source/` batch.
Tests that need system tools (inkscape, gs, qpdf, poppler-utils) skip
cleanly when those are absent — a fresh checkout without tools won't look
broken.

---

## More reading

- `docs/OVERVIEW.md` — what the project is, why there are two gates, and the design
  reasoning. Start here.
- `docs/HOWTO-print-check.md` — running and reading the print check: the garment
  setting, every rule and its severity, the measurement caveats, and the stage 3
  helpers.
- `docs/runner-http-contract.md` — the HTTP surface `runner.py` exposes to
  Chopshop Studio: every route, the job/validation lifecycle, the archive and
  file proxy, the disk gate, and what is in-memory. Start here if you are writing
  a client rather than running the pipeline.
- `scripts/TRACE_STAGE.md` — the trace stage in detail (sweep, fidelity metric,
  parallelism, and where the triage layer attaches).
- `CONTEXT.md` — the working state: what is in flight, the caveats that are not
  obvious from the code, and the **canonical test count**.
- `scripts/NODE_REDUCTION.md` — why no external tool was adopted for
  `geometry_overload`: byte optimizers do not reduce nodes, svg-simplifier
  crashes on 40% of real paths, Inkscape's default threshold costs 18% of the
  ink, and every tool ignores the shared boundaries between adjacent colour
  regions. Includes the measured trade-off curves.
- `AGENTS.md` — handoff notes for an AI agent (GPT/Hermes/etc.) picking this up.
