# Candidate comparison: 00-example

- spec: `spec.json`
- candidates: 12
- layer A (source validation): available
- layer B (render preflight): available
- sorted by: fewest hard gates failed, then whether the artwork survived the trace, then fewest advisories

| # | file | sweep | LayerA | LayerB | declared | inks | nodes (total/max) | size | MAE all/art | hard | adv | fidelity |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | candidate_04.svg | variant=source preset=poster speckle=2 hier=cutout palette=False | PASS | PASS | 5 | 5 | 189/96 | 16837 | 0.14 / 0.01 | 0 | 0 | faithful |
| 2 | candidate_05.svg | variant=source preset=poster speckle=8 hier=cutout palette=False | PASS | PASS | 5 | 5 | 189/96 | 16837 | 0.14 / 0.01 | 0 | 0 | faithful |
| 3 | candidate_06.svg | variant=source preset=poster speckle=16 hier=cutout palette=False | PASS | PASS | 5 | 5 | 189/96 | 16837 | 0.14 / 0.01 | 0 | 0 | faithful |
| 4 | candidate_10.svg | variant=source preset=poster speckle=2 hier=cutout palette=True | PASS | PASS | 5 | 5 | 189/96 | 16837 | 0.14 / 0.01 | 0 | 0 | faithful |
| 5 | candidate_11.svg | variant=source preset=poster speckle=8 hier=cutout palette=True | PASS | PASS | 5 | 5 | 189/96 | 16837 | 0.14 / 0.01 | 0 | 0 | faithful |
| 6 | candidate_12.svg | variant=source preset=poster speckle=16 hier=cutout palette=True | PASS | PASS | 5 | 5 | 189/96 | 16837 | 0.14 / 0.01 | 0 | 0 | faithful |
| 7 | candidate_01.svg | variant=source preset=bw speckle=2 hier=cutout palette=False | PASS | PASS | 1 | 2 | 44/23 | 4273 | 8.47 / 104.21 | 0 | 2 | artwork_lost |
| 8 | candidate_02.svg | variant=source preset=bw speckle=8 hier=cutout palette=False | PASS | PASS | 1 | 2 | 44/23 | 4273 | 8.47 / 104.21 | 0 | 2 | artwork_lost |
| 9 | candidate_03.svg | variant=source preset=bw speckle=16 hier=cutout palette=False | PASS | PASS | 1 | 2 | 44/23 | 4273 | 8.47 / 104.21 | 0 | 2 | artwork_lost |
| 10 | candidate_07.svg | variant=source preset=bw speckle=2 hier=cutout palette=True | PASS | PASS | 1 | 2 | 44/23 | 4273 | 8.47 / 104.21 | 0 | 2 | artwork_lost |
| 11 | candidate_08.svg | variant=source preset=bw speckle=8 hier=cutout palette=True | PASS | PASS | 1 | 2 | 44/23 | 4273 | 8.47 / 104.21 | 0 | 2 | artwork_lost |
| 12 | candidate_09.svg | variant=source preset=bw speckle=16 hier=cutout palette=True | PASS | PASS | 1 | 2 | 44/23 | 4273 | 8.47 / 104.21 | 0 | 2 | artwork_lost |

## By pixel fidelity (accuracy view)

Rendered back to the source resolution and diffed against the source raster. Lower MAE = closer to the original. `MAE art` ignores the background and measures the subject only.

A candidate can pass every gate and still not be the artwork: a `bw`/binary trace of a colour image discards the design entirely and comes back with hard=0. The verdict column above is that check, and the main table is sorted on it. It is advisory, not a gate -- a single-colour job is a legitimate ask.

| rank | file | MAE all | MAE art | P95 | within-10 |
|---|---|---|---|---|---|
| 1 | candidate_04.svg | 0.139 | 0.005 | 0.000 | 99.9% |
| 2 | candidate_05.svg | 0.139 | 0.005 | 0.000 | 99.9% |
| 3 | candidate_06.svg | 0.139 | 0.005 | 0.000 | 99.9% |
| 4 | candidate_10.svg | 0.139 | 0.005 | 0.000 | 99.9% |
| 5 | candidate_11.svg | 0.139 | 0.005 | 0.000 | 99.9% |
| 6 | candidate_12.svg | 0.139 | 0.005 | 0.000 | 99.9% |
| 7 | candidate_01.svg | 8.467 | 104.214 | 0.000 | 92.0% |
| 8 | candidate_02.svg | 8.467 | 104.214 | 0.000 | 92.0% |
| 9 | candidate_03.svg | 8.467 | 104.214 | 0.000 | 92.0% |
| 10 | candidate_07.svg | 8.467 | 104.214 | 0.000 | 92.0% |
| 11 | candidate_08.svg | 8.467 | 104.214 | 0.000 | 92.0% |
| 12 | candidate_09.svg | 8.467 | 104.214 | 0.000 | 92.0% |

## Per-candidate findings

### candidate_04.svg
- Layer A: pass, no findings
- Layer B: pass, no findings

### candidate_05.svg
- Layer A: pass, no findings
- Layer B: pass, no findings

### candidate_06.svg
- Layer A: pass, no findings
- Layer B: pass, no findings

### candidate_10.svg
- Layer A: pass, no findings
- Layer B: pass, no findings

### candidate_11.svg
- Layer A: pass, no findings
- Layer B: pass, no findings

### candidate_12.svg
- Layer A: pass, no findings
- Layer B: pass, no findings

### candidate_01.svg
- Layer A: pass, no findings
- Layer B: pass (0 hard, 2 advisory)
  - [advisory] INK_COVERAGE: Total area coverage reaches 300% (limit 300%), and 3.66% of the sheet is over the limit. Ink that heavy will not dry and will set off onto the sheets beneath it. This is a relative warning: the source is not CMYK, so Ghostscript invented the separation. Convert with your printer's profile and re-run on the PDF for an exact reading.
  - [advisory] FIDELITY_ARTWORK_LOST: artwork MAE 104.2 exceeds 8.0 -- the trace does not reproduce the design; check colormode (binary/bw discards colour)

### candidate_02.svg
- Layer A: pass, no findings
- Layer B: pass (0 hard, 2 advisory)
  - [advisory] INK_COVERAGE: Total area coverage reaches 300% (limit 300%), and 3.66% of the sheet is over the limit. Ink that heavy will not dry and will set off onto the sheets beneath it. This is a relative warning: the source is not CMYK, so Ghostscript invented the separation. Convert with your printer's profile and re-run on the PDF for an exact reading.
  - [advisory] FIDELITY_ARTWORK_LOST: artwork MAE 104.2 exceeds 8.0 -- the trace does not reproduce the design; check colormode (binary/bw discards colour)

### candidate_03.svg
- Layer A: pass, no findings
- Layer B: pass (0 hard, 2 advisory)
  - [advisory] INK_COVERAGE: Total area coverage reaches 300% (limit 300%), and 3.66% of the sheet is over the limit. Ink that heavy will not dry and will set off onto the sheets beneath it. This is a relative warning: the source is not CMYK, so Ghostscript invented the separation. Convert with your printer's profile and re-run on the PDF for an exact reading.
  - [advisory] FIDELITY_ARTWORK_LOST: artwork MAE 104.2 exceeds 8.0 -- the trace does not reproduce the design; check colormode (binary/bw discards colour)

### candidate_07.svg
- Layer A: pass, no findings
- Layer B: pass (0 hard, 2 advisory)
  - [advisory] INK_COVERAGE: Total area coverage reaches 300% (limit 300%), and 3.66% of the sheet is over the limit. Ink that heavy will not dry and will set off onto the sheets beneath it. This is a relative warning: the source is not CMYK, so Ghostscript invented the separation. Convert with your printer's profile and re-run on the PDF for an exact reading.
  - [advisory] FIDELITY_ARTWORK_LOST: artwork MAE 104.2 exceeds 8.0 -- the trace does not reproduce the design; check colormode (binary/bw discards colour)

### candidate_08.svg
- Layer A: pass, no findings
- Layer B: pass (0 hard, 2 advisory)
  - [advisory] INK_COVERAGE: Total area coverage reaches 300% (limit 300%), and 3.66% of the sheet is over the limit. Ink that heavy will not dry and will set off onto the sheets beneath it. This is a relative warning: the source is not CMYK, so Ghostscript invented the separation. Convert with your printer's profile and re-run on the PDF for an exact reading.
  - [advisory] FIDELITY_ARTWORK_LOST: artwork MAE 104.2 exceeds 8.0 -- the trace does not reproduce the design; check colormode (binary/bw discards colour)

### candidate_09.svg
- Layer A: pass, no findings
- Layer B: pass (0 hard, 2 advisory)
  - [advisory] INK_COVERAGE: Total area coverage reaches 300% (limit 300%), and 3.66% of the sheet is over the limit. Ink that heavy will not dry and will set off onto the sheets beneath it. This is a relative warning: the source is not CMYK, so Ghostscript invented the separation. Convert with your printer's profile and re-run on the PDF for an exact reading.
  - [advisory] FIDELITY_ARTWORK_LOST: artwork MAE 104.2 exceeds 8.0 -- the trace does not reproduce the design; check colormode (binary/bw discards colour)


## Common to all candidates

(none)
