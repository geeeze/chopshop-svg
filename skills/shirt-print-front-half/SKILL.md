---
name: shirt-print-front-half
description: Use when tracing a raster into candidate SVGs for print.
---

# Shirt-print pipeline: front half (raster → traced candidates)

This repository is a two-half T-shirt print pipeline.
The BACK half (`validate_svg.py` Layer A, `preflight.py` Layer B,
`snap_colors.py`, `pipeline.sh`) checks an SVG for print readiness. The FRONT
half answers what comes before: given a raster, which vector traces are worth
feeding to the back half.

It NEVER picks a winner. Trace quality is a visual call for the human (or
GPT/Astra). It produces candidates + metrics only.

## Workflow

```bash
cd <repo root>
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

- `rembg` (`pip install 'rembg[cli]'`) — background removal. Skip via
  `spec.print.assume_opaque_bg: true`.
- `realesrgan-ncnn-vulkan` (GitHub release binary) — upscale. Falls back to
  Pillow LANCZOS.
- `pngquant` (`apt-get install pngquant`) — quantise to `spec.print.prep_colors`.
  `null` disables.

Missing binaries: skip the step, log a warning, record it in the sidecar —
NEVER crash the run. CMYK TIFF → convert to sRGB first. Very large inputs:
downscale-then-upscale (bounded memory).

## Sweep config: preset_params (new)

The sweep JSON now accepts a `preset_params` key that maps preset name →
dict of VTracer params, merged over the built-in PRESETS.  This lets the user
lower `color_precision` for a flatter trace without editing code:
```json
{"presets": ["poster", "poster_flat"], "preset_params": {"poster_flat":
 {"colormode": "color", "mode": "spline", "color_precision": 2}}}
```

## Tracer parameter levers: measured, orthogonal, and mostly inert

`scripts/tune_sweep.py` is a batch experiment bench (NOT the candidate
generator `trace_sweep.py` is). It sweeps trace settings against one raster,
measures every cell on the same axes, and writes `08_tune/<stem>/tune.md` +
`tune.json` + an optional `contact-sheet.png`. It ranks by MAE_art as a
MEASUREMENT ORDER and never recommends a cell.

```bash
.venv/bin/python scripts/tune_sweep.py 01_prepped/art.prepped.png \
    --spec spec.json --preset baseline,nodewise,coarse,flat4 \
    --fidelity --contact-sheet --with-node-reduce
```

Presets: `baseline` `flat` `flat4` `coarse` `nodewise` `nodewise_coarse`
`binary` `polygon`. Flags: `--filter-speckle --hierarchical --splice --length
--color-precision --layer-difference` (comma lists), `--max-cells`,
`--no-fidelity`, `--with-node-reduce`, `--contact-sheet`.

Measured on this repo's `01_prepped/00-example.prepped.png` (1024x1024, 245 608
source colours) — the two node levers are ORTHOGONAL and neither alone works:

- `layer_difference` moves path/colour COUNT (4->64: paths 18 053->2 863) and
  leaves max nodes at 1 528 throughout.
- `splice_threshold` moves the WORST SINGLE PATH (45->150: 798->588, monotone,
  colours unchanged) and leaves path count alone.
- `filter_speckle` moves path count, not max nodes.

**`layer_difference` is NOT monotone on max nodes**: ld32 gives 601, ld64 gives
763 — coarser, worse. Same non-monotonicity as Inkscape's simplify threshold.
Any one-axis-at-a-time sweep misleads; the node route needs a 2-D sweep.

Confirmed INERT (do not offer these as knobs):
- `max_iterations` — identical output at 10/20/50.
- `path_precision` — identical output at 1/2/3/4 (vtracer 0.6.15 ignores it).
- `corner_threshold` — default 60 is already the floor; 100 and 150 make max
  nodes WORSE (1 528 -> 1 624 -> 1 633).

`color_precision` 2 is a CLIFF: 1 path, 1 fill, whole image collapses. 3 is the
usable floor (3 127 paths, max 353), not 2.

**Quantising the SOURCE raster does not reduce node count.** Traced over
pngquant / PIL median / max-coverage / octree sources at 6-48 colours: max
nodes went 967 -> 912 (median 16) or -> 1 415 (median 6). It is a colour tool,
not a node tool. `print.prep_colors` will not help `geometry_overload`.

## Node reduction: `scripts/node_reduce.py`

The §5 solution, implemented. Clears the gate on every candidate measured
(965->467, 972->391, 1 054->499), touching 1-4 paths of 1 646-4 981, in ~3.7 s.
`--verify` on candidate_11: 0.0024% pixels changed, largest background blob
2 px.

```bash
.venv/bin/python scripts/node_reduce.py art.svg --out reduced.svg --verify
```

Targeted by default (`--all-paths` for the measured-worse whole-file mode).
Per-path tolerance escalation, doubling from `--tolerance` (0.5) to
`--tolerance-cap` (8.0). **Regression rejection** is TRANSACTIONAL: fits are
staged and only applied once every path passes, and the run is rejected if ANY
path grew — not merely `path_nodes_max` (a damaged small path used to be
committed as long as the worst path improved). A fit whose path data does not
re-parse is refused, since its node count then comes from a command-letter
regex that can under-count and read as a reduction.

The prototype's deviation-metric bug is fixed. Deviation is now a TWO-SIDED
nearest-point distance after Schneider re-parameterisation:
original->fit AND fit->original. Two traps found while fixing it, both of
which report a clean fit as dirty or vice versa:

- **Nearest-SAMPLE, not nearest-point-to-polyline.** Measuring the fitted curve
  against the nearest original *sample* reports half a sample spacing of
  discretisation error — 1.27 units on a perfectly straight 100-unit line.
  Distance must be to the POLYLINE through the original points.
- **t snapped to the sample grid, not refined.** Snapping t to the nearest of
  `count` curve samples leaves up to half a spacing (0.41 units at
  CURVE_DENSITY=3). Reuse the ternary-search refinement; it takes 0.41 -> 0.003.
- numpy: `np.abs(complex)**2` collapses to a REAL array, so there is no axis 2
  to reduce over. Do point-to-segment distance in explicit (x, y).

`_invert` note for the twin below: an LA image has 2 bands, RGBA 4. Converting
to RGB before `Image.merge` yields 4 bands and fails to merge back into LA.

## Colour-inverted source twin (`prep_raster.py`)

`--invert` / `spec.print.invert` writes `<stem>.prepped.inverse.png` beside the
normal prepped file, in BOTH check and fix mode, recorded as a top-level
`inverse` key in the `.prep.json` sidecar. The key is ALWAYS present (with
`ran: false` + a reason) so a consumer can tell "not produced" from "not
requested".

- `photometric` (default): every channel `c -> 255-c`. Exact — verified
  `max |a+b-255| == 0`. It inverts EVERY band, so it FLATTENS a transparent
  background to opaque; do not describe it as alpha-preserving.
- `negative`: inverts chroma, PRESERVES alpha. This is the one a transparent
  matte needs. Handle RGBA, LA and tRNS palette images by band count.

The twin is purely ADDITIVE: check mode must leave the prepped file
byte-identical (test-pinned), because that is the file VTracer consumes.
Already-vector input records `ran: false` + "nothing to invert" rather than
silently omitting the key. `runner.py::_publish_source_previews` copies both to
the stable names `source.png` / `source.inverse.png`; `artifact_path` also
resolves them from `01_prepped` so older jobs still serve them. The studio job
page shows both chips and each `img` self-hides on 404, so a missing twin
degrades to the single source chip.

Why it is not a cosmetic swap: speckle filtering keys on local contrast, so a
design that reads clean on white can pick up a halo of spurious regions on
black. Producing both at prep lets a sweep compare the two readings.

`spec["print"]` may be absent, null, or a non-dict in a hand-written spec.
`spec.setdefault("print", {})` returns the existing `None` rather than
replacing it, so the next item assignment raised `TypeError` and killed prep
with a traceback (rc=1) instead of its documented rc=2. Use
`if not isinstance(spec.get("print"), dict): spec["print"] = {}`. A bad spec is
data — normalise and carry on.

Two more invert traps, both silent:

- **`--invert` must not clobber the spec's mode.** `--invert-mode` defaulting to
  `"photometric"` meant a bare `--invert` overwrote a spec's
  `{"mode": "negative"}` — flattening exactly the transparent matte the user
  chose `negative` to protect. Default the flag to `None` and only write the
  mode when it was explicitly given.
- **Palette images carry transparency in a tRNS index, not an alpha band.** A
  `P`-mode PNG with `transparency` fell through to `convert("RGB")`, destroying
  the matte while the sidecar still reported `negative`. Promote `P`/`PA` to
  `RGBA` first. And `_write_inverse` must UNLINK a twin left by an earlier run
  when `invert` is now off — `trace_sweep.py`/`run_record.py` glob the prep dir,
  so a stale file reads as current.

### Four ways a simplifier silently deletes artwork (all found by review)

Each of these produced a SUCCESS message while damaging the file. Node-count
checks cannot catch any of them; only geometry checks can.

1. **Unsupported segment types serialise to `""`.** svgpathtools'
   `QuadraticBezier` and `Arc` have NO `.d()` method. A fallback of
   `seg.d() if hasattr(seg, "d") else ""` therefore yields `""`, the trailing
   `if p` filter drops it, and a path of unsupported segments becomes the empty
   string — reported as `reduced, nodes 30 -> 0`. **A deleted path clears any
   node gate.** Emit `Q` for quadratics (exact) and RAISE
   `UnsupportedSegmentError` for anything else; `reduce_svg` then records
   `unsupported_segment` and leaves the path alone.

2. **Multi-subpath `d` gets welded.** One `d` can hold several `M`-separated
   subpaths; svgpathtools returns them as one continuous segment list with a
   discontinuity. Fitting across a discontinuity joins two disjoint shapes —
   and because the welded result is SHORTER it passes the "nodes strictly
   decreased" test and gets accepted. Split on discontinuity
   (`split_subpaths`), fit each separately, re-emit an `M` per subpath.

3. **`t` snapped to the sample grid.** See the deviation notes above.

4. **Refuse an empty fit explicitly.** Guard `if not new_d.strip()` in the
   escalation loop; a fit that empties a path is a deletion, not a reduction.

Also: the line-collapse branch must advance by the RUN length (`i = j`), never
by a count derived from the sample list — a sample-derived increment
re-visits segments and desynchronises the walk.

When nothing could be reduced, still WRITE the output (status `no_change`) so
a caller never has to special-case a missing file.

### Detect closure GEOMETRICALLY, per subpath — never by segment type

`svgpathtools` parses `Z` into an ordinary trailing segment running back to the
subpath's start, and `Path.closed` is wrong as soon as there is more than one
subpath (first subpath closed + second open reports `closed=False`). Worse, the
trailing segment's TYPE is not a reliable signal: the fitter closes a subpath
with a CUBIC, so `isinstance(tail, Line)` silently drops the `Z` from exactly
the paths the tool rewrote. Both variants were live here; the second only
surfaced when I compared raw `Z` counts on real output, because a geometric
total had looked fine (a dropped `Z` can be masked by a gain elsewhere).

Test closure as: the subpath's last segment ends where the subpath started AND
is non-degenerate. No fallback to `path.closed` — on a genuinely open path that
invents a `Z` and adds a node. Verify by counting `Z` CHARACTERS before and
after, not just comparing closed-subpath totals.

### A "perfect" fidelity report is the most suspicious result there is

`--verify` rendered "before" from the same path it had just overwritten, so
in-place mode reported 0.000% changed — from the exact check meant to catch
geometry damage. When a measurement comes back perfect, suspect the
measurement: confirm the two inputs are genuinely different files. Always keep a
pre-reduction copy when verifying in place.

## Colour reduction to <20 colours

The `poster` preset produces hundreds of colours on complex images (VTracer's
color mode clusters aggressively but antialiasing creates many intermediate
hues).  The path to <20 colours is **snap_colors.py --force** as a
post-processing step: it snaps every fill/stroke to the spec palette, giving
3-6 colours.  `prep_colors` (pngquant) and `color_precision` alone are NOT
sufficient on photographic input.

## Round-robin truncation (fixed)

`build_candidates` now round-robins across per-speckle groups (zip_longest)
before truncating to the cap, and within each speckle group round-robins
across presets.  Before this fix, with a palette and cap 12, all 12 were
speckle=2 and speckle=16 was never traced.

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

## Optional generation stacks (not in this repo)

This copy is generated from a private working skill and has had its
generation-stack sections removed. The local skill also documents a
text-to-image / img2img bridge used to *generate* a source raster
before tracing, including a specific GPU stack and local install
paths. None of that is part of this pipeline, and none of the scripts
it describes exist here.

If you need it, see the private `chopshop-sui` repo. Nothing in
`front_pipeline.sh` or `pipeline.sh` calls it: generation is opt-in and
happens upstream of the pipeline, never inside it.

## Palette variations (auxiliary layer)

`scripts/palette_variants.py` re-colours a FINISHED trace onto the palettes in
`scripts/palettes.json` while leaving the CAD structure byte-identical (only
`fill`/`stroke`/`stop-color` change; every `d=`, `viewBox`, `width`/`height` is
untouched). It is an aux layer like `<private-script>` -- it calls the pipeline,
nothing calls it. Output: `07_palettes/<stem>/<palette-id>/` + `report.json|md`.

```bash
.venv/bin/python scripts/palette_variants.py --list
.venv/bin/python scripts/palette_variants.py 00_source/art.svg --only cool-luxe
.venv/bin/python scripts/palette_variants.py --from-final 00-example --preflight
.venv/bin/python scripts/palette_variants.py art.svg --map '#c1440e=#9caf88'
```

Mapping priority: explicit `map` (palette entry or `--map`) > background anchor
(largest-area source colour -> palette `background`) > strategy
(`--strategy area` default, or `nearest` = snap_colors semantics).

Two hard-won facts:

- **No colour-space heuristic recovers intent.** Verified on a real fixture: with
  `area`, red `#c1440e` mapped to sage and green `#6a8a3f` to ivory -- arbitrary.
  Never present heuristic output as "the" recolour; `mapping.json` is written
  beside every variant precisely so the result can be pinned into an explicit
  `map` in one edit. That pin is the production path.
- **`validate_svg.normalize_color()` passes unknown strings through unchanged**
  (`'not-a-colour' -> 'not-a-colour'`; it only resolves named colours and hex).
  It is therefore NOT a validator. Palette entries and `--map` values must be
  gated by a strict `^#[0-9a-f]{6}$` check (`palette_variants.as_hex`), or a typo
  reaches the artwork as `fill="not-a-colour"` and crashes `hex_to_rgb`.

Weights come from ONE Inkscape render (96 dpi, area only) reused across every
palette; without Inkscape they degrade to element counts and the report says so.
The biggest *visible* area wins the background anchor -- a motif covering most of
the canvas beats the ground, which is right for print (ink area) but worth
knowing when a design is densely covered.

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
