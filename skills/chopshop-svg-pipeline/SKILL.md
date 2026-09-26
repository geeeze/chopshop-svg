---
name: chopshop-svg-pipeline
description: Use when tracing a raster into SVG candidates or checking an SVG for print readiness (the chopshop-svg pipeline).
---

# chopshop-svg — raster to print-ready SVG

This repo is a tracing pipeline in two stages:

- **Stage 1** turns a raster into candidate SVGs (prep → trace sweep → compare).
- **Stage 2** checks an SVG for print readiness and reports what a renderer will
  actually paint.

Both stages are ordinary command-line code here: `front_pipeline.sh` drives
Stage 1, `pipeline.sh` (via `validate_svg.py` and `preflight.py`) drives Stage 2.
See `README.md` for the quickstart and `AGENTS.md` for orientation.

**Vocabulary.** A finished run that a person has accepted is a **proof** — the
word "validation" survives here only where it is a real identifier
(`validate_svg.py`, the `04_validated/` directory, the script's own
`VALIDATION PASSED for <file>` output). Chopshop Studio, the operator front end,
calls the accepted artifact a proof and numbers it after the candidate it came
from (`candidate_06.svg` → `proof_06`).

**It never picks a winner.** Trace quality is a visual call for a human or a
reviewer model. The pipeline produces candidates and metrics, and stops there.

## Stage 1 — raster to traced candidates

Given a raster, which vector traces are worth feeding to the print check. It
produces candidates and metrics only — never a winner.

### Workflow

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

### Semi-interactive loop (--loop)

`front_pipeline.sh --loop` appends a pick loop: after compare it hands off to
`scripts/pick_finish.py`, which prints a menu (preset, speckle, hier, palette,
MAE-all/MAE-art, Layer A/B, hard/adv), waits for the user to pick up to 3
candidates (space-separated numbers, or `q` to quit), runs `pipeline.sh` on each
pick (full Layer A+B + proof + print.pdf + manifest), then loops back to the
menu. Max 3 picks per iteration; re-tracing with new params = re-run
front_pipeline.sh. Parse logic is in `pick_finish.parse_picks` (pure, unit-tested);
the loop itself is `interactive_loop` (reads stdin, not unit-tested).

### Fidelity (accuracy) metric — the user's actual goal

`compare_candidates.py` also measures PIXEL FIDELITY: it renders each candidate
back to a raster with Inkscape (at the source's dimensions) and diffs it against
the source raster. Source is auto-resolved from `sweep.json`'s `input.file`
(the prepped PNG) or the `--source` flag. Per candidate it records
`fidelity: {measured, mae, p95, within10, mae_art}`.

- `mae` = mean abs error over ALL pixels. On dark-art-on-dark-background images
  this is dominated by the background and is misleadingly low.
- `mae_art` = MAE over ARTWORK pixels only (source pixels deviating >20 from the
  modal colour). THIS is the number that matters for raster→vector accuracy.
- The report keeps a separate "By pixel fidelity" ranking (sorted by mae_art)
  **and** ranks the main table on a fidelity verdict — see below. A separate
  accuracy table nobody acts on is not a check.

**"No gate fired" is not "this is the design".** A candidate that discards the
artwork entirely can pass every gate: on the shipped example all six
`bw`/binary candidates reported `passed=True`, `hard=0`, `advisory=0`, and
passed both layers — while their `mae_art` was **104.214** against **0.005**
for the six colour-preserving ones. Sorted on `(hard, advisory)` alone they
ranked level with the faithful candidates, so a trace that had thrown the
whole design away was indistinguishable from one that reproduced it.

`compare_candidates.py` now emits `fidelity_verdict` —
`faithful` < `drift` < `colour_dropped` < `artwork_lost` — as the second sort
key, after hard gates and before advisories. Two independent checks, because
they fail differently:

1. `mae_art > 8.0` (the real gap is ~20 000x, so a cliff, not a curve);
2. `rendered_ink_colors < declared_colors * 0.5`, which catches a candidate
   that is pixel-perfect only because the source it was diffed against was
   itself quantised to one ink.

It is **advisory, never a hard gate** — a deliberately single-colour job is a
legitimate ask and hard-failing it would reject that. It raises the advisory
count and adds a `FIDELITY_ARTWORK_LOST` finding so it reaches the UI without
being prescriptive.

The rank table counts DOWN because the sort is ascending and the best
candidate must come first. Ranking `artwork_lost` as 0 put the six candidates
that discarded the design at the TOP of the table — the exact inverse of the
fix. Assert the tie-break in a test, not the constants.

**Generalise it: a passing gate is evidence about the gate, not about the
result.** `VALIDATION PASSED` fired on a 92%-black inverse trace (ink coverage
91.93% over the 300% limit) and on a per-plate trace whose coordinates spanned
`-240..421` inside a `0 0 1024 1024` viewBox. Both are internally consistent
and both are wrong. Geometry, emptiness and rendered-ink fraction need their
own assertions; a structural validator cannot see them.

Measured on a real 1080² dark-art image (1472 unique colours, 92% near-black):
`bw` = silhouette, mae_art≈49 (bad); `poster` no-palette = mae_art≈9.6 (best);
`poster` palette-quantized = mae_art≈45. Conclusion: the palette axis and the
SPEC'S DEFAULT PALETTE hurt fidelity unless the palette matches the artwork's
real colours. The trace's true colours are visible in the PALETTE advisory —
read it before trusting the palette axis.

### Prep is CHECK-FIRST (do not modify by default)

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

### Size/orientation is NOT prescribed

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

### Tracer: VTracer

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
physical size on the chosen candidate before the print check.

### Prep helpers (all optional, degrade gracefully)

- `rembg` (`pip install 'rembg[cli]'`) — background removal. Skip via
  `spec.print.assume_opaque_bg: true`.
- `realesrgan-ncnn-vulkan` (GitHub release binary) — upscale. Falls back to
  Pillow LANCZOS.
- `pngquant` (`apt-get install pngquant`) — quantise to `spec.print.prep_colors`.
  `null` disables.

Missing binaries: skip the step, log a warning, record it in the sidecar —
NEVER crash the run. CMYK TIFF → convert to sRGB first. Very large inputs:
downscale-then-upscale (bounded memory).

### Sweep config: preset_params (new)

The sweep JSON now accepts a `preset_params` key that maps preset name →
dict of VTracer params, merged over the built-in PRESETS.  This lets the user
lower `color_precision` for a flatter trace without editing code:
```json
{"presets": ["poster", "poster_flat"], "preset_params": {"poster_flat":
 {"colormode": "color", "mode": "spline", "color_precision": 2}}}
```

### Tracer parameter levers: measured, orthogonal, and mostly inert

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

Measured on this repo's `01_prepped/00-example-tonal-reference.prepped.png` (1024x1024, 245 608
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
- `color_precision` — **NOT a colour-count control**, despite the name and
  despite `inspect.signature` showing it accepted and forwarded. Reproduced
  with the bare `vtracer.convert_image_to_svg_py` API, no pipeline involved:
  2/3/6/8 all give byte-identical 2 291-fill output with near-black hexes
  (`#060000`, `#180202`) that exist nowhere in the input. `=1` is worse than
  inert: a 400-byte SVG, the whole image one rectangle filled `#D1AFAF`, a
  colour absent from the source. Treat it as a vtracer 0.6.15 bug and stop
  reaching for it as a palette knob.
- `corner_threshold` — default 60 is already the floor; 100 and 150 make max
  nodes WORSE (1 528 -> 1 624 -> 1 633).

**Tracing can PANIC or return NOTHING. Guard every trace.**
- `corner_threshold=110` and `length_threshold=10.0` abort the process inside
  `clusters.rs:323` (integer overflow) on a dense plate.
- `filter_speckle >= 16` does not raise — it returns **0 paths, no
  exception**. A sweep cell that silently produces nothing reads as "this
  preset is clean" and will be selected. Assert non-empty output.
- The panic is content- and density-dependent, not a pixel-count limit: a red
  plate panicked at 1024px and at every other setting tried, while the same
  plate at 900px traced fine. Do not conclude "it is only large images".
- `Image.new("1", ...)` DEFAULTS TO 0 = BLACK. Building a per-colour mask
  with it inverts the mask: a colour covering 13% of the sheet came out as 87%
  ink, and the trace then panicked because the ink was the minority region.
  Fill the plate explicitly.

**Quantising the SOURCE raster does not reduce node count.** Traced over
pngquant / PIL median / max-coverage / octree sources at 6-48 colours: max
nodes went 967 -> 912 (median 16) or -> 1 415 (median 6). It is a colour tool,
not a node tool. `print.prep_colors` will not help `geometry_overload`.

### Node reduction: `scripts/node_reduce.py`

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

### Colour-inverted source twin (`prep_raster.py`)

`--invert` / `spec.print.invert` writes `<stem>.prepped.inverse.png` beside the
normal prepped file, in BOTH check and fix mode, recorded as a top-level
`inverse` key in the `.prep.json` sidecar. The key is ALWAYS present (with
`ran: false` + a reason) so a consumer can tell "not produced" from "not
requested".

- `negative` (default): inverts chroma, PRESERVES alpha. Handle RGBA, LA and
  tRNS palette images by band count.
- `photometric`: every channel `c -> 255-c`. Exact — verified
  `max |a+b-255| == 0`. It inverts EVERY band, so it FLATTENS a transparent
  background to opaque; do not describe it as alpha-preserving.

**The substrate is not part of the artwork — leave it alone.** A blind
per-channel flip over everything inverts the GARMENT as well as the ink, so a
92%-white shirt becomes a 92%-black flood: the trace is real, every gate
passes, and the proof is 91.93% over the ink-coverage limit. Inverting only
pixels that differ from the substrate (with a tolerance, or antialiased edges
leave inverted fringe) took the same example to 7.94% opaque and 178% max TAC.

`print.substrate` (a hex, or `substrate_index` into the palette) names the
fabric colour. Resolution belongs in `load_print_options`, not `preflight()`,
because the raw spec is only in scope at spec-load time -- and it must be
propagated from there so the tracer, the manifest and the UI cannot disagree.
A value that is neither valid hex nor a palette member is a SPEC ERROR, not a
silent fallback to paper; that silent fallback is how the flood came back
unnoticed.

The manifest gains a top-level `substrate` block (`colour`, `is_fabric`,
`screens`) so a consumer can tell fabric from ink without re-deriving it.

The old test asserted the bug (`white background -> black` as correct) and
pinned it in place. When fixing behaviour, grep the tests for the broken
expectation first -- it will be there, and it will pass.

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

#### Four ways a simplifier silently deletes artwork (all found by review)

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

#### Detect closure GEOMETRICALLY, per subpath — never by segment type

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

#### A "perfect" fidelity report is the most suspicious result there is

`--verify` rendered "before" from the same path it had just overwritten, so
in-place mode reported 0.000% changed — from the exact check meant to catch
geometry damage. When a measurement comes back perfect, suspect the
measurement: confirm the two inputs are genuinely different files. Always keep a
pre-reduction copy when verifying in place.

#### A 6-colour gate cannot be satisfied by tonal art — pick a flat source

Before tracing anything, check the source: count distinct colours and ask how
many colours cover ~95% of pixels. A screen print has a hard colour ceiling, so
tonal source is dead on arrival — not a tuning problem.

The tell: the example this repo shipped was an engraved floral with **245 608
distinct colours**, where even 256 colours covered only **34%** of pixels. Every
candidate that "passed" the gates did so by discarding the artwork down to **1
declared colour** (a black silhouette, `mae_art` 114). It passed and demonstrated
nothing, which is worse than failing visibly.

A source that passes cleanly: flat, hard-edged fills, ≤6 colours drawn from
`spec.json`'s own palette, no gradients. That traces at `mae_art` 0.005 with
zero hard gates and zero advisories.

**Two traps that cost a full rebuild each** (both measured, not guessed):

- **Pure `#000000` alone trips INK_COVERAGE.** Layer B separates to CMYK, and a
  solid black comes out near 400% coverage by itself — over the 300% limit. The
  identical design with a black ring reported 300% and 3.4% of the sheet over;
  swap the black for blue and the advisory vanished. Use a saturated hue for key
  lines and registration marks.
- **Overlapping plates also trip it**, because coverage is summed per pixel.
  Separate the shapes; don't stack them.

When a "passing" candidate has `mae_art` > 50, suspect it passed by deleting the
artwork rather than by tracing it well. Check `declared_colors` and
`colormode` before believing a pass.

#### Repo visibility is not uniform: chopshop-svg is PUBLIC

Checked via the GitHub API, not assumed:

| repo | visibility |
|---|---|
| `chopshop-svg` | **public** |
| `chopshop-studio`, `chopshop-jev`, `chopshop-sui`, `console` | private |

So anything committed to `chopshop-svg` is world-readable, including its
`skills/` directory. Consequences, all learned the hard way:

- No hostnames, hostnames-derived labels, or private network addresses
  names, or machine nicknames in tracked files — including as code defaults.
  A hardcoded host in `runner.py` or `app/models/job.rb` is a publication, not
  a convenience. Use env vars (`CHOPSHOP_RUNNER_<LABEL>_URL`,
  `CHOPSHOP_EXTRA_HOSTS`, `CHOPSHOP_RUNNER_LABEL`, `RUNNER_BIND`).

**Host-agnostic by construction, not by scrubbing.** The rule is that no
tracked file encodes a deployment. When a host does leak in, grep the whole
repo for it, then fix the DEFAULT rather than the instance — the recurring
shape is a bind address: `ThreadingHTTPServer(("127.0.0.1", PORT), ...)` makes
the service unreachable from a container while working perfectly on the host
that ran it. `MOCK_BIND` exists for exactly this, defaulting to loopback and
overridable, with the reason in a comment so the next reader does not "tidy"
it back.

The same failure has a non-network form: `mount -v "$PWD:/w"` inside a `sh -c`
whose `$PWD` did not expand silently mounted NOTHING and the run appeared to
work. Absolute paths, always.

When a container cannot reach a service, check in this order: is the service
bound off-loopback, is the hostname resolvable from inside (`--add-host
host.containers.internal:host-gateway`), and is the env var actually passed to
the container. A 502 from the app usually means the app has no URL for the
runner, not that the runner is down.
- Never hand-copy a working skill into the public repo. `sync-skills` in the
  repo root strips sections whose backing code is absent here (the SwarmUI /
  ComfyUI layer lives in the PRIVATE `chopshop-sui`) and redacts literals.
  The repo copy is downstream; never import it back over the working skill.
- Generated files (`db/schema.rb`) are not an editing target: the change is
  reverted to nothing rather than to a replacement, because the next
  `db:schema:dump` overwrites it anyway.

Check visibility before publishing, not after: `curl -s -H "Authorization:
token $TOK" https://api.github.com/repos/geeeze/<repo> | jq .visibility`.

### Colour reduction to <20 colours

The `poster` preset produces hundreds of colours on complex images (VTracer's
color mode clusters aggressively but antialiasing creates many intermediate
hues).  The path to <20 colours is **snap_colors.py --force** as a
post-processing step: it snaps every fill/stroke to the spec palette, giving
3-6 colours.  `prep_colors` (pngquant) and `color_precision` alone are NOT
sufficient on photographic input.

### Round-robin truncation (fixed)

`build_candidates` now round-robins across per-speckle groups (zip_longest)
before truncating to the cap, and within each speckle group round-robins
across presets.  Before this fix, with a palette and cap 12, all 12 were
speckle=2 and speckle=16 was never traced.

### Spec keys Stage 1 owns (all under `print`, optional)

```json
"print": {
  "assume_opaque_bg": false,   // skip bg removal
  "prep_colors": 16,           // pngquant budget; null = off
  "background_hex": "#ffffff", // flatten alpha onto this
  "sweep_max_candidates": 12   // cap candidates
}
```

### Parallelism

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

### Optional generation stacks (not in this repo)

This copy is generated from a private working skill and has had its
generation-stack sections removed. The local skill also documents a
text-to-image / img2img bridge used to *generate* a source raster
before tracing, including a specific GPU stack and local install
paths. None of that is part of this pipeline, and none of the scripts
it describes exist here.

If you need it, see the private `chopshop-sui` repo. Nothing in
`front_pipeline.sh` or `pipeline.sh` calls it: generation is opt-in and
happens upstream of the pipeline, never inside it.

### Palette variations (auxiliary layer)

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

### Conventions / pitfalls

- Log to stdout in the same `--- stage N ---` banner style as `pipeline.sh`;
  version-stamp every tool call in the JSON (mirror `preflight.tool_versions()`).
- Every script idempotent: rerunning produces the same result, no corruption.
- Tests use synthetic Pillow fixtures only; never touch `00_source/` (that's
  the print check's batch).
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

---

## Stage 2 — print preflight and programmatic SVG inspection

Validating an SVG for production means judging what a renderer will actually
paint, not what the attributes literally say: paint arrives through a CSS
cascade, lengths arrive in mixed units, and geometry arrives as path data that
may be open, empty or collapsed. Every rule below exists because the naive
version of it silently passes a file that prints wrong.

### Deliverable convention

A validator is a **fixed-spec script**, so the failure-mode table is written as
tests **before** the implementation: one pytest case per named rule, one passing
document, one malformed-input case. The passing document is the **control** for
those rejections, not a happy-path tour: a validator is only proven useful by
what it rejects, and a rejection with no control may be tripping a different
rule entirely. When the caller names N failure modes, write exactly those N and
give each one a control. Drive the real CLI entry point and assert on
the observable contract — exit code plus the exact `[RULE_TAG]` text and the
numbers in the message — so the suite fails on output-contract drift, not just
on logic regressions. See `references/validator-testing.md`.

The tests are the cheapest bug-finder on the finished script: expect them to
catch real defects (crash paths, cascade gaps, inconsistent treatment of
degenerate input). When one fails, first work out whether the script or the test
is wrong — asserting the wrong unit or a wrong expansion constant is a common
test-side bug, and "fixing" the script to match it ships the bug.

### Procedure

1. **Environment.** On PEP 668 hosts install into a venv and run everything as
   `.venv/bin/python`:
   `python3 -m venv .venv && .venv/bin/pip install lxml svgpathtools pytest`
   For rendering and prepress measurement (print work), add the CLI set:
   `sudo apt-get install -y inkscape ghostscript qpdf poppler-utils potrace
   colord-data fonts-dejavu-core`
   plus `Pillow` and `numpy` in the venv. Inkscape's CLI export works headless,
   so a GUI install is fine on a server.
2. **Parse once, defensively.** `etree.XMLParser(resolve_entities=False,
   no_network=True, recover=False, huge_tree=True)`. Catch `XMLSyntaxError` and
   report it as a validation failure with line/column from
   `exc.error_log.last_error` — a malformed file is a *finding*, never a
   traceback. Also fail when the root element is not `<svg>`.
3. **Match by local name**, never by literal `{ns}tag` or `svg:tag`:
   `tag.split('}', 1)[1]`. Skip nodes whose `element.tag` is not a `str`
   (comments, processing instructions). Real files mix prefixed and default
   namespaces freely.
4. **Build one style context** for the document (inline `style=`, `<style>`
   blocks, presentation attributes) and query it for every property.
5. **Run the rules in a fixed order** — raster embeds, colour budget, stroke
   widths, path geometry — each wrapped so a crash becomes a failure line
   instead of aborting the run, and run **all** of them: report every failure in
   one pass so the user fixes a batch, not one item per invocation.
6. **Report and exit.** One line per failure, then exit 1 if any, 0 if none.
   Reserve a distinct code (2) for bad usage so a pipeline can tell "file is
   bad" from "you called me wrong".
7. **Probe adversarially** before declaring done: `scripts/svg_edge_case_probe.py`
   runs a batch of degenerate documents (moveto-only paths, `1em` widths,
   negative widths, nested `<svg>`, BOM, XXE, at-rules) against the validator
   and flags any case whose outcome is not what you expected.

### Invariants

- **Resolve paint through the cascade**: inline `style` > stylesheet rule (by
  specificity, later wins on a tie) > presentation attribute. Reading
  `element.get('fill')` misses every `<style>`-driven file.
- **`stroke` and `stroke-width` are inherited.** Thread the inherited pair down
  the tree so a `<path>` stroked by its parent `<g>` is still measured.
- **A user unit is not a CSS px.** SVG resolves a length to *user units* (CSS
  ratios: `1pt` = 1.3333 units, `1mm` = 3.7795), then the viewBox maps those
  onto the physical viewport. Reading `stroke-width="1"` as 0.75pt is wrong in
  both directions once a viewBox rescales: `width="210mm" viewBox="0 0 595
  842"` makes it 1.0005pt (false failure on printable art), and `width="300mm"
  viewBox="0 0 3000 4000"` makes it 0.85pt (false pass on a hairline).
  `mm_per_unit = width_mm / viewBox_width`, then
  `pt = user_units * mm_per_unit * 72/25.4`. Print the basis with every finding.
- **Verify unit handling against renders, not against the formula.** Render a
  stroke, integrate its ink coverage across a column at a known dpi
  (`sum(255 - value) / 255`), and compare. Measured at 600 dpi: `210mm/0 0 210
  297` + `sw=1` -> 2.8353pt; `210mm/0 0 595 842` + `sw=1` -> 1.0009;
  `100mm/0 0 1000 1000` + `sw=1` -> 0.2833; `210mm/0 0 210 297` + `sw=1pt` ->
  3.7802; `210mm/0 0 210 297` + `sw=1mm` -> 10.7139; `595px/0 0 595 842` +
  `sw=1pt` -> 1.0009. Implementation and renderer must agree to 0.02pt.
- **A unitless length is `px`** (SVG spec) — but that only means 0.75pt when
  nothing rescales the viewBox, per the point above.
- **`inkcov` always answers "CMYK"** — it converts before reporting, so it
  cannot detect a file's real colour space. Scan the PDF bytes for
  `/DeviceRGB` / `/DeviceCMYK` / `/DeviceGray` / `/ICCBased`, decompressing with
  `qpdf --qdf --object-streams=disable` first, and strip the `Device` prefix or
  `"CMYK" in spaces` fails against `DeviceCMYK`.
- **Conversion table**: `1in = 96px = 72pt = 25.4mm = 2.54cm`; therefore
  px→pt ×0.75, mm→pt ×2.834645, cm→pt ×28.346457, pc→pt ×12, Q→pt ×0.708661.
- **Never silently pass a value you cannot measure.** A `%`, `em`, `rem`,
  negative or unparseable width is reported as a failure *with the reason*.
  A validator that passes what it cannot resolve is worse than no validator.
- **Fail loud on artwork that renders nothing**: a `<path>` with missing, blank
  or moveto-only `d` is zero-area by definition → report it.
- **Count colours after normalisation** to lowercase `#rrggbb`; drop `none`,
  `transparent`, `currentColor`, `url(#...)` and `var(...)`; keep unrecognised
  keywords in the count (they still occupy a palette slot).
- **Report the element, the value, the source and the limit** in one line:
  `Path id='rule' has stroke-width 0.5pt, below minimum 1.5pt
  (stroke-width='0.5pt' from css)`. Identify elements by `id` first, then
  class, then index — never invent an id.
- **State your limitations in the script header.** Unresolved percentages, an
  unapplied `transform="scale(...)"`, unresolved `var()`: name them as
  documented limits rather than letting a reader assume full fidelity. And when
  a limitation can silently flip a verdict, emit a per-file ADVISORY naming the
  affected elements at run time as well — a header note is not read by the
  pipeline. An unapplied `scale()` is the model case: `stroke-width="10"`
  inside `<g transform="scale(0.01)">` passes a 1.5pt minimum while really
  measuring ~0.28pt, so the validator reports "pass" on a stroke it never
  measured. Emit that as a non-failing note (element label first, capped like
  every other repeated finding), not a hard failure: the true width cannot be
  computed without a full transform stack, and failing the job would assert
  something the tool has not established.
- **A passing result demands as much scrutiny as a failing one.** A clean run on
  artwork that should not have passed means a rule is not measuring. When
  everything passes, confirm the *inputs* were genuinely what you assumed (see
  the render-from-the-same-file trap below), and re-derive one finding
  independently — via `gs -sDEVICE=inkcov`, or a pixel diff against the source —
  before reporting the pass. Verify a run from a clean slate, removing the
  stage directories and re-running the documented commands, so the claim in the
  docs is the claim you actually tested.
- **Derive a spec option only when its key is absent, and report a conflict.**
  Where one option implies another (an underbase implies white is an ink),
  compute the implied value only if the key is missing. If both are set and
  disagree, honour the explicit value and warn — the dangerous direction is
  always the undercount, and an inferred default must never be presented as a
  stated requirement.

### Prepress measurement — what only a render can tell you

Attribute-level checking cannot see any of this. Render the artwork, then
measure the pixels and the CMYK plates.

- **Count inks from the render, never from declared attributes.** A two-stop
gradient is declared as 2 colours and rasterises to hundreds (verified: 5
declared → 115 rendered, 33% of the sheet). Render to PNG on white with
`inkscape in.svg --export-type=png --export-dpi=300 --export-background=#ffffff
--export-background-opacity=255`.
- **Ink count and continuous tone are two different failures.** Comparing an ink
count against a spot-colour budget is the wrong model for a gradient: at
Euclidean tolerance 20 a whole gradient ramp collapses to ~7 "inks" and would
*wrongly pass* a 6-colour limit. Detect ramps separately — a merged cluster
built from many near-neighbour colours (≥6) covering real area is continuous
tone, i.e. a gradient or photograph that needs halftone screening. Give it an
explicit opt-out (`allow_gradients`) for CMYK process work.
- **Rendered colour counts need de-noising, in two stages.** An *area
threshold* (a colour must cover ≥0.05% of the sheet to count) removes most
antialias fringe; a *Euclidean merge in sRGB* (tolerance ~20 of 441) collapses
what survives. 20 merges fringe but keeps deliberate neighbours apart
(`#1a1a1a` vs `#2a2a2a` are 27.7). Without merging, a pale background plus its
own AA edge reports one phantom extra ink.
- **White's status depends on the garment, not on the file.** No ink is no ink
  only when the fabric is already white; on a dark garment white is usually the
  *first* plate down (the underbase), so the same file needs one more screen.
  Exclude pure white from the ink count by default and make that overridable with
  an explicit spec key — but **warn in the output whenever white is in the
  artwork and is not being counted**, because the tool cannot know the substrate
  and silence here ships an undercounted job. Price the warning in the job's own
  terms (`this job needs 5 screens, not 4`).
- **A white underbase is a screen, and no render can show it.** Dark-garment
printing lays a white plate under everything; on a proof rendered onto white
paper it is invisible by construction. Model screens explicitly —
`screens = non-white inks + (1 if the artwork declares white or the spec
requires an underbase)` — and gate on *screens*, not on `inks`, because the
screen is what costs money.
- **White in a render is ambiguous; only the source can resolve it.** A white
pixel may be artwork or may be the transparent background showing the paper.
Inferring "white is an ink" from the render is accidentally correct on
transparent-background art and silently wrong on full-bleed art, which has no
background and therefore loses a real underbase screen. Pass the source's
declared palette into the render measurement and decide there.
- **An underbase makes TAC a lower bound.** A CMYK separation measured on paper
cannot represent a plate that lies under every other one, so report the figure
as a bound when an underbase is in play, not as the job's total.
- **Total area coverage (TAC) comes from `gs -sDEVICE=tiffsep`**, which writes
one grayscale plate per ink. See `references/prepress-measurement.md` for the
exact invocations and the traps below.
- **Placed bitmap resolution**: `pdfimages -list` reports the *effective* ppi of
an image as placed, which is the number that matters. A 64×64 px bitmap scaled
across a sheet legitimately measures **9 ppi** — a dramatic, real reject that no
source inspection would catch.
- **State the provenance of every number.** Exact (measured from a CMYK source)
  vs estimate (converted from RGB). A confidently wrong prepress figure is worse
  than no figure.
- **Inverting a source inverts its SUBSTRATE, and the result will pass every
  gate.** A colour inverse maps every channel, so a design that is 92% white
  shirt becomes a design that is 92% solid black — a full-bleed flood that will
  not dry and will set off onto every sheet beneath it. Measured on one job,
  same spec and both gates green:

  | | original | inverted twin |
  |---|---|---|
  | screens | 5 | 4 |
  | max TAC | 194% | 300% |
  | mean TAC | 12% | 283% |
  | sheet over limit | 0.00% | 91.93% |
  | verdict | PASSED | PASSED |

  Two independent reasons it slips through, and both must be fixed:
  1. **The TAC gate is advisory on RGB input**, correctly, because Ghostscript
     invents the separation — so 91.93% over the limit is a log note, never a
     failure. Nothing else looks at coverage magnitude.
  2. **`white_in_artwork` is decided by DECLARATION, not by area.** It is set
     from `"#ffffff" in declared`, so a small white accent flips it true while
     the actual 92% background is black — and black gets no substrate-aware
     treatment at all, because only white has any.

  So the one signal that would catch this — a single colour dominating the sheet
  — is never asked. `ink_area_threshold_percent` (0.05%) is a per-colour
  *inclusion* floor, a different question. Add a **dominant-colour flood
  check**: computable straight from the SVG with no CMYK separation, so it
  holds on RGB input where TAC cannot. A spot-colour job where one colour
  covers more than roughly half the sheet is a flood, whatever TAC says.

  The real fix is upstream of the check: **invert the artwork, not the
  pixels.** Keep the substrate transparent (mask by alpha, or matte against
  white then invert then re-apply alpha) so the twin traces as artwork on
  nothing. Background removal does not help here — it would have to run BEFORE
  the inversion, because by then a flattened substrate is baked in as pixels.
  Implemented and measured: 92.06% opaque -> **7.94%**, max TAC 300% -> 178%,
  mean 283% -> 9%, sheet-over-limit 91.93% -> **0.00%**.

  Three rules make the fix hold:

  - **Declare the substrate; never infer it silently.** Take the fabric colour
    from the spec (`print.substrate` as hex, or `print.substrate_index` into
    the palette) so the dark-garment case is reachable — there the majority
    colour is the ART and the substrate is the minority one, which any
    majority-colour inference gets backwards. A value that is neither valid hex
    nor a palette member is a spec ERROR to report, never a fallback to "paper":
    the silent fallback is exactly how the flood returned unnoticed.
  - **Resolve it ONCE, on the shared options object**, so tracer, manifest,
    proof and any advisory reader all get the same answer. Resolving it
    independently in each stage is how two stages disagree about what the fabric
    is.
  - **Invert only pixels differing from the substrate, with a tolerance.** A
    bare `255 - c` over every pixel inverts the antialiased edge pixels too,
    leaving a halo of inverted fringe around every shape; match-then-invert
    avoids it.

  **When a test pins the buggy behaviour, replace the test.** A case asserting
  "white background becomes black" is a regression test FOR the bug, and
  satisfying it preserves the flood. Rewrite it to assert substrate
  transparency, and add a second case where the declared substrate is the
  MINORITY colour so a majority-colour shortcut cannot pass it.

### Pitfalls

- **Differential-test a rewritten numeric routine against the old one** rather
  than re-reading the new code for equivalence. Keep the previous
  implementation verbatim, run both over randomised inputs, and assert they
  agree on the same output. A numpy rewrite of `_blend_target` (pairwise
  colour-projection, was O(anchors²) pure Python at ~125k inner iterations per
  stray colour) matched the original on 3000 random anchor sets plus edge cases.
  Rewrites of this kind *read* as obviously equivalent, which is exactly why
  re-reading is not evidence: tie-break order and last-bit float drift are
  invisible in the source and only a differential run finds them.
- **`_blend_target` invariants to preserve when editing it**: paper (`None`)
  anchors are blended *through* but never own a stray colour; zero-length and
  same-index anchor pairs are excluded; on an equal gap the `i<j` pair wins.
  Keep them and the O(n²) pair set collapses to the same owner as the nested
  loop, which is what stops an antialiasing chain becoming phantom inks.
- **An advisory must be written to `notes`/`stats`, never to `failures`.**
  `validate_svg.main` exits 1 on *any* entry in `failures`, so a non-fatal
  observation placed there silently converts a PASS into a FAIL. When adding a
  finding that cannot flip the verdict, route it to `notes` and let
  `preflight.classify_findings` decide severity.
- **Two "optional-looking" apt packages are required and fail tests when
  absent: `poppler-utils` and `colord-data`.** The preflight shells out to
  `pdfimages`/`pdfinfo` (poppler-utils) for placed-bitmap resolution and
  colour-space detection — without them the `pdf_images` stat is missing and the
  image tests fail. `pick_cmyk_profile` prefers colord's SWOP/FOGRA ICC
  profiles; without `colord-data` it falls back to Ghostscript's default profile
  and the RGB→CMYK ink estimate changes enough that the "ink is advisory on RGB
  input" test never fires. Both belong in any Dockerfile/apt line that packs the
  stack — this is exactly what breaks when containerising a working host.
- **Corollary: a suite that needs a system tool must SKIP, not fail.** Because
  these tools are genuinely required, guard every test that shells out to one
  with `pytest.mark.skipif(shutil.which('<tool>') is None)` — one marker per
  tool, or a single marker covering the set (`inkscape`, `gs`, `qpdf`,
  `pdfinfo`, `pdfimages`) applied at class level. A suite that goes red on a
  checkout lacking the tools trains the reader to ignore red, and the real
  regression then hides in the noise. Check the *library* import separately
  (`import vtracer`) for tracer-dependent tests — a pip package has no binary
  for `which` to find, so a `which`-only guard silently skips nothing.
- **Colour-cluster merging needs a shape test, not a size test.** Antialiasing
  lays a chain of near-identical colours along every boundary. Merging by
  colour-space distance leaves those links as separate clusters, and two size
  heuristics both fail: comparing a link against a *neighbouring* colour lets
  each grey veto the next (18 thin rules reported 4 inks), and exempting
  multi-member clusters lets two adjacent greys that merged under the tolerance
  pass as a "ramp" (same file, 2 inks). Decide by shape instead — erode the
  colour's mask by one pixel: an edge ribbon vanishes, a filled region survives.
  That also protects gradient bands and flat fills, and it stops a solid grey
  bar being swallowed as a blend of black and white (neutral grey lies exactly
  on that line, so geometry alone cannot tell them apart). Once shape decides,
  keep the colour distance *loose* — it is only a coarse prefilter, and a
  measured antialiasing band sat 12.25 units off the segment it blends, so
  tuning the tolerance tight buys nothing and drops real blends.
- **Tone detection must use collective area, not per-colour area.** A
  photograph has no band large enough to see, so every one of its ~700k colours
  falls below the per-colour area threshold and the artwork reports as **one
  ink**, passing a spot-colour gate. Detect it by how much of the sheet the
  sub-threshold colours cover *together*: measured 44.4% for a photograph
  against 0.00-0.20% for every flat file in the same batch. Keep the separate
  ramp detector for gradients (a merged cluster with many members) — and note a
  *vector* halftone correctly trips neither, which is the point of converting
  gradients to halftone dots.
- **Cap repeated findings.** A hairline pattern spanning a sheet yields one
  finding per path; twenty near-identical lines bury the message. Report the
  first ~10 in full and summarise the rest with the distinct values involved —
  the fix is the same for all of them. Same for path-complexity findings.
- **A passing artefact can have discarded the artwork — and no gate will say
  so.** Gate-passing and faithful are different properties, and a bw/binary
  trace of a colour image separates cleanly: it passes every layer with
  `hard=0, advisory=0` while reproducing nothing. Measured on a real 5-colour
  example, six such candidates reported `passed=True` and `mae_art` **104.2**
  against **0.005** for the six faithful ones — a ~20,000x gap, invisible in
  every gate column, and a total tie on `(hard, advisory)` so gates-only
  sorting ranks them level with faithful candidates.

  Check artwork deviation against the declared colour count, and sort on that
  AFTER hard gates and BEFORE advisories. Keep it **advisory, not a gate** — a
  deliberately single-colour job is legitimate, so raise the advisory and add a
  named finding rather than overruling the operator. Name the CAUSE in the
  message, not just the number. And check any prose describing the sort still
  describes it: a footer reading "sorted on gates and nothing more" is what
  makes the missing dimension invisible. Full procedure, the two independent
  checks, and the ascending-sort rank inversion that put the worst candidates
  on top: `references/passed-is-not-faithful.md`.

- **Cap rules must be checked against the candidate list that was actually
  produced.** A cap applied per group (per speckle level, per preset) silently
  starves the others: with a palette and cap 12, all twelve came from speckle=2
  and speckle=16 was never traced. Round-robin across groups before truncating
  (`zip_longest`), then verify by listing the presets present in the OUTPUT
  rather than by reading the cap arithmetic.

- **Status goes in stats; assumptions go on stdout.** Notes print above the
  pass/fail line and break any caller asserting on output, so an absent optional
  key — mere status — belongs in stats, not stdout. But an assumption the caller
  cannot otherwise see (white uncounted because the garment is unknown, a plate
  inferred rather than stated) is a finding wearing a note's clothing: state it
  in the report, with the consequence in the job's own units. Ask whether silence
  would change what the user ships; if it would, print it.
- **Merge a supplied spec fragment; never let it replace the spec.** A caller
  hands over the keys they are thinking about, so overwriting the file silently
  drops every limit they did not mention (a colour budget, a dimension set) and
  the pipeline becomes permissive with no error anywhere. Merge into the current
  file, keep unmentioned keys, and say which keys came from where — including a
  limit the fragment implies is missing.
- **A diff built in memory is a plan, not a change.** After editing a spec,
  config or validator, read the artifact back before reporting it: a merge that
  landed only in a local variable reads identically in the transcript and leaves
  the pipeline running the old file.
- **An ad-hoc run under a different spec poisons a batch result set.** Per-job
  artifacts are keyed by filename, so re-running one file by hand with another
  spec overwrites its manifest in place; the batch JSON, and any document quoting
  it, then describes a mixture of configurations that nothing in the numbers
  reveals. Re-run the whole batch before quoting figures from it.
- **A summary table is an assertion; check its numbers against the artifacts.**
  Label a column by the quantity it actually holds — rendered inks and printer
  screens are equal in the common case and differ in the exception, so a wrong
  heading will not be caught by a reader, only by a programmatic comparison of
  every stated figure against the file it came from.
- **Gate the pipeline, not the number.** Classify each rule hard vs advisory and
  exit non-zero only on hard findings. TAC on RGB input must be advisory — it
  physically cannot fire a 300% limit — and font substitution is advisory unless
  the spec says otherwise. Deriving `allow_open_paths` from `print_method` must
  keep the *conservative* default when neither is given, or an existing strict
  contract silently becomes permissive.
- **Regex at-rule stripping leaks screen rules.** A non-greedy
  `\{[^{}]*\}` cannot match a nested `@media` block, so text after it survives
  and `@media screen` rules get applied to a print check. Walk the CSS with a
  brace counter: apply `@media print` / `@media all` bodies, drop
  `@media screen`, `@page`, `@font-face`, `@keyframes`.
- **Match descendant selectors right-to-left**, walking
  `element.iterancestors()`, and *skip* what you do not model (`>`, `+`, `~`,
  `[attr]`, `:` pseudo). Applying a selector you half-understand is worse than
  ignoring it: it invents failures on files that are fine.
- **Index stylesheet rules by property and memoise per (element, property).**
  Nested-selector matching is O(rules × elements × chain depth); a few thousand
  rules against a few thousand elements turns seconds into minutes otherwise.
- **Check closure per subpath, not per path.** `path.isclosed()` on a
  multi-subpath `Path` is all-or-nothing; split with `continuous_subpaths()`
  and test each, ignoring zero-length moveto stubs.
- **Keep every threaded state slot a single shape.** A recursive walker that
  carries `(width, source)` in one place and a bare string in another fails
  later inside string formatting (`not all arguments converted`) with a message
  that points nowhere near the walker. One shape per slot, consistently.
- **A documented spec key may be read by nothing — grep for readers before
  relying on it.** A key can sit in every spec file and be described in the docs
  as the budget that governs a behaviour while no code path reads it; two
  similarly-named knobs (a human menu cap, an automated retry budget) are easy to
  conflate from the names alone. Find the consumer before wiring a control to it.
- **A normaliser is not a validator.** A helper that maps what it recognises and
  returns everything else *unchanged* will happily "accept" a typo — verified:
  `normalize_color('not-a-colour')` returns `'not-a-colour'`, so a bad value
  reaches the artifact as `fill="not-a-colour"` and crashes later in the pipeline
  instead of failing at the gate. Gate every externally-supplied value (a palette
  entry, a user-supplied colour map) with a strict pattern check of its own
  (`^#[0-9a-f]{6}$` and the like); the normaliser is for *reading* values you
  already trust.
- **Name the lever, or say there isn't one.** A rejection is only actionable if
  the operator can see what to change. Where a limit has a known remedy, say so;
  where nothing downstream can fix it, say *that* — a node-count limit has no
  post-process reducer, so the only fix is a coarser upstream re-trace, and a
  finding that implies a fixable knob which does not exist sends the reader
  hunting for it.
- **A spot-colour gate cannot be satisfied by tonal source — screen the input
  before the trace, not after the failure.** A hard ink limit is unsatisfiable
  by a tonal image by construction, so tuning the tracer cannot rescue it. Check
  the source first: count distinct colours, and ask how many colours cover ~95%
  of pixels. Measured on a real failure: an engraved floral with **245 608**
  distinct colours, where even 256 colours covered only **34%** of pixels.
  Zero of twelve candidates passed with colour intact; the two marked `passed`
  did so by collapsing the artwork to **1 declared colour** (a silhouette) —
  a pass that discards the artwork is worse than a visible failure, because it
  reports success and ships nothing. Flat, hard-edged source (≤ the ink limit,
  drawn from the spec palette, no gradients) traced at `mae_art` 0.005 with
  zero hard findings. **When a passing artefact has an implausibly high
  deviation score, check `declared_colors` and the colormode before believing
  the pass** — it likely passed by deleting the art.
- **Solid `#000000` alone can exceed the TAC limit.** TAC is measured from a
  CMYK separation, and a solid black separates to a rich black near 400%
  coverage on its own — over a 300% ceiling with no other ink present.
  Measured: an otherwise-identical design tripped the advisory at 300% with
  3.4% of the sheet over the limit using a black keyline, and reported no
  advisory at all with a saturated hue in the same role. Recommend a saturated
  hue for key lines, rules and registration marks; **overlapping plates trip it
  too**, because coverage is summed per pixel, so separate shapes rather than
  stacking them. Attribute this as a design finding, not a pipeline fault: the
  number is correct, the artwork is the problem.
- **Wrap rule crashes for production, unwrap them for debugging.** When checks
  are wrapped so a crash becomes a `[RULE] check crashed: ...` line, the
  traceback is gone — for diagnosis, import the module and call the check
  function directly on the parsed tree.
- **Prefer `svgpathtools` behaviour you can verify over intuition**: a
  moveto-only `d` parses to an empty `Path` (falsy `len`), and a closed path
  that doubles back has length but zero area. See
  `references/path-geometry-svgpathtools.md` for the API recipes and fallbacks.

### References

- `references/svg-paint-and-units.md` — colour normalisation decision table,
  unit conversions, cascade resolution order, at-rule handling.
- `references/path-geometry-svgpathtools.md` — svgpathtools recipes for
  closure, emptiness, length and area, with defensive fallbacks.
- `references/validator-testing.md` — the pytest suite shape: parametrised unit
  cases, two-way threshold checks, dedupe cases that use genuinely equal values.
- `references/passed-is-not-faithful.md` — ranking candidates on whether the
  artwork survived the trace, the ascending-sort rank inversion, and why the
  verdict stays advisory.
- `references/prepress-measurement.md` — rendered ink counting, the tiffsep
  plate-polarity calibration, the CMYK re-conversion trap, and TAC provenance.
- `references/two-layer-pipeline.md` — orchestrating source validation and
  render preflight as two gates, the hard/advisory table, the facts each layer
  owns (source, render, operator), and the diverse batch that exposed each
  measurement bug.
- `scripts/svg_edge_case_probe.py` — run a validator against a batch of
  degenerate SVGs and flag unexpected outcomes.
