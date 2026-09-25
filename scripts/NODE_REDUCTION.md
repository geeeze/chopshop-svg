# Node reduction — sourcing research and findings

**Status: research complete, no tool adopted.** Every number below was measured
on this repo's own traced candidates using the repo's own gate
(`geometry.max_nodes_per_path = 500`, counted as `len(parse_path(d))`, matching
`validate_svg.py`).

The problem: `geometry_overload` is real and present in the example sweep.

| candidate | paths | max nodes | total | paths > 500 |
|---|---|---|---|---|
| `candidate_04` | 2 491 | **2 364** | 56 376 | 3 |
| `candidate_07` | 22 244 | 1 526 | 247 567 | **19** |
| `candidate_08` | 12 517 | **2 240** | 154 367 | **13** |
| `candidate_09` | 204 | 826 | 7 725 | 1 |

So the gate is missed by up to **4.5x** on a single path, and a sweep can produce
19 offending paths at once.

---

## 1. External tools: three routes, three dead ends

### 1a. Byte optimizers do not reduce nodes — at all

**Scour, SVGO and svgcleaner are ruled out on principle, not on measurement.**
Their own documentation is explicit:

> SVGO *"does not remove anchor points. A path that arrives with 4,000 points
> leaves with 4,000 points. Everything above makes each point cost fewer bytes;
> nothing here refits the curve."*

Scour's `--set-precision` and SVGO's `convertPathData`/`cleanupNumericValues`
round coordinates and shorten command syntax. The path keeps every node.

This matters because it is the *intuitive* first answer, and it is wrong. A byte
optimizer applied to a `NODE_COUNT` failure silently does nothing while looking
like it worked (the file does get smaller). Do not wire one in.

### 1b. `svg-simplifier` 1.0.2 — correct idea, crashes on our data

`pip install svg-simplifier` is the most promising PyPI candidate: adaptive
flattening, Douglas-Peucker, Schneider Bézier fitting, curve merging, with a
`--tolerance` flag. It advertises 60–90% node reduction.

**It raises `TypeError: object of type 'CubicBezier' has no len()` on 40.7% of
paths in real VTracer output** — 83 of 204 paths in `candidate_09`, and it is
deterministic (the same input fails identically every run, so this is a latent
bug, not flakiness).

What we established about the trigger, having ruled out the obvious guesses:

- **not segment count** — synthetic paths of 1…100 smooth cubics all pass,
  including 100 segments
- **not degenerate geometry** — coincident control points, control point on an
  endpoint, zero-length cubics, fully-collinear cubics: all pass in isolation
- **not the document** — the failing path fails in `viewBox` 20, 100 and 2000
- **correlates with complexity** — failing paths average 57 segments, passing
  paths average 24.7; and every feature we tested (collinear, coincident
  control points) is present in 98–100% of *both* groups, so no single feature
  discriminates
- **it is the exact byte-string, not the geometry** — passing the same path back
  through `svgpathtools`'s `d()` serialiser (which respaces the commands) makes
  it pass

That last point localises the bug to its path-data handling of VTracer's compact
serialisation. Using the tool would mean forking and repairing a third-party
package — it also drags in **shapely** (GEOS), a heavy new dependency for a
repo whose requirements are currently all pure-Python-plus-Pillow.

### 1c. Inkscape `path-simplify` — works, and is already installed

Inkscape is already a **required** system tool, so this costs nothing new to
depend on. Driven headlessly:

```
inkscape in.svg --actions="select-all;path-simplify;export-filename:out.svg;export-do"
```

On `candidate_09`: **max 826 → 234 (−71.7%), total 7 725 → 1 801, paths > 500:
1 → 0**, colours preserved, bytes −86.7%, path count unchanged. It clears the
gate.

Two real problems.

**The threshold is a preference, not a flag.** `<group id="simplifythreshold"
value="0.002"/>` in `preferences.xml`. There is no CLI option. Scripting it means
writing a preferences file and pointing `HOME` at it, which is fragile and
undiscoverable.

**The default wrecks the artwork.** Sweeping the threshold and measuring ink
coverage against a render:

| threshold | max nodes | >500 | ink lost | ink gained |
|---|---|---|---|---|
| 5e-05 | 836 | 1 | 0.199% | 0.138% |
| 0.0001 | 780 | 1 | 0.511% | 0.355% |
| **0.0002** | **499** | **0** | **1.639%** | 1.065% |
| 0.0005 | 276 | 0 | 6.201% | 3.902% |
| 0.001 | 204 | 0 | 11.799% | 7.132% |
| 0.002 *(default)* | 234 | 0 | **18.116%** | 12.203% |
| 0.005 | 226 | 0 | 28.128% | 23.881% |

Reading this:

- **Clearing the gate costs roughly 1.6% of ink coverage.** That is the price of
  admission, not an accident of tuning.
- **The default threshold loses 18% of the ink.** Anyone who reaches for
  "just run Inkscape simplify" gets a materially different picture, and nothing
  in either tool tells them.
- **It is not monotonic.** Threshold 0.001 yields max 204 while 0.002 yields
  max 234 — *more* nodes at a more aggressive setting. So the threshold is not a
  clean dial, which rules it out as a parameter a deterministic loop can walk.

### 1d. No fixed threshold generalises

The gate-clearing threshold on `candidate_09` is **0.0002**, which leaves a
**0.2% margin** (max 499 against a limit of 500). Testing whether that value
transfers to a heavier candidate says no:

| candidate | max nodes | clears at | ink lost at clearance |
|---|---|---|---|
| `candidate_09` | 826 | 0.0002 | 1.639% |
| `candidate_04` | 2 364 | **0.0008** | **1.050%** |

Three things follow, none of them convenient:

- **The threshold differs by 4x** between two candidates. A single configured
  value cannot serve both.
- **The ink cost at clearance is not predicted by node count.** The file with
  2.9x more nodes needed 4x the threshold and paid *less* ink (1.05% vs 1.64%).
  So the price of clearing the gate has to be measured per file, not assumed.
- **The response is cliff-like, not gradual.** On `candidate_04`, threshold
  0.0004 still had 1 path over the limit (max 723); 0.0008 cleared it (max 402).
  Bisecting for the minimum clearing threshold is therefore not cheap.

And a genuine hazard for any automated loop: at threshold `2e-05` on
`candidate_04`, Inkscape **increased** the worst node count above the original —
2 364 → 2 752 — while reporting a successful simplify and losing 0.010% of the
ink. A run that naively re-simplifies could make a path *worse*. Any wrapper must
compare against the input and reject a step that regresses `path_nodes_max`.

## 2. The retrace route is too weak on its own

VTracer's pip API exposes four curve-fitting parameters the repo's `PRESETS`
never set: `corner_threshold`, `length_threshold`, `max_iterations`,
`splice_threshold`. Retracing `00_source/00-example.png`:

| config | max nodes | total | bytes | secs |
|---|---|---|---|---|
| baseline | 970 | 92 224 | 6 563 231 | 3.1 |
| `max_iterations` 20 | 970 | 92 224 | 6 563 231 | 3.1 |
| `max_iterations` 50 | 970 | 92 224 | 6 563 231 | 3.1 |
| `length_threshold` 8 | 801 | 85 028 | 5 825 586 | 3.2 |
| `length_threshold` 50 | 757 | 84 183 | 5 687 506 | 3.3 |
| `splice_threshold` 90 | 818 | 76 302 | 5 224 297 | 3.3 |
| `mode` polygon | 990 | 95 849 | 897 917 | 3.2 |

- **`max_iterations` is inert** — identical output at 10, 20 and 50. Do not
  present it as a knob.
- `length_threshold` and `splice_threshold` do bite, but only **9–22%**.
- The gate needs **78%** on the worst candidate. Retracing cannot get there.

Worth knowing: `mode` polygon cuts bytes by 86% while *increasing* nodes — it is
a file-size lever, not a node lever. Keep it away from `geometry_overload`.

**Conclusion: `geometry_overload` cannot be fixed by retracing alone.** It needs
a genuine simplifier.

## 3. The finding that constrains every option: shared boundaries

Independent per-path simplification is what every tool above does, and it is
wrong for this artwork.

VTracer traces each colour region as its own `<path>`. Adjacent regions **share
boundaries** — measured on `candidate_08` by sampling every path's outline onto a
2-unit grid:

- **36.4% of occupied grid cells are covered by more than one path**
- 8 267 cells are covered by 3 or more paths

If a simplifier moves the shared edge on one side and not the other, the
background shows through between two flat inks. On a screen print that is a
white hairline through the artwork: invisible in the SVG, obvious on a shirt.

A controlled test (two regions meeting along an identical zigzag boundary, area
accounting `delta = (area_A + area_B) - area_union`) shows the failure appears as
soon as the tolerance exceeds the boundary detail:

| eps | delta | verdict |
|---|---|---|
| 0.25 | 0 | clean |
| 0.5 | 0 | clean |
| 1.0 | +5.0 sq units | gap begins |
| 2.0 | +120.0 sq units | **gap** |

And it is visible in real output. At 1400×1400 on `candidate_09`:

| | max nodes | reduction | ink lost | ink gained | largest gap blob |
|---|---|---|---|---|---|
| Inkscape @ 0.0002 | 499 | 40.4% | 1.65% | 1.26% | **5 px** |
| my prototype @ 0.25 | 622 | 24.9% | 7.05% | 17.13% | **1 002 px** |
| Inkscape @ default | 234 | — | 18.12% | 12.20% | — |

Inkscape's worst gap is **5 px** — a hairline. The prototype's is **1 002 px** —
a shape actually moved. **No external tool measures this at all**, and none of
them even has a concept of a shared edge.

This is the requirement that decides the design: a simplifier for flat-colour
print must reason about *regions*, not *paths*.

## 4. A prototype was written, and it is not good enough yet

`simplify_paths.py` (scratch; not committed) implements greedy run-fitting:
grow the longest run of segments replaceable by ONE cubic whose deviation stays
within tolerance, endpoints fixed so closure and connectivity are exact by
construction. Least-squares control points from chord-length parameters,
`samples_per_segment = 12`, `max_run = 64`. Dependencies: `svgpathtools` +
`numpy` + `lxml` only — all already required.

It is fast and it does clear the gate:

| tolerance | max nodes | >500 | reduction | time |
|---|---|---|---|---|
| 0.25 | 622 | 1 | 24.9% | 1.18 s |
| 0.50 | **439** | **0** | 48.6% | 0.79 s |
| 1.00 | 300 | 0 | 65.3% | 0.61 s |
| 2.00 | 189 | 0 | 78.3% | 0.66 s |

But its **geometric fidelity is 8x worse than Inkscape's at lower reduction**
in whole-file mode (see the table in §3: 1.627% of frame changed vs Inkscape's
0.196%). Known cause: deviation is measured by comparing the fitted curve and
the original sample **at the same chord-length parameter**, which is not a true
geometric distance. A correct implementation needs the nearest-point-on-curve
distance plus Schneider-style re-parameterisation.

**Important qualification:** that verdict applies to *whole-file* simplification.
Applied only to the paths that actually fail the gate (§5) the same fitter beats
Inkscape on ink preservation, because the flaw's impact scales with how much of
the file is being refitted. The metric should still be corrected, but the tool is
not the dead end §3's numbers suggest.

## 5. The solution: simplify only the paths that fail

All of the above assumed the whole file has to be simplified. It does not. The
failure is a handful of outliers — **1 path of 204** on `candidate_09`, **3 of
2 491** on `candidate_04`, **13 of 12 517** on `candidate_08`. Simplifying
everything spends fidelity on thousands of paths that were already fine.

Restricting the work to paths whose `path_nodes_max` exceeds the gate, and
escalating tolerance per path until it clears, changes the economics completely.

`candidate_09` — **identical node outcome, one path touched instead of 204**:

| mode | paths touched | max nodes | gate | ink lost | frame changed |
|---|---|---|---|---|---|
| **targeted** tol 0.5 | **1** | 439 | cleared | **1.192%** | **0.432%** |
| whole file tol 0.5 | 204 | 439 | cleared | 6.615% | 1.510% |

Same `max` (439), same gate result — **5.6x less ink lost, 3.5x less of the
frame disturbed.** Targeted also leaves `total` at 7 338 versus 3 971, so the
detail that was already acceptable is preserved rather than flattened.

It also beats every external tool measured here:

| approach | max nodes | ink lost |
|---|---|---|
| Inkscape, gate-clearing threshold | 499 | 1.639% |
| Inkscape, default threshold | 234 | 18.116% |
| **targeted, tol 0.5** | **439** | **1.192%** |

And it holds on the hard candidates — **all four clear the gate**:

| candidate | over-gate paths | touched | max before | max after | px changed | darkened |
|---|---|---|---|---|---|---|
| `candidate_09` | 1 of 204 | 1 | 826 | **439** | 0.696% | 0.463% |
| `candidate_04` | 3 of 2 491 | 3 | 2 364 | **490** | 2.725% | **0.007%** |
| `candidate_08` | 13 of 12 517 | 13 | 2 240 | **497** | 2.420% | **2.326%** |
| `candidate_07` | 19 of 22 244 | 19 | 1 526 | **477** | 0.080% | 0.056% |

`darkened` is the column that matters: it is the share of changed pixels that
moved *toward* the background, i.e. erosion — the direction that opens a white
hairline. `px changed` minus `darkened` is ink gained, which is benign for flat
spot colour.

Read that way the four candidates split into two groups:

- **`candidate_04` and `candidate_07` are essentially pure gain** — 0.007% and
  0.056% darkened against 2.725% and 0.080% total change. The simplification
  moves boundaries *outward*, filling rather than eroding. That is the safe
  direction and it costs the artwork nothing.
- **`candidate_08` and `candidate_09` do erode** — 2.326% and 0.463% darkened.
  `candidate_08` is the one that needs the render verification to sign off,
  because 2.3% of the frame getting lighter is exactly the gap signature.

So the earlier claim that `candidate_08` costs "0.005% of ink" was an artifact of
the metric — see the warning below. Its real figure is 2.326% darkened.

### Warning: the ink-vs-white metric is degenerate on full-bleed artwork

The measurements in §1c and the first pass at §5 split the render into "ink"
versus "background" by comparing against white. **That silently fails when the
artwork has no white background.**

`candidate_07` renders **100.00% "ink"** — every pixel is non-white — so the
metric had no reference and reported **0.000% change** for a file where 19 paths
had just been rewritten. Almost reported as a perfect result. It was a broken
measurement.

Always check the ink fraction before trusting an ink-based number:

| candidate | ink fraction | metric valid? |
|---|---|---|
| `candidate_09` | 6.89% | yes |
| `candidate_08` | 53.91% | yes |
| `candidate_07` | **100.00%** | **no — degenerate** |

Any implementation must either detect the degenerate case and fall back to a
pixel-difference metric (as the table above does), or refuse to report a number it
cannot support. This is the second time in this investigation that a
plausible-looking figure was produced by a metric that could not discriminate;
the render verification step is where that gets caught.

### Two design consequences

**Targeting is engine-independent — it is the strategy, not the tool.** The same
restriction applies to Inkscape: select only the offending paths and simplify
those. `candidate_09`'s 1.639% was the price of simplifying all 204 paths; a
targeted Inkscape run should do better still, because Inkscape's reduction was
the best-measured per path. Worth measuring before choosing an engine.

**The gate can now be met without mangling the art**, which means
`geometry_overload` stops being the "unfixable" mode it was treated as in the
remediation plan. It becomes loopable with a bounded, targeted remedy.

### What still needs doing

1. **Fix the fitted-deviation metric** in `simplify_paths.py` — it currently
   compares the fit and the original at the same chord-length parameter rather
   than by true nearest-point distance, which is why whole-file mode perturbs 8x
   more than Inkscape. In targeted mode the flaw matters much less (one path,
   and it already beats Inkscape), but it should still be corrected before the
   tool is relied on.
2. **Weld shared boundaries** (§3) — still the real remaining risk. Targeted
   simplification limits which edges can move, which reduces exposure, but does
   not eliminate it.
3. **Wire the verification** — render before/after, report ink lost/gained and
   the largest gap blob. Nothing in the toolchain does this, and it is what
   detected the 18% default-threshold loss.
4. **Decide the escalation policy** — each path gets a bounded tolerance search
   (double up to a cap). Record the per-path tolerance actually used in the
   sidecar so a run is explainable.

## 6. Recommendation

1. **Do not adopt a byte optimizer.** It cannot fix this and will look like it
   did.
2. **Do not adopt `svg-simplifier`** without forking it — 40.7% failure on real
   input, plus a GEOS dependency.
3. **Do not simplify the whole file.** Target the over-gate paths (§5). This is
   the single biggest lever and it is independent of which engine runs the fit.
4. **Prefer Inkscape as the engine**, for reduction quality per path, with the
   wrapper supplying what it lacks:
   - threshold control → write a scoped `preferences.xml` and point `HOME` at it
   - gate targeting → select only the offending paths (by id), then simplify
   - **escalate per file**, because a fixed threshold provably does not transfer
     (4x spread, §1d) and the gate-clearing value leaves a 0.2% margin
   - **reject regressions** → compare against the input; Inkscape can *increase*
     the worst node count at low thresholds (§1d)
   - **verify by render** → ink drift and gap blobs
5. **`geometry_overload` is now remediable** — a bounded, targeted simplification
   clears the gate on every example candidate tested, at ink costs from 0.005%
   to 1.19%. Update the remediation table to make it loopable, citing
   `NODE_COUNT`, with the render verification as the pass condition rather than
   the node count alone.
6. **Raise the gate instead of forcing the fix if the render says no.** For a
   complex illustration, one contour at 826 nodes is a legitimate finding to
   report. `max_nodes_per_path: 500` is a default, not a law — the pipeline's job
   is to report, and the human decides. Do not silently trade artwork for a
   number.

Everything above was measured with the repo's own gate and a real Inkscape
render. The harnesses (shared-boundary probe, threshold sweep, print test,
deterministic compare) are in the session scratch directory; the measurements
are reproducible from `02_traced/00-example/` and
`00_source/00-example.png` without any network access.
