# passed is not faithful

**`passed` means no gate fired. It does not mean the design survived.**

These are different properties, and the pipeline has a case where they come
apart cleanly: a binary/bw trace of a colour image. It reproduces nothing, and
it passes.

## what it looks like

Measured on the shipped example — a 5-colour design, twelve candidates from the
same sweep (six colour-preserving, six bw/binary):

| property | faithful six | bw/binary six |
|---|---|---|
| `hard` gates failed | 0 | **0** |
| `advisory` | 0 | **0** |
| `layer_a/b` status | pass | **pass** |
| `mae_art` (artwork MAE) | 0.005 | **104.214** |
| declared colours | 5 | **1** |

Every gate column is identical. The two groups tie on `(hard, advisory)`, so a
table sorted on gates alone ranks a candidate that threw the artwork away level
with one that reproduced it — and nothing in the gate columns distinguishes
them. The gap is only visible in `mae_art`: a factor of roughly 20,000.

The same shape appears whenever the source is quantised before diffing: a
candidate can be *pixel-perfect* against a source that was itself reduced to one
ink, and still have discarded the design.

## the two independent checks

Both live in `_fidelity_verdict()` in `scripts/compare_candidates.py`, and both
are needed because they fail in different directions:

1. **Pixel deviation — how far the artwork drifted from its source.**
   `mae_art` is the mean absolute error over the *artwork* pixels only (`mae` is
   over all pixels, and is dominated by large flat areas, so it hides artwork
   damage). Compared against `MAE_ART_FAIL = 8.0`.
2. **Colour retention — how much of the declared palette came back.**
   `rendered_ink_colors` against `declared_colors`, floor
   `MIN_COLOUR_RETENTION = 0.5`. This catches the case check 1 cannot: a
   zero-error candidate that still dropped inks.

Check 1 alone misses a colour-dropped candidate diffed against a quantised
source; check 2 alone misses a candidate that dropped *structure* while keeping
the palette. Neither is a gate — see below.

The threshold is deliberately a cliff, not a curve. The measured populations sit
at 0.005 and 104.214, so anything in between is not a measurement gap, it is a
different job. `rendered_ink_colors` counts what the render produced *including
the background*, so only a large shortfall is meaningful — hence the 0.5 floor
rather than an exact match.

## the verdict ladder

| verdict | condition | rank |
|---|---|---|
| `faithful` | `mae_art` ≤ 1.0 | 0 |
| `drift` | `mae_art` > 1.0 | 1 |
| `colour_dropped` | rendered < declared × 0.5 | 2 |
| `artwork_lost` | `mae_art` > 8.0 | 3 |

`mae_art` unmeasured (no source resolved) returns **neither** pass nor failure:
it must not read as `artwork_lost`. An absent measurement is not evidence the
artwork survived, and it is not evidence it was lost.

The checks are evaluated in order, so a candidate that both lost the artwork and
dropped colours reports `artwork_lost`; the ladder is a precedence order, not a
set of independent symptoms.

## advisory, never a gate

`artwork_lost` raises the advisory count and appends a
`FIDELITY_ARTWORK_LOST` finding. It never touches `hard`.

That is deliberate: a deliberately single-colour job is a legitimate ask, and a
hard failure here would reject it. What the verdict must not do is be
*invisible*. So the fix is a named finding and an advisory — the operator keeps
the call. Name the **cause** in the message, not just the number: the reason
string says `check colormode (binary/bw discards colour)`, because "MAE 104.2"
alone leaves the reader to guess what to change.

## the sort — and the inversion trap

Attach the verdict **before** sorting, or the candidate that discarded the
artwork ranks level with the ones that did not. The sort key is:

```python
records.sort(key=lambda c: (c["hard"],
                            _FID_RANK.get(c.get("fidelity_verdict"), 2),
                            c["advisory"],
                            (c.get("fidelity") or {}).get("mae_art") or 0.0))
```

i.e. **fewest hard gates → did the artwork survive → fewest advisories → lowest
artwork MAE**. An unknown verdict defaults to rank 2 (treated as dropped, not
trusted).

The ranks count **down** because the sort is ascending and the best candidate
must come first. The first version of this ranked `artwork_lost` as 0, which put
the six candidates that discarded the design at the *top* of the table — the
exact opposite of the fix. `test_faithful_candidates_sort_above_artwork_lost`
locks it.

## prose describing the sort must describe the sort

The invisible-dimension failure is usually in the *description*, not the code: a
footer or docstring that says "sorted on gates and nothing more" keeps the
missing dimension missing even after the sort is fixed, and reads as
authoritative. When you change the ordering, grep for prose that describes it —
module docstrings, the table footer, the JSON summary, any README — and check
the words still match the key. In this repo the table footer and the module
docstring both describe the sort; they have disagreed before.

## checklist when touching fidelity or ordering

- Did the verdict get attached before the sort, not after?
- Does an unmeasured fidelity still avoid both `artwork_lost` and pass?
- Is `artwork_lost` still advisory — `hard` untouched, advisory +1, finding added?
- Does the reason string name the cause, not only the number?
- Do all prose descriptions of the ordering still match the key?
- Do the gates-only candidates still rank below the faithful ones?

## tests that lock this

`tests/test_compare_candidates.py`:

| test | locks |
|---|---|
| `test_bw_candidate_is_flagged_as_artwork_lost` | verdict + the word `colormode` in the reason |
| `test_colour_candidate_is_faithful` | the real 0.005 reading |
| `test_small_drift_is_distinguished_from_artwork_loss` | `drift` is its own verdict |
| `test_dropped_colours_are_caught_even_at_zero_error` | check 2 fires where check 1 cannot |
| `test_unmeasured_fidelity_is_not_judged_artwork_lost` | absent measurement is not a verdict |
| `test_artwork_lost_is_advisory_not_a_hard_gate` | `hard == 0`, `advisory == 1`, finding raised |
| `test_faithful_candidates_sort_above_artwork_lost` | the rank table and the tie it breaks |
