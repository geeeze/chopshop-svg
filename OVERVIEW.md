# Shirt artwork → print-ready SVG: overview

A two-gate quality system that checks vector artwork for T-shirt printing
**before** it goes to the printer, and tells you in plain language what will go
wrong if it does.

It answers one question: *will this file print, and how many screens will it
cost?* Not "does it look right" — that's your eyes — but the mechanical reasons
printers reject files and re-quote jobs.

> **Scope note:** this document covers the **print check** (the two gates and the
> reasoning behind them). The project has since grown a **trace stage** that turns
> a *raster* into vector candidates before the print check ever runs — see
> `README.md`, `scripts/TRACE_STAGE.md`, and the stage table below. The design
> reasoning here still holds; the "not built" caveats in the stage table were
> resolved when the trace stage shipped.

```bash
cd /path/to/chopshop-svg
./pipeline.sh 00_source/your-artwork.svg
```

Exit code 0 means send it. 1 means it lists what to fix. Every run produces a
rendered proof you can look at and a print PDF you can hand over.

---

## Why there are two gates

This is the whole idea, and it came from measurement rather than reasoning:

> **A two-stop gradient is written in the file as 2 colours and renders as
> hundreds.** It passes every check that reads the file. It is unprintable as
> spot colour.

So a file can be structurally perfect and still not printable. Source-level
checking is necessary but not sufficient — it cannot see what the artwork
*renders as*. The system therefore runs two independent gates and reports both:

| | **Layer A — source validation** | **Layer B — render preflight** |
|---|---|---|
| Input | the SVG's attributes and CSS | the rasterised artwork |
| Speed | fast, deterministic | slower (renders + measures) |
| Sees | colour count, stroke widths, path closure, node counts, size, palette | rendered ink/screen count, continuous tone, ink coverage, placed-bitmap resolution |
| Blind to | anything only visible in a render | anything invisible in pixels — a 0.85pt hairline renders as a normal line |

Neither replaces the other, and **where they disagree is the interesting part**:

| test file | Layer A | Layer B | why |
|---|---|---|---|
| 4-colour flat logo | pass | pass | agreement |
| 9-colour illustration | FAIL | FAIL | both count 9 against a budget of 6 |
| photographic | FAIL (raster embed) | FAIL (continuous tone) | agree, for different reasons |
| one-ink vector halftone | pass | pass | a dot field is binary in the render — correctly *not* "tone" |
| fine line art (0.3mm rules) | **FAIL** | pass | **B cannot see stroke width** |
| fixture (all labelled violations) | FAIL | FAIL | 7 declared colours, 7 screens — agrees with the labels |

The line-art row is the argument for keeping Layer A running: Layer B passes a
file whose lines will break up on press. The gradient above is the argument for
Layer B — a file that is clean on paper and unprintable in ink.

---

## How it works

```
   00_source/artwork.svg
            │
            ├──────────────────────────────┐
            ▼                              ▼
   ┌──────────────────┐          ┌──────────────────────┐
   │  STAGE 4         │          │  STAGE 4b            │
   │  Layer A         │          │  Layer B             │
   │  validate_svg.py │          │  preflight.py        │
   │                  │          │                      │
   │  reads the file  │          │  renders it:         │
   │  · raster embeds │          │  · Inkscape → proof  │
   │  · colour budget │          │  · Inkscape → PDF    │
   │  · stroke widths │          │  · counts inks       │
   │  · open paths    │          │  · detects tone      │
   │  · node counts   │          │  · CMYK separations  │
   │  · size, palette │          │  · fonts, image ppi  │
   └────────┬─────────┘          └──────────┬───────────┘
            │                               │
            └───────────────┬───────────────┘
                            ▼
                  05_final/manifest.json
                  (both layers, one record)
```

`pipeline.sh` runs both and decides. **A job passes only if neither layer raises
a hard finding**; advisories are reported and do not fail the run.

### What each stage does

| Stage | Tool | Status |
|---|---|---|
| 0. Print constraints (`spec.json`) | you | **built** |
| 1. Raster prep (background removal, upscaling) | `prep_raster.py` + `rembg`/Real-ESRGAN (optional) | **built** — trace stage, check-first |
| 2. Vectorisation (tracing) | `trace_sweep.py` (VTracer) | **built** — trace stage |
| 3. Cleanup & colour snapping | `snap_colors.py`, SVGO config | **built** (SVGO needs Node — unused here) |
| 4. Source validation (Layer A) | `validate_svg.py` | **built, 91 tests** |
| 4b. Render preflight (Layer B) | `preflight.py` | **built, 52 tests** |
| 5. Creative review | you / the printer | **manual** |
| 6. Export & handoff | `pipeline.sh` | **built** |

---

## What you need to provide

### 1. The artwork — an SVG

Vector artwork (`.svg`). Naming: put it in `00_source/`. The pipeline also
accepts a **PDF** (`.pdf`), which matters for the exact ink reading below.

### 2. A spec — `spec.json`

The contract for the job. A working default is provided; the important fields:

```json
{
  "print_method": "screen_print",     // or "vinyl" / "plotter" for cut work
  "max_colors": 6,                    // screens you're willing to pay for
  "dimensions": { "width_mm": 300, "height_mm": 400 },
  "palette": ["#000000", "#FFFFFF", …],  // agreed colours
  "geometry": {
    "min_stroke_width_pt": 1.5,       // ≈0.53mm — thinnest line that survives
    "max_nodes_per_path": 500,        // tracing bloat
    "allow_raster_embed": false,      // photos allowed?
    "allow_gradients": false,         // true only for CMYK process work
    "allow_open_paths": true          // derived from print_method if omitted
  },
  "print": {
    "dpi": 300,
    "min_image_ppi": 300,
    "dark_garment_underbase": true,   // ← see the next section
    "count_white_as_ink": true
  }
}
```

`spec.example.json` is the annotated starting point if you'd rather copy than edit.

### 3. Printer information — **you don't have this yet, and it's the thing to get**

Two numbers from the printer turn the ink-coverage check from *indicative* into
*exact*:

- **Their ICC profile** → `"icc_profile_path": "/path/to/profile.icc"`
- **Their ink limit** (total area coverage) → `"ink_limit_percent": 300`

Without them the tool still runs, but it says `ESTIMATE` and means it. One
conversation with the printer, before the first big job, removes most of the
remaining uncertainty.

### 4. The garment colour — this one is on you

See the next section. It is the only input that cannot be read from the file.

---

## Read this first if the shirts aren't white

**The tool assumes white fabric unless told otherwise.** This is the single
setting most likely to cost money.

No ink is no ink only when the fabric is already white. On a **dark** garment,
white is usually the **first** ink down — the underbase, printed beneath
everything else so the other colours have something to sit on.

| garment | white in the design | screens |
|---|---|---|
| white / light fabric | `#ffffff` fill | **not counted** — it is the garment |
| black / dark fabric | `#ffffff` fill | **counted** — it is the underbase |

For one test file that is the difference between **4 screens and 5**. On a
six-colour budget, that is the difference between passing and rejected.

Set it per job:

```json
"print": { "dark_garment_underbase": true }
```

The tool also **warns you when it thinks you've got this wrong**. Whenever white
is in the artwork but not being counted, it says so and gives both numbers:

```
  white: paper, not an ink (assumes a light/white garment)
LOG
  - white is declared in the artwork but is NOT counted as an ink, which assumes
    the garment is white — on white fabric that is correct... On a DARK garment
    white is very often the first ink down, laid as an underbase beneath
    everything else, so this job would need 5 screens, not 4.
```

If you don't know the garment yet, that warning is the thing to come back to
before the job goes to the printer.

---

## What you get back

Every run writes four things:

| File | What it's for |
|---|---|
| `05_final/<name>.proof.png` | What the artwork actually looks like at print resolution. Send this to the client. |
| `05_final/<name>.print.pdf` | The PDF for the printer. |
| `05_final/<name>.manifest.json` | Everything both gates found, with severities and the tool versions. The record. |
| `04_validated/<name>.layer_a.txt` | The source-level result on its own. |

Per-run copies are kept as well (`05_final/<stamp>.proof.png`, where `<stamp>`
is a timestamp + a few random bytes), so consecutive runs never overwrite each
other.

---

## Hard gates vs advisories

Not everything reported deserves equal authority. **Only hard gates fail the
run.** The manifest records which is which.

**Hard — the file cannot be printed as specified:**
raster embeds when disallowed · too many screens (declared *or* rendered) ·
strokes below the minimum · open paths on a cutting job · zero-area shapes ·
malformed SVG · placed images below the resolution floor · CMYK demanded but
absent · wrong physical size · continuous tone on a spot-colour job.

**Advisory — reported, does not fail:**
RGB-derived ink coverage (it cannot be trusted as a limit — see limitations) ·
non-embedded fonts (set `require_embedded_fonts: true` to make it a gate) ·
paths with more nodes than the limit · colours outside the palette · continuous
tone when `gradient_handling` is `embedded_raster`.

---

## Three findings that shaped the design

All three came from measuring real files, not from reasoning about them.

**1. Declared colour count ≠ rendered colour count.** Measured on a test poster:
**5 declared colours, 115 once rendered.** A gradient is a promise the file
cannot keep.

**2. A user unit is not a pixel.** SVG resolves a length to *user units*, then
the viewBox maps those onto the physical page. This was wrong in my own code
until I rendered strokes and measured them:

| document | prints at | naive px assumption said |
|---|---|---|
| `width="210mm" viewBox="0 0 210 297"`, `stroke-width="1"` | 2.8353pt | 0.75pt — **false failure** |
| `width="300mm" viewBox="0 0 3000 4000"`, `stroke-width="3"` | 0.850pt | 2.25pt — **false pass on a hairline** |

Wrong in both directions: rejecting printable art *and* waving through hairlines.
Now exact to **0.0000pt** against Inkscape across seven measured cases, and every
finding prints the basis (`so 1 user unit = 1.0000mm`) so the arithmetic can be
checked by hand.

**3. A photograph passed the ink gate as "1 ink".** Every one of its ~700,000
colours was individually too small to count, while 44.4% of the sheet was
covered by colours too small to see. Detecting tone needs the *collective* area
of those colours, not the per-colour area — measured 44.4% for a photograph
against 0.00–0.20% for every flat file in the batch.

---

## Current state — what's verified

**369 tests, all passing** (`pytest tests/`), lint clean. The print check:

| suite | tests | covers |
|---|---|---|
| `test_validate_svg.py` | 91 | every Layer A rule, unit scale, cascade, malformed input |
| `test_preflight.py` | 52 | ink measurement, tone detection, gate classification, garment assumption |
| `test_snap_colors.py` | 8 | colour snapping incl. CSS cascade writes |
| `test_validate_svg_negative.py` | 4 | the four headline failure modes, as a standalone contract |

The trace stage adds its own suites (prep, trace sweep, comparison, pick/loop,
palette variants, run record, requirements closeout); see
`scripts/TRACE_STAGE.md` and `README.md` for that coverage.

**Regression guards for the two measurement traps**, with synthetic fixtures so
they can't silently return:

- a **CMYK PDF with known 100 / 280 / 400% patches** — must read back as
  100 / 279 / 400%. Fails if an ICC profile is ever applied to CMYK input again,
  which is the bug that turned a measured 400% into 293%.
- **plate-polarity guards** — a white page must read ~0% ink and a black page
  must read high. tiffsep's 255 = *no ink* convention is undocumented, and
  getting it backwards produced a confident, meaningless 298% on bright orange.

**Test batch** (`00_source/`, 7 files → `04_validated/batch_results.json`), run
under `spec.json` (dark garment, white counted):

| file | Layer A | Layer B | rendered inks | declared | screens |
|---|---|---|---|---|---|
| flat logo | pass | pass | 5 | 5 | 5 |
| 9-colour illustration | FAIL | FAIL | 9 | 9 | **10** |
| photographic | FAIL | FAIL | 2 | 2 | 2 |
| vector halftone | pass | pass | 2 | 2 | 2 |
| fine line art | FAIL | pass | 2 | 2 | 2 |
| viewBox scale case | pass | pass | 3 | 3 | 3 |
| fixture (all violations) | FAIL | FAIL | 7 | 7 | 7 |

**Rendered ink count equals declared colour count on every file** — that is the
internal consistency check that the ink counter is honest.

The **screens** column is the money number, and the 9-colour illustration shows
why it is not always the same figure: its artwork contains no white, so the
underbase adds a **tenth** screen. Under a light-fabric spec the flat logo drops
to 4 screens — the same file, one screen fewer, purely because of the garment.

**Toolchain** (versions recorded in every manifest, because a result is hard to
interpret later without them): Inkscape 1.4 · Ghostscript 10.05.1 · qpdf 12.2.0 ·
lxml 6.1.3 · Pillow 12.3.0 · numpy 2.5.3 · Python 3.13.5.

---

## Honest limitations

These are real. They affect when you can trust a number.

**Ink coverage is only exact if the file is genuinely CMYK.** Your SVG is RGB.
Ghostscript converts it for the check but applies no press-grade black
generation: measured, a **50% grey comes out at 145% ink** instead of ~50%, and
**nothing exceeds ~296%**, so a 300% limit can never fire on an RGB input. The
report says `ESTIMATE` and prints a note. For an exact reading, get the printer's
profile, convert once, and re-run on the PDF — then it is exact and becomes a
hard gate. On a dark garment that coverage figure is also a **lower bound**,
because a CMYK separation on paper cannot represent a white laydown under the
whole design.

**Colour similarity is judged by rough RGB distance.** Good enough to separate
deliberate ink choices from rendering noise. Not a colour-managed proof.

**Overlapping transparent shapes and overprint effects** produce blended colours
counted as screens. If the design leans on those, ask the printer.

**Stage 3's SVGO half cannot run here** — SVGO needs Node.js, which this machine
does not have. The config (`scripts/svgo_print.yml`) is correct and ready for a
machine that has it; `scripts/snap_colors.py` covers the colour half in pure
Python.

**It doesn't replace a printed proof.** It catches the mechanical reasons files
get rejected. It can't tell you a colour looks wrong on the chosen stock, and it
can't tell you whether the job is *economically* right — process vs spot, ink
charges, stock choice. That's a conversation with the printer.

---

## Command reference

```bash
# the whole pipeline, both gates
./pipeline.sh 00_source/art.svg
./pipeline.sh 00_source/art.svg --dpi=300      # proof resolution
./pipeline.sh 00_source/art.svg --layer-a-only # skip the render
SPEC=other.json ./pipeline.sh x.svg            # different spec
./pipeline.sh                                  # first SVG in 00_source/

# the layers separately
.venv/bin/python validate_svg.py art.svg spec.json
.venv/bin/python preflight.py art.svg spec.json --json report.json
.venv/bin/python preflight.py converted.pdf spec.json   # exact ink reading

# stage 3: snap colours to the palette
.venv/bin/python scripts/snap_colors.py 00_source/art.svg spec.json --dry-run
.venv/bin/python scripts/snap_colors.py 00_source/art.svg spec.json -o 03_cleaned/art.svg

# node reduction — the geometry_overload remedy (§5 of scripts/NODE_REDUCTION.md).
# Target the over-gate paths only; rewriting the whole file makes things worse.
.venv/bin/python scripts/node_reduce.py 02_traced/00-example/candidate_11.svg \
    --out reduced.svg --max-nodes 500 --verify

# batch experiment bench: sweep trace settings against one raster, measure every
# cell on the same axes, pick nothing. `--with-node-reduce` also reports each
# cell's post-reduction figures and a `structure` column (Z/M counts vs input).
.venv/bin/python scripts/tune_sweep.py 01_prepped/00-example.prepped.png \
    --spec spec.json --preset baseline,nodewise,coarse,flat4 \
    --fidelity --contact-sheet --with-node-reduce

# the batch
.venv/bin/python scripts/run_batch.py          # → 04_validated/batch_results.json
.venv/bin/python -m pytest tests/ -q           # 369 tests

# open the artwork by hand
inkscape 00_source/art.svg
```

Exit codes: **0** passed · **1** failed (reasons printed) · **2** bad usage.

---

## A sensible first job

1. Put the artwork in `00_source/`.
2. Set `max_colors`, `dimensions` and the **garment colour** in `spec.json`.
3. Run `./pipeline.sh 00_source/art.svg`.
4. Fix what Layer A reports first — it's usually cheaper, and often clears the
   render result too.
5. Then re-run. Look at `05_final/<name>.proof.png` — that's what will print.
6. Send the printer `05_final/<name>.print.pdf`, and ask for their ICC profile
   and ink limit.
7. Convert with their profile, then re-run on the PDF with
   `"require_cmyk": true`. Now the coverage check is exact, and the manifest is
   your record of what was checked and with which tools.

---

## Where things live

```
.venv/                     python environment (lxml, svgpathtools, Pillow, numpy, pytest)
spec.json                  the active job contract
spec.example.json          annotated starting point
pipeline.sh                runs both gates, writes the manifest
validate_svg.py            Layer A — source validation
preflight.py               Layer B — render preflight
HOWTO-print-check.md       practical usage guide + full settings table
OVERVIEW.md                this document
scripts/snap_colors.py     stage 3 — colour snapping
scripts/svgo_print.yml     stage 3 — SVGO config (needs Node.js)
scripts/run_batch.py       batch runner
scripts/prep_raster.py     trace stage — raster prep (check-first)
scripts/trace_sweep.py     trace stage — VTracer candidate sweep
scripts/compare_candidates.py  trace stage — Layer A/B + fidelity report
tests/                     369 tests
00_source/                 input artwork (+ the test batch)
01_prepped/ 02_traced/ 03_cleaned/   stages 1–3 (trace stage writes 01/02)
04_validated/              Layer A output + batch_results.json + comparison reports
05_final/                  proof.png, print.pdf, manifest.json
06_run/                    run-record JSON (archive spine)
```

---

## The one-line version

Two gates check the artwork — one reads it, one renders it — and the printer
gets a PDF, a proof and a manifest. Set the garment colour before you start, and
get the printer's ICC profile before the first big job. Everything the tool
can't know is stated rather than guessed.
