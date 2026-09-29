# Pipeline schemas — the two machine-readable artefacts

Two JSON files carry a trace run's results. Both are written by the trace stage,
both are read by the operator front end and by anything that wants to reason
about a run later, and neither was documented anywhere before this file:

| artefact | written by | holds |
|---|---|---|
| `04_validated/<stem>.comparison.json` | `scripts/compare_candidates.py` | every graded candidate: its sweep parameters, Layer A and Layer B results, and the pixel-fidelity measurement |
| `06_run/<stem>.run.json` | `scripts/run_record.py` | the same candidates stitched into one provenance record per run, plus the back-half artefacts and the human decision |

Keys below were read from the code and checked against the tracked example
`04_validated/00-example.comparison.json` — the example is a real 12-candidate
run and confirms the per-candidate shape, but it predates the sweep-ownership
fix, so it carries no `graded_set_source`, `stale_candidates` or
`stale_candidate_count`; those three are read from
`scripts/compare_candidates.py`. Where a value has a documented meaning — a
1-based index, a sort order, a verdict that is deliberately not a winner-pick —
this file says so, because the difference between "reported" and "decided" is the
whole point of the pipeline.

The human-readable halves (`<stem>.comparison.md`, the run record's consumers)
are not schemas and are not described here.

---

## 1. `comparison.json`

Written to `--out-dir` (default `04_validated/`) as `<stem>.comparison.json`,
where `<stem>` is the traced directory's name (`02_traced/00-example` →
`00-example.comparison.json`). `generated` is UTC ISO-8601 to the second.

```json
{
  "tool": "compare_candidates.py",
  "generated": "2026-09-26T11:59:28+00:00",
  "traced_dir": "02_traced/00-example",
  "spec": "spec.json",
  "candidate_count": 12,
  "graded_set_source": "sweep.json",
  "stale_candidates": [],
  "stale_candidate_count": 0,
  "layers_available": {"a": true, "b": true},
  "common_findings": [],
  "sort": "fewest hard gates failed, then whether the artwork survived …",
  "fidelity_verdicts": ["artwork_lost", "faithful"],
  "candidates": [ { … one record per graded candidate … } ]
}
```

### Top level

| key | type | meaning |
|---|---|---|
| `tool` | string | always `compare_candidates.py`. Version-stamp every tool call so a result can be interpreted later. |
| `generated` | string | UTC ISO-8601 (`timespec="seconds"`). |
| `traced_dir` | string | the traced directory **as passed** on the command line. |
| `spec` | string | the spec path **as passed** — relative or absolute, whichever the caller used. Do not assume one. |
| `candidate_count` | int | the number of **graded** candidates (`len(candidates)`). Excludes the stale bucket. |
| `graded_set_source` | string | `"sweep.json"` when the graded set came from the run's own record, `"glob"` when it fell back to listing `candidate_\d+\.svg` because `sweep.json` was absent, unreadable, or carried no `candidates` list. A reader needs this to know whether the set is the sweep's own output. |
| `stale_candidates` | list[string] | on-disk `candidate_NN.svg` names the record does **not** list. They were never rendered, measured or gated — this bucket is a statement of what was *not* looked at. |
| `stale_candidate_count` | int | `len(stale_candidates)`. |
| `layers_available` | object | `{"a": bool, "b": bool}` — whether each layer module imported. A missing layer gives candidates `"status": "not_run"` and a `reason`, not a crash. |
| `common_findings` | list[string] | rule tags present on **every** graded candidate (usually the uniform ones inherited from the tracer, e.g. a dimension mismatch). Sorted; `[]` when nothing is common. |
| `sort` | string | the ordering applied to `candidates`, as a sentence. Describes the sort in force; it is not a machine field, so parse the order of `candidates` itself. |
| `fidelity_verdicts` | list[string] | the distinct `fidelity_verdict` values present, sorted. |
| `candidates` | list[object] | the graded candidates, in the reported order — **already sorted** by the rule in `sort`. |

**The sort order, explicitly**, ascending on each key in turn: `hard` (gates
failed) → the `fidelity_verdict` rank, `faithful` (0) < `drift` (1) <
`colour_dropped` (2) < `artwork_lost` (3) → `advisory` → `fidelity.mae_art`.
The verdict term exists because gates alone rank a trace that discarded the
artwork level with one that reproduced it; the ranks count *down* because the
sort is ascending and the best candidate has to come first. Ranking
`artwork_lost` as 0 put the candidates that threw the design away at the top of
the table.

**Zero candidates is a normal outcome** (exit 0, not an error). That payload is
smaller than the one above — `tool`, `generated`, `traced_dir`, `spec`,
`candidate_count: 0`, `graded_set_source`, `stale_candidates`,
`stale_candidate_count`, `note` (a sentence saying why: `"no candidates found in
…"`, or the sweep record listed none and N stale files were left alone),
`layers_available`, `candidates: []`. It has **no** `common_findings`, `sort` or
`fidelity_verdicts`, so read those keys defensively.

### One candidate record

```json
{
  "file": "candidate_04.svg",
  "file_size": 16837,
  "layer_a": {"status": "pass", "hard": 0, "advisory": 0, "findings": []},
  "layer_b": {"status": "pass", "hard": 0, "advisory": 0, "findings": []},
  "declared_colors": 5,
  "rendered_ink_colors": 5,
  "node_count_total": 189,
  "node_count_max": 96,
  "hard": 0,
  "advisory": 0,
  "passed": true,
  "sweep": { "file": "candidate_04.svg", "index": 4, "preset": "poster", "…": "…" },
  "fidelity": {"measured": true, "mae": 0.139, "mae_art": 0.005, "p95": 0.0,
               "within10": 0.9985, "source": "01_prepped/00-example.prepped.png"},
  "fidelity_verdict": "faithful",
  "fidelity_reason": "artwork MAE 0.005"
}
```

| key | type | meaning |
|---|---|---|
| `file` | string | the candidate SVG's filename. The candidate's `id` in the run record is this without the `.svg`. |
| `file_size` | int or null | bytes on disk. `null` when the file named by `sweep.json` is not present; the layers then report an error, which is the honest reading of "the record says this was written and it is not there". |
| `layer_a` | object | Layer A (source validation) result — see below. |
| `layer_b` | object | Layer B (render preflight) result — see below. |
| `declared_colors` | int or null | `len(stats["declared_colors"])` from Layer A: colours **declared**, which includes the implicit black fill of a shape whose cascade never sets `fill` and `currentColor` resolved from the inherited `color`. |
| `rendered_ink_colors` | int or null | inks counted in the render (Layer B). `null` when no render happened — the reason is in `layer_b.reason`. |
| `node_count_total`, `node_count_max` | int or null | `path_nodes_total` / `path_nodes_max` from Layer A's stats. `node_count_max` is what the per-path node limit is measured against; the total is context. |
| `hard`, `advisory` | int | **sums over both layers**. `hard > 0` is the file's disqualification; `advisory` is reported and does not fail it. |
| `passed` | bool | true iff both layers' `status` is `"pass"`. **This is not the fidelity verdict**: a candidate that discarded the artwork can be `passed: true` and is a `fidelity_verdict` of `artwork_lost`. Read both. |
| `sweep` | object | the `sweep.json` entry that produced this file (preset, `filter_speckle`, `hierarchical`, `use_palette`, `variant`, `params`, `source_sha256`, `output_sha256`, `palette_quantized_sha256`, `variant_input`/`variant_sha256`) — copied verbatim, so the record of what produced a candidate travels with it. **`{}`** under the legacy glob fallback: with no record there are no parameters to attach. |
| `fidelity` | object | the pixel-fidelity measurement — see below. |
| `fidelity_verdict` | string | `faithful` \| `drift` \| `colour_dropped` \| `artwork_lost`. A **verdict about whether the artwork survived the trace**, never a winner-pick: the pipeline does not choose. `artwork_lost` is raised when `mae_art > 8.0` (the measured gap between a faithful trace and a bisected one is ~20 000×, so this is a cliff, not a curve); `colour_dropped` when `rendered_ink_colors < declared_colors * 0.5`; `drift` when `mae_art > 1.0`; `faithful` otherwise. |
| `fidelity_reason` | string | the sentence behind the verdict, carrying the number. |
| `error` | string | **only on failure** — the exception raised while running the layers, formatted `"Type: message"`, when Layer B was the layer running. In that case *both* layers report `status: "error"`; when only Layer A is importable the failure is recorded on `layer_a` alone and `layer_b` stays `not_run`. |

When `fidelity_verdict` is `artwork_lost`, the record also gains one advisory: 1
is added to `advisory` and to `layer_b.advisory`, and a
`{"rule": "FIDELITY_ARTWORK_LOST", "severity": "advisory", "detail": …}` finding
is appended to `layer_b.findings`. It is advisory by design — a deliberately
single-colour job is a legitimate ask, so it raises the count and names the cause
rather than overruling the operator.

### `layer_a` / `layer_b`

```json
{"status": "pass", "hard": 0, "advisory": 0,
 "findings": [{"rule": "OPEN_PATH", "severity": "hard", "detail": "Path id='outline' …"}]}
```

| key | meaning |
|---|---|
| `status` | `"pass"`, `"fail"`, `"error"`, or `"not_run"`. |
| `hard`, `advisory` | counts of classified findings in this layer. |
| `findings` | every classified finding for the layer, hard ones then advisories, each `{rule, severity, detail}` with `severity` ∈ `hard`\|`advisory`. The `FIDELITY_ARTWORK_LOST` entry described above additionally carries `layer`. Repeated findings are capped by the validator (the first ~10 in full, then a summary naming the distinct values), so the count in `hard`/`advisory` can be smaller than the underlying number of offenders. |
| `reason` | **only when `status` is `"not_run"`** — why: `"render tools unavailable"`, `"render produced no ink reading"`, `"preflight.py unavailable"`, or `"validate_svg.py unavailable"`. A layer that did not run reports this instead of silently passing. |

Layer B "ran" only if it produced a render. When the render tools are absent,
`preflight` returns before rasterising, `rendered_ink_colors` is `null`, and the
status is `not_run` with a reason — the absent layer is never read as a pass.

**Which bucket a finding lands in is preflight's attribution, not the rule's
name.** `preflight.LAYER_A_RULES` names the Layer A rule tags; a Layer A rule
that is *not* listed there is attributed to `render_preflight`, so a source-level
failure would appear under `layer_b` with the right severity and the wrong layer.
That is why a new source-level rule has to be added to that set when it lands —
`GRADIENT_NOT_ALLOWED` is the worked example.

### `fidelity`

```json
{"measured": true, "mae": 0.139, "mae_art": 0.005, "p95": 0.0,
 "within10": 0.9985, "source": "01_prepped/00-example.prepped.png"}
```

| key | meaning |
|---|---|
| `measured` | bool. `true` only when a real diff was computed. When `false` the object carries `note` instead and **no numbers** — `"inkscape not installed"`, `"render produced no PNG"`, `"render size … != source …"`, `"no source raster"`, or the exception. A missing measurement is reported, never substituted. |
| `mae` | mean absolute error over **all** pixels. On dark-art-on-dark-background images this is dominated by the background and is misleadingly low. |
| `mae_art` | MAE over **artwork** pixels only — the source pixels deviating >20 from the most common colour. This is the number that matters for raster→vector accuracy, and the one the fidelity sort uses. `null` when the source has no artwork pixels at all (`measured` stays true). |
| `p95` | 95th percentile of the per-pixel difference. |
| `within10` | fraction of pixels whose **worst** channel differs by < 10, as 0–1 (the markdown table prints it ×100). |
| `source` | the raster the candidate was diffed against: `--source` if given, else `sweep.json`'s `input.file` (the prepped PNG). A pitch-shift/inverted variant is diffed against **its own** variant raster when `sweep.json` names one (`variant_input`), because diffing it against the prepped source would report a misleadingly large MAE. |
| `note` | present only when `measured` is `false`. |

The fidelity metric renders the candidate back to a raster at the **source's own
dimensions** with Inkscape and diffs it against the source. It is the "accuracy"
view, and it never picks a winner either.

---

## 2. `run.json` (the run record)

Written to `--out-dir` (default `06_run/`) as `<stem>.run.json`; with `--all`
also an index, `06_run/runs.index.json`
(`{schema, generated, tool, runs: [stem, …]}`). The stem is the traced directory
name. Every `path` in this record is **absolute**, resolved from the project root
at the time the record was built (the shapes below show them relative for
readability). This is the archive spine: it joins the per-stage provenance
(`prep.json`'s source hash, `sweep.json`'s per-candidate parameters, the
comparison's Layer A/B and fidelity) into one reconstructable record per run,
links the back-half artefacts for the candidates the human ran through the full
pipeline, and flags every missing link. It reads; it never renders and never
picks.

```json
{
  "schema": "chopshop-run-record-0.1",
  "generated": "2026-09-26T12:00:00+00:00",
  "tool": "run_record.py",
  "stem": "00-example",
  "run": {
    "source": {"path": "…", "sha256": "…", "size_px": [1024, 1024], "found": true},
    "prep": {"path": "01_prepped/00-example.prep.json", "found": true,
             "acceptable": true, "checks": [ … ], "versions": { … },
             "input": {"file": "…", "sha256": "…", "size_px": [1024, 1024]}},
    "sweep": {"path": "02_traced/00-example/sweep.json", "found": true,
              "backend": { … }, "deterministic": true, "truncated": { … },
              "sweep": { … }, "candidates": [ … ]},
    "comparison": {"path": "04_validated/00-example.comparison.json", "found": true,
                   "candidate_count": 12, "layers_available": {"a": true, "b": true},
                   "candidates": [ … ]},
    "duplicate_groups": {"<svg sha256>": ["candidate_02", "candidate_07"]},
    "missing_links": []
  },
  "candidates": [ { … one record per candidate … } ]
}
```

### The `run` block

| key | meaning |
|---|---|
| `source` | `{path, sha256, size_px, found}`. Prefers `prep.json`'s own `input` record; otherwise globs `00_source/` for a matching stem. `path: null` + `found: false` when there is nothing to find. |
| `prep` | `{path, found}` plus `acceptable`, `checks`, `versions`, `input` copied out of `prep.json` when it exists. Just `{path, found: false}` when it does not. |
| `sweep` | `{path, found}` plus `backend`, `deterministic`, `truncated`, `sweep` (the axes in force) and `candidates` (the whole recorded list, verbatim). |
| `comparison` | `{path, found}` plus `candidate_count`, `layers_available` and `candidates` (verbatim). |
| `duplicate_groups` | `{svg sha256 → [candidate id, …]}` for groups of **exactly identical** SVGs (2+ members only). A sha shared by several candidates means the sweep produced the same bytes twice — for example two axes that did not change the output — and it is recorded, not deduplicated. |
| `missing_links` | run-level blocking gaps: `"source raster not found"`, `"prep.json missing"`, `"sweep.json missing"`, `"comparison.json missing"`. Empty means the run's own stages line up. |

### One candidate record

```json
{
  "id": "candidate_04",
  "file": "candidate_04.svg",
  "svg_sha256": "c28cd3c8…",
  "file_size": 16837,
  "sweep": {"preset": "poster", "filter_speckle": 2, "hierarchical": "cutout",
            "use_palette": false, "params": { … }, "source_sha256": "05cd812c…"},
  "comparison": {"passed": true, "hard": 0, "advisory": 0, "node_count_max": 96,
                 "node_count_total": 189, "rendered_ink_colors": 5,
                 "declared_colors": 5, "layer_a": { … }, "layer_b": { … },
                 "fidelity": { … }},
  "back_half": { … }, "visual_review": { … }, "human_decision": { … },
  "anomalies": [],
  "duplicate_of": null,
  "missing_links": [],
  "open_items": ["no visual review", "no human decision"]
}
```

| key | meaning |
|---|---|
| `id` | the candidate file's name without `.svg`. This is the key the human decision and the back-half artefacts are filed under. |
| `file`, `file_size` | as in the comparison; `file_size` falls back to the file on disk when the comparison had no record. |
| `svg_sha256` | from `sweep.json`'s `output_sha256` when present, else hashed from disk. This is the identity the duplicate groups and the learning event log key on. `null` when the file is gone — which adds `"svg hash unknown"` to `missing_links`. |
| `sweep` | the parameters that produced the candidate, lifted out of the sweep entry. |
| `comparison` | the candidate's comparison figures, or **`null`** when it was traced but never compared. `null` is a blocking `missing_links` entry — "traced but not compared (Layer A/B + fidelity)" — not an absent optional. |
| `back_half` | the full-pipeline artefacts — see below. |
| `visual_review` | `{path, found, path_scope}` for `05_final/<artwork>/<id>.visual_review.md`, falling back to the flat `05_final/<id>.visual_review.md` (marked `path_scope: "flat-shared"` when it does). |
| `human_decision` | `{found, path, path_scope, selected, label, reason}` from, in order of preference: `05_final/<artwork>/<id>.pick.json` (the artwork's own pick) → the shared flat `05_final/<id>.pick.json` (read, and marked `flat-shared`) → the run-level `04_validated/<stem>.decision.json`, which maps id → pick and is already artwork-keyed. `label` is the operator's own vocabulary (`production`, `style_reference`, `needs_retrace`, `discard`); `selected` is the pick itself. `found: false` + `path: null` + `path_scope: "absent"` when none exists. |
| `anomalies` | non-blocking provenance warnings: one entry per artefact that had to be read from a **shared flat** name, naming the path, because such a file is written by *every* artwork whose trace produced that candidate stem and reading it therefore proves nothing about this run. Kept separate from `missing_links`: a legacy flat artefact is readable data, not a failed stage. |
| `duplicate_of` | the id of the first candidate in the record with the same `svg_sha256`, or `null`. Additive information only — nothing is dropped for being a duplicate. |
| `missing_links` | **blocking** gaps: `"svg hash unknown"`, `"not compared (Layer A/B + fidelity)"`, and — when the back half ran — `"back half ran but proof PNG/print PDF/Layer A log missing"`. |
| `open_items` | the *normal* not-yet-done state of a candidate the human has not reached: `"back half not run (candidate not chosen)"`, `"no visual review"`, `"no human decision"`, `"sha duplicate of <id>"`. Counted, not treated as failures — a closeout gate counts `missing_links`, not these. |

### `back_half`

```json
{
  "run": true, "cand_id": "candidate_04", "artwork_stem": "00-example",
  "manifest": {"path": "05_final/00-example/candidate_04.manifest.json",
               "found": true, "path_scope": "artwork", "sha256": "…",
               "passed": true, "hard": 0, "advisory": 0,
               "stats": {"tac_exact": true, "rendered_continuous_tone": false,
                         "rendered_ink_colors": 5, "rendered_distinct_colors": 5,
                         "hard_findings": 0, "advisory_findings": 0,
                         "node_count_max": 96, "color_count": 5}},
  "proof_png": {"path": "…", "found": true, "path_scope": "artwork", "sha256": "…"},
  "print_pdf": {"path": "…", "found": true, "path_scope": "artwork", "sha256": "…"},
  "layer_a_txt": {"path": "…", "found": true, "path_scope": "artwork"},
  "layer_b_txt": {"path": "…", "found": true, "path_scope": "artwork"}
}
```

`run` is true when the manifest exists — i.e. the back half was actually run for
this candidate. `cand_id` / `artwork_stem` state what the artefact names were
resolved against, so a path can be checked by hand.

**`path_scope` is the point of this block.** Values, in preference order:

| value | meaning |
|---|---|
| `"artwork"` | the name carries this run's artwork stem, so the artefact is this run's own. **Preferred**, and what the resolver picks whenever it exists. |
| `"flat-shared"` | the flat `05_final/<candidate>.*` name — read only for continuity with pre-fix runs, and always flagged, because `<candidate>` is the *input file's* stem and the same string (`candidate_04`) is shared by every artwork that ever traced one. A run record reading it could be reading a different artwork's manifest, proof and human pick. The fallback is also recorded in the candidate's `anomalies`. |
| `"absent"` | no file at either name. `path` then still points at the name the pipeline *should* have written (the artwork-scoped one) so a missing-file message is actionable — except in `human_decision`, which reports `path: null`. |

Artefact resolution for `manifest`/`proof_png`/`print_pdf`/`layer_b_txt`
prefers `05_final/<artwork_stem>/<cand_id>.…`; `layer_a_txt` prefers the
artwork-stem-prefixed **sibling** in `04_validated/`,
`<artwork_stem>.<cand_id>.layer_a.txt` (not a subdirectory — the name has to stay
artwork-stem-prefixed for the orphan classifier), falling back to
`04_validated/<cand_id>.layer_a.txt`.

### `manifest.stats`

Lifted from the print-check manifest, so a run record can be read without opening
it: `tac_exact` (was the ink-coverage figure measured from a genuinely CMYK
file), `rendered_continuous_tone`, `rendered_ink_colors`,
`rendered_distinct_colors`, `hard_findings`, `advisory_findings`,
`node_count_max` (from `static.path_nodes_max`) and `color_count` (from
`static.color_count`). A key that is absent in the manifest appears as `null`
here rather than being dropped.

---

## 3. Where the rest of the machine-readable state lives

- **`02_traced/<stem>/sweep.json`** — the sweep's own record and the authority
  for what a run produced: `backend`, `input{file,sha256}`,
  `sweep` (the axes in force), `skipped_axes`, `deterministic` +
  `determinism_note`, `truncated{selected,total_available,max_candidates}`,
  `stale_removed` (candidates this sweep deleted because it did not write them),
  and `candidates[]`. **`compare_candidates.py` grades exactly that `candidates`
  list** — the directory listing is not the graded set.
- **`06_run/_learning/events.jsonl`** — the candidate-triage event log, one JSON
  object per line, field order `t, job, svg_sha, event, pos, ranker`. `pos` is
  the **1-based** rank in the order the candidates were presented (first
  candidate = 1), matching the comparison table's own display loop and the
  operator UI's position lookup, so the exposure signal and the pick signal are
  on one scale.
- The print-check **manifest** (`05_final/<candidate>.manifest.json`) has its own
  shape, owned by `preflight.py`, and is not described here.
