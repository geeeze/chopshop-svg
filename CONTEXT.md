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

- **Suite count drifts across docs.** `git log` says 419; `docs/OVERVIEW.md`
  says 369; `AGENTS.md` says 400. The truth is whatever `pytest tests/` prints;
  sync the prose count when you touch it.
- **Real-ESRGAN is host-only** (needs a Vulkan device). The in-container path
  falls back to Pillow LANCZOS. Every optional prep step defaults OFF and must
  be enabled per-spec (`print.tools.<step>: true`); a missing tool must fail
  loudly (exit 3), never silently skip.
- **`print.invert` (prep inverted twin) overlaps conceptually with the
  pitch-shift `inverse` variant in `trace_sweep.py`** — different mechanisms in
  different stages, deliberately NOT reconciled. Don't assume one replaces the
  other.
- **Side tools are not wired into stages:** `scripts/node_reduce.py`,
  `scripts/tune_sweep.py`, `scripts/im_filters.py`,
  `scripts/palette_variants.py`. None runs automatically.
- **Inkscape races under concurrency** (D-Bus `GApplication` registration);
  `front_common.py` sets `DBUS_SESSION_BUS_ADDRESS=disabled:` before parallel
  workers. Keep it.
- **`vtracer` is the Python-API-only wheel (0.6.15):** no CLI, no
  `gradient_step`, no `--palette`/`--max-colors` (standalone-CLI only).
- **The docker image build is slow (~15 min)** because the Dockerfile runs
  `pytest` inside the image (inkscape present), so inkscape-dependent tests
  actually run there.
- **Runner state is in-memory** (`runner.py`): a restart drops jobs and
  validations. See `skills/chopshop-svg-pipeline/references/runner-http-contract.md`.

## Doc layout note (2026-09-28)

`OVERVIEW.md` and `HOWTO-print-check.md` moved from the repo root into `docs/`
to consolidate the `.md` spread. Update any external link to the old root
paths accordingly.
