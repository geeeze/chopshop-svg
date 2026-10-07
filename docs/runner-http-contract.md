# Runner HTTP contract (`runner.py`)

Canonical copy. `runner.py` is the pure-stdlib HTTP server that wraps the
pipeline and exposes it to Chopshop Studio. It is the operator-facing surface:
Studio does not shell the pipeline scripts itself, it POSTs jobs to the runner
and reads results back. The pipeline image and the runner are separate
deliverables — a built image proves the pipeline runs; it does not prove a runner
is listening on the port.

A second copy of this contract lives in-repo as
`skills/chopshop-svg-pipeline/references/runner-http-contract.md` (the
repo-owned agent skill); this file is the one linked from `README.md`. If you
change the runner's routes, change both.

It serves `RUNNER_PORT` (default `8787`) bound to `RUNNER_BIND` (default
`0.0.0.0`; bind a real deployment to a private address).

## Auth

All routes require a `Bearer` token unless `RUNNER_TOKEN` is unset (empty means
"no auth"). The token is read from the environment, never baked into this repo.
A missing or wrong token answers `401 {"error": "unauthorized"}`.

## Endpoints

| method | path | result |
|---|---|---|
| GET | `/health` | status (`ok`, `host`, `mode`, `tools`) |
| POST | `/jobs` | create a job; `202 {id, stem}` |
| GET | `/jobs/:id` | job state: `id`, `state`, `log_tail` (last 40 lines), `prep_summary`, `prep_expand`, `candidate_count`, `comparison`, `error` |
| POST | `/jobs/:id/cancel` | mark a running job cancelled |
| DELETE | `/jobs/:id` | drop in-memory state **and** the job directory; `200 {"deleted": "<id>"}` |
| GET | `/jobs/:id/archive` | 7z-compress the whole job dir on demand, stream it, then unlink |
| GET | `/jobs/:id/dataset` | 7z-compress ONE `<candidate_stem>.dataset` (`?candidate=<file>`) on demand, stream it, then unlink |
| GET | `/files/:job_id/<name>` | file proxy — serves one artifact, resolved by basename |
| GET | `/files/:job_id/<stem>.dataset/<name>` | file proxy for one variant file inside a dataset (`raster/<transform>.png`, `vector/<transform>.svg`, `dataset.json`, `dataset.md`) |
| POST | `/validations` | create a validation (print check) for a chosen candidate; `202 {id}` |
| GET | `/validations/:id` | `{id, state, manifest, log, error}` |
| POST | `/validations/:id/jev` | optional Jev add-on (see below) |
| POST | `/recolour` | **unbuilt seam** — rewrite one candidate SVG's paint from an explicit `{src: dst}` map, return the SVG bytes inline |
| POST | `/recolour/colours` | **unbuilt seam** — list one SVG's distinct declared colours, most-used first |
| POST | `/compose` | **the composition operation** — merge inline layers (vector SVG strings + raster data-URIs) into one SVG with the vectors intact; returns the SVG inline. `op` is accepted and ignored — only `spec` is read |

Anything else answers `404 {"error": "unknown route"}`.

`GET /jobs/:id/candidates` is **not** a route here — this server answers `404
unknown route` for it. Nothing calls it (the studio reads the candidate list out
of `GET /jobs/:id`'s `comparison`), and the studio's mock runners serve it for
older clients; a doc copy that lists it is describing those mocks, not this
runner.

## Creating a job

`POST /jobs` takes `{name, source_kind, input_path, spec}`. `input_path` must be
readable and inside the shared upload root (`HOST_UPLOAD_ROOT` →
`CONTAINER_UPLOAD_ROOT`), or the answer is `400` naming the path. The upload is
copied into the job directory and downscaled when its longest side exceeds
`MAX_INPUT_DIMENSION` (default 1500), so the pipeline runs on the per-job copy,
never the caller's original path. `source_kind` is accepted and ignored. The
reply is `202 {id, stem}`, and the front half then runs asynchronously with
`SPEC=<job dir>/spec.json`, `FRONT_PIPELINE_WORKERS=1` and
`PREP_EXPAND_OUT=<job dir>/prep_expand`.

The JOB DIRECTORY IS THE UNIT OF DELIVERY: `GET /jobs/:id/archive` 7-Zips
exactly that directory and `DELETE` removes it, so a per-job artifact written
into a shared project tree is missing from the download and survives the delete.

## Cancel

`POST /jobs/:id/cancel` raises a flag the job's log pump checks between output
lines and, once set, terminates the running stage's process. It answers
`200 {state: "cancelling"}` while the job is `queued`/`running`,
`200 {state: "cancelled"}` when it is already cancelled, and `409 {state}` once
it is `done`/`error`. A cancelled job settles to `state: "cancelled"` with
`error: "cancelled by operator"`.

## The archive endpoint

`GET /jobs/:id/archive`:

- `409` unless the job is `done`
- `404` if the job dir is missing
- `507` if less than 1 GiB free (`MIN_ARCHIVE_FREE`)
- `500` if no `7z`/`7za`/`7zr` is on PATH

The archive is streamed as `<stem>.7z` (attachment) and unlinked after the
response. The image must ship a 7z binary or the endpoint 500s.

## File proxy

`GET /files/<job_id>/<name>` serves one artifact; the name must match
`[\w.\-]+`, so a traversal attempt is `400 {"error": "invalid file name"}`, and
an unknown job is `404 {"error": "no such job"}`. Resolution order is the job
directory, then a `source.png` fallback into `input/` (the plain source only —
never reused for `source.inverse.png`, which must 404 so the studio's inverse
tile self-hides), then `02_traced/<stem>/<name>`, then `05_final/<name>`, then
`01_prepped/<stem>.prepped[.inverse].png`. A manifest's absolute `artifacts`
paths are NOT evidence the artifact is servable — resolve the basename through
this proxy and check the status before claiming it serves.

## Published artifact names — a job's name is part of what it ships

A job filed through `POST /jobs` with a `name` publishes its artifacts under
that name, so a downloaded 7z (or a saved favourite) says which run it came from:

    <name slug>-candidate-01.svg
    <name slug>-proof-01.png / .pdf / .manifest.json

The slug is lowercased with runs of non-alphanumerics collapsed to `-`
(`slug()`), so `jobby the boat` publishes `jobby-the-boat-proof-01.png`.

- **The pipeline's own names are untouched.** `02_traced/<stem>/candidate_NN.svg`
  and `05_final/<candidate stem>.proof.png` are written by the trace stage and
  the print check exactly as before — those names are that half's contract. The
  runner renames at PUBLISH time (the copy into the job directory) and rewrites
  the two things that carry a name back to a caller: the comparison entry's
  `file` and the manifest's `proof` / `print_pdf`.
- **A job with no name of its own publishes the pipeline's names verbatim.** A
  job dict rebuilt by the startup reindex has no `name_slug`, and a job directory
  published before this change already holds `candidate_01.svg` /
  `candidate_01.proof.png` on disk — inventing a new spelling there would break
  every stored reference to those files. Both spellings are therefore live, and
  the file proxy resolves either (a `<name>-candidate-NN.svg` whose job-dir copy
  is absent falls through to `02_traced/<stem>/candidate_NN.svg`; a
  `<name>-proof-NN.png|.pdf|.manifest.json` to the matching `05_final` file).
- **`POST /validations` accepts EITHER spelling** of `candidate_file` and maps a
  published one back to the pipeline's own before `pipeline.sh` runs on it.

## Recolour seam — implemented, **unbuilt** (no consumer)

`POST /recolour` and `POST /recolour/colours` **exist and are unit-tested**
(`tests/test_runner.py`), but **nothing calls them**: the studio's
"cycle colours" preview they were written for was never built, so there is no UI
behind these routes. Treat them as an unbuilt seam, not a live feature — do not
document studio behaviour that assumes them, and do not change or remove them
without checking the studio side first.

What they do, if you do call them: `/recolour` takes
`{job_id, file, map}` where `map` is an explicit `{source: target}` map and both
sides must be a plain `#rrggbb` (a bad pair is `400`, never silently dropped);
it returns the recoloured SVG inline with `Content-Type: image/svg+xml` — no
raster round-trip. `/recolour/colours` takes `{job_id, file}` and returns
`{file, colours}`, the distinct declared colours most-used first, so a picker
can offer "replace THIS colour". Both rewrite paint only: `fill` / `stroke` /
`stop-color` through the CSS cascade, with geometry, node count, `viewBox` and
dimensions untouched by construction (the work is
`palette_variants.apply_mapping`, imported lazily so the runner's startup stays
light). Errors: `400` for a missing `job_id`/`file` or a bad map, `404` when the
artifact is not resolvable, `500` when a rewrite fails.

## The dataset proxy and archive (`validation.variants`)

When the spec opts in (`"validation": {"variants": true}`, or
`{"enabled": true, "transforms": [...]}`), `run_back` runs
`scripts/proof_variants.py` after the proof, PDF and manifest are in the job
directory and adds a `dataset` key to the manifest
(`{dir, manifest, sheet, package, count, transforms}`). The stage is additive: a
failure in it is appended to the manifest's `notes` and the validation still
settles to `done`.

- The three FLAT names (`<stem>.dataset.json`, `<stem>.dataset.md`,
  `<stem>.dataset-contact-sheet.png`) sit at the job root, so they go through
  the ordinary `/files/:job_id/<name>` proxy.
- `GET /files/:job_id/<stem>.dataset/<name>` is the FOUR-segment form and is the
  only route whose request may contain a slash; `<name>` may be a nested
  `raster/<transform>.png` or `vector/<transform>.svg`. Containment is
  re-checked on the RESOLVED path (a `..` segment, an absolute name, a backslash
  or a symlink out of the tree is `400`), because the flat route's name regex
  cannot see across the extra segment. An unknown dataset dir is `404 {"error":
  "no such dataset"}`.
- `GET /jobs/:id/dataset` mirrors `/jobs/:id/archive` exactly: `409` unless the
  job is `done`, `404` for a missing job dir or no dataset, `507` below the
  1 GiB floor, `500` when no `7z` is on PATH. `?candidate=<file>` names which
  dataset when the job has more than one; with exactly one it may be omitted,
  and an ambiguous request without it is `400` (never a guess). The stream is
  `<stem>.dataset.7z` (attachment) and the temporary archive is unlinked in a
  `finally`.

## Composition — `POST /compose` (the studio consumes this)

The pipeline traces one raster into SVG candidates and checks one SVG; it could
not MERGE several pieces into one printable artwork until this route. The obvious
implementation — rasterise the layers, then re-trace the composite — throws away
the vectors the trace stage just produced (re-quantised colours, doubled node
counts, lost text and geometry), so `/compose` merges instead: **every vector
layer stays a vector**. The merge itself is `scripts/compose_svg.py`
(`compose(spec) -> str`, also a CLI); the route adds only the envelope, the
refusal status and the log line.

Request: `{"op": "compose", "spec": {...}}`. `op` is informational — the route
reads only `spec` and ignores `op` (the same accepted-and-ignored convention as
`source_kind` on `POST /jobs`). The spec is

| key | meaning |
|---|---|
| `width`, `height` | output canvas in px — both required, both positive |
| `background` | `#rrggbb` (a named SVG colour is accepted too) or `"transparent"` |
| `palette` | OPTIONAL — the composite's OWN palette, applied to the merged result |
| `layers` | required list, painted in order |

Each layer is `{type, src, x, y, w, h, opacity, hue, crop}`:

- `type` is `"svg"` (a full inline SVG document string) or `"raster"` (an inline
  `data:` URI). **`src` is ALWAYS inline** — compose never touches the
  filesystem for layer content, so a compose request cannot read the runner's
  disk (a raster `src` that is a path is refused, not resolved).
- `x`/`y` place the layer; `w`/`h` size it (both positive).
- A VECTOR layer's inline SVG is parsed and its inner content wrapped in
  `<g transform="translate(x,y) scale(w/nw, h/nh)" opacity="...">`, where
  `(nw, nh)` is the layer's natural size: its `viewBox` when it has one,
  otherwise its `width`/`height`. A viewBox with a non-zero origin is offset back
  into place, so a layer drawn around (100, 100) still lands where the caller
  asked. A layer with neither a viewBox nor a width/height is refused.
- `hue` on a vector layer becomes an `feColorMatrix type="hueRotate"` filter in
  `<defs>` (one unique id per layer, checked against ids the layers already
  carry) — a FILTER ELEMENT, not a CSS `filter:` declaration, because a
  rasteriser (rsvg, an Inkscape export) honours the former and would silently
  ignore the latter, leaving a preview that looks right and a print that is
  wrong.
- A RASTER layer embeds verbatim as `<image href="data:..." x y width height
  opacity>`. `hue` on a raster layer is NOT applied yet (a documented
  follow-up): it is reported in the runner's log rather than silently dropped.
- OPTIONAL `crop` (raster only): `{x, y, w, h}` as **fractions of the source
  image** (0..1, positive width/height, inside the image) — the studio
  builder's manual crop. The embed becomes a nested `<svg>` viewport whose
  `viewBox` IS the window, the image fills 0..1 of it, and
  `preserveAspectRatio="none"` maps it onto `x/y/w/h` exactly like the
  builder's `drawImage` — one rect everywhere, no re-encoding. A `crop`
  outside 0..1 or on a non-object is a SpecError (refused, never painted);
  a layer without `crop` embeds verbatim as before.

If the spec carries a `palette`, the merged document's `fill` / `stroke` /
`stop-color` are snapped to it through `snap_colors.py`'s own colour maths (its
`PROPS` / `nearest_palette` / `write_property`, driven over the merged tree
through the same CSS cascade — imported, not re-implemented).
A near miss is snapped; a colour further than the tolerance is a FINDING, not an
error. The composite owns the palette, so a colour it did not ask for is logged
and left in place, never forced onto a nearest match and never a reason to fail
the merge. The canvas background is part of that pass (it is a `fill`), so a
palette that omits the canvas colour reports it.

Responses: `200` with `Content-Type: image/svg+xml` and the merged SVG as the
body. A spec that cannot be merged — no `width`/`height`, no `layers`, an unknown
layer type, an inline layer SVG with neither a viewBox nor a width/height, a
raster `src` that is not a data URI, a palette entry that is not a colour — is
`422 {"error": ..., "kind": "invalid_spec"}`; a missing or non-object `spec`
(a body that is not a JSON object at all included) is the same `422` envelope,
naming what was missing. `500` is only for a merge that fails unexpectedly.
Off-palette colours and an unapplied raster hue never change the status: they are
logged, and the merged SVG is returned.

## Disk gate

The runner refuses to archive below 1 GiB free (`MIN_ARCHIVE_FREE`) and answers
`507 insufficient disk to archive`. The same 1 GiB floor, with a 2 GiB warning
band above it, is the pipeline's `scripts/disk_check.py`, called by
`front_pipeline.sh` and `pipeline.sh` before any stage runs — see the README's
"Housekeeping" section for the exit codes and wiring.

## State is mostly in-memory

Job and validation state lives in memory. `reindex_jobs()` does rebuild the JOBS
index from disk at startup, so a finished job's files stay reachable through
`/files/...` across a restart — but the rebuilt record is `state: "done"` with an
empty `log_tail` and no `prep_summary`. Validations are NOT reindexed: they are
gone on restart, so a stored `runner_validation_id` can point at nothing, and the
studio resolves the manifest from disk (`job_id` + `candidate_file`) rather than
trusting the id alone.

## Validations

`POST /validations` takes `{job_id, candidate_file}` and answers `202 {id}`;
`409 {"error": "job not ready"}` when the job is not `done`, `404 {"error": "no
such candidate"}` when the name is not in the job's candidate list.
`GET /validations/:id` returns `{id, state, manifest, log, error}`.

## Optional Jev add-on (`POST /validations/:id/jev`)

The runner **shells** an external annotator (the private `chopshop-jev` repo);
it never re-implements it — the annotator is mounted, not copied, and its own
suite is the behavioural spec. The request body is `{job_id, candidate_file}`
(the manifest itself is the annotator's business); that pair is what lets a
restarted runner answer for a validation it has forgotten. A `200` returns the
envelope `{model, endpoint, latency_ms, answers, verdict}`. Status contract:

| situation | answer |
|---|---|
| add-on not configured (no annotator or no key) | `503` with the reason — never `404` |
| validation known and still running | `409` |
| unknown validation and no `job_id`/`candidate_file` fallback | `404` |
| manifest missing on disk | `404` naming the candidate |

A `404` reads as "this runner has no such route" and sends the operator hunting
a wiring bug that does not exist — which is why the unconfigured case is `503`
with a reason, not `404`. The relevant env vars are declared in
`docker-compose.yml` with empty defaults (`JEV_ANNOTATOR`, `JEV_API_KEY`,
`JEV_ENDPOINT`, `JEV_MODEL`); the machine-specific values live in a host-local,
gitignored override, never in a tracked file. `runner.py` itself reads
`JEV_ANNOTATOR`, `JEV_API_KEY` (or the `TYPESAFE_API_KEY` alias), `JEV_ENDPOINT`
and `JEV_TIMEOUT` (default 180s, which compose does not declare); `JEV_MODEL` it
passes through untouched to the annotator.

## Health

`GET /health` reports `{ok, host, mode, tools}`. `host` is a **label**, read from
`CHOPSHOP_RUNNER_LABEL` (falling back to the machine's own name) — not a literal
in this file. `mode` is always `"real"` here (the studio's mock runners report
`"mock"`). `tools` reports which binaries are present (`inkscape`, `gs`) and
`tools.jev` for the optional add-on, so a missing key is visible before the
operator presses the button.
