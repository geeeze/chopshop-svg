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
(see the table in §3: 1.627% of frame changed vs Inkscape's 0.196%). Known
cause: deviation is measured by comparing the fitted curve and the original
sample **at the same chord-length parameter**, which is not a true geometric
distance. A correct implementation needs the nearest-point-on-curve distance
plus Schneider-style re-parameterisation. Until then, shipping it would be
shipping something strictly worse than a tool already on the box.

## 5. Recommendation

1. **Do not adopt a byte optimizer.** It cannot fix this and will look like it
   did.
2. **Do not adopt `svg-simplifier`** without forking it — 40.7% failure on real
   input, plus a GEOS dependency.
3. **Use Inkscape as the reduction engine.** It is already required, it reduces
   best, and it perturbs least. Treat the two problems as work to wrap, not
   reasons to reject it:
   - threshold control → write a scoped `preferences.xml` and point `HOME` at it
   - gate targeting → iterate the threshold to the smallest value that clears
     `max_nodes_per_path`, since the default is 12x more aggressive than needed
   - **verification → render before/after and report ink drift and gap blobs.**
     Nothing in the toolchain does this today, and it is what caught the 18%
     ink loss. This is the single most valuable addition.
4. **Treat shared-edge preservation as the real engineering task.** Either weld
   the boundary vertices globally before simplifying (so two regions that shared
   a vertex still do), or accept Inkscape's hairlines and manage them. A
   region-aware simplifier is the correct long-term answer and no external tool
   provides one.
5. **Keep `geometry_overload` out of the automated loop until (3) exists.** A
   remediation that silently loses 18% of the ink is worse than one that stops
   and says `not_loopable`.

## 6. Reproduction

Everything above was measured with the repo's own gate and a real Inkscape
render. The harnesses (shared-boundary probe, threshold sweep, print test,
deterministic compare) are in the session scratch directory; the measurements
are reproducible from `02_traced/00-example/` and
`00_source/00-example.png` without any network access.
