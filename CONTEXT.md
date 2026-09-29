# CONTEXT.md — chopshop-svg

Working state for agents and humans: what is in flight, what is blocked, and
the caveats that are not obvious from the code. Update this as work lands.

**Repo visibility: PUBLIC.** Nothing host-specific, no private-stack detail,
no hostnames/IPs/VPN names, no SwarmUI/ComfyUI references. The `sync-skills`
script enforces this for `skills/`.

## What this repo is

A T-shirt print pipeline in two stages, kept separate and never picking a
winner: the **trace stage** (`front_pipeline.sh`: `prep_raster.py` →
`trace_sweep.py` → `compare_candidates.py`) turns a raster into traced SVG
candidates plus a comparison report; the **print check** (`pipeline.sh`:
`validate_svg.py` + `preflight.py` + `snap_colors.py`) validates one chosen
SVG and emits a proof PNG, print PDF and JSON manifest. The only human decision
is which candidate to print.

## Work in progress

- Recent, landed (see `git log`): the chopshop-im filter bench
  (`scripts/im_filters.py`), tune sweep and node-reduction driven from the spec,
  and the orphan sweep for artifacts of deleted jobs.
- **Layer A now enforces the spec it claims to (landed, `validate_svg.py`).**
  Seven reproduced gaps are closed. `geometry.allow_gradients` was parsed and
  never read, so a two-stop gradient passed the "independent gate" with zero
  findings — `--layer-a-only`, or `validation.run_preflight: false`, let
  continuous tone through and only layer B's rendered ink count caught it; it is
  now a hard failure (`GRADIENT_NOT_ALLOWED`, reported for both the declaration
  and each `url(...)` paint site, including one that resolves to nothing).
  Raster detection was `<image>`-only: `<feImage>` with a data/external href,
  `<foreignObject>`, an external `<use>`/`<image>` and `url(data:image/...)` in
  a style all passed as "vector-only" and are now `RASTER_EMBED`. The
  non-painting skip matched camelCase set members (`clipPath`,
  `linearGradient`, `radialGradient`) against a lowercased element name, so
  they could never match and a stroked path inside a `<clipPath>` was reported
  as a hairline; the sets and the lookup now share one case, and the skip covers
  whole subtrees. Unreferenced `<defs>`/`<symbol>`/`<pattern>`/`<mask>`/`<marker>`
  content (and anything under `display:none` / `visibility:hidden`) no longer
  hard-fails: it is reported once as `UNCHECKED_DEFINITION` with a count, while
  `<use>`-instantiated content is still checked. `!important` was stripped, so an
  inline declaration always won; importance is now carried and ranked (inline
  important > stylesheet important > inline normal > stylesheet normal >
  presentation attribute). Implicit black fill and `currentColor` (resolved from
  the inherited `color`, initial black) were not counted at all, so a black-fill
  artwork read as using no ink; they are counted now. `stroke-opacity="0"`,
  `opacity="0"`, `display:none` and `visibility:hidden` no longer trip
  MIN_STROKE_WIDTH, and `filter`/`mask`/`clip-path` references are one
  aggregated `EFFECT_REFERENCE` NOTE.
  - Caveats: the advisory tags (`EFFECT_REFERENCE`, `TRANSLUCENT_PAINT`,
    `UNCHECKED_DEFINITION`) are notes by contract, not entries in `failures` —
    deliberately, because `preflight.classify()` fails safe to HARD for a rule it
    does not know, so an advisory routed through `failures` would silently become
    a hard gate. `preflight.py` is otherwise untouched.
    The one integration gap this change had to close inside the frozen print
    check was attribution, not severity: `preflight.LAYER_A_RULES` did not list
    `GRADIENT_NOT_ALLOWED`, so the unified manifest labelled a SOURCE-level
    failure `layer: render_preflight`. **Freeze lift, one line:** the change
    owner lifted `AGENTS.md`'s freeze on `preflight.py` for that entry alone,
    and `tests/test_preflight.py` now pins that every rule `validate_svg.py` can
    RAISE maps to `LAYER_A` (the advisories excepted, being notes).
    `snap_colors.py` was not touched.
    Layer A still cannot prove the RENDERED result: it reads declarations, so an
    allowed gradient contributes only its declared stops, and a blend, a font
    substitution, a RIP thinning or an out-of-gamut colour remain layer B's job.
    `snap_colors.py`'s own colour maths and `validate_svg.py`'s "Known
    limitations" docstring section are the authoritative statements of what is
    and is not covered.
- **Artefact identity: every published artefact now names its artwork (landed).**
  `pipeline.sh` publishes an artwork-scoped set next to the canonical flat names —
  `05_final/<artwork>/<candidate>.{manifest.json,proof.png,print.pdf,layer_b.txt}`,
  `04_validated/<artwork>.<candidate>.layer_a.txt` — and the run-unique copies are
  now `<artwork>.<candidate>.<RUN_ID>.<ext>`; they previously carried no stem at
  all, so a stamped artefact could not be traced back to anything. The flat
  `05_final/<candidate>.manifest.json` is shared by every artwork whose trace
  produced that candidate file stem, so `scripts/run_record.py` now prefers the
  artwork-scoped name and, when it must fall back to the flat one, records
  `"path_scope": "flat-shared"` on that artefact plus a candidate `anomalies`
  note — a shared file is never recorded as if it were this run's. The preflight
  scratch dir is per (artwork, candidate) (`.preflight-work/<artwork>/<candidate>/`)
  instead of one shared `.preflight-work`, which two runs of the same candidate
  stem used to race on. `front_pipeline.sh`'s `--sweep` now passes its value as
  real argv (a bash array) and rejects a missing value with a usage error instead
  of a `set -u` `$2: unbound variable`.
  **Freeze lift, this change only:** `AGENTS.md:53` freezes `pipeline.sh` along
  with `validate_svg.py`, `preflight.py` and `snap_colors.py`; the change's owner
  lifted it for `pipeline.sh` for this fix. `snap_colors.py` was not touched.
  The artwork stem is the containing directory's name (`runner.py` runs the back
  half from `02_traced/<artwork>/`), falling back to the candidate's own stem for
  a bare `00_source/art.svg`, so a hand-run in an ad-hoc directory takes that
  directory's name — there is no `ARTWORK_STEM` override.
- **The comparison grades the sweep's own record, not the directory (landed).**
  `02_traced/<stem>` is shared and persistent per artwork stem, so a rerun with a
  smaller cap or a different preset set used to leave the previous run's
  higher-numbered `candidate_NN.svg` files on disk and have them graded as if this
  sweep had produced them. `compare_candidates.py` now grades exactly the
  `candidates` list in `sweep.json`; anything else matching `candidate_\d+\.svg`
  is reported as a named NON-graded stale bucket (`stale_candidates`,
  `stale_candidate_count`; a *Not graded (stale)* markdown table) and is never
  rendered, measured or counted, and `graded_set_source` says `sweep.json` or
  `glob`. The directory glob remains as a documented legacy fallback when
  `sweep.json` is absent or unreadable. `trace_sweep.py` additionally prunes
  `candidate_\d+\.svg` files it did not write, only AFTER a successful sweep,
  leaving `sweep.json`, the checksum sidecars, `.variants/` and `.palette/`
  alone, and records `stale_removed`. The record is the authority if the two ever
  disagree. Candidate naming and the per-stem/continued-numbering behaviour are
  unchanged.
- **The im_filters bench is reproducible on any ImageMagick major (landed).**
  The `-define png:exclude-chunk=date,time` the bench relied on is inert on
  ImageMagick 6 (measured: 6.9.12 survives all three chunks), so
  `test_parallel_matches_sequential` failed whenever a second boundary fell
  inside the run. A documented post-pass (`strip_png_time_chunks`) now drops
  exactly the `date:create`/`date:modify`/`date:timestamp` tEXt chunks from every
  non-`identity` output; every other chunk (including profiles) and the pixel
  data are copied through byte for byte. `-strip` is deliberately not used.
  - Caveat: this box runs ImageMagick 7.1.1-43, where the old define already
    worked and the test passed before and after. The fix removes the version
    dependency by construction; the 6.x path was **not** observed on a real 6.x
    build (none available here), only argued from the define's documented
    semantics plus the synthetic-PNG unit test.
- **Proof-variant dataset stage (landed, opt-in, OFF by default).**
  `scripts/proof_variants.py` derives a deterministic family of "similar copies"
  of one chosen proof (mirrored / rotated / colour-shifted) and writes the
  raster **and** the vector of each under `<stem>.dataset/` plus `dataset.json`
  and `dataset.md`; `runner.py`'s `run_back` invokes it after `pipeline.sh` has
  produced the proof, gated on `validation.variants`. Wiring point is the runner
  and NOT `pipeline.sh`, because `AGENTS.md` freezes `pipeline.sh`,
  `validate_svg.py`, `preflight.py` and `snap_colors.py` as the tested print
  check. Two runner routes carry it: the four-segment
  `GET /files/<job>/<stem>.dataset/<name>` (containment-checked on the resolved
  path) and `GET /jobs/<job>/dataset?candidate=<file>`, which 7-Zips one dataset
  on demand with the same 409/507/500 contract as `/jobs/<id>/archive`.
  - Caveats: `rot90`/`rot270` are clockwise quarter turns on both halves and
    exchange the raster's pixel dimensions for a non-square proof (the art is
    never resampled or cropped); `hue-*` rotates through PIL's 8-bit HSV, so a
    90° turn is a 64-step offset and the last bit of each channel is
    approximate; `palette-cycle` permutes the declared `screens` in order.
  - **`palette-cycle` on real art is a requantisation, not a permutation, and it
    says so.** An exact-match cycle only touches pixels/fills that EQUAL a
    palette colour; on traced continuous-tone proofs (measured on two: 3200x3200
    with ~69k rendered colours against a 6-colour palette moved 0.069% of the
    pixel mass and 0 of 339 fill values; 4688x2694 with 87880 colours moved
    0.013% and 0 of 581) that emitted a file that was visually the proof. The
    figures are per-artifact — quote the one you measured. It now MEASURES the exact pass and, below `MIN_VARIANT_CHANGE` (0.5%
    of pixel mass / of paint values), redoes the cycle through PIL's
    nearest-colour search over the palette and records a `REQUANTISATION` note in
    `dataset.json`/`dataset.md`; if even that cannot move the image, the
    transform simply cannot apply to THAT artwork and is skipped (see the
    outcomes bullet below) — never emitted as a copy, and never a reason to
    discard the rest of the set.
    The general invariant: **no variant may be a silent duplicate.** Every
    transform reports the share it moved, and one that changed less than the
    floor is named in the notes (a flip of symmetric art, a hue shift of an
    achromatic proof). A second cause of that no-op was case: declared colours
    come back lowercase while a manifest palette is uppercase, so the mapping
    missed every entry — both sides are now normalised (`norm_hex`).
  - **`build_dataset` has THREE outcomes, and they are not the same failure.**
    (1) A transform that applies is written. (2) A transform that cannot apply to
    THIS artwork — `palette-cycle` on single-ink line art, an ordinary output of
    the trace stage — raises `TransformNotApplicable` (a `TransformError`
    subclass, so the CLI's exit code and the runner's manifest note are
    unchanged), is SKIPPED, and is NAMED in the notes. That costs one variant and
    nothing else, where it used to cost all twelve: measured on a real job
    through the deployed runner, the one refusal discarded the other eleven
    transforms and `manifest["dataset"]` came back `None`, because the artwork
    was single-ink and the default opt-in asked for `palette-cycle` too. (3) A
    real fault — an unreadable image, a document with no frame, a name outside
    the vocabulary — still aborts the whole build atomically: the tree is built
    in a staging dir beside the target and swapped in (`os.replace`) only after
    every requested transform is written, so a fault leaves no partial
    `.dataset/` (which the archive route would otherwise list as a real dataset).
    It refuses to replace a non-empty directory that carries no `dataset.json` —
    that one is not ours.
  - **The count is the FILES, never the request.** `count` / `transforms`
    describe what was actually emitted (plus a structured `skipped` list), and a
    skipped transform is named in `notes`, which flow into `dataset.json`,
    `dataset.md` and the runner's log. 11 of 12 with a note is the good outcome;
    12 claimed with 11 held is the failure. If EVERY requested transform is
    skipped, no `.dataset/` is left behind at all — an empty one would be listed
    by the archive route as a real dataset.
  - A `transform` attribute on the outermost `<svg>` is **not** honoured by SVG
    1.1 renderers, so a spatial variant carries its matrix on one wrapping
    `<g>` and the root's viewBox/width/height are rewritten only where the axes
    exchange. Do not "simplify" that back onto the root element.
- Also landed: the **composition operation** — `scripts/compose_svg.py` (pure
  `compose(spec) -> str` plus a CLI) and its `POST /compose` route on the runner,
  which merge inline layers into one SVG with every vector layer still a vector
  (see "Composition" in `README.md` and `docs/runner-http-contract.md`).
- No other tracked in-flight feature at the time of writing. If you start one,
  list it here with an owner and a note.
- **Published artifact names (landed in `runner.py`, not a pipeline change).** A
  job filed with a name publishes `<name slug>-candidate-NN.svg` and
  `<name slug>-proof-NN.png/.pdf/.manifest.json` in its JOB DIRECTORY; the trace
  stage's and the print check's own names (`02_traced/<stem>/candidate_NN.svg`,
  `05_final/<candidate stem>.proof.png`) are untouched, and a job dict with no
  `name_slug` — a reindexed job dir — keeps the pipeline's names exactly.
  Both spellings resolve through the file proxy and through `POST /validations`.
  Contract section: `docs/runner-http-contract.md` ("Published artifact names").

## Blockers

- None known.

## Caveats / known issues

- **Suite count has one canonical home: this file.** No other document states a
  test count — `README.md` and `docs/OVERVIEW.md` point here instead, because the
  number moves with every change and three stale copies were three chances to
  mislead. Verified on a full checkout with the repo venv:
- **Suite count has one canonical home: this file.** No other document states a
  test count — `README.md` and `docs/OVERVIEW.md` point here instead, because the
  number moves with every change and three stale copies were three chances to
  mislead. Verified on a full checkout with the repo venv:
  `.venv/bin/python -m pytest tests -q` collects **853** and passed **853**
  (0 skipped) on the merged tree — the two suites that produced this tree were
  each measured on a checkout without the other's tests (842 for the
  artefact/sweep/layer-A work, 774 collected / 729 passed + 45 skipped for the
  published-names work), so re-measure rather than adding the numbers. Re-run it
  and update this line when you touch the suite.
  (`AGENTS.md`'s "Running tests" section used to carry a stale count — 400 — and
  an agent session could not correct it, because the write guard refuses edits to
  a protected agent-instruction file. The owner's direction on 2026-09-29 lifted
  that for the count line and it now reads 763 — a SNAPSHOT, not the live number:
  this file is the one to trust, and the two must not be allowed to disagree
  silently again. The pass count is also environment-dependent in a way the
  collected total is not: the same checkout has been observed at `853 passed` and
  at `808 passed, 45 skipped`, so compare COLLECTED totals when judging drift.)

- **Two Pythons, and the docs name only one.** `AGENTS.md:76` says "Python 3.13
  through a repo-root venv", which is TRUE of the container: the Dockerfile is
  `debian:13` and Debian 13's system Python is 3.13, so the venv baked into the
  image is 3.13. The HOST checkout's `~/chopshop-svg/.venv/bin/python` is
  **3.12.3**, because this box has no 3.13 to build one from. Both statements can
  be right at once and neither file says which is which — a reader who checks the
  host venv concludes the doc is wrong, which is exactly what happened when the
  proof-variant brief and its independent review both reported the discrepancy.
  It is not drift; it is an unstated distinction.

- **The dataset stage writes into the JOB directory, not the project tree.** A
  per-job artifact left in `05_final/` or `08_tune/` would survive `DELETE` and
  leak into the next job; `runner.py` copies the three flat names into the job
  dir and the nested variant files stay under `<stem>.dataset/` there.
- **Real-ESRGAN is host-only** (needs a Vulkan device). The in-container path
  falls back to Pillow LANCZOS. Every optional prep step defaults OFF and must
  be enabled per-spec (`print.tools.<step>: true`); a missing tool must fail
  loudly (exit 3), never silently skip.
- **`print.invert` (prep inverted twin) overlaps conceptually with the
  pitch-shift `inverse` variant in `trace_sweep.py`** — different mechanisms in
  different stages, deliberately NOT reconciled. Don't assume one replaces the
  other.
- **Three side tools ARE wired, one is not.** `scripts/im_filters.py`,
  `scripts/tune_sweep.py` and `scripts/node_reduce.py` are driven by
  `scripts/prep_expand.py`, which `front_pipeline.sh` calls at stage 1b (filters
  + tune, after prep) and stage 2b (node_reduce, after the sweep, because it
  needs candidates). All three hang off `spec.print.prep_expand` under the
  sub-blocks `filters`, `tune` and `node_reduce`; every block defaults OFF, so a
  spec without the key behaves exactly as before and no bench changes which
  candidate is chosen. Still unwired **as a stage**: `scripts/palette_variants.py`
  — no stage and no spec key of its own. Its colour-mapping machinery IS now
  imported by the proof-variant dataset stage (`proof_variants.py` reuses
  `declared_colours` / `apply_mapping` so both rewrite paint through the same
  tested CSS-cascade code), but nothing runs `palette_variants` itself.
- **Inkscape races under concurrency** (D-Bus `GApplication` registration);
  `front_common.py` sets `DBUS_SESSION_BUS_ADDRESS=disabled:` before parallel
  workers. Keep it.
- **`vtracer` is the Python-API-only wheel (0.6.15):** no CLI, no
  `gradient_step`, no `--palette`/`--max-colors` (standalone-CLI only).
- **The docker image build is slow (~15 min)** because the Dockerfile runs
  `pytest` inside the image (inkscape present), so inkscape-dependent tests
  actually run there.
- **Runner state is in-memory** (`runner.py`): a restart drops jobs and
  validations. See `docs/runner-http-contract.md` (the promoted copy of the
  skill's `references/runner-http-contract.md`).
- **The candidate-triage layer (P1–P5) is a library, not a stage.**
  `scripts/triage.py` (gates, bucket key, text similarity), `triage_events.py`
  (the job-independent event log) and `triage_ranker.py` (preference ranker +
  bucket bandit) are unit-tested and CLI-reachable, but the ONLY part the
  pipeline calls is the log: `compare_candidates.py` records the `shown` events
  and `trace_sweep.py` stamps each candidate's checksum sidecar. The gate, the
  ranker and the bandit have no pipeline call site yet — deliberate seams,
  documented under "Candidate triage + learning stack" in `README.md`. None of
  them picks a winner.
- **The runner's recolour seam is unbuilt.** `POST /recolour` and
  `POST /recolour/colours` are implemented and unit-tested, but no studio
  consumer exists — the colour-cycling preview they were written for was never
  built. Do not assume a UI behind them; see `docs/runner-http-contract.md`.
- **The composition operation (`POST /compose`, `scripts/compose_svg.py`) is
  built and has a consumer** (the studio sends the layer stack inline). Two
  things it does NOT do yet: `hue` on a RASTER layer is skipped (reported in the
  runner's log, never applied), and colliding layer `id`s are merged as-is, so a
  caller handing in two layers that both define `#gradient1` owns that clash.
  Its palette pass composes `snap_colors.py`'s own colour maths
  (`PROPS` / `nearest_palette` / `write_property`) with a mirrored walk, because
  `AGENTS.md` keeps `snap_colors.py` off limits for edits — if that module ever
  learns a new paint property or colour source, compose's loop has to learn it
  too.
- **`pipeline.sh` fails OPEN on an unparseable spec, and that is intended.** The
  `run_preflight` probe falls back to `true` when `spec.json` is missing or cannot
  be parsed, so Layer B (the render preflight) still runs. It is safe by
  construction: a spec that cannot be parsed is not a spec a candidate can pass
  Layer A against, so `validate_svg.py` fails the run on its own — the render can
  cost time, never turn a bad run into a PASS. Do not "fix" this into a
  fail-closed skip without re-confirming that Layer A still fails first.
- **`05_final/<artwork>/` is never swept.** `orphan_sweep.py`'s columns are
  `01_prepped/`, `02_traced/`, `04_validated/`, so the artwork-scoped copies in
  `05_final/` accumulate exactly like the canonical flat names already did (see
  the dataset-stage note above): they survive a job `DELETE` and the hourly sweep
  cannot reclaim them. Deliberate for now — they are evidence, not cache — but a
  long-lived box will want a policy.
- **An unmeasured fidelity verdict reads as `faithful`.** `compare_candidates._fidelity_verdict`
  returns `faithful` (rank 0, reason "artwork MAE n/a") whenever the fidelity
  metric could not be measured, so a candidate nobody managed to score sorts
  WITH the faithful ones rather than being held apart from them.
  `tests/test_compare_candidates.py::test_unmeasured_fidelity_is_not_judged_artwork_lost`
  pins only that it is not called `artwork_lost`, so nothing currently stops that
  reading from being widened. If the distinction matters, it needs its own verdict
  value — a deliberate code change, not a doc edit.
- **The runner's `prep_expand` summary reaches no screen.** `GET /jobs/:id`
  returns it (see `docs/runner-http-contract.md`) and the pipeline writes the bench
  reports at the JOB ROOT (`prep-expand.json`/`.md`, `tune.json`/`.md`,
  `tune-contact-sheet.png`, `node-reduce.json`, `filters.*`), so the bundle or the
  file proxy by name is where you read them; the studio stores `prep_summary` but
  has no column for `prep_expand`. The studio's contract docs state this rather
  than promising a UI.

## Doc layout note (2026-09-28)

`OVERVIEW.md` and `HOWTO-print-check.md` moved from the repo root into `docs/`
to consolidate the `.md` spread. Update any external link to the old root
paths accordingly.
