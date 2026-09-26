---
name: svg-print-preflight
description: Use when validating SVG files in code (print preflight).
---

# SVG print preflight & programmatic SVG inspection

Validating an SVG for production means judging what a renderer will actually
paint, not what the attributes literally say: paint arrives through a CSS
cascade, lengths arrive in mixed units, and geometry arrives as path data that
may be open, empty or collapsed. Every rule below exists because the naive
version of it silently passes a file that prints wrong.

## Deliverable convention

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

## Procedure

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

## Invariants

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

## Prepress measurement — what only a render can tell you

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

## Pitfalls

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

## References

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
