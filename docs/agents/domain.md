# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## This repo's shape (single-context)

chopshop-svg is a **single-context repo**: the one context document is
**`CONTEXT.md` at the repo root**, and it is the only place domain vocabulary,
working state and caveats live. There is **no `CONTEXT-MAP.md`** and **no
`docs/adr/`** today — do not go looking for either, and do not create them
speculatively. If a decision is worth an ADR it will be created lazily, by the
`/domain-modeling` skill, the moment a term or decision actually needs it.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root — the context document for this repo.
- **`CONTEXT-MAP.md`** at the repo root *if it appears*: it would point at one `CONTEXT.md` per context, and you should read each one relevant to the topic. It does not exist here yet.
- **`docs/adr/`** *if it appears*: read ADRs that touch the area you're about to work in. It does not exist here yet either.

If any of these files don't exist, **proceed silently**. Don't flag their absence; don't suggest creating them upfront. The `/domain-modeling` skill (reached via `/grill-with-docs` and `/improve-codebase-architecture`) creates them lazily when terms or decisions actually get resolved.

## File structure

This repo today (single-context; `docs/adr/` is not created until a decision needs it):

```
/
├── CONTEXT.md            ← the context document (repo root)
├── AGENTS.md             ← agent orientation / hard rules
├── README.md
├── docs/                 ← OVERVIEW, HOWTO-print-check, pipeline-schemas,
│                           runner-http-contract, agents/
└── scripts/  tests/  skills/
```

The generic single-context shape, for comparison (most repos):

```
/
├── CONTEXT.md
├── docs/adr/
└── src/
```

Multi-context repo (presence of `CONTEXT-MAP.md` at the root):

```
/
├── CONTEXT-MAP.md
├── docs/adr/                          ← system-wide decisions
└── src/
    ├── ordering/
    │   ├── CONTEXT.md
    │   └── docs/adr/                  ← context-specific decisions
    └── billing/
        ├── CONTEXT.md
        └── docs/adr/
```

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in `CONTEXT.md`. Don't drift to synonyms the glossary explicitly avoids.

If the concept you need isn't in the glossary yet, that's a signal: either you're inventing language the project doesn't use (reconsider) or there's a real gap (note it for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0007 (event-sourced orders), but worth reopening because…_
