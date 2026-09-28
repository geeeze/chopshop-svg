#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
triage_events.py -- the candidate-triage learning store (Phase 2: event log).

Phase 0 (``scripts/triage.py``) supplies the pure primitives -- canonical form,
hashes, bucket keys.  This module supplies the one thing that must outlive a
single job: the record of what the pipeline SHOWED a human, and the statistics
learned from what the human then did.  It is deliberately a thin, boring
persistence layer; the gate rules, the ranker and the bandit logic land later
and live elsewhere.

Why a JOB-INDEPENDENT path
--------------------------
The pipeline sweeps ``06_run/<stem>/`` when a job is deleted, so anything that
has to survive that deletion -- the shown-event log and the learned bucket
statistics -- cannot live under it.  Both files therefore live at a
job-independent location, shared by every job, resolved from the environment so
no machine path is ever hardcoded (this repo is public):

    PIPELINE_LEARNING_DIR   default: <repo>/06_run/_learning/

``06_run/`` is the run-record tree the rest of the pipeline already writes to;
``_learning/`` is the one subdirectory deletion does NOT sweep, which is what
makes "survives job deletion" true by construction rather than by convention.

Two files live there:

* ``events.jsonl`` -- append-only, one JSON object per line, one line per
  candidate SHOWN to a human.  Line shape (exact field names):

      {"t": <ISO-8601 UTC>, "job": <run stem>, "svg_sha": <sha256 hex>,
       "event": "shown", "pos": <0-based index in the presented order>,
       "ranker": null}

  ``pos`` is the position in the order the candidate was actually presented in,
  so a later phase can learn a preference from mere exposure.  ``ranker`` is
  ``null`` until a ranker exists.

* ``learned_buckets.json`` -- ``bucket_key -> stats``.  Only the location and
  the read/write skeleton are here; the accumulation and the decision rule are
  a later phase.

Call sites for the checksum sidecar (``<stem>.sha256.json``)
------------------------------------------------------------
``file_sha256`` (from ``scripts/triage.py``) is the "checksum at creation"
primitive.  Every artifact the pipeline writes should be stamped with
``stamp_checksum`` immediately after it is written, so a later stage can tell
whether the bytes it is reading are the bytes that were produced:

    prepped    scripts/prep_raster.py   01_prepped/<stem>.prepped.png
    traced     scripts/trace_sweep.py   02_traced/<stem>/candidate_NN.svg
               (stamped per candidate, in the trace worker, right after the
               tracer writes the file -- see trace_sweep._trace_one)
    candidate  the comparison stage's per-candidate record
    proof      05_final/<id>.proof.png / the print PDF

Only the trace-stage candidate stamp is wired today (see the calls in
``scripts/trace_sweep.py``); the other three are listed so the seam is explicit
rather than rediscovered.

Non-fatal by contract
---------------------
Logging must never be able to break a trace or a comparison.  Every public
function here returns a benign value (``None``/``False``/``0``) instead of
raising when the learning directory cannot be created or written, and the
stage call sites wrap their calls so a filesystem surprise degrades to "no
event recorded" rather than "stage failed".
"""

from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

# The checksum-at-creation primitive lives in triage.py (Phase 0) and is the
# single source of truth for "what sha256 does this pipeline mean".  Imported,
# never reimplemented -- with front_common as a fallback so a triage.py that
# cannot import (e.g. Pillow absent in a stripped runtime) still leaves the
# event log working.
try:  # pragma: no cover - the import path is exercised by monkeypatch tests
    from triage import file_sha256  # noqa: F401  (re-exported)
except Exception:  # noqa: BLE001
    from front_common import sha256_file as file_sha256  # noqa: F401

#: Repository root, derived from this file -- never a hardcoded machine path.
ROOT = os.path.dirname(_SCRIPT_DIR)

#: Environment variable that relocates the job-independent learning store.
LEARNING_DIR_ENV = "PIPELINE_LEARNING_DIR"

#: Default learning directory, relative to the repo (the 06_run tree the rest
#: of the pipeline already uses).
DEFAULT_LEARNING_SUBDIR = os.path.join("06_run", "_learning")

#: Name of the append-only shown-event log inside the learning directory.
EVENTS_FILENAME = "events.jsonl"

#: Name of the learned bucket statistics inside the learning directory.
LEARNED_BUCKETS_FILENAME = "learned_buckets.json"

#: The one event type this phase emits.  Kept as a constant so the wire-in and
#: the tests cannot drift apart on the string.
EVENT_SHOWN = "shown"

#: Field order of an event line.  The line is serialized from a dict built in
#: exactly this order, and the tests pin it, so the JSONL is stable and
#: diffable rather than dict-order-dependent.
EVENT_FIELDS = ("t", "job", "svg_sha", "event", "pos", "ranker")

#: Version stamped into learned_buckets.json so a future phase can migrate
#: without guessing what shape it is holding.
LEARNED_BUCKETS_VERSION = 1

#: The statistics a bucket accumulates.  Documented here (and returned by
#: ``empty_bucket_stats``) so the writer and the future ranker agree on the
#: shape; NO accumulation or ranking happens in this module.
BUCKET_STATS_FIELDS = ("shown", "chosen", "rejected")

# Serializes appends WITHIN one process.  Across processes the atomicity comes
# from O_APPEND on a single write() (see append_event), not from this lock.
_APPEND_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# #triage-events-locate-learning-dir
# --------------------------------------------------------------------------

def learning_dir():
    """The job-independent learning directory.

    ``PIPELINE_LEARNING_DIR`` wins when set (a deployment can point the store
    anywhere); otherwise ``<repo>/06_run/_learning``.  Deriving the default
    from this file's location means the path is portable and never a hardcoded
    machine path.
    """
    env = os.environ.get(LEARNING_DIR_ENV)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(ROOT, DEFAULT_LEARNING_SUBDIR)


def ensure_learning_dir(path=None):
    """Create the learning directory if absent; return it, or None on failure.

    Returning None (instead of raising) is the contract that lets every caller
    treat logging as best-effort: a read-only or uncreatable filesystem must
    never break a trace or a comparison.
    """
    target = path or learning_dir()
    try:
        os.makedirs(target, exist_ok=True)
    except OSError:
        return None
    return target


def events_path(path=None):
    """Absolute path of ``events.jsonl`` in the learning directory."""
    return os.path.join(path or learning_dir(), EVENTS_FILENAME)


def learned_buckets_path(path=None):
    """Absolute path of ``learned_buckets.json`` in the learning directory."""
    return os.path.join(path or learning_dir(), LEARNED_BUCKETS_FILENAME)


def _now():
    """ISO-8601 UTC timestamp, second resolution, matching the rest of the
    pipeline's ``generated`` stamps."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# #triage-events-append-shown
# --------------------------------------------------------------------------

def event_line(job, svg_sha, pos, ranker=None, event=EVENT_SHOWN, t=None):
    """Build one event dict in the exact contract field order.

    Returned as a plain dict (insertion-ordered) rather than a string so tests
    and callers can assert on the fields; ``_serialize`` turns it into the
    newline-terminated JSONL payload.
    """
    return {
        "t": t or _now(),
        "job": job,
        "svg_sha": svg_sha,
        "event": event,
        "pos": pos,
        "ranker": ranker,
    }


def _serialize(line):
    """One compact JSON line, newline-terminated, ASCII-leaning but UTF-8 safe."""
    return (json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")


def append_event(job, svg_sha, pos, ranker=None, event=EVENT_SHOWN, t=None,
                 path=None):
    """Append ONE shown-event line to ``events.jsonl``.  Returns True on write.

    Concurrency: the file is opened O_APPEND|O_CREAT|O_WRONLY and the whole
    line is emitted in a single ``os.write``.  A single O_APPEND write is
    atomic on POSIX, so concurrent workers cannot interleave halves of a line
    and produce a corrupt JSONL.  An in-process lock additionally serializes
    threads sharing this module.  Never raises: returns False when the
    directory cannot be created or the write fails.
    """
    directory = ensure_learning_dir(path)
    if directory is None:
        return False
    target = os.path.join(directory, EVENTS_FILENAME)
    payload = _serialize(event_line(job, svg_sha, pos, ranker=ranker,
                                    event=event, t=t)).encode("utf-8")
    try:
        with _APPEND_LOCK:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                written = os.write(fd, payload)
                # A short write on a regular file is vanishingly rare; loop so
                # the line is never silently truncated even if it happens.
                while written < len(payload):
                    written += os.write(fd, payload[written:])
            finally:
                os.close(fd)
    except OSError:
        return False
    return True


def _sha_of(entry):
    """Resolve the sha256 for one candidate descriptor.

    Accepts a bare sha string, or a dict carrying the sha under any of the
    names the pipeline already uses (``svg_sha`` here, ``output_sha256`` in
    sweep.json).  Anything unresolvable becomes None, which the event log
    records rather than guessing.
    """
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        for key in ("svg_sha", "output_sha256", "file_sha256", "sha256"):
            value = entry.get(key)
            if value:
                return value
    return None


def record_shown(job, candidates, ranker=None, t=None, path=None):
    """Append one ``shown`` event per candidate, in presented order.

    ``candidates`` is the ordered sequence the comparison stage produced: each
    entry is either a sha256 hex string or a dict resolvable by ``_sha_of``.
    ``pos`` is the 1-based rank (first candidate = 1), matching the comparison
    stage's own display loop (``enumerate(candidates, 1)``) and the studio's
    ``Event.position_of`` (also 1-based) — so the exposure signal here and the
    action signal on the studio side are on ONE scale when a later ranker joins
    them. Never 0-based: a candidate shown at rank 1 must read rank 1 in the
    log too.

    Returns the number of lines written.  A failure on any single line stops
    the loop and returns what was written so far; it never raises, and it is a
    no-op when the learning directory cannot be created.
    """
    directory = ensure_learning_dir(path)
    if directory is None:
        return 0
    written = 0
    for pos, entry in enumerate(candidates, 1):
        if not append_event(job, _sha_of(entry), pos, ranker=ranker, t=t,
                            path=directory):
            break
        written += 1
    return written


# --------------------------------------------------------------------------
# #triage-events-learned-buckets
# --------------------------------------------------------------------------

def empty_bucket_stats():
    """A fresh, zeroed stats record for one bucket key.

    Shape only: this module does not accumulate, rank or decide.  It exists so
    the location/read/write skeleton and the future learner share one spelling
    of the stats keys.
    """
    return {field: 0 for field in BUCKET_STATS_FIELDS}


def default_learned_buckets():
    """The empty structure learned_buckets.json starts (and degrades) to."""
    return {"version": LEARNED_BUCKETS_VERSION, "buckets": {}}


def load_learned_buckets(path=None):
    """Read ``learned_buckets.json`` (``bucket_key -> stats``).

    Returns the default empty structure when the file is absent, unreadable or
    not a JSON object -- a learning store that can block a stage by being
    missing would be worse than useless.  Never raises.
    """
    target = learned_buckets_path(path)
    try:
        with open(target, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return default_learned_buckets()
    if not isinstance(payload, dict):
        return default_learned_buckets()
    merged = default_learned_buckets()
    merged.update(payload)
    if not isinstance(merged.get("buckets"), dict):
        merged["buckets"] = {}
    return merged


def save_learned_buckets(buckets, path=None):
    """Write ``bucket_key -> stats`` to the job-independent store.

    Returns the path written, or None when the directory cannot be created or
    the write fails.  Written via a same-directory temp file + ``os.replace``
    so a concurrent reader never sees a half-written JSON document.
    """
    directory = ensure_learning_dir(path)
    if directory is None:
        return None
    target = os.path.join(directory, LEARNED_BUCKETS_FILENAME)
    tmp = target + ".tmp.%d" % os.getpid()
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(buckets, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, target)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None
    return target


# --------------------------------------------------------------------------
# #triage-events-stamp-checksum
# --------------------------------------------------------------------------

def checksum_sidecar_path(artifact_path):
    """Sidecar path for an artifact: ``<stem>.sha256.json`` beside it."""
    directory = os.path.dirname(os.path.abspath(artifact_path))
    stem = os.path.splitext(os.path.basename(artifact_path))[0]
    return os.path.join(directory, "%s.sha256.json" % stem)


def stamp_checksum(artifact_path, sha=None, sidecar=None, t=None):
    """Record the sha256 of a just-written artifact into its sidecar.

    The checksum is taken at CREATION time, from the bytes on disk right after
    the producing stage wrote them, so a later reader can tell whether the file
    it holds is the file that was produced.  The sidecar holds the artifact's
    basename (never an absolute path -- this repo is public) plus the digest,
    byte size and the stamp time.

    ``sha`` may be supplied when the producer already computed it (the trace
    worker does, for sweep.json); otherwise it is computed here with the
    Phase 0 ``file_sha256`` primitive, which is imported rather than
    reimplemented.

    Returns the sidecar path, or None when the artifact is missing or the
    sidecar cannot be written.  Non-fatal by contract: a checksum is
    provenance, it is never allowed to fail the stage that produced the file.
    """
    if not os.path.exists(artifact_path):
        return None
    if sha is None:
        try:
            sha = file_sha256(artifact_path)
        except OSError:
            return None
    target = sidecar or checksum_sidecar_path(artifact_path)
    try:
        size = os.path.getsize(artifact_path)
    except OSError:
        size = None
    payload = {
        "algo": "sha256",
        "file": os.path.basename(artifact_path),
        "sha256": sha,
        "size": size,
        "t": t or _now(),
    }
    tmp = "%s.tmp.%d" % (target, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, target)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None
    return target


def svg_file_sha(path):
    """sha256 of a candidate SVG's bytes -- the value the event log records.

    This is the same digest sweep.json records as ``output_sha256``, so a shown
    event can be joined back to the trace that produced the candidate.  Returns
    None if the file cannot be read.
    """
    try:
        return file_sha256(path)
    except OSError:
        return None


if __name__ == "__main__":  # pragma: no cover - exercised manually
    print("triage_events.py -- learning store for the candidate triage")
    print("  learning dir : %s" % learning_dir())
    print("  events       : %s" % events_path())
    print("  buckets      : %s" % learned_buckets_path())
