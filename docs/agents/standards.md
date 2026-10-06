# Standards adherence, and issue tracking

Two things go wrong in a repository several agents work in: conventions that exist
in writing get bypassed, and work that is found but not finished goes unrecorded.
This file is the short version of both.

`AGENTS.md` comes first — its "Hard policy" section is binding and it names this
repo's own rules. This is the part that is the same everywhere, plus the tracker
discipline.

## Read the standard; do not infer it

| Where | What it settles |
|---|---|
| `AGENTS.md` | this repo's binding policy — read it before editing |
| `CONTEXT.md` | what is in flight, what is blocked, the non-obvious caveats |
| the contract/convention doc the repo names (`docs/contract.md`, `docs/ui-conventions.md`, `docs/API.md`, `docs/runner-http-contract.md`) | the interface. **The contract is the interface.** |
| `docs/agents/domain.md` | this repo's domain model and its vocabulary |
| `docs/agents/triage-labels.md` | the only issue labels to use |

## The rules most often broken

1. **A behaviour change and its documentation land in the SAME commit.** A contract
   that no longer describes the code is worse than no contract, because it is
   believed.
2. **Never reimplement a sibling repo's logic — call it.** Where a repo declares
   this in its policy, it means it: a second implementation is a second thing to
   keep in step, and it will not be.
3. **A hand-mirrored copy is updated with its source, in the same commit.** If this
   repo transcribes another's registry or contract, the transcription is part of
   the change, not a follow-up.
4. **A test that cannot fail is decoration.** Prove a guard by making it fail. And
   a suite that SKIPS is not a green suite: compare the skip count and read the
   reasons before quoting the run as evidence — a rule enforced nowhere looks
   exactly like a rule that passes.
5. **Measure; do not assert.** Mark a fact MEASURED and say how it was measured;
   label ESTIMATES as estimates. A comment that promises behaviour is a test spec.
6. **Host-agnostic.** No absolute paths, hostnames or secrets in code or docs;
   configuration is environment, read at call time. A fact true only of one box is
   tagged HOST-SPECIFIC and is expected to be removed before reuse.
7. **Comments are section-level blocks that carry the WHY**, with a small
   `#label-name-function` breadcrumb at the call sites that cross a file or repo
   boundary. Never line-by-line narration.
8. **Prefer a named failure to a silent default.** A field a caller cannot use is
   refused, not accepted and ignored.
9. **Never change someone else's work in flight.** A dirty tree you did not make is
   not yours to commit, stash, or copy over.
10. **Do not install toolchains into someone else's environment.** Ask first; a
    container without a dependency is a decision somebody made.
11. **A secret purge rewrites every SHA, so every older clone diverges.** Rewriting
    history to remove a leaked secret gives every commit a new hash while the
    content stays identical, so a clone from before the purge reports the same
    hundreds of commits as "only on local" and "only on origin". **Before your
    first commit in any repo here: `git fetch`, then
    `git rev-list --count origin/main..main`.** Non-zero on a repo you have not
    touched means stop and `git reset --hard origin/main` — committing onto a
    pre-purge branch republishes the secret the purge was for.
12. **Pin every dependency; a bare host venv is not a pin.** A venv's
    `bin/python` symlinks to `/usr/bin/python3`, so a system Python upgrade moves
    it off its own `site-packages` and it loses its packages with no error at all —
    observed as a service in a 799-restart loop that systemd reported only as
    `status=1`. Container, or at minimum a `requirements.txt` with versions in it.
13. **Verify a fault against the running system before reporting it.** A
    post-incident report claimed a wiped user table, a Ruby version mismatch and
    an invalidated secret; all three were false while the real fault was a missing
    package. Run the check and quote its output (`select count(*) from users`,
    `ruby -v` against `.ruby-version`, `md5sum`) — a confident wrong diagnosis
    costs the operator more time than an unfinished one.

## Issue tracking

- Issues are GitHub issues **in the repo that owns the work**. File it there, not in
  the repo where you happened to notice it.
- **Every issue carries one of the five canonical labels** (`triage-labels.md`). An
  unlabelled issue is invisible to triage, which reads those exact strings.
- **The labels must exist in the repo.** A create naming an unknown label is a 422,
  so list the labels before filing, and create the canonical set if it is missing
  rather than dropping the label.
- **One issue = one deliverable, with the evidence it is real** — a measured
  number, a failing case, a file and line. Not a wish.
- **Do not file what you can finish in the same pass.** The fix is the artifact and
  the issue is for work left behind; if you fix it, say so in the commit instead.
- **Filing must be idempotent** — skip a title that is already open, so a re-run
  after a partial failure cannot duplicate.
- **`ready-for-agent` means specified enough that an agent starts with no further
  questions.** If it is not, it is `needs-triage`.
- **Never leave the tracker stale.** When a change makes an issue obsolete, comment
  why and close it.

## Before calling it done

- [ ] the repo's own suite is green — and the skip count is understood
- [ ] every contract/convention doc the change touches is updated in the same commit
- [ ] measured claims carry their method; estimates are labelled as estimates
- [ ] work left behind is filed as a labelled issue in the owning repo
- [ ] nothing in the commit belongs to another workstream
