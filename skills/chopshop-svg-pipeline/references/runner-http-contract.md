# Runner HTTP contract (`runner.py`)

`runner.py` is the pure-stdlib HTTP server that wraps the pipeline and exposes
it to Chopshop Studio. It is the operator-facing surface: Studio does not shell
the pipeline scripts itself, it POSTs jobs to the runner and reads results back.
The pipeline image and the runner are separate deliverables — a built image
proves the pipeline runs; it does not prove a runner is listening on the port.

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
| POST | `/recolour` | **unbuilt seam** (no consumer) — rewrite one candidate SVG's paint from an explicit `{src: dst}` map, return the SVG bytes inline |
| POST | `/recolour/colours` | **unbuilt seam** (no consumer) — list one SVG's distinct declared colours, most-used first |
| POST | `/compose` | **the studio consumes this** — merge inline layers into one SVG with the vectors intact; returns the SVG inline |

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

## Recolour seam — implemented, **unbuilt** (no consumer)

`POST /recolour` and `POST /recolour/colours` exist and are unit-tested
(`tests/test_runner.py`), but **no studio consumer calls them** — the
colour-cycling preview they were written for was never built. Treat them as a
seam, not a live feature. `/recolour` takes `{job_id, file, map}` (both sides of
`map` must be plain `#rrggbb`) and returns the recoloured SVG inline;
`/recolour/colours` returns `{file, colours}`, most-used first. Paint only:
`fill` / `stroke` / `stop-color` through the CSS cascade, geometry untouched.
Full contract: `docs/runner-http-contract.md` in the repo.

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

## Composition (`POST /compose`) — the studio consumes this

The MISSING composition operation: merge layers into one printable SVG WITHOUT
rasterising, so every vector layer stays a vector. Body `{op: "compose", spec}`,
spec `{width, height, background, palette?, layers[]}`, each layer
`{type: "svg"|"raster", src, x, y, w, h, opacity, hue}`. `src` is ALWAYS inline
(an SVG document string, or a `data:` URI) — the route never touches the
filesystem, so it cannot read the runner's disk.

Vector layers land as `<g transform="translate(x,y) scale(w/nw,h/nh)" opacity>`
where `(nw, nh)` is the layer's natural size (its viewBox, else width/height); a
non-zero viewBox origin is offset back into place. A `hue` becomes an
`feColorMatrix type="hueRotate"` filter element — NOT a CSS filter, which rsvg
and Inkscape export would ignore. Raster layers embed as `<image href="data:…">`;
`hue` on a raster layer is a documented follow-up (reported, not dropped). A
`palette` snaps the merged paint through `snap_colors`' own colour maths
(`nearest_palette` / `write_property`, same CSS cascade); a colour further than
tolerance is REPORTED and left alone, never forced.

`200 image/svg+xml` with the merged document. An unmergeable spec is
`422 {"error", "kind": "invalid_spec"}`. Implementation: `scripts/compose_svg.py`
(pure `compose(spec) -> str`, plus a CLI; exit 0 merged, 2 bad usage, 3 invalid
spec).

## Disk gate

`scripts/disk_check.py` warns below 2 GiB free and refuses (exit 2) below 1 GiB.
It is wired into `front_pipeline.sh` and `pipeline.sh` before any stage runs
(README → "Housekeeping" has the exit codes and the argument). The runner enforces
the same 1 GiB floor itself before building an archive (`MIN_ARCHIVE_FREE`, `507`).

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
