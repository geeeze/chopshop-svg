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

The `.md` files are readable as-is for a human too — they are the design notes
and pitfall list for the code they describe. `AGENTS.md` is the shorter
orientation; these skills are the deep reference.
