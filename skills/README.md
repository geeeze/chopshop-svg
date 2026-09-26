# Skills

These are the two agent skills that encode the hard-won knowledge of this
pipeline, exported from the author's Hermes install for inclusion in the repo.
They are plain Markdown with YAML frontmatter (Hermes/Claude-skill format), so
they double as deep documentation and can be re-imported by any agent running
Hermes or a compatible skill system.

| Skill | Covers | Backing code |
|---|---|---|
| `shirt-print-front-half` | The **front half**: raster → prep → trace sweep → comparison, the fidelity metric, parallelism (and the Inkscape D-Bus race), check-first prep, VTracer backend detection. | `scripts/prep_raster.py`, `scripts/trace_sweep.py`, `scripts/compare_candidates.py`, `front_pipeline.sh` |
| `svg-print-preflight` | The **back half**: validating an SVG for print — CSS-cascade paint resolution, stroke-width units, rendered ink counting, continuous-tone detection, TAC/coverage, the two-layer gate design. | `validate_svg.py`, `preflight.py`, `snap_colors.py`, `pipeline.sh` |

Each skill's directory mirrors a Hermes skill directory (`SKILL.md` plus any
`references/` and `scripts/`), so importing one is a straight copy:

```bash
# into a Hermes install:
cp -r skills/shirt-print-front-half ~/.hermes/skills/shirt-print-front-half
cp -r skills/svg-print-preflight  ~/.hermes/skills/svg-print-preflight
```

### These are generated — use `./sync-skills`, not a copy

The working skills on the author's machine run ahead of these copies, so they
drift. `./sync-skills` refreshes them:

```bash
./sync-skills            # refresh both
./sync-skills --check    # report drift, change nothing
./sync-skills --diff     # show what would change
```

It is **not** a plain copy, and the reason matters: this repo is **public**,
while the working skills are private and have grown sections describing stacks
that are not in this repo at all (see Scope below). `sync-skills` replaces any
section whose backing code is absent here with a pointer to the private repo,
and redacts hostnames, private addresses, and private-script filenames. Copying
by hand would publish all of that.

The direction is one-way on purpose: **never import a repo copy back over a
working skill.** The repo copy is downstream; importing it would silently undo
local work.

The `.md` files are readable as-is for a human too — they are the design notes
and pitfall list for the code they describe. `AGENTS.md` is the shorter
orientation; these skills are the deep reference.

## Scope

These two cover the pipeline in this repo. The optional layers that sit *around*
it — SwarmUI/ComfyUI generation and the Jev decision sidecar — are separate,
private-stack projects (`chopshop-sui`, `chopshop-jev`) with their own skills,
because they encode a specific generation stack and local install paths rather
than anything about the pipeline itself. Nothing here depends on them: both are
opt-in and neither is called by `front_pipeline.sh` or `pipeline.sh`.
