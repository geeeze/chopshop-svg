# Two-layer print pipeline: what each gate can and cannot see

Source validation and render preflight are not substitutes. Each has a blind
spot the other covers, and the disagreements are the useful signal.

## The split

| | Layer A: source validation | Layer B: render preflight |
|---|---|---|
| Input | the SVG attributes and cascade | the rasterised artwork |
| Speed | fast, deterministic | slower, needs Inkscape + Ghostscript |
| Sees | declared colours, stroke widths, path closure, node counts, size, palette | rendered ink count, continuous tone, ink coverage, placed-ppi |
| Blind to | anything only a render shows (gradients, tone) | anything not visible in pixels (a 0.85pt hairline renders fine) |

## Where they disagree (measured, 300x400mm shirt artwork at 300 dpi)

| file | layer A | layer B | why |
|---|---|---|---|
| flat logo, 4 inks | pass | pass | agreement |
| 9-colour illustration | FAIL | FAIL | both count 9 against a budget of 6 |
| photographic | FAIL (raster embed) | FAIL (continuous tone) | agree, different reasons |
| one-ink vector halftone | pass | pass | a dot field is binary in the render, correctly not "tone" |
| fine line art, 0.3mm rules | FAIL (hairline) | pass | **B cannot see stroke width** |
| 300mm/300-unit viewBox | pass | pass | would have failed before viewBox-aware units |

Two of these are the whole argument for running both gates. The line-art file
fails A and passes B; a two-stop gradient does the reverse.

## Facts only the source knows

The gates are not merely complementary in what they *measure*; some facts are
owned by one layer and must be handed across, because the render is ambiguous
where the source is definitive.

- **White is the standard case.** A rendered white pixel may be artwork or may be
  the transparent background showing the paper. Layer A knows (white is in the
  declared palette, or it is not); Layer B cannot tell. Pass
  `white_declared_in_artwork` from A into B's measurement.
- **The error is direction-dependent.** Inferring white-is-an-ink from the render
  is accidentally correct on transparent-background art and *silently drops a real
  screen* on full-bleed art, which has no background at all. Only a full-bleed
  fixture exposes this — a fixture with a background will happily confirm the bug.
- **Generalise the pattern:** whenever a rule's answer depends on intent rather
  than pixels (is this white an ink? is this a cutting job? is this gradient the
  plan?), the source layer decides and passes the fact down as an explicit input.
  Do not let the render layer re-derive it.
- **Re-check the arithmetic across the batch after any counting change.** The
  rendered ink count should equal the declared non-white count on every file; a
  *uniform* offset across all jobs means a background is being counted, not that
  the artwork gained an ink.

## The garment: a fact only the operator knows

The source can say white is in the artwork; it cannot say what colour the shirt
is, and that decides whether white costs a screen.

| garment | white in the design | screens |
|---|---|---|
| white / light fabric | `#ffffff` fill | **not counted** — it is the garment |
| dark fabric | `#ffffff` fill | **counted** — it is the underbase plate |

Measured on one flat logo: **4 screens on light fabric, 5 on dark.** Against a
6-colour budget that is the difference between passing and rejected, so a
light-fabric default is a silent undercount on every dark-garment job.

- Derive the implied `count_white_as_ink` from the underbase key only when that
  key is *absent*; with both present, honour the explicit value and warn. The
  dangerous direction is always the undercount.
- Do not count artwork white and an underbase as two screens — they are the same
  physical plate. Underbase on with no artwork white still yields exactly one
  extra screen.
- **Say the assumption out loud.** The report names it and prices it
  (`white: paper, not an ink (assumes a light/white garment)`), and the LOG
  carries the consequence (`this job needs 5 screens, not 4`). A garment warning
  that lives only in a stats dict is one nobody reads.
- **An input the tool cannot read belongs at the front of the human doc**, framed
  as a question to answer before shipping rather than as a footnote under the
  setting that implements it. The highest-consequence item is the one a reader is
  least likely to reach if it is buried in a settings table.

## Gate classification

Only hard findings should fail a job, or people learn to ignore the tool.

**Hard:** raster embeds when disallowed, declared or rendered colour count over
budget, stroke below minimum, open paths on a cutting job, zero-area shapes,
malformed SVG, placed bitmap below the ppi floor, wrong physical size, CMYK
demanded but absent, continuous tone on a spot-colour job.

**Advisory:** RGB-derived TAC (it cannot fire a 300% limit — see below),
non-embedded fonts by default, path node counts, off-palette colours, and
continuous tone when `gradient_handling` is `embedded_raster` (smooth tone is
the plan there, not a mistake).

## Manifest

One JSON per job, carrying both layers, every finding with its layer and
severity, the artifacts, and the tool versions — a result is uninterpretable
months later without the Ghostscript version. Write it from the orchestrator so
there is exactly one copy.

Artifact naming: write `<stem>.proof.png` / `<stem>.print.pdf` /
`<stem>.manifest.json` as the canonical per-job names, **plus a run-unique
stamped copy** (`<YYYYmmdd-HHMMSS>-<4 random hex>.proof.png`, etc.). Never write a
fixed plain name (`proof.png` / `manifest.json`) — consecutive runs silently
overwrite each other, which is why the user asked for stamping. The stamped copy
keeps a single-job handoff convenient; the run stamp means a batch never has one
job clobber another.

## Batch design

The batch is not a smoke test, it is the measurement instrument. Pick
archetypes that stress *different gates*: flat logo (both pass), complex
illustration (both fail, colour budget), photographic (raster + tone), halftone
(binary in render, so tone must NOT fire), fine line art (A only), and a
document that exercises the unit conversion. Three real bugs — the photograph
reading as one ink, a solid grey bar swallowed as a blend, and a grey chain kept
as four phantom inks — were all found by this batch and by nothing in the unit
tests written before it.

Run it through the real orchestrator (`pipeline.sh`), not by importing modules,
so the orchestration itself is exercised.
