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
- No tracked in-flight feature at the time of writing. If you start one, list it
  here with an owner and a note.

## Blockers

- None known.

## Caveats / known issues

- **Suite count has one canonical home: this file.** No other document states a
  test count — `README.md` and `docs/OVERVIEW.md` point here instead, because the
  number moves with every change and three stale copies were three chances to
  mislead. Verified 2026-09-28 on a full checkout with the repo venv:
  `.venv/bin/python -m pytest tests -q` collects and passes **590 tests**. Re-run
  it and update this line when you touch the suite. (`AGENTS.md` still carries an
  older number; it is owned by the parent agent, not by this working-state file.)
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
  candidate is chosen. Still unwired: `scripts/palette_variants.py` — no stage,
  no spec key, nothing calls it but its own tests and the docs.
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

## Doc layout note (2026-09-28)

`OVERVIEW.md` and `HOWTO-print-check.md` moved from the repo root into `docs/`
to consolidate the `.md` spread. Update any external link to the old root
paths accordingly.
