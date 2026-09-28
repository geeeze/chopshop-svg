#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
triage_ranker.py -- candidate-triage Phase 4 (preference ranker) and Phase 5
(bucket bandit).

Phase 0 (``scripts/triage.py``) supplies the pure primitives -- canonical form,
hashes, the gate, the bucket key, tf-idf intent grouping.  Phase 2
(``scripts/triage_events.py``) supplies the job-independent learning store --
the append-only ``shown`` event log and the ``learned_buckets.json`` skeleton.
This module is the learner that sits on top of both, and it is deliberately the
only place in the pipeline that learns anything:

* **Phase 4 -- the preference ranker.**  The training signal is the event log:
  the pipeline records what a human was SHOWN (``shown`` with a 1-based ``pos``),
  and the human's own action on a candidate (``star``/``pick`` vs a candidate
  that was shown and never acted on) is the label.  A small pairwise logistic
  scorer is fit so that, for two candidates the human was shown in the SAME job,
  the one that was acted on scores higher.  Cold start: until enough picks have
  been observed across enough jobs, ``rank`` falls back to the pipeline's own
  hand order -- fewest hard gates failed, then fewest advisories, then artwork
  MAE -- which is exactly the order AGENTS.md permits and no more.

* **Phase 5 -- the bucket bandit.**  A beta-bernoulli / Thompson-sampling bandit
  over BUCKET KEYS (``triage.bucket_of``) that answers one narrow question:
  *which bucket should the sweep try next?*  It carries per-bucket
  successes/attempts, an epsilon explore rate, a staleness TTL and a veto list.
  It never picks a candidate and never picks a winner.

The binding rules this module is written against
-----------------------------------------------
1. **The pipeline never picks a winner.**  ``rank`` ORDERS and ``choose_bucket``
   selects WHICH FAMILY to trace next; both hand every candidate to a human.
   Nothing here deletes, hides or filters a candidate, and no ordering is
   presented as a decision.
2. **Determinism.**  Every loop is over a sorted list or a ``range``; no
   iteration over a set, a dict view or an unsorted mapping ever reaches a
   result.  Identical input yields byte-identical output, including when the
   caller hands the same events over in a different order.  Text is handled by
   code point (``str``), never by byte length; the only place bytes appear is
   a sha256 digest, where that is the point.
3. **The anti-feedback-loop rule.**  A learner that only ever re-presents the
   order it already produces trains on its own output forever.  ``rank``
   therefore always keeps ``n_random_slots`` non-greedy slots visible: the
   candidates sitting just outside the visible window that the score cannot
   separate confidently still get seen (see ``rank``).
4. **The split is by JOB, never by candidate.**  A preference only exists inside
   one exposure set, so pairs are formed within a job -- but a job's candidates
   never straddle the fit/holdout boundary, and a model is never fit on the
   candidates of a single job alone (``learn_min_jobs``).  See ``job_split``.
5. **Pure Python.**  No numpy/scipy/sklearn/lightgbm: the pipeline runs wherever
   the pipeline runs.  Floats, dicts and a full-batch gradient loop are plenty
   for a few thousand candidate pairs.
6. **Non-fatal by contract.**  Reading a corrupt learning store, a missing model
   or an unwritable directory degrades to the hand order / the prior -- it never
   raises into a trace or a comparison.

Public surface
--------------
Phase 4::

    model  = learn_weights(events)                  # or learn_weights() -> log
    value  = score(candidate, model)                # higher = more preferred
    order  = rank(candidates, model, n_random_slots=1)
    save_ranker_model(model) / load_ranker_model()

Phase 5::

    stats  = load_bucket_stats()
    choice = choose_bucket(buckets, stats)           # -> one bucket key, or None
    record_outcome(choice, success, stats)           # beta update
    save_bucket_stats(stats)

Supporting: ``observe_events`` (event log -> observations), ``job_split``,
``hand_key`` / ``hand_scalar`` (the cold-start order), ``bucket_decision``
(the explainable form of ``choose_bucket``), ``normalise_bucket_stats``,
``set_veto`` / ``vetoed_buckets`` / ``expired``.

Where the seams are
-------------------
* The sweep's parameter choice is the Phase 5 call site: a caller asks
  ``choose_bucket`` for the bucket to try and passes the result into the
  sweep's preset/parameter selection.  The bucket key already encodes the
  preset and the binned speckle/colour counts (``triage.bucket_of``), so "which
  bucket" is "which parameter family".  Nothing in ``scripts/trace_sweep.py``
  is changed by this phase.
* The comparison stage is the Phase 4 call site: it already emits the ``shown``
  events; pairing them with the human's action lines is all ``learn_weights``
  needs.  Until the action lines carry metrics, a caller can join them to the
  run-record candidates with ``observe_events(events, candidates=...)``.

Usage::

    python3 scripts/triage_ranker.py --selftest     # deterministic self-check
    python3 scripts/triage_ranker.py train          # fit + persist learned_ranker.json
    python3 scripts/triage_ranker.py choose poster|s1|c0| bw|s2|c1|

The ``train`` command writes only into the job-independent learning store
(``PIPELINE_LEARNING_DIR``, default ``06_run/_learning/``) and never picks a
winner.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import sys
from datetime import datetime, timedelta, timezone

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import triage_events  # noqa: E402  (the Phase 2 learning store)

# The bucket key and the spec's triage thresholds come from Phase 0's triage.py.
# It is imported for those two things only and never reimplemented here; when it
# cannot be imported (a stripped runtime without Pillow) the module still works
# -- bins are parsed out of an already-built bucket key, and a candidate that
# carries no key simply contributes no bin reading rather than a guessed one.
try:  # pragma: no cover - the import path is exercised by monkeypatch tests
    import triage  # noqa: E402
except Exception:  # noqa: BLE001
    triage = None

# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #

#: Name of the learned ranker model inside the learning directory.  It lives
#: beside ``events.jsonl`` and ``learned_buckets.json`` for the same reason they
#: do: a model that dies with the job it was trained on is not a learner.
RANKER_FILENAME = "learned_ranker.json"

#: Version stamped into the model so a later phase can migrate without guessing.
RANKER_VERSION = 1

#: Model kinds.  ``cold_start`` is a real model document, not an error: it
#: carries the counts that justified falling back, so a reader can see WHY the
#: pipeline is still on the hand order.
KIND_COLD_START = "cold_start"
KIND_PAIRWISE = "pairwise_logistic"

#: Direction of ``score``, recorded in the model so a JSON consumer cannot get
#: it backwards.  Higher means more likely to be preferred by the human.
SCORE_DIRECTION = "higher_is_more_preferred"

#: Numeric features, in the fixed order they occupy in every weight vector.
#: These are the readings the pipeline already computes (see ``scripts/triage.py``
#: gate rules and ``scripts/run_record.py`` for the record shape).  Raw units are
#: deliberately NOT pre-scaled here -- the fit standardises with the training
#: means and scales it records, which keeps ``node_count_max`` (hundreds) from
#: drowning ``advisory`` (0-3) without a hand-picked constant per feature.
NUMERIC_FEATURES = (
    "hard",
    "advisory",
    "node_count_max",
    "mae_art",
    "colours",
    "speckles",
    "bucket_speckle_bin",
    "bucket_colour_bin",
)

#: The intercept.  Named so its weight is readable in the JSON.
BIAS_FEATURE = "bias"

#: Prefix for the per-preset one-hot features.  Presets are open (a spec may
#: override ``print.sweep.preset_params``), so the feature set is data-driven:
#: the model records the presets it saw and one feature per preset.
PRESET_PREFIX = "preset:"

#: Event names carrying a human action.  Phase 2 wires ``shown``; the studio's
#: own rows record star/unstar/pick/delete, and the plan's event contract lists
#: them all, so an event log that gains action lines trains this module without
#: a change here.  Both sides share the 1-based ``pos`` scale.
POSITIVE_EVENTS = ("star", "pick", "proof", "chosen", "selected")
NEGATIVE_EVENTS = ("unstar", "discard", "delete", "reject", "rejected")

#: Boolean fields a line may carry instead of an ``event`` name, and the text
#: labels ``05_final/<cand>.pick.json`` uses.  Read as an action signal when
#: present -- "any human-action fields the event line carries".
POSITIVE_FIELDS = ("starred", "star", "picked", "pick", "chosen", "selected")
NEGATIVE_FIELDS = ("rejected", "discarded", "deleted")
POSITIVE_LABELS = ("star", "pick", "proof", "production", "style_reference")
NEGATIVE_LABELS = ("discard", "delete", "reject", "needs_retrace")

#: Cold start: how many distinct human PICK observations (a starred/picked
#: candidate, counted once per job+candidate) and how many distinct jobs must be
#: in the log before a fit is allowed.  ``triage.load_triage(spec)`` can override
#: both (``learn_min_picks`` / ``learn_min_jobs``); ``learn_min_jobs`` defaults to
#: the documented 2, which is what "never fit within a single job" means
#: mechanically.
COLD_START_PICKS = 30
LEARN_MIN_JOBS = 2

#: Fit hyper-parameters.  Fixed, not tuned: a deterministic full-batch gradient
#: descent that converges on a few thousand pairs is the whole requirement.
FIT_ITERATIONS = 300
FIT_LEARNING_RATE = 0.5
FIT_L2 = 0.01

#: Holdout job fraction for the honest held-out pair accuracy.  0.0 means "fit on
#: everything"; the split exists so a caller CAN measure generalisation across
#: jobs, never so the model can be tuned per candidate.
HOLDOUT_JOB_FRACTION = 0.0

#: Number of leading positions ``rank`` treats as the visible window when the
#: caller does not say.  A reviewer looks at a handful of candidates first, and
#: that handful is what the anti-feedback rule has to keep honest.
RANK_VISIBLE_WINDOW = 8

#: Default number of non-greedy slots kept visible in an ordering.
DEFAULT_RANDOM_SLOTS = 1

#: How many of the pool candidates nearest the window cut are eligible for the
#: non-greedy slots.  "Random/uncertain": the draw is seeded and reproducible,
#: but it is drawn from the boundary cases rather than from the whole tail, so
#: the slots surface the candidates the score genuinely cannot separate.
UNCERTAIN_POOL_FACTOR = 4

#: Sentinel for an unmeasured hand axis.  "Not measured" is not "measured good",
#: so an unmeasured axis sorts behind every measured one in the hand order.
HAND_MISSING = 1e9

#: Weights of the hand key, used only to fold (hard, advisory, mae_art) into the
#: single float ``score`` returns under cold start.  They order, they never judge.
HAND_HARD_WEIGHT = 1e6
HAND_ADVISORY_WEIGHT = 1e3
HAND_MAE_CAP = 999.999

#: Keys of the Phase 5 bandit statistics carried beside the Phase 2 skeleton
#: fields in ``learned_buckets.json``: the beta-bernoulli counters.
BANDIT_FIELDS = ("successes", "attempts")

#: The non-counter fields beside them: when a bucket was last tried, and whether a
#: human retired it.  A fresh bucket is neither dated nor vetoed.
BUCKET_EXTRA_DEFAULTS = {"last_t": None, "veto": False}

#: Thompson-sampling parameters.
DEFAULT_EPSILON_EXPLORE = 0.1

#: A bucket whose last outcome is older than this is re-explored from the prior:
#: a stale preference must not pin the sweep for ever.  Kept as a parameter so a
#: job can shorten it; 30 days is the default.
BANDIT_TTL_SECONDS = 30 * 24 * 3600

#: Matches the Phase 0 bucket key: ``{preset}|s{speckle_bin}|c{colour_bin}|{family}``.
BUCKET_RE = re.compile(
    r"^(?P<preset>[^|]*)\|s(?P<speckles>\d+)\|c(?P<colours>\d+)\|(?P<family>.*)$")

#: Process-wide RNG for the bandit.  ``choose_bucket`` accepts an explicit
#: ``rng`` for a reproducible draw; the default is seeded by the OS, because a
#: bandit that explores the same way every run is not exploring.
_RNG = random.Random()

_MASK64 = (1 << 64) - 1


# --------------------------------------------------------------------------- #
# #triage-ranker-reading-candidates
# --------------------------------------------------------------------------- #
# One candidate shape, read by name and never guessed: the run-record candidate
# ``scripts/run_record.py`` assembles (``sweep`` / ``comparison`` blocks), plus
# the flat spellings an event line may carry.  Every reader returns None for a
# missing reading, and the feature builder substitutes the TRAINING MEAN for a
# missing value -- "not measured" is neutral in a weight vector, and it is
# explicitly not "measured perfect" (that conflation is the failure mode the
# gate rules are written to avoid, and it applies here too).


def _first_number(*values):
    """The first real number among ``values``, in the order given; else None.

    ``bool`` is rejected: it is an ``int`` in Python, and a True where a
    measurement belongs is a broken record, not a reading of 1.
    """
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        return float(value)
    return None


def _block(mapping, key):
    """A nested dict block, or an empty dict when absent/not a dict."""
    if not isinstance(mapping, dict):
        return {}
    block = mapping.get(key)
    return block if isinstance(block, dict) else {}


def view_of(entry):
    """The measurement view of an entry: the event/candidate dict, inner keys first.

    An entry may be a plain candidate record, an event line carrying its
    metrics under ``features``/``candidate``/``metrics``, or a candidate joined
    onto an event line.  The merged view reads either layout, with the inner
    (metric) mapping winning over the outer line.
    """
    if not isinstance(entry, dict):
        return {}
    for key in ("features", "candidate", "metrics"):
        inner = entry.get(key)
        if isinstance(inner, dict):
            merged = dict(entry)
            merged.update(inner)
            return merged
    return entry


def bucket_bins(bucket_key):
    """``(preset, speckle_bin, colour_bin, family)`` parsed out of a bucket key.

    Returns ``(None, None, None, None)`` for a key that is not the Phase 0 shape,
    so a caller holding an opaque key gets a missing reading rather than a wrong
    one.
    """
    if not isinstance(bucket_key, str):
        return (None, None, None, None)
    match = BUCKET_RE.match(bucket_key)
    if match is None:
        return (None, None, None, None)
    return (match.group("preset"),
            int(match.group("speckles")),
            int(match.group("colours")),
            match.group("family"))


def raw_values(entry):
    """The raw feature readings for one candidate or event line.

    Keys: the numeric features, plus the non-numeric ``preset``/``family``/
    ``bucket`` the one-hot and the bins are built from.  A reading that is not
    present is None -- never a zero that would read as a perfect measurement.
    """
    view = view_of(entry)
    comparison = _block(view, "comparison")
    metrics = _block(comparison, "metrics") or _block(view, "metrics")
    fidelity = _block(comparison, "fidelity")
    stats = _block(_block(_block(view, "back_half"), "manifest"), "stats")

    mae_art = None
    if fidelity.get("measured"):
        mae_art = _first_number(fidelity.get("mae_art"))
    if mae_art is None:
        mae_art = _first_number(metrics.get("mae_art"), comparison.get("mae_art"),
                               view.get("mae_art"))

    hard = _first_number(comparison.get("hard"),
                         _block(comparison, "layer_a").get("hard"),
                         _block(comparison, "layer_b").get("hard"),
                         view.get("hard"))
    advisory = _first_number(comparison.get("advisory"),
                             _block(comparison, "layer_a").get("advisory"),
                             _block(comparison, "layer_b").get("advisory"),
                             view.get("advisory"))
    node_count_max = _first_number(comparison.get("node_count_max"),
                                   metrics.get("node_count_max"),
                                   stats.get("node_count_max"),
                                   view.get("node_count_max"))
    colours = _first_number(comparison.get("declared_colors"),
                            comparison.get("rendered_ink_colors"),
                            metrics.get("colours"), view.get("colours"),
                            metrics.get("color_count"), stats.get("color_count"))
    speckles = _first_number(metrics.get("speckle_count"),
                             comparison.get("speckle_count"),
                             view.get("speckles"), view.get("speckle_count"))

    sweep = _block(view, "sweep")
    preset = sweep.get("preset") or view.get("preset")
    family = view.get("prompt_family") or view.get("family") or ""
    bucket = view.get("bucket")
    if not bucket and triage is not None:
        # triage-bucket-of: one spelling of the family key, from Phase 0.
        bucket = triage.bucket_of(preset or "",
                                  int(speckles) if speckles is not None else 0,
                                  int(colours) if colours is not None else 0,
                                  family)

    return {
        "hard": hard,
        "advisory": advisory,
        "node_count_max": node_count_max,
        "mae_art": mae_art,
        "colours": colours,
        "speckles": speckles,
        "preset": preset,
        "family": family,
        "bucket": bucket,
    }


def stable_key(entry):
    """A candidate's identity for sorting and tie-breaks: sha, else id/file, else "".

    The ordering has to be a total order that does not depend on the order the
    caller happened to hand the candidates over in, or "same input, same order"
    would be false for a permuted-but-identical input.
    """
    view = view_of(entry)
    for key in ("svg_sha", "svg_sha256", "output_sha256", "file_sha256", "sha256",
                "id", "file"):
        value = view.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def feature_names(presets=()):
    """The ordered feature names for a model that saw ``presets``.

    Bias first, then the numeric readings, then one indicator per preset.  The
    order IS the weight-vector order, so it is recorded in the model and every
    later dot product follows it rather than a dict's iteration order.
    """
    return ((BIAS_FEATURE,) + NUMERIC_FEATURES
            + tuple(PRESET_PREFIX + name for name in sorted(set(presets))))


def feature_vector(entry, names, means=None):
    """One candidate's feature vector, in ``names`` order.

    A missing reading is filled from ``means`` (the training mean, i.e. neutral
    in the standardised space) so an unmeasured candidate is not silently
    treated as a perfect one.  The bucket bins come from the bucket key, which
    means they are available even when the raw counts are not.
    """
    means = means or {}
    raw = raw_values(entry)
    preset, speckle_bin, colour_bin, _family = bucket_bins(raw.get("bucket"))
    lookup = dict(raw)
    lookup["bucket_speckle_bin"] = None if speckle_bin is None else float(speckle_bin)
    lookup["bucket_colour_bin"] = None if colour_bin is None else float(colour_bin)
    lookup["preset"] = raw.get("preset") or preset

    values = []
    for name in names:
        if name == BIAS_FEATURE:
            values.append(1.0)
        elif name.startswith(PRESET_PREFIX):
            wanted = name[len(PRESET_PREFIX):]
            values.append(1.0 if lookup.get("preset") == wanted else 0.0)
        else:
            value = lookup.get(name)
            values.append(means.get(name, 0.0) if value is None else float(value))
    return values


# --------------------------------------------------------------------------- #
# #triage-ranker-hand-order
# --------------------------------------------------------------------------- #
# The cold-start fallback, and the ONLY ordering the repo already sanctions:
# fewest hard gates failed, then fewest advisories, then artwork MAE (AGENTS.md:
# "never rank beyond 'fewest hard gates failed, then fewest advisories'").
# A missing reading sorts LAST, because "not measured" must never outrank
# "measured clean".


def hand_key(candidate):
    """``(hard, advisory, mae_art)`` with unmeasured axes sorting last."""
    raw = raw_values(candidate)
    hard = raw["hard"]
    advisory = raw["advisory"]
    mae = raw["mae_art"]
    if mae is not None:
        mae = min(max(mae, 0.0), HAND_MAE_CAP)
    return (HAND_MISSING if hard is None else hard,
            HAND_MISSING if advisory is None else advisory,
            HAND_MISSING if mae is None else mae)


def hand_scalar(candidate):
    """The hand key folded into one float (lower = better).  Ordering only."""
    hard, advisory, mae = hand_key(candidate)
    return (hard * HAND_HARD_WEIGHT + advisory * HAND_ADVISORY_WEIGHT + mae)


# --------------------------------------------------------------------------- #
# #triage-ranker-observe-events
# --------------------------------------------------------------------------- #
# The training signal.  The log records exposure (``shown`` + 1-based pos) and,
# once a studio or a human writes one, an action on the same (job, svg_sha).
# Everything is read tolerantly: an action arrives as an event name, a boolean
# field or a label, whichever the writer had available.


def _read_jsonl(path):
    """Every JSON object on the file, in file order.  Skips junk, never raises."""
    out = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    out.append(payload)
    except OSError:
        return []
    return out


def _event_dicts(events):
    """Normalise the ``events`` argument to a list of event dicts.

    Accepts None (read the learning directory), a path to a JSONL file or to a
    learning directory, or an iterable of already-parsed lines.
    """
    if events is None:
        return triage_events.read_events()
    if isinstance(events, (str, os.PathLike)):
        target = str(events)
        if os.path.isdir(target):
            return triage_events.read_events(target)
        return _read_jsonl(target)
    return [event for event in events if isinstance(event, dict)]


def action_of(entry):
    """The human action on one line: +1 acted on, -1 rejected, 0 exposure only.

    Reads, in order: the ``event`` name, an explicit ``action``/``signal``
    number, a boolean field, then the ``label`` text a pick.json carries.  An
    unrecognised line is exposure (0), never a guessed label.
    """
    if not isinstance(entry, dict):
        return 0
    event = entry.get("event")
    if isinstance(event, str):
        name = event.strip().lower()
        if name in POSITIVE_EVENTS:
            return 1
        if name in NEGATIVE_EVENTS:
            return -1
    for key in ("action", "signal"):
        value = entry.get(key)
        if isinstance(value, bool):
            return 1 if value else -1
        if isinstance(value, (int, float)):
            if value > 0:
                return 1
            if value < 0:
                return -1
    for key in POSITIVE_FIELDS:
        if entry.get(key) is True:
            return 1
    for key in NEGATIVE_FIELDS:
        if entry.get(key) is True:
            return -1
    label = entry.get("label") or entry.get("decision")
    if isinstance(label, str):
        name = label.strip().lower()
        if name in POSITIVE_LABELS:
            return 1
        if name in NEGATIVE_LABELS:
            return -1
    return 0


def _line_sort_key(entry):
    """A total, input-order-independent key for one event line."""
    value = entry.get("pos")
    pos = value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
    return (str(entry.get("t") or ""), str(entry.get("job") or ""),
            str(entry.get("svg_sha") or ""), str(entry.get("event") or ""), pos)


def observe_events(events=None, candidates=None):
    """Fold the event log into one observation per ``(job, svg_sha)``.

    ``events`` is None (the learning directory), a path, or parsed lines.
    ``candidates`` is an optional iterable of candidate records: the join that
    supplies the metrics a Phase 2 ``shown`` line does not carry yet.  The join
    is by sha, and the candidate record wins over the event line's own fields
    (``observation_entry`` merges them in that order).

    Returns ``{(job, sha): observation}`` where each observation holds the
    exposure (``shown``/``pos``), the action (-1/0/+1), the last timestamp, the
    event lines and the joined candidate.  A line with no sha is skipped: it
    cannot be joined to a job and a candidate, and guessing an identity is how
    one job's preference leaks into another's.
    """
    joined = {}
    for candidate in candidates or ():
        key = stable_key(candidate)
        if key:
            joined[key] = candidate

    observations = {}
    for line in sorted(_event_dicts(events), key=_line_sort_key):
        sha = line.get("svg_sha")
        if not isinstance(sha, str) or not sha:
            continue
        job = str(line.get("job") or "")
        record = observations.get((job, sha))
        if record is None:
            record = {"job": job, "svg_sha": sha, "shown": False, "pos": None,
                      "action": 0, "t": None, "sources": [],
                      "joined": joined.get(sha)}
            observations[(job, sha)] = record
        record["sources"].append(line)
        record["t"] = str(line.get("t") or record["t"] or "")
        if line.get("event") == triage_events.EVENT_SHOWN:
            record["shown"] = True
            value = line.get("pos")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                record["pos"] = int(value)
        elif line.get("pos") is not None and record["pos"] is None:
            # An action line carries the 1-based pos too (one scale, both sides).
            value = line.get("pos")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                record["pos"] = int(value)
        action = action_of(line)
        if action > 0:
            record["action"] = 1
        elif action < 0 and record["action"] == 0:
            record["action"] = -1
    return observations


def _observations(events, candidates=None):
    """``observe_events``, accepting an already-folded observation mapping."""
    if isinstance(events, dict) and all(
            isinstance(key, tuple) for key in events):
        return events
    return observe_events(events, candidates=candidates)


def observation_entry(observation):
    """The entry one observation's features are read from.

    The event lines are merged in file order (a later action line may carry a
    reading the exposure line did not), and the joined candidate record wins over
    all of them -- the run record is the measurement, the line is the event.
    """
    entry = {}
    for line in observation.get("sources") or ():
        if isinstance(line, dict):
            entry.update(line)
    joined = observation.get("joined")
    if isinstance(joined, dict):
        entry.update(joined)
    return entry


def job_pairs(observations):
    """Preference pairs, formed WITHIN each job, in sorted order.

    A pair is ``(acted-on, not-acted-on)``: the positive is a candidate the human
    starred or picked, the negative is one shown in the same job that was not
    acted on (or was explicitly rejected).  Only candidates inside the same
    exposure set are ever compared -- a preference across two jobs would be
    comparing two different asks.
    """
    pairs = []
    for job in sorted({record["job"] for record in observations.values()}):
        positives = []
        negatives = []
        for key in sorted(observations):
            record = observations[key]
            if record["job"] != job:
                continue
            if record["action"] > 0:
                positives.append(record)
            elif record["action"] < 0 or record["shown"]:
                negatives.append(record)
        for positive in positives:
            for negative in negatives:
                pairs.append((positive, negative))
    return pairs


def job_split(jobs, holdout_fraction=HOLDOUT_JOB_FRACTION):
    """Split job stems into ``(fit_jobs, holdout_jobs)`` -- by JOB, never candidate.

    Sorted order, the last ``ceil(fraction * n)`` stems held out: reproducible,
    and it cannot put two candidates of one job on opposite sides of the fence
    (which would let the model memorise a job and score it as generalisation).
    """
    ordered = sorted({str(job) for job in jobs})
    fraction = max(0.0, min(1.0, float(holdout_fraction)))
    if not ordered or fraction <= 0.0:
        return ordered, []
    count = int(math.ceil(fraction * len(ordered)))
    if count >= len(ordered):
        count = len(ordered) - 1
    return ordered[:len(ordered) - count], ordered[len(ordered) - count:]


# --------------------------------------------------------------------------- #
# #triage-ranker-learn
# --------------------------------------------------------------------------- #


def _mean_scale(values):
    """Population mean and standard deviation of the non-None values."""
    present = [value for value in values if value is not None]
    if not present:
        return 0.0, 1.0
    mean = sum(present) / len(present)
    variance = sum((value - mean) ** 2 for value in present) / len(present)
    scale = variance ** 0.5
    if scale <= 1e-9:
        return mean, 1.0
    return mean, scale


def _standardise(values, means, scales):
    """``(value - mean) / scale`` per element; a neutral-filled vector stays ordered."""
    return [(value - means[index]) / scales[index] for index, value in enumerate(values)]


def _sigmoid(value):
    """Numerically safe logistic function (never overflows on a large |value|)."""
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exp_value = math.exp(value)
    return exp_value / (1.0 + exp_value)


def _fit_pairwise(vectors, names, iterations=FIT_ITERATIONS,
                  learning_rate=FIT_LEARNING_RATE, l2=FIT_L2):
    """Full-batch pairwise logistic fit: the positive must outrank the negative.

    Loss per pair is ``log(1 + exp(-(s_pos - s_neg)))``, so the gradient of the
    pair's score difference is ``-sigmoid(-(s_pos - s_neg))`` times the feature
    difference.  Ridge on every weight except the intercept.  Deterministic:
    fixed iteration count, fixed step, pairs in a fixed order, no shuffling and
    no randomness anywhere.
    """
    weights = {name: 0.0 for name in names}
    if not vectors:
        return weights
    count = len(vectors)
    for _ in range(iterations):
        gradient = [0.0] * len(names)
        for positive, negative in vectors:
            difference = [p - n for p, n in zip(positive, negative)]
            margin = sum(weights[name] * delta
                         for name, delta in zip(names, difference))
            scale = -_sigmoid(-margin)
            for index, delta in enumerate(difference):
                gradient[index] += scale * delta
        inverse = 1.0 / count
        for index, name in enumerate(names):
            step = gradient[index] * inverse
            if name != BIAS_FEATURE:
                step += l2 * weights[name]
            weights[name] -= learning_rate * step
    return weights


def _pair_accuracy(vectors, names, weights, means, scales):
    """Fraction of held-out pairs the model orders correctly (ties count 0.5).

    ``means``/``scales`` are the per-feature lists in ``names`` order -- the same
    standardisation the fit used, applied to vectors the fit never saw.
    """
    if not vectors:
        return None
    hits = 0.0
    for positive, negative in vectors:
        left = _dot(_standardise(positive, means, scales), names, weights)
        right = _dot(_standardise(negative, means, scales), names, weights)
        if left > right:
            hits += 1.0
        elif left == right:
            hits += 0.5
    return hits / len(vectors)


def _dot(values, names, weights):
    """The weighted sum in ``names`` order -- the one place the score is formed."""
    return sum(weights.get(name, 0.0) * value
               for name, value in zip(names, values))


def cold_model(reason, picks=0, jobs=0, cold_start_picks=COLD_START_PICKS,
               min_jobs=LEARN_MIN_JOBS, events=0):
    """A cold-start model document: the hand order, and why it is still in use."""
    return {
        "version": RANKER_VERSION,
        "kind": KIND_COLD_START,
        "reason": reason,
        "direction": SCORE_DIRECTION,
        "features": [],
        "weights": {},
        "means": {},
        "scales": {},
        "presets": [],
        "picks": picks,
        "jobs": jobs,
        "events": events,
        "cold_start_picks": cold_start_picks,
        "min_jobs": min_jobs,
        "generated": _now(),
    }


def learn_weights(events=None, candidates=None, spec=None, cold_start_picks=None,
                  min_jobs=None, holdout_fraction=HOLDOUT_JOB_FRACTION,
                  iterations=FIT_ITERATIONS, learning_rate=FIT_LEARNING_RATE,
                  l2=FIT_L2):
    """Fit the pairwise preference model from the event log.

    ``events`` is None (the job-independent learning directory), a path, or
    parsed event lines; ``candidates`` is the optional sha-keyed join that
    supplies the metrics the log does not carry yet.  Returns a model dict (see
    ``cold_model`` and the pairwise branch below) -- never raises.

    Cold start: fewer than ``cold_start_picks`` distinct human picks, or fewer
    than ``min_jobs`` distinct jobs carrying any signal, or no comparable pair at
    all, yields the hand order.  ``spec`` is a parsed spec (or a candidate
    carrying a ``triage`` block): its ``learn_min_picks``/``learn_min_jobs``
    override these.
    """
    observations = _observations(events, candidates=candidates)
    thresholds = triage.load_triage(spec) if triage is not None else {}
    need_picks = cold_start_picks
    if need_picks is None:
        need_picks = thresholds.get("learn_min_picks", COLD_START_PICKS)
    need_jobs = min_jobs
    if need_jobs is None:
        need_jobs = thresholds.get("learn_min_jobs", LEARN_MIN_JOBS)

    picks = sum(1 for record in observations.values() if record["action"] > 0)
    signal_jobs = sorted({record["job"] for record in observations.values()
                          if record["action"] != 0})
    pairs = job_pairs(observations)

    if not observations:
        return cold_model("no events", cold_start_picks=need_picks,
                          min_jobs=need_jobs)
    if picks < need_picks:
        return cold_model("picks %d < %d" % (picks, need_picks), picks=picks,
                          jobs=len(signal_jobs), cold_start_picks=need_picks,
                          min_jobs=need_jobs, events=len(observations))
    if len(signal_jobs) < need_jobs:
        return cold_model("jobs with signal %d < %d" % (len(signal_jobs), need_jobs),
                          picks=picks, jobs=len(signal_jobs),
                          cold_start_picks=need_picks, min_jobs=need_jobs,
                          events=len(observations))
    if not pairs:
        return cold_model("no comparable pair", picks=picks,
                          jobs=len(signal_jobs), cold_start_picks=need_picks,
                          min_jobs=need_jobs, events=len(observations))

    # Split by JOB: a job's candidates are all on one side of the fence.
    fit_jobs, holdout_jobs = job_split(signal_jobs, holdout_fraction)
    holdout_set = set(holdout_jobs)
    fit_pairs = [(p, n) for p, n in pairs if p["job"] not in holdout_set]
    held_pairs = [(p, n) for p, n in pairs if p["job"] in holdout_set]
    if not fit_pairs:
        fit_pairs, held_pairs = pairs, []

    # Standardisation from the fit pairs only -- a held-out job must not leak
    # its scale into the fit any more than its preference.
    fit_entries = []
    seen = set()
    for positive, negative in fit_pairs:
        for record in (positive, negative):
            key = (record["job"], record["svg_sha"])
            if key not in seen:
                seen.add(key)
                fit_entries.append(record)
    presets = sorted({raw["preset"] for raw in
                      (raw_values(observation_entry(record)) for record in fit_entries)
                      if raw["preset"]})
    names = feature_names(presets)

    # Means/scales come from the readings that are actually present, so a missing
    # measurement is excluded from the mean instead of poisoning it with a zero.
    readings = {name: [] for name in NUMERIC_FEATURES}
    for record in fit_entries:
        entry = observation_entry(record)
        for name in NUMERIC_FEATURES:
            readings[name].append(_raw_or_none(entry, name))
    means = {}
    scales = {}
    for name in NUMERIC_FEATURES:
        means[name], scales[name] = _mean_scale(readings[name])
    for name in names:
        means.setdefault(name, 0.0)
        scales.setdefault(name, 1.0)
    means_list = [means[name] for name in names]
    scales_list = [scales[name] for name in names]

    fit_vectors = []
    for positive, negative in fit_pairs:
        left = feature_vector(observation_entry(positive), names, means=means)
        right = feature_vector(observation_entry(negative), names, means=means)
        fit_vectors.append((_standardise(left, means_list, scales_list),
                            _standardise(right, means_list, scales_list)))
    held_vectors = []
    for positive, negative in held_pairs:
        held_vectors.append(
            (feature_vector(observation_entry(positive), names, means=means),
             feature_vector(observation_entry(negative), names, means=means)))

    weights = _fit_pairwise(fit_vectors, names, iterations=iterations,
                            learning_rate=learning_rate, l2=l2)
    # triage-ranker-learn: the model is the memory; the JSON below is its wire form.
    return {
        "version": RANKER_VERSION,
        "kind": KIND_PAIRWISE,
        "reason": "trained",
        "direction": SCORE_DIRECTION,
        "features": list(names),
        "weights": {name: weights[name] for name in names},
        "means": {name: means[name] for name in names},
        "scales": {name: scales[name] for name in names},
        "presets": presets,
        "picks": picks,
        "jobs": len(signal_jobs),
        "events": len(observations),
        "cold_start_picks": need_picks,
        "min_jobs": need_jobs,
        "fit_jobs": len(fit_jobs),
        "holdout_jobs": len(holdout_jobs),
        "fit_pairs": len(fit_vectors),
        "holdout_pairs": len(held_vectors),
        "holdout_accuracy": _pair_accuracy(held_vectors, names, weights,
                                           means_list, scales_list),
        "iterations": iterations,
        "learning_rate": learning_rate,
        "l2": l2,
        "generated": _now(),
    }


def _raw_or_none(entry, name):
    """The raw numeric reading for one feature name, or None when unmeasured.

    Used for the training means: a reading that is absent must be excluded from
    the mean rather than contributing a zero, or an unmeasured candidate would
    drag the scale.
    """
    raw = raw_values(entry)
    value = raw.get(name)
    if name in NUMERIC_FEATURES and value is not None:
        return float(value)
    if name == "bucket_speckle_bin":
        bucket_bin = bucket_bins(raw.get("bucket"))[1]
        return None if bucket_bin is None else float(bucket_bin)
    if name == "bucket_colour_bin":
        bucket_bin = bucket_bins(raw.get("bucket"))[2]
        return None if bucket_bin is None else float(bucket_bin)
    return None


# --------------------------------------------------------------------------- #
# #triage-ranker-score
# --------------------------------------------------------------------------- #


def score(candidate, model=None):
    """The preference score of one candidate: HIGHER means more preferred.

    With a fitted model, the score is the standardised weighted sum of the
    candidate's features, in the model's recorded feature order, with a missing
    reading filled from the model's training mean (neutral).  Without one -- a
    cold-start model, a missing model, a model document with no weights -- the
    score is the negated ``hand_scalar``, so a plain descending sort reproduces
    the hand order (fewest hard, then advisories, then artwork MAE).

    This is a preference value, not a verdict: it is never a reason to hide,
    delete or auto-select a candidate.
    """
    if not isinstance(model, dict) or model.get("kind") != KIND_PAIRWISE:
        return -hand_scalar(candidate)
    names = model.get("features")
    weights = model.get("weights")
    if not names or not isinstance(weights, dict):
        return -hand_scalar(candidate)
    means = model.get("means") if isinstance(model.get("means"), dict) else {}
    scales = model.get("scales") if isinstance(model.get("scales"), dict) else {}
    values = feature_vector(candidate, names, means=means)
    total = 0.0
    for name, value in zip(names, values):
        scale = scales.get(name) or 1.0
        total += weights.get(name, 0.0) * ((value - means.get(name, 0.0)) / scale)
    return total


def _order_key(entry, model):
    """The total ordering key: score desc, then the hand key, then identity."""
    return (-score(entry, model),) + hand_key(entry) + (stable_key(entry),)


def _digest(values):
    """A sha256 int over the given strings, joined in the order given.

    ENCODED utf-8 because it is a digest (bytes are the point), not a length:
    no measurement in this module is ever taken in bytes.
    """
    joined = "\x1f".join(str(value) for value in values)
    return int(hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16], 16)


def _mix64(value):
    """splitmix64 -- a tiny deterministic mixer; the same seed gives the same draw.

    Hand-rolled rather than ``random.Random`` so the draw cannot shift under a
    Python version that changes its generator: "same input, same order" has to
    hold across upgrades, not just across runs.
    """
    value = (value + 0x9E3779B97F4A7C15) & _MASK64
    mixed = value
    mixed = ((mixed ^ (mixed >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
    mixed = ((mixed ^ (mixed >> 27)) * 0x94D049BB133111EB) & _MASK64
    return (mixed ^ (mixed >> 31)) & _MASK64


def rank(candidates, model=None, n_random_slots=DEFAULT_RANDOM_SLOTS, window=None):
    """Order ``candidates`` best-first, keeping ``n_random_slots`` non-greedy slots.

    The return value is a permutation of the SAME candidates -- nothing is
    dropped, filtered or hidden, and the human still chooses.  The first
    ``window`` positions are the visible window (default: a reviewer's first
    handful, ``RANK_VISIBLE_WINDOW``; the whole list when it is shorter).  Within
    that window the last ``n_random_slots`` positions are reserved for
    candidates the score would have pushed OUTSIDE it: drawn from the
    ``UNCERTAIN_POOL_FACTOR`` pool candidates nearest the cut, in a seeded,
    reproducible order.  The displaced candidates are not demoted past anyone
    else -- they re-join the order at their score position below the window.

    Why: a ranker trained on what the pipeline showed learns the order it
    already produces, and then only ever shows itself its own opinion.  Keeping
    a couple of boundary candidates visible on every presentation is what breaks
    that loop.  With nothing outside the window there is nothing to surface, so
    the greedy order is returned unchanged.

    Deterministic: the same candidates and model give the same order, in any
    input order (the draw is seeded from the sorted identity of the set).
    """
    items = list(candidates)
    if not items:
        return []
    keys = [_order_key(entry, model) for entry in items]
    greedy = sorted(range(len(items)), key=lambda index: keys[index])
    slots = max(0, int(n_random_slots))
    size = len(items)
    visible = (min(size, RANK_VISIBLE_WINDOW) if window is None
               else max(0, min(int(window), size)))
    if slots <= 0 or visible <= 1 or size <= visible:
        return [items[index] for index in greedy]
    slots = min(slots, visible - 1)

    head = greedy[:visible]
    tail = greedy[visible:]
    cut = keys[head[-1]][0]
    pool = sorted(tail, key=lambda index: (abs(keys[index][0] - cut), keys[index]))
    draw_pool = pool[:max(slots, slots * UNCERTAIN_POOL_FACTOR)]
    if not draw_pool:
        return [items[index] for index in greedy]

    seed = _digest([stable_key(items[index]) for index in greedy]
                   + [str(slots), str(visible)])
    drawn = []
    remaining = list(draw_pool)
    state = seed
    for _ in range(slots):
        state = _mix64(state)
        drawn.append(remaining.pop(state % len(remaining)))
    drawn_set = set(drawn)

    kept = head[:visible - slots]
    displaced = head[visible - slots:]
    rest = sorted([index for index in tail if index not in drawn_set] + displaced,
                  key=lambda index: keys[index])
    return [items[index] for index in kept + drawn + rest]


# --------------------------------------------------------------------------- #
# #triage-ranker-store
# --------------------------------------------------------------------------- #


def ranker_model_path(path=None):
    """Path of ``learned_ranker.json``: the learning dir, or an explicit path."""
    if path is None:
        return os.path.join(triage_events.learning_dir(), RANKER_FILENAME)
    target = str(path)
    if target.endswith(".json"):
        return target
    return os.path.join(target, RANKER_FILENAME)


def save_ranker_model(model, path=None):
    """Write the model JSON beside the event log.  Returns the path, or None.

    Atomic (same-directory temp + ``os.replace``) so a reader never sees half a
    model, and non-fatal: an unwritable store degrades to cold start, it never
    fails the stage that wanted an ordering.
    """
    target = ranker_model_path(path)
    directory = os.path.dirname(target)
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        return None
    tmp = "%s.tmp.%d" % (target, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(model, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, target)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return None
    return target


def load_ranker_model(path=None):
    """Read the model JSON; None when absent, unreadable or not a model object.

    None is a valid answer: ``score`` treats it as cold start, so a missing
    model is a fallback rather than an error.
    """
    target = ranker_model_path(path)
    try:
        with open(target, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or "kind" not in payload:
        return None
    return payload


# --------------------------------------------------------------------------- #
# #triage-ranker-bandit
# --------------------------------------------------------------------------- #
# Phase 5: a beta-bernoulli bandit over BUCKET KEYS.  "Success" is whatever the
# caller decides an outcome means (a candidate in that family survived the gate,
# or a human picked one) -- this module records the count and samples the
# posterior; it never decides what success is and never picks a candidate.
#
# Two guards keep it from pinning the sweep:
#   * EXPIRY -- statistics older than the TTL are ignored for the decision, so a
#     bucket re-enters exploration from its prior.
#   * VETO -- a bucket a human has retired is never chosen, whatever it shows.


def default_bucket_stats():
    """A fresh bucket record: the Phase 2 skeleton fields plus the bandit's.

    Additive over ``triage_events.empty_bucket_stats()``, so the file keeps its
    version 1 and a reader written against the skeleton still reads it.
    """
    record = triage_events.empty_bucket_stats()
    for name in BANDIT_FIELDS:
        record[name] = 0
    record.update(BUCKET_EXTRA_DEFAULTS)
    return record


def normalise_bucket_stats(record):
    """Fill in any missing field of one bucket record, keeping unknown keys."""
    merged = default_bucket_stats()
    if isinstance(record, dict):
        merged.update(record)
    return merged


def _records(stats):
    """The ``bucket_key -> record`` mapping of a loaded store, created if absent."""
    if not isinstance(stats, dict):
        stats = {}
    stats.setdefault("version", triage_events.LEARNED_BUCKETS_VERSION)
    records = stats.get("buckets")
    if not isinstance(records, dict):
        records = {}
        stats["buckets"] = records
    return records


def bucket_stats(stats, bucket):
    """The (normalised) stats record for one bucket, without creating it."""
    return normalise_bucket_stats(_records(stats).get(bucket))


def set_veto(stats, bucket, veto=True):
    """Record (or lift) the human veto on a bucket.  Returns the record."""
    records = _records(stats)
    record = normalise_bucket_stats(records.get(bucket))
    record["veto"] = bool(veto)
    records[bucket] = record
    return record


def vetoed_buckets(stats):
    """Sorted bucket keys carrying a veto."""
    return sorted(bucket for bucket, record in _records(stats).items()
                  if normalise_bucket_stats(record).get("veto"))


def expired(stats, bucket, now=None, ttl_seconds=None):
    """True when a bucket's statistics are too old to trust (or unreadable).

    An absent ``last_t`` is NOT expired: that is a bucket with no attempts, i.e.
    the prior, which is already maximal exploration.  A ``last_t`` that cannot be
    parsed IS expired -- be conservative and re-explore rather than act on a
    statistic nobody can date.
    """
    record = bucket_stats(stats, bucket)
    stamp = record.get("last_t")
    if not stamp:
        return False
    moment = _as_datetime(stamp)
    if moment is None:
        return True
    ttl = BANDIT_TTL_SECONDS if ttl_seconds is None else float(ttl_seconds)
    return (now or _now_dt()) - moment > timedelta(seconds=max(0.0, ttl))


def record_outcome(bucket, success, stats, t=None):
    """Beta update: one attempt, one success when ``success``.  Returns the record.

    ``attempts``/``successes`` are the bandit's counts; the Phase 2 ``shown``/
    ``chosen``/``rejected`` fields are the human-rejection counters and are
    deliberately untouched here -- a trace attempt and a human choice are
    different events and merging them would make the collapse rule lie.
    """
    records = _records(stats)
    record = normalise_bucket_stats(records.get(bucket))
    record["attempts"] = int(record.get("attempts") or 0) + 1
    if success:
        record["successes"] = int(record.get("successes") or 0) + 1
    record["last_t"] = t or _now()
    records[bucket] = record
    return record


def beta_sample(rng, successes, attempts):
    """One draw from Beta(1 + successes, 1 + attempts - successes).

    Sampled as ``g1 / (g1 + g2)`` with two Gamma draws (``gammavariate`` is
    stdlib, numpy is not available here by policy).  The +1 prior keeps an
    untouched bucket fully explorable and keeps both shape parameters positive,
    which ``gammavariate`` requires.
    """
    successes = max(0, int(successes))
    failures = max(0, int(attempts) - successes)
    left = rng.gammavariate(1.0 + successes, 1.0)
    right = rng.gammavariate(1.0 + failures, 1.0)
    total = left + right
    if total <= 0:
        return 0.5
    return left / total


def _select(buckets, stats, rng=None, epsilon=None, ttl_seconds=None,
            veto=None, now=None):
    """The bandit's decision, in its explainable form.  See ``choose_bucket``."""
    rng = rng or _RNG
    epsilon = DEFAULT_EPSILON_EXPLORE if epsilon is None else max(0.0, min(1.0, float(epsilon)))
    eligible = sorted({str(bucket) for bucket in buckets if bucket})
    vetoed = set(vetoed_buckets(stats))
    vetoed.update(str(bucket) for bucket in (veto or ()))
    live = [bucket for bucket in eligible if bucket not in vetoed]

    decision = {"bucket": None, "mode": "none", "reason": "",
                "eligible": live, "vetoed": sorted(vetoed),
                "expired": [], "sampled": {}}
    if not eligible:
        decision["reason"] = "no buckets given"
        return decision
    if not live:
        decision["reason"] = "every bucket is vetoed"
        return decision

    sampled = {}
    stale = []
    for bucket in live:
        record = bucket_stats(stats, bucket)
        if expired(stats, bucket, now=now, ttl_seconds=ttl_seconds):
            stale.append(bucket)
            # triage-ranker-bandit: a stale bucket re-enters exploration from the prior.
            sampled[bucket] = beta_sample(rng, 0, 0)
            continue
        sampled[bucket] = beta_sample(rng, record.get("successes", 0),
                                      record.get("attempts", 0))
    decision["sampled"] = sampled
    decision["expired"] = stale

    if epsilon > 0.0 and rng.random() < epsilon:
        decision["bucket"] = live[rng.randrange(len(live))]
        decision["mode"] = "explore"
        decision["reason"] = "epsilon %.2f explore draw" % epsilon
        return decision

    best = None
    best_value = None
    for bucket in live:  # sorted order, strict '>' -- ties keep the first
        value = sampled[bucket]
        if best is None or value > best_value:
            best, best_value = bucket, value
    decision["bucket"] = best
    decision["mode"] = "exploit"
    if stale and best in stale:
        decision["reason"] = "thompson sample; expired statistics re-explored from the prior"
    elif all(bucket_stats(stats, bucket)["attempts"] == 0 for bucket in live):
        decision["reason"] = "thompson sample over the prior (no outcome recorded yet)"
    else:
        decision["reason"] = "thompson sample over %d live buckets" % len(live)
    return decision


def bucket_decision(buckets, stats, rng=None, epsilon=None, ttl_seconds=None,
                    veto=None, now=None):
    """Which bucket to try next, with the reason and the sampled thetas.

    This is a choice of PARAMETER FAMILY, never of candidate and never of
    winner: the human still picks the candidate.
    """
    return _select(buckets, stats, rng=rng, epsilon=epsilon,
                   ttl_seconds=ttl_seconds, veto=veto, now=now)


def choose_bucket(buckets, stats, rng=None, epsilon=None, ttl_seconds=None,
                  veto=None, now=None):
    """The bucket key to try next, or None when every bucket is vetoed/absent.

    Thompson sampling over Beta(1 + successes, 1 + attempts - successes), with an
    epsilon explore draw, expired statistics re-explored from the prior, and the
    veto list excluded.  Passing a seeded ``rng`` makes a draw reproducible; the
    default is OS-seeded, because a bandit that explores identically every run is
    not exploring.
    """
    return _select(buckets, stats, rng=rng, epsilon=epsilon,
                   ttl_seconds=ttl_seconds, veto=veto, now=now)["bucket"]


def load_bucket_stats(path=None):
    """Read ``learned_buckets.json``, normalised.  Never raises.

    Round-trips through ``triage_events.load_learned_buckets`` -- one file, one
    location, one reader for both the Phase 2 counters and the Phase 5 bandit.
    """
    stats = triage_events.load_learned_buckets(path)
    records = stats.get("buckets")
    if isinstance(records, dict):
        stats["buckets"] = {key: normalise_bucket_stats(records[key])
                            for key in sorted(records)}
    return stats


def save_bucket_stats(stats, path=None):
    """Write the store back; returns the path written, or None.  Never raises."""
    return triage_events.save_learned_buckets(stats, path)


# --------------------------------------------------------------------------- #
# Small time helpers (shared by the model stamp and the bandit expiry)          #
# --------------------------------------------------------------------------- #


def _now_dt():
    """Now, in UTC."""
    return datetime.now(timezone.utc)


def _now():
    """ISO-8601 UTC, second resolution -- the store's timestamp spelling."""
    return _now_dt().isoformat(timespec="seconds")


def _as_datetime(value):
    """Parse a timestamp (ISO string or datetime) into an aware UTC datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return None


# --------------------------------------------------------------------------- #
# Self-check                                                                   #
# --------------------------------------------------------------------------- #


def _selftest():
    """Deterministic smoke check; no test framework needed."""
    strong = {"svg_sha": "a" * 64,
              "comparison": {"hard": 0, "advisory": 0,
                             "fidelity": {"measured": True, "mae_art": 0.2}},
              "sweep": {"preset": "poster"}}
    weak = {"svg_sha": "b" * 64,
            "comparison": {"hard": 1, "advisory": 1,
                           "fidelity": {"measured": True, "mae_art": 3.0}},
            "sweep": {"preset": "bw"}}

    assert hand_key(strong) < hand_key(weak), "hand order unstable"
    cold = cold_model("selftest")
    assert [r["svg_sha"] for r in rank([weak, strong], cold)] == ["a" * 64, "b" * 64]
    assert score(strong, cold) > score(weak, cold)

    # A starred candidate with the SAME hand metrics but the metric the human
    # actually cared about must be learnable.
    events = []
    for job in range(4):
        for index in range(3):
            sha = "%d%d" % (job, index)
            events.append({"t": "2026-01-0%dT00:00:0%d" % (job + 1, index),
                           "job": "job-%d" % job, "svg_sha": sha,
                           "event": "shown", "pos": index + 1, "ranker": None,
                           "features": {"hard": 0, "advisory": 0,
                                        "mae_art": 0.5, "node_count_max": 100,
                                        "speckles": 3, "colours": 6 if index == 0 else 2,
                                        "preset": "poster"}})
            if index == 0:
                events.append({"t": "2026-01-0%dT00:01:0%d" % (job + 1, index),
                               "job": "job-%d" % job, "svg_sha": sha,
                               "event": "star", "pos": 1, "ranker": None})
    model = learn_weights(events, cold_start_picks=4)
    assert model["kind"] == KIND_PAIRWISE, model.get("reason")
    assert model["weights"].get("colours", 0.0) > 0, model["weights"]
    starred = {"svg_sha": "00", "features": {"hard": 0, "advisory": 0, "mae_art": 0.5,
                                             "node_count_max": 100, "speckles": 3,
                                             "colours": 6, "preset": "poster"}}
    other = {"svg_sha": "01", "features": {"hard": 0, "advisory": 0, "mae_art": 0.5,
                                           "node_count_max": 100, "speckles": 3,
                                           "colours": 2, "preset": "poster"}}
    assert score(starred, model) > score(other, model), "starred did not outrank"
    assert rank([other, starred], model)[0]["svg_sha"] == "00"
    assert rank([other, starred], model) == rank([starred, other], model), "order not input-stable"

    # Non-greedy slots stay visible.
    many = [{"svg_sha": "%02d" % i, "hard": i} for i in range(12)]
    order = rank(many, cold, n_random_slots=2, window=5)
    assert sorted(entry["svg_sha"] for entry in order) == sorted(e["svg_sha"] for e in many)
    greedy = [entry["svg_sha"] for entry in rank(many, cold, n_random_slots=0, window=5)]
    assert len(set(e["svg_sha"] for e in order[:5]) - set(greedy[:5])) >= 2, "no non-greedy slot"

    # Bandit: veto honoured, beta update recorded.
    stats = load_bucket_stats()
    record_outcome("poster|s1|c2|", True, stats)
    record_outcome("poster|s1|c2|", False, stats)
    assert bucket_stats(stats, "poster|s1|c2|")["attempts"] == 2
    assert bucket_stats(stats, "poster|s1|c2|")["successes"] == 1
    set_veto(stats, "bw|s0|c0|", True)
    picks = {choose_bucket(["poster|s1|c2|", "bw|s0|c0|"], stats)
             for _ in range(30)}
    assert "bw|s0|c0|" not in picks, "vetoed bucket chosen"
    print("triage_ranker selftest OK")
    return 0


def _main(argv):
    """``--selftest``, ``train`` (fit + persist), ``choose`` (one bandit draw)."""
    if "--selftest" in argv:
        return _selftest()
    if "train" in argv:
        model = learn_weights()
        path = save_ranker_model(model)
        print("kind: %s (%s)" % (model["kind"], model.get("reason")))
        print("picks: %s  jobs: %s" % (model.get("picks"), model.get("jobs")))
        print("model: %s" % (path or "not written"))
        return 0 if path else 1
    if "choose" in argv:
        buckets = [arg for arg in argv[1:] if "|" in arg]
        decision = bucket_decision(buckets, load_bucket_stats())
        print(json.dumps(decision, indent=2, sort_keys=True))
        return 0 if decision["bucket"] else 1
    print("triage_ranker.py -- Phase 4 ranker + Phase 5 bucket bandit")
    print("  model   : %s" % ranker_model_path())
    print("  events  : %s" % triage_events.events_path())
    print("  buckets : %s" % triage_events.learned_buckets_path())
    print("  commands: --selftest | train | choose <bucket|bucket|...>")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised manually
    raise SystemExit(_main(sys.argv[1:]))
