# Prepress measurement recipes

Everything here was established by measurement on Debian 13, Inkscape 1.4,
Ghostscript 10.05. Nothing is from documentation.

## Render a proof

```bash
inkscape in.svg --export-type=png --export-filename=out.png \
  --export-dpi=300 --export-background=#ffffff --export-background-opacity=255
```

Works headless — no X server required. Inkscape is the authoritative renderer
(the one the designer sees). `rsvg-convert` is faster but has known fidelity
gaps around filters and text, so do not mix the two: pick one and be consistent,
otherwise "what does this look like" has two answers.

For PDF input (end-of-workflow checking) rasterise with Ghostscript instead:

```bash
gs -dNOPAUSE -dBATCH -dSAFER -sDEVICE=png16m -r300 \
   -dTextAlphaBits=4 -dGraphicsAlphaBits=4 -dUseCropBox \
   -sOutputFile=out.png in.pdf
```

## Calibrate plate polarity BEFORE trusting any TAC

`tiffsep` separates into one grayscale TIFF per ink, named
`prefix(Cyan).tif`, `prefix(Magenta).tif`, `prefix(Yellow).tif`,
`prefix(Black).tif` (plus a composite `prefix.tif`). The polarity is
undocumented and **inverted from what you would guess**:

- **255 = no ink, 0 = full ink**

So coverage per plate is `(255 - value) / 255 * 100`, and TAC is the sum across
the four plates. Always confirm with a patch chart and sample patch *centres*:

| patch | Cyan | Magenta | Yellow | Black |
|---|---|---|---|---|
| `#ffffff` | 255 | 255 | 255 | 255 |
| `#ffff00` | 241 | 255 | 8 | 255 |
| `#000000` | 70 | 82 | 84 | 29 |

Reading it the other way round produces a confident, meaningless number (it
reported a bright orange gradient as 298% TAC with a 99% black plate).

## Measure TAC

```bash
# CMYK source — pass values through untouched
gs -dNOPAUSE -dBATCH -dSAFER -sDEVICE=tiffsep -r300 \
   -dProcessColorModel=/DeviceCMYK \
   -sColorConversionStrategy=LeaveColorUnchanged \
   -sOutputFile=sep.tif in.pdf

# RGB source — must be converted (produces an ESTIMATE, see below)
gs -dNOPAUSE -dBATCH -dSAFER -sDEVICE=tiffsep -r300 \
   -dProcessColorModel=/DeviceCMYK \
   -sColorConversionStrategy=CMYK \
   -sOutputFile=sep.tif in.pdf
```

### Trap 1 — never pass `-sOutputICCProfile` for CMYK input

Forcing a profile makes gs re-convert CMYK → profile → CMYK and **rewrite the
values**. Measured: a known 400% rich black came back as **293%**. Debian ships
press profiles in `/usr/share/color/icc/colord/` (`SWOP_TR003_coated_3.icc`,
`FOGRA45L_lwc.icc`, `FOGRA29L_uncoated.icc`) — use them only when converting
*from* RGB.

### Trap 2 — TAC from RGB input is not a gate

gs's RGB→CMYK applies no press-grade GCR/UCR:

- a 50% grey builds at **145%** ink (should be ~50%)
- every dark colour lands in **273–296%**
- pure black peaks at **296%**, and passing a SWOP profile changes the K split
  but not the total

Consequence: a **300% limit can never fire on RGB input**, and a 240% limit
fires on every dark colour. Report the figure as an estimate, say plainly that
the gate cannot validate on RGB, and offer a PDF input path so it becomes exact
after a real conversion.

### Regression test for the ink path

Build a CMYK PDF with *known* patches via PostScript, then confirm the reader
returns them exactly:

```postscript
%!PS-Adobe-3.0
<< /PageSize [300 100] >> setpagedevice
0.5 0.3 0.2 0.0 setcmykcolor   0 0 100 100 rectfill      % 100%
0.7 0.7 0.7 0.7 setcmykcolor   100 0 100 100 rectfill    % 280%
1 1 1 1 setcmykcolor           200 0 100 100 rectfill    % 400%
showpage
```

```bash
gs -q -dBATCH -dNOPAUSE -sDEVICE=pdfwrite -dProcessColorModel=/DeviceCMYK \
   -sColorConversionStrategy=LeaveColorUnchanged -o cmyk.pdf cmyk.ps
```

Expected read-back: 100%, 279%, 400%. Any drift means the colour handling path is
wrong — check for a stray output profile first.

## Fonts and images

```bash
pdffonts in.pdf          # emb column: yes/no
pdfimages -list in.pdf   # effective x-ppi / y-ppi as placed
```

`pdffonts` has no machine-readable mode: slice by the header offsets of
`emb`/`uni`, with a standalone `yes`/`no` search as fallback.

`pdfimages -list` **data-row** field indices (verified): width `[3]`, height
`[4]`, x-ppi `[12]`, y-ppi `[13]`. The header row is shifted by the two-word
`object ID` column; data rows are not.

## Rendered ink counting

Pack each pixel to a single int (`r<<16 | g<<8 | b`), `np.unique(return_counts=True)`,
and work in percent-of-canvas shares.

1. Keep colours covering ≥0.05% of the sheet (drops most AA fringe).
2. Greedily merge remaining colours within Euclidean distance ~20 in sRGB.
3. A merged cluster is an **ink** if its total share clears the threshold.
4. A cluster with ≥6 member colours is a **ramp** → continuous tone.

Euclidean sRGB is a rough perceptual proxy. It separates deliberate ink choices
from rasterisation noise; it is not a colour-managed proof, and `var()`,
overprint and multiply blends remain out of scope.

If the significant-colour count is huge (photographs), bucket to 6 bits/channel
before the O(n × clusters) merge, and note that the count was pre-quantised.

## Screens vs inks, and the white underbase

A screen is a physical plate; an ink is a colour. On a spot-colour garment job
they are not the same count, and the difference is the white underbase laid
under everything else.

```
screens = len(non_white_inks) + (1 if artwork_declares_white or underbase else 0)
```

- **White on white paper is unmeasurable.** The proof renders onto white, so an
  underbase and the paper behind it are the same pixels. No render-side test can
  find it; the spec and the source palette must say so.
- **Do not count background white as artwork white.** A transparent-background
  file renders white wherever the paper shows through. Counting it as an ink
  invents an ink on every such file — it added exactly +1 ink to *every* job in a
  batch, which is the signature to look for. Dropping it everywhere instead loses
  a genuine underbase screen on full-bleed art, which has no background at all.
  Feed the source's declared palette in and decide on that.
- **Add the underbase once.** If the artwork already declares white, that screen
  *is* the underbase, so fold them; two white entries is the bug the "+1"
  phrasing invites.
- **Verify on a full-bleed fixture.** A fixture with a background cannot expose
  this. Render artwork whose ink covers the whole canvas and confirm the screen
  count differs with and without the underbase option (measured 3 vs 2). If the
  two agree, the white is coming from the paper rather than from the spec.
- **TAC becomes a lower bound** — `tiffsep` separates what is on the paper, and a
  plate underneath every other plate is not representable in that measurement.
