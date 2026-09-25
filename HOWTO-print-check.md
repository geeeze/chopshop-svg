# SVG print check — how to use this

> **New here?** Read `OVERVIEW.md` first — it explains what the system is, why
> there are two gates, and what inputs you need. This document is the hands-on
> usage guide and the full settings reference.

One command runs both quality gates:

```bash
cd /path/to/chopshop-svg
./pipeline.sh 00_source/your-artwork.svg
```

Exit code 0 means send the file. 1 means it lists what to fix. It writes:

| File | What it is |
|---|---|
| `05_final/<name>.proof.png` | What the artwork actually looks like at print resolution. Send this to the client. |
| `05_final/<name>.print.pdf` | The PDF for the printer. |
| `05_final/<name>.manifest.json` | Everything both gates found, with severities. This is the record. |
| `04_validated/<name>.layer_a.txt` | The source-level result on its own. |

Per-run copies are kept too (`05_final/<stamp>.proof.png`, where `<stamp>` is a
timestamp + a few random bytes), so consecutive runs never overwrite each other.
Open the artwork by hand with `inkscape 00_source/your-artwork.svg`.

Options: `./pipeline.sh file.svg --dpi=300` overrides resolution;
`--layer-a-only` skips the render step; `SPEC=other.json ./pipeline.sh x.svg`
uses a different spec. With no argument it takes the first SVG in `00_source/`.

To run the individual pieces: `.venv/bin/python validate_svg.py art.svg spec.json`
and `.venv/bin/python preflight.py art.svg spec.json --json report.json`.

---

## Read this first if the shirts aren't white

**The tool assumes white fabric unless you tell it otherwise.** This is the
single setting most likely to cost money, because it is the only one that
depends on the *garment* rather than the artwork.

No ink is no ink only when the fabric is already white. On a **dark** garment,
white is usually the **first** ink down — the underbase, printed beneath
everything else so the other colours have something to sit on. Same file, same
artwork, different screen count:

| garment | white in the design | screens |
|---|---|---|
| white / light fabric | `#ffffff` fill | **not counted** — it is the garment |
| black / dark fabric | `#ffffff` fill | **counted** — it is the underbase |

For one of the test files that is the difference between **4 screens and 5**. On
a six-colour budget, that is the difference between passing and being rejected.

Set it for the job:

```json
"print": {
  "dark_garment_underbase": true
}
```

That implies `count_white_as_ink`, adds the white screen to the count, and
caveats the ink-coverage figure — the CMYK measurement is made on paper and
cannot represent a white laydown under the whole design, so treat that number as
a lower bound on a dark garment.

**The tool will warn you if it thinks you've got this wrong.** Whenever white is
in the artwork and is *not* being counted, it says so and gives both numbers:

```
  white: paper, not an ink (assumes a light/white garment)
LOG
  - white is declared in the artwork but is NOT counted as an ink, which assumes
    the garment is white -- on white fabric that is correct... On a DARK garment
    white is very often the first ink down, laid as an underbase beneath
    everything else, so this job would need 5 screens, not 4. Set
    print.dark_garment_underbase to true if these are dark shirts.
```

If you don't know the garment yet, that warning is the thing to come back to
before the job goes to the printer.

---

## The two gates

**Layer A — source validation** (`validate_svg.py`). Fast, deterministic, reads
the file itself. Catches raster embeds, too many declared colours, hairline
strokes, open paths (on cutting jobs), zero-area shapes, paths with too many
nodes, the wrong physical size, and colours outside the agreed palette.

**Layer B — render preflight** (`preflight.py`). Slower; renders the file and
measures what it actually prints as. Catches what Layer A structurally cannot:
how many inks the artwork *really* needs, whether it contains continuous tone,
total ink coverage, and placed-bitmap resolution.

Both run every time. Neither replaces the other, and where they disagree is
informative:

> A two-stop gradient is **2 colours** in the file and **hundreds** once
> rendered. Layer A passes it; Layer B fails it. Declaring a gradient is not the
> same as printing one.

A file can also pass Layer B and fail Layer A: a 0.85pt hairline renders as a
perfectly ordinary line, so only the source check can see that it will break up
on press.

---

## Hard gates vs advisories

Not everything reported should be treated with equal authority. The manifest
records which is which, and **only hard gates fail the run**.

**Hard gates** — the file cannot be printed as specified:
raster embeds when disallowed · too many inks (declared *or* rendered) · strokes
below the minimum · open paths on a cutting job · zero-area shapes · malformed
SVG · placed images below the resolution floor · CMYK demanded but the output
isn't · the wrong physical size · continuous tone on a spot-colour job.

**Advisory** — reported, does not fail:
RGB-derived ink coverage (it cannot be trusted as a limit — see below) ·
non-embedded fonts (set `"require_embedded_fonts": true` to make it a gate) ·
paths with more nodes than the limit · colours outside the palette ·
continuous tone when `gradient_handling` is `embedded_raster`, where smooth tone
is expected rather than a mistake.

---

## What it checks, and why it matters

**1. How many inks the artwork needs.** This is the one that catches people.

A gradient between two colours is written as *2* colours and renders as
*hundreds*, one per step of the blend. On screen print, vinyl, foil — anything
where each colour is a separate pass and a separate charge — that is not
printable.

Colours within a hair of each other are merged, so the soft antialiased edge of
a shape doesn't count as ten extra inks. Two refinements came out of testing on
real files, and both matter:

- An antialiasing **edge** is 1–2 pixels wide. A deliberate flat colour is a
  filled region. The counter tells them apart by shape, not by size — a solid
  grey bar was being swallowed as a "blend" because neutral grey sits exactly on
  the line between black and white.
- A **photograph** has no single band large enough to see, so every one of its
  colours falls below the per-colour area threshold. Judged per colour, a
  photograph reports as *one ink* and passes. It is caught instead by how much
  of the sheet those tiny colours cover *between them* — measured 44.4% for a
  photograph against ≤0.20% for every flat file tested.

**2. Tone.** Gradients and photographs are flagged `CONTINUOUS_TONE`. They need
halftone screening, which means process (CMYK) printing, not spot colours. If
your printer *is* doing CMYK process work, set `"allow_gradients": true` and
this stops being an error.

**3. Ink coverage.** How much ink is on the darkest part of the sheet, summed
across the four plates. Above the limit the ink doesn't dry and the sheets stick
together. Default limit 300%.

**4. Fonts and images.** Fonts that aren't embedded get substituted by the
printer and your layout shifts. Images placed at too low a resolution print
visibly soft — a 240×320 bitmap stretched across a shirt measures 30 ppi.

**5. Source-level problems.** Raster images embedded when they shouldn't be, too
many colours declared, hairline strokes that will break up, paths left open,
shapes enclosing no area, traced outlines bloated with thousands of nodes,
artwork that is the wrong size, colours off the agreed palette.

---

## The honest caveats

**Ink coverage is only exact if the file is genuinely CMYK.** Your SVG is RGB.
Ghostscript converts it for the check, but applies no press-grade black
generation: measured, a 50% grey comes out at **145%** ink instead of ~50%, and
nothing exceeds ~296%, so a 300% limit can never fire on RGB input. The report
says `ESTIMATE` and prints a note.

For an exact reading, ask your printer for their ICC profile, convert once, then
re-run on the PDF — the pipeline takes PDFs:

```bash
./pipeline.sh converted.pdf
```

On a real CMYK file the measurement is exact: verified against a file with known
100%, 280% and 400% patches, read back as 100%, 279%, 400%. Then set
`"require_cmyk": true` and it becomes a hard gate; point `"icc_profile_path"` at
the printer's profile to use it for the conversion.

**Colour similarity is judged by rough RGB distance.** Good enough to separate
deliberate ink choices from rendering noise. Not a colour-managed proof.

**Stroke width is measured against the real document scale.** A user unit is
*not* a pixel once a viewBox rescales the document: in
`width="210mm" viewBox="0 0 595 842"` one unit is 0.3529mm, so a unitless
`stroke-width="1"` prints at 1.0005pt — not the 0.75pt a naive px assumption
reports. Verified against Inkscape renders in both directions, and the basis is
printed with every finding so you can check the arithmetic yourself.

**Overlapping transparent shapes and overprint effects** produce blended colours
counted as inks. If the design leans on those, ask the printer rather than
trusting the count.

---

## Stage 3 helpers

**`scripts/snap_colors.py`** — snaps every colour to the palette in spec.json,
which is what makes a 9-colour design printable on 6 screens:

```bash
python3 scripts/snap_colors.py 00_source/art.svg spec.json --dry-run
python3 scripts/snap_colors.py 00_source/art.svg spec.json -o 03_cleaned/art.svg
```

It refuses to guess: a colour further than `--tolerance` (default 32, Euclidean
sRGB) from every palette entry is reported and left alone, because a colour 180
units away is a *different colour*, not a near miss. `--force` overrides.

**`scripts/run_batch.py`** — runs the pipeline over every SVG in `00_source/` and
writes `04_validated/batch_results.json`.

**`scripts/svgo_print.yml`** — SVGO config for the other half of stage 3.
**SVGO needs Node.js, which this machine does not have.** The config is correct
and ready for a machine that does; `snap_colors.py` covers the colour half in
pure Python.

---

## Changing the rules

Edit `spec.json`. Everything under `print` is optional; any omitted key falls
back to its default. The complete field-by-field reference (top-level,
`geometry`, `validation`, and `print`, with defaults and meanings) is in the
**"The job contract"** section of `README.md` — it is the single canonical
table, so it is not duplicated here.

Two knobs deserve the extra context the table can't carry:

- **`print.dark_garment_underbase`** — see "Read this first if the shirts aren't
  white" above. This is the one setting that depends on the *garment*, not the
  artwork, and the tool warns you when it thinks you've got it wrong.
- **`print.ink_merge_tolerance` / `ink_blend_tolerance` / `tone_min_*`** — these
  are the boundaries between "a deliberate ink", "an antialiasing edge", and
  "continuous tone". The ink-counting section above explains what each one does
  and the measurements that set the defaults; don't tune them without reading
  that first.

---

## Where this stops

The tool won't tell you whether the job is *economically* right — how many inks
the printer charges for, process versus spot, which stock. That's a conversation
with the printer, and it's worth having early, before the artwork is finished.

It also doesn't replace a printed proof. It catches the mechanical reasons files
get rejected; it can't tell you a colour looks wrong on the chosen stock.

**The one thing worth doing before any big job:** send the printer a test file
and ask for their ICC profile and their ink limit. Feed both back in and the
checks stop being indicative and become exact.