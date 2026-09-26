# Skills

`chopshop-svg-pipeline` is the agent skill for this repo. It is plain Markdown
with YAML frontmatter (Hermes/Claude-skill format), so it doubles as deep
documentation and can be re-imported by any agent running Hermes or a compatible
skill system.

| Skill | Covers | Backing code |
|---|---|---|
| `chopshop-svg-pipeline` | **Stage 1**: raster → prep → trace sweep → comparison, the fidelity metric, parallelism (and the Inkscape D-Bus race), check-first prep, VTracer backend detection, node reduction, the inverted source twin, palette variations. **Stage 2**: checking an SVG for print readiness — CSS-cascade paint resolution, stroke-width units, rendered ink counting, continuous-tone detection, TAC/coverage, the two-layer gate design. | `scripts/prep_raster.py`, `scripts/trace_sweep.py`, `scripts/compare_candidates.py`, `scripts/node_reduce.py`, `front_pipeline.sh`, `validate_svg.py`, `preflight.py`, `snap_colors.py`, `pipeline.sh` |

The directory mirrors a Hermes skill directory (`SKILL.md` plus `references/` and
`scripts/`), so importing it is a straight copy:

```bash
# into a Hermes install:
cp -r skills/chopshop-svg-pipeline ~/.hermes/skills/chopshop-svg-pipeline
```

The `.md` files are readable as-is for a human too — they are the design notes
and pitfall list for the code they describe. `AGENTS.md` is the shorter
orientation; this skill is the deep reference.

## Vocabulary

A finished run that a person has accepted is a **proof**. The word "validation"
appears in this repo only where it is a real identifier — `validate_svg.py`, the
`04_validated/` directory, and the script's own `VALIDATION PASSED for <file>`
output. Chopshop Studio, the operator front end, calls the accepted artifact a
proof and numbers it after the candidate it came from (`candidate_06.svg` →
`proof_06`).

## This skill is hand-maintained here

`./sync-skills` used to generate the copies in this directory from the author's
local Hermes skills, sanitising them for a public repo. Those local skills were
since consolidated, so the generated copies lost their source and silently
stopped updating — `--check` exited 0 while skipping them, and the published
copies drifted for as long as nobody looked.

The script now behaves differently:

* **Repo-owned skills** (the default — anything not listed in `GENERATED`) are
  maintained in this repo, have no on-disk source, and are reported as
  `repo-owned` rather than silently skipped.
* **Generated skills** must have a matching `SKILL.md` on disk. If one is listed
  in `GENERATED` and its source is missing, `sync-skills` fails with a non-zero
  exit instead of skipping. That silent skip is what let the old copies rot.

```bash
./sync-skills            # refresh generated copies (none at present)
./sync-skills --check    # report drift, change nothing; non-zero on drift or a missing source
./sync-skills --diff     # show what would change
```

When generated copies do exist, `sync-skills` is **not** a plain copy, and the
reason matters: this repo is **public**, while the working skills are private and
have grown sections describing stacks that are not in this repo at all (see Scope
below). It replaces any section whose backing code is absent here with a pointer
to the private repo, and redacts hostnames, private addresses, and private-script
filenames. Copying by hand would publish all of that.

The direction is one-way on purpose: **never import a repo copy back over a
working skill.** A repo copy is downstream; importing it would silently undo
local work.

## Scope

This skill covers the pipeline in this repo. The optional layers that sit
*around* it — image generation/reprocessing and the Jev decision sidecar — are
separate, private-stack projects (`chopshop-sui`, `chopshop-jev`) with their own
skills, because they encode a specific generation stack and local install paths
rather than anything about the pipeline itself. Nothing here depends on them:
both are opt-in and neither is called by `front_pipeline.sh` or `pipeline.sh`.
