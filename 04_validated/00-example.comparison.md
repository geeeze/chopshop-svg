# Candidate comparison: 00-example

- spec: `/home/monop/Documents/GitHub/chopshop-svg/spec.json`
- candidates: 12
- layer A (source validation): available
- layer B (render preflight): available
- sorted by: fewest hard gates failed, then fewest advisories

| # | file | sweep | LayerA | LayerB | declared | inks | nodes (total/max) | size | MAE all/art | hard | adv |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | candidate_04.svg | variant=source preset=poster speckle=2 hier=cutout palette=False | PASS | PASS | 5 | 5 | 189/96 | 16837 | 0.14 / 0.01 | 0 | 0 |
| 2 | candidate_05.svg | variant=source preset=poster speckle=8 hier=cutout palette=False | PASS | PASS | 5 | 5 | 189/96 | 16837 | 0.14 / 0.01 | 0 | 0 |
| 3 | candidate_06.svg | variant=source preset=poster speckle=16 hier=cutout palette=False | PASS | PASS | 5 | 5 | 189/96 | 16837 | 0.14 / 0.01 | 0 | 0 |
| 4 | candidate_10.svg | variant=source preset=poster speckle=2 hier=cutout palette=True | PASS | PASS | 5 | 5 | 189/96 | 16837 | 0.14 / 0.01 | 0 | 0 |
| 5 | candidate_11.svg | variant=source preset=poster speckle=8 hier=cutout palette=True | PASS | PASS | 5 | 5 | 189/96 | 16837 | 0.14 / 0.01 | 0 | 0 |
| 6 | candidate_12.svg | variant=source preset=poster speckle=16 hier=cutout palette=True | PASS | PASS | 5 | 5 | 189/96 | 16837 | 0.14 / 0.01 | 0 | 0 |
| 7 | candidate_01.svg | variant=source preset=bw speckle=2 hier=cutout palette=False | PASS | PASS | 1 | 2 | 44/23 | 4273 | 8.47 / 104.21 | 0 | 1 |
| 8 | candidate_02.svg | variant=source preset=bw speckle=8 hier=cutout palette=False | PASS | PASS | 1 | 2 | 44/23 | 4273 | 8.47 / 104.21 | 0 | 1 |
| 9 | candidate_03.svg | variant=source preset=bw speckle=16 hier=cutout palette=False | PASS | PASS | 1 | 2 | 44/23 | 4273 | 8.47 / 104.21 | 0 | 1 |
| 10 | candidate_07.svg | variant=source preset=bw speckle=2 hier=cutout palette=True | PASS | PASS | 1 | 2 | 44/23 | 4273 | 8.47 / 104.21 | 0 | 1 |
| 11 | candidate_08.svg | variant=source preset=bw speckle=8 hier=cutout palette=True | PASS | PASS | 1 | 2 | 44/23 | 4273 | 8.47 / 104.21 | 0 | 1 |
| 12 | candidate_09.svg | variant=source preset=bw speckle=16 hier=cutout palette=True | PASS | PASS | 1 | 2 | 44/23 | 4273 | 8.47 / 104.21 | 0 | 1 |

## By pixel fidelity (accuracy view)

Rendered back to the source resolution and diffed against the source raster. Lower MAE = closer to the original. `MAE art` ignores the background and measures the subject only. This is the accuracy view; it does NOT pick a winner.

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
- Layer B: pass (0 hard, 1 advisory)
  - [advisory] INK_COVERAGE: Total area coverage reaches 300% (limit 300%), and 3.66% of the sheet is over the limit. Ink that heavy will not dry and will set off onto the sheets beneath it. This is a relative warning: the source is not CMYK, so Ghostscript invented the separation. Convert with your printer's profile and re-run on the PDF for an exact reading.

### candidate_02.svg
- Layer A: pass, no findings
- Layer B: pass (0 hard, 1 advisory)
  - [advisory] INK_COVERAGE: Total area coverage reaches 300% (limit 300%), and 3.66% of the sheet is over the limit. Ink that heavy will not dry and will set off onto the sheets beneath it. This is a relative warning: the source is not CMYK, so Ghostscript invented the separation. Convert with your printer's profile and re-run on the PDF for an exact reading.

### candidate_03.svg
- Layer A: pass, no findings
- Layer B: pass (0 hard, 1 advisory)
  - [advisory] INK_COVERAGE: Total area coverage reaches 300% (limit 300%), and 3.66% of the sheet is over the limit. Ink that heavy will not dry and will set off onto the sheets beneath it. This is a relative warning: the source is not CMYK, so Ghostscript invented the separation. Convert with your printer's profile and re-run on the PDF for an exact reading.

### candidate_07.svg
- Layer A: pass, no findings
- Layer B: pass (0 hard, 1 advisory)
  - [advisory] INK_COVERAGE: Total area coverage reaches 300% (limit 300%), and 3.66% of the sheet is over the limit. Ink that heavy will not dry and will set off onto the sheets beneath it. This is a relative warning: the source is not CMYK, so Ghostscript invented the separation. Convert with your printer's profile and re-run on the PDF for an exact reading.

### candidate_08.svg
- Layer A: pass, no findings
- Layer B: pass (0 hard, 1 advisory)
  - [advisory] INK_COVERAGE: Total area coverage reaches 300% (limit 300%), and 3.66% of the sheet is over the limit. Ink that heavy will not dry and will set off onto the sheets beneath it. This is a relative warning: the source is not CMYK, so Ghostscript invented the separation. Convert with your printer's profile and re-run on the PDF for an exact reading.

### candidate_09.svg
- Layer A: pass, no findings
- Layer B: pass (0 hard, 1 advisory)
  - [advisory] INK_COVERAGE: Total area coverage reaches 300% (limit 300%), and 3.66% of the sheet is over the limit. Ink that heavy will not dry and will set off onto the sheets beneath it. This is a relative warning: the source is not CMYK, so Ghostscript invented the separation. Convert with your printer's profile and re-run on the PDF for an exact reading.


## Common to all candidates

(none)
