# Runner HTTP contract (`runner.py`)

`runner.py` is the pure-stdlib HTTP server that wraps the pipeline and exposes
it to Chopshop Studio. It is the operator-facing surface: Studio does not shell
the pipeline scripts itself, it POSTs jobs to the runner and reads results back.
The pipeline image and the runner are separate deliverables — a built image
proves the pipeline runs; it does not prove a runner is listening on the port.

## Auth

All routes require a `Bearer` token unless `RUNNER_TOKEN` is unset (empty means
"no auth"). The token is read from the environment, never baked into this repo.

## Endpoints

| method | path | result |
|---|---|---|
| GET | `/health` | status (`ok`, `host`, `mode`, `tools`) |
| POST | `/jobs` | create a job; returns the job id |
| GET | `/jobs/:id` | job state: `state`, `log_tail` (last 40 lines), `prep_summary`, `candidate_count`, `comparison`, `error` |
| POST | `/jobs/:id/cancel` | mark a running job cancelled |
| DELETE | `/jobs/:id` | drop in-memory state **and** the job directory |
| GET | `/jobs/:id/archive` | 7z-compress the whole job dir on demand, stream it, then unlink |
| GET | `/files/:job_id/<name>` | file proxy — serves one artifact, resolved by basename inside the job dir |
| POST | `/validations` | create a validation (print check) for a chosen candidate |
| GET | `/validations/:id` | validation state, manifest, log |
| POST | `/validations/:id/jev` | optional Jev add-on (see below) |

Anything else answers `404 {"error": "unknown route"}`.

`/health`'s `host` is a **label**, read from `CHOPSHOP_RUNNER_LABEL` (falling
back to the machine's own name) — not a literal in this file. `tools` reports
which binaries are present (`inkscape`, `gs`) and `tools.jev` for the optional
add-on, so a missing key is visible before the operator presses the button.

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
`[\w.\-]+` (which rejects path traversal). A manifest's absolute `artifacts`
paths are NOT evidence the artifact is servable — resolve the basename through
this proxy and check the status before claiming it serves.

## Disk gate

`scripts/disk_check.py` warns below 2 GiB free and refuses (exit 2) below 1 GiB.
It is wired into `front_pipeline.sh` and `pipeline.sh` before any stage runs,
and re-checked before archiving.

## State is in-memory

Job and validation state lives in memory and is gone on restart. That wipes
in-memory validations, so a stored `runner_validation_id` can point at nothing;
the studio resolves the manifest from disk (`job_id` + `candidate_file`) rather
than trusting the id alone.

## Optional Jev add-on (`POST /validations/:id/jev`)

The runner **shells** an external annotator (the private `chopshop-jev` repo);
it never re-implements it — the annotator is mounted, not copied, and its own
suite is the behavioural spec. Status contract:

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
gitignored override, never in a tracked file.
