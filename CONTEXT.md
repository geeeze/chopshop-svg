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

## Blockers

- None known.

## Caveats / known issues

- **Suite count has one canonical home: this file.** No other document states a
  test count — `README.md` and `docs/OVERVIEW.md` point here instead, because the
  number moves with every change and three stale copies were three chances to
  mislead. Verified on a full checkout with the repo venv:
  `.venv/bin/python -m pytest tests -q` collects **700** and passes
  **655 passed, 45 skipped**. Re-run it and update this line when you touch the
  suite. (`AGENTS.md` still carries an older number — 400 — and it is owned by the
  parent agent, not by this working-state file; an agent session CANNOT correct it,
  because the write guard refuses edits to a protected agent-instruction file and
  says not to route around it. It needs a human or an approved edit.)

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

## Doc layout note (2026-09-28)

`OVERVIEW.md` and `HOWTO-print-check.md` moved from the repo root into `docs/`
to consolidate the `.md` spread. Update any external link to the old root
paths accordingly.
