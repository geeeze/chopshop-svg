#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for scripts/triage_ranker.py (Phase 4 ranker + Phase 5 bucket bandit).

The properties worth pinning here are the ones that stay silent when they break:

* **The ranker learns a PREFERENCE, not a metric.**  A candidate the human acted
  on has to outrank one that was shown and never acted on, even when their
  hand-order metrics are identical -- otherwise the fit is re-deriving the
  ordering the pipeline already produced and the learner is decoration.
* **Cold start is the hand order.**  Until enough picks exist, ``rank`` must
  reproduce "fewest hard, then advisories, then artwork MAE" and nothing more;
  the phase plan's rule (AGENTS.md) is that the pipeline never ranks beyond it.
* **A model is never fit within one job's candidates.**  Pairs are formed inside
  a job (that is what a preference is), but the fit is across jobs.
* **The anti-feedback rule holds.**  Every ordering keeps non-greedy slots
  visible, or the ranker trains on its own output for ever.
* **Determinism.**  Same input, same order -- including when the caller hands
  the same events or candidates over in a different order.
* **The bandit explores, updates, expires and vetoes, and it never picks a
  candidate** -- it answers "which bucket to try next".

Every test writes into ``tmp_path`` through ``PIPELINE_LEARNING_DIR`` (the
conftest fixture does it for the whole suite; the local fixture below makes it
explicit).  No test touches the real ``06_run/_learning/``.
"""

import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
for path in (ROOT, SCRIPTS):
    if path not in sys.path:
        sys.path.insert(0, path)

import triage_events  # noqa: E402
import triage_ranker  # noqa: E402

SHA_A = "a" * 64
SHA_B = "b" * 64


@pytest.fixture
def learning(tmp_path, monkeypatch):
    """Point the learning store at a temp dir for one test."""
    target = tmp_path / "learning"
    monkeypatch.setenv("PIPELINE_LEARNING_DIR", str(target))
    return target


# --------------------------------------------------------------------------- #
# Fixtures: candidates and event lines, synthetic and deterministic             #
# --------------------------------------------------------------------------- #


def candidate(sha, *, hard=0, advisory=0, mae_art=0.5, node_count_max=100,
              colours=3, speckles=3, preset="poster", measured=True):
    """A candidate in the shape scripts/run_record.py assembles."""
    return {
        "id": sha,
        "svg_sha256": sha,
        "sweep": {"preset": preset, "filter_speckle": 8},
        "comparison": {
            "hard": hard,
            "advisory": advisory,
            "node_count_max": node_count_max,
            "declared_colors": colours,
            "metrics": {"speckle_count": speckles},
            "fidelity": {"measured": measured, "mae_art": mae_art},
        },
    }


def event_line(job, sha, pos, event="shown", t="2026-01-01T00:00:00+00:00",
               features=None):
    """One event line: the Phase 2 contract fields, plus optional metrics."""
    line = {"t": t, "job": job, "svg_sha": sha, "event": event, "pos": pos,
            "ranker": None}
    if features is not None:
        line["features"] = features
    return line


def flat_features(*, hard=0, advisory=0, mae_art=0.5, node_count_max=100,
                  colours=3, speckles=3, preset="poster"):
    """The flat metric spelling an event line may carry."""
    return {"hard": hard, "advisory": advisory, "mae_art": mae_art,
            "node_count_max": node_count_max, "colours": colours,
            "speckles": speckles, "preset": preset}


def prefer_colours_events(jobs=4, per_job=3, colour_preferred=6, colour_other=2):
    """Event log where the human consistently starred the higher-colour candidate.

    Every candidate has the SAME hard/advisory/mae_art/node_count/speckles, so the
    hand order cannot separate them: the only signal is the metric the human
    actually acted on.  THE POSITIVE ARRIVES SECOND IN THE LOG whatever its pos,
    so a learner that read file order instead of ``pos`` would still be caught by
    the preference test.

    Returns ``(events, starred_sha, other_sha)``.
    """
    events = []
    first_starred = None
    for job in range(jobs):
        for index in range(per_job):
            sha = "%02d%02d" % (job, index)
            starred = index == 0
            events.append(event_line(
                "job-%02d" % job, sha, index + 1, "shown",
                t="2026-01-%02dT00:00:00+00:00" % (job + 1),
                features=flat_features(colours=colour_preferred if starred
                                       else colour_other)))
        starred_sha = "%02d%02d" % (job, 0)
        events.append(event_line("job-%02d" % job, starred_sha, 1, "star",
                                 t="2026-01-%02dT00:01:00+00:00" % (job + 1)))
        if job == 0:
            first_starred = starred_sha
    return events, first_starred, "%02d%02d" % (0, 1)


# --------------------------------------------------------------------------- #
# 1. Reading a candidate: the run-record shape and the flat event line          #
# --------------------------------------------------------------------------- #


def test_raw_values_read_the_run_record_shape():
    raw = triage_ranker.raw_values(candidate(SHA_A, hard=1, advisory=2, colours=6,
                                             speckles=55, preset="bw"))

    assert raw["hard"] == 1.0
    assert raw["advisory"] == 2.0
    assert raw["colours"] == 6.0
    assert raw["speckles"] == 55.0
    assert raw["preset"] == "bw"
    # The bucket key comes from Phase 0's bucket_of, with the bins in it.
    assert raw["bucket"].startswith("bw|s")
    assert triage_ranker.bucket_bins(raw["bucket"])[0] == "bw"


def test_raw_values_read_the_flat_event_line_and_a_missing_reading_is_none():
    flat = triage_ranker.raw_values({"features": flat_features(colours=4)})
    assert flat["colours"] == 4.0
    assert flat["preset"] == "poster"

    # No comparison block, no metrics: the readings are absent, not zero.  "Not
    # measured" and "measured perfect" must never collapse into one value.
    empty = triage_ranker.raw_values({"sweep": {"preset": "bw"}})
    assert empty["hard"] is None
    assert empty["advisory"] is None
    assert empty["mae_art"] is None
    assert empty["colours"] is None


def test_unmeasured_fidelity_does_not_count_as_a_measurement():
    raw = triage_ranker.raw_values(
        candidate(SHA_A, mae_art=104.0, measured=False))
    assert raw["mae_art"] is None


def test_feature_vector_substitutes_the_training_mean_for_a_missing_reading():
    names = triage_ranker.feature_names(["poster"])
    means = {"hard": 2.0}
    values = triage_ranker.feature_vector({"svg_sha": SHA_A}, names, means=means)
    got = dict(zip(names, values))
    assert got[triage_ranker.BIAS_FEATURE] == 1.0
    assert got["hard"] == 2.0  # neutral, not a perfect zero
    assert got["preset:poster"] == 0.0  # no preset read -> indicator stays 0


# --------------------------------------------------------------------------- #
# 2. Cold start: the hand order, and only the hand order                       #
# --------------------------------------------------------------------------- #


def test_cold_start_falls_back_to_the_hand_order(learning):
    events, starred, other = prefer_colours_events(jobs=2)
    model = triage_ranker.learn_weights(events)  # 2 picks < the default 30

    assert model["kind"] == triage_ranker.KIND_COLD_START
    assert "picks 2 < 30" == model["reason"]
    assert model["picks"] == 2
    assert model["weights"] == {}

    # rank() reproduces (hard, advisory, mae_art) and nothing else -- the
    # higher-colour candidate the human starred is NOT promoted on cold start.
    clean = candidate(SHA_A, hard=0, advisory=0, mae_art=0.5)
    poorer = candidate(SHA_B, hard=0, advisory=0, mae_art=4.0)
    gate_fail = candidate("c" * 64, hard=1, advisory=0, mae_art=0.1)
    order = triage_ranker.rank([gate_fail, poorer, clean], model)
    assert [entry["id"] for entry in order] == [SHA_A, SHA_B, "c" * 64]

    # The score is the hand key folded into one float: lower is worse, so a
    # descending sort IS the hand order.
    assert triage_ranker.score(clean, model) > triage_ranker.score(poorer, model)
    assert triage_ranker.score(poorer, model) > triage_ranker.score(gate_fail, model)


def test_hand_order_reads_hard_then_advisory_then_mae():
    assert (triage_ranker.hand_key(candidate(SHA_A, hard=0, advisory=5, mae_art=9.0))
            < triage_ranker.hand_key(candidate(SHA_B, hard=1, advisory=0, mae_art=0.0)))
    assert (triage_ranker.hand_key(candidate(SHA_A, hard=0, advisory=1, mae_art=9.0))
            < triage_ranker.hand_key(candidate(SHA_B, hard=0, advisory=2, mae_art=0.0)))
    assert (triage_ranker.hand_key(candidate(SHA_A, hard=0, advisory=0, mae_art=1.0))
            < triage_ranker.hand_key(candidate(SHA_B, hard=0, advisory=0, mae_art=2.0)))


def test_unmeasured_axes_sort_last_in_the_hand_order():
    measured = candidate(SHA_A, hard=0, advisory=0, mae_art=9.0)
    unmeasured = {"svg_sha": SHA_B, "sweep": {"preset": "bw"}}
    assert (triage_ranker.hand_key(measured)
            < triage_ranker.hand_key(unmeasured))


def test_a_model_is_never_fit_within_a_single_job(learning):
    events = []
    for index in range(4):
        sha = "%02d" % index
        events.append(event_line("job-solo", sha, index + 1, "shown",
                                 t="2026-01-01T00:00:0%d+00:00" % index,
                                 features=flat_features(
                                     colours=6 if index == 0 else 2)))
        if index == 0:
            events.append(event_line("job-solo", sha, 1, "pick",
                                     t="2026-01-01T00:01:0%d+00:00" % index))

    model = triage_ranker.learn_weights(events, cold_start_picks=1)

    assert model["kind"] == triage_ranker.KIND_COLD_START
    assert model["reason"] == "jobs with signal 1 < 2"
    assert model["jobs"] == 1


def test_an_empty_or_unreadable_store_is_cold_start_not_an_error(learning):
    model = triage_ranker.learn_weights()
    assert model["kind"] == triage_ranker.KIND_COLD_START
    assert model["reason"] == "no events"

    learning.mkdir(parents=True, exist_ok=True)
    (learning / "events.jsonl").write_text("{ not json\n\n,,\n", encoding="utf-8")
    assert triage_events.read_events() == []
    assert triage_ranker.learn_weights()["reason"] == "no events"


def test_the_spec_can_raise_or_lower_the_cold_start_thresholds(learning):
    events, _starred, _other = prefer_colours_events(jobs=2)
    spec = {"triage": {"learn_min_picks": 2, "learn_min_jobs": 2}}

    model = triage_ranker.learn_weights(events, spec=spec)
    assert model["kind"] == triage_ranker.KIND_PAIRWISE

    strict = triage_ranker.learn_weights(events, spec={"triage": {"learn_min_picks": 99}})
    assert strict["kind"] == triage_ranker.KIND_COLD_START
    assert "picks 2 < 99" == strict["reason"]


# --------------------------------------------------------------------------- #
# 3. The preference fit                                                        #
# --------------------------------------------------------------------------- #


def test_pairwise_scorer_learns_starred_above_shown_not_starred(learning):
    events, starred_sha, other_sha = prefer_colours_events()
    model = triage_ranker.learn_weights(events, cold_start_picks=4)

    assert model["kind"] == triage_ranker.KIND_PAIRWISE
    assert model["picks"] == 4
    assert model["jobs"] == 4
    assert model["fit_jobs"] == 4 and model["holdout_jobs"] == 0
    # The learned direction is the human's: more colours -> more preferred.
    assert model["weights"]["colours"] > 0
    assert model["weights"]["bucket_colour_bin"] > 0

    starred = candidate(starred_sha, colours=6)
    other = candidate(other_sha, colours=2)
    # Identical hand key: only the learned score separates them.
    assert triage_ranker.hand_key(starred) == triage_ranker.hand_key(other)
    assert triage_ranker.score(starred, model) > triage_ranker.score(other, model)
    assert [entry["id"] for entry in triage_ranker.rank([other, starred], model)] == \
        [starred_sha, other_sha]


def test_the_fit_reads_pos_and_the_action_event_not_the_file_order(learning):
    events, starred_sha, other_sha = prefer_colours_events()
    model = triage_ranker.learn_weights(list(reversed(events)), cold_start_picks=4)
    forward = triage_ranker.learn_weights(events, cold_start_picks=4)

    assert model["weights"] == forward["weights"]
    starred = candidate(starred_sha, colours=6)
    other = candidate(other_sha, colours=2)
    assert triage_ranker.score(starred, model) > triage_ranker.score(other, model)


def test_learn_weights_is_deterministic_across_runs_and_permutations(learning):
    events, _starred, _other = prefer_colours_events()
    first = triage_ranker.learn_weights(events, cold_start_picks=4)
    again = triage_ranker.learn_weights(list(reversed(events)), cold_start_picks=4)

    assert first["features"] == again["features"]
    assert first["weights"] == again["weights"]
    assert first["means"] == again["means"]
    assert first["scales"] == again["scales"]
    assert first["fit_pairs"] == again["fit_pairs"] == 8


def test_events_join_supplies_the_metrics_the_log_does_not_carry(learning):
    # The Phase 2 producer writes six contract fields and no metrics; the join is
    # how the ranker gets them, keyed by the sha both sides already share.
    candidates = [candidate("%02d%02d" % (job, index),
                            colours=6 if index == 0 else 2)
                  for job in range(4) for index in range(3)]
    events = []
    for job in range(4):
        for index in range(3):
            sha = "%02d%02d" % (job, index)
            events.append(event_line("job-%02d" % job, sha, index + 1, "shown",
                                     t="2026-01-%02dT00:00:00+00:00" % (job + 1)))
        events.append(event_line("job-%02d" % job, "%02d00" % job, 1, "star",
                                 t="2026-01-%02dT00:01:00+00:00" % (job + 1)))

    model = triage_ranker.learn_weights(events, candidates=candidates,
                                        cold_start_picks=4)
    assert model["kind"] == triage_ranker.KIND_PAIRWISE
    assert model["weights"]["colours"] > 0


def test_job_split_holds_out_whole_jobs_sorted(learning):
    fit, held = triage_ranker.job_split(["e", "c", "a", "b", "d"], 0.4)
    assert fit == ["a", "b", "c"]
    assert held == ["d", "e"]
    assert not set(fit) & set(held)

    assert triage_ranker.job_split(["a", "b"], 0.0) == (["a", "b"], [])
    # A fraction that would hold out everything still fits on something.
    fit, held = triage_ranker.job_split(["a"], 0.5)
    assert fit == ["a"] and held == []


def test_a_holdout_job_never_lands_on_both_sides_of_the_fit(learning):
    events, _starred, _other = prefer_colours_events(jobs=5)
    model = triage_ranker.learn_weights(events, cold_start_picks=4,
                                        holdout_fraction=0.4)

    assert model["fit_jobs"] == 3
    assert model["holdout_jobs"] == 2
    assert 0.0 <= model["holdout_accuracy"] <= 1.0
    # Pairs are formed WITHIN a job (1 positive x 2 non-acted-on candidates) and
    # held out by whole job: 2 held-out jobs, 4 pairs, and no pair straddles the
    # fit/holdout boundary.
    assert model["holdout_pairs"] == 4
    assert model["fit_pairs"] == 6


# --------------------------------------------------------------------------- #
# 4. Ordering: non-greedy slots, determinism                                   #
# --------------------------------------------------------------------------- #


def test_rank_always_keeps_n_random_slots_non_greedy_and_drops_nobody(learning):
    model = triage_ranker.cold_model("test")
    many = [{"svg_sha": "%02d" % index, "comparison": {"hard": index}}
            for index in range(12)]

    greedy = [entry["svg_sha"] for entry
              in triage_ranker.rank(many, model, n_random_slots=0, window=5)]
    order = [entry["svg_sha"] for entry
             in triage_ranker.rank(many, model, n_random_slots=2, window=5)]

    # Nothing is dropped, hidden or filtered -- a permutation, not a subset.
    assert sorted(order) == sorted(greedy)
    assert len(order) == len(many)
    # The visible window contains slots the greedy order would not have shown.
    assert len(set(order[:5]) - set(greedy[:5])) >= 2
    # ...and the default window is honoured too.
    default_order = [entry["svg_sha"] for entry
                     in triage_ranker.rank(many, model, n_random_slots=1)]
    default_greedy = [entry["svg_sha"] for entry
                      in triage_ranker.rank(many, model, n_random_slots=0)]
    window = triage_ranker.RANK_VISIBLE_WINDOW
    assert len(set(default_order[:window]) - set(default_greedy[:window])) >= 1


def test_rank_without_slots_is_exactly_the_greedy_order(learning):
    events, _starred, _other = prefer_colours_events()
    model = triage_ranker.learn_weights(events, cold_start_picks=4)
    many = [candidate("%02d" % index, colours=index % 7) for index in range(10)]

    order = [entry["id"] for entry
             in triage_ranker.rank(many, model, n_random_slots=0)]
    expected = [entry["id"] for entry in sorted(
        many, key=lambda entry: (-triage_ranker.score(entry, model),)
        + triage_ranker.hand_key(entry) + (triage_ranker.stable_key(entry),))]

    assert order == expected
    # The score actually reordered: a plain pass-through would be the bug.
    assert order != [entry["id"] for entry in many]


def test_rank_is_deterministic_and_input_order_independent(learning):
    events, _starred, _other = prefer_colours_events()
    model = triage_ranker.learn_weights(events, cold_start_picks=4)
    many = [candidate("%02d" % index, colours=index % 7, hard=index % 2)
            for index in range(12)]

    first = [entry["id"] for entry
             in triage_ranker.rank(many, model, n_random_slots=2, window=6)]
    again = [entry["id"] for entry
             in triage_ranker.rank(many, model, n_random_slots=2, window=6)]
    permuted = [entry["id"] for entry
                in triage_ranker.rank(list(reversed(many)), model,
                                      n_random_slots=2, window=6)]

    assert first == again
    assert first == permuted


def test_rank_of_nothing_is_empty_and_rank_never_needs_a_model(learning):
    assert triage_ranker.rank([], None) == []
    one = [candidate(SHA_A)]
    assert triage_ranker.rank(one, None) == one


def test_rank_keeps_every_candidate_when_the_window_is_the_whole_list(learning):
    model = triage_ranker.cold_model("test")
    three = [{"svg_sha": "%02d" % index, "comparison": {"hard": index}}
             for index in range(3)]
    order = triage_ranker.rank(three, model, n_random_slots=2)
    # Nothing sits outside the window, so there is nothing to surface: the
    # greedy order is returned unchanged rather than inventing a shuffle.
    assert [entry["svg_sha"] for entry in order] == ["00", "01", "02"]


# --------------------------------------------------------------------------- #
# 5. Actions, event reading                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("line,expected", [
    ({"event": "star"}, 1),
    ({"event": "pick"}, 1),
    ({"event": "unstar"}, -1),
    ({"event": "delete"}, -1),
    ({"event": "shown"}, 0),
    ({"event": "open_findings"}, 0),
    ({"event": "shown", "starred": True}, 1),
    ({"event": "shown", "rejected": True}, -1),
    ({"event": "shown", "label": "production"}, 1),
    ({"event": "shown", "label": "discard"}, -1),
    ({"event": "shown", "label": "needs_retrace"}, -1),
    ({"event": "shown", "label": "unrecognised"}, 0),
    ({"event": "shown", "action": -1}, -1),
])
def test_action_fields_a_line_may_carry(line, expected):
    assert triage_ranker.action_of(line) == expected


def test_observe_events_folds_a_job_and_a_sha_into_one_observation(learning):
    events = [
        event_line("job-a", SHA_A, 1, "shown", t="2026-01-01T00:00:00+00:00"),
        event_line("job-a", SHA_A, 1, "star", t="2026-01-01T00:05:00+00:00"),
        event_line("job-a", SHA_B, 2, "shown", t="2026-01-01T00:00:01+00:00"),
        event_line("job-a", None, 3, "shown"),  # no sha: not joinable, skipped
    ]
    observations = triage_ranker.observe_events(events)

    assert sorted(observations) == [("job-a", SHA_A), ("job-a", SHA_B)]
    assert observations[("job-a", SHA_A)]["shown"] is True
    assert observations[("job-a", SHA_A)]["pos"] == 1
    assert observations[("job-a", SHA_A)]["action"] == 1
    assert observations[("job-a", SHA_B)]["action"] == 0

    pairs = triage_ranker.job_pairs(observations)
    assert len(pairs) == 1
    assert pairs[0][0]["svg_sha"] == SHA_A
    assert pairs[0][1]["svg_sha"] == SHA_B


def test_job_pairs_never_pair_candidates_from_different_jobs(learning):
    events = [
        event_line("job-a", SHA_A, 1, "shown"),
        event_line("job-a", SHA_A, 1, "star"),
        event_line("job-b", SHA_B, 1, "shown"),
        event_line("job-b", SHA_B, 1, "star"),
    ]
    pairs = triage_ranker.job_pairs(triage_ranker.observe_events(events))
    # Each job has a positive and no negative, so there is nothing to compare
    # across -- and no pair may be invented by reaching into the other job.
    assert pairs == []


def test_read_events_degrades_on_a_missing_or_junk_file(learning):
    assert triage_events.read_events() == []

    learning.mkdir(parents=True, exist_ok=True)
    (learning / "events.jsonl").write_text(
        '{"t":"2026-01-01T00:00:00+00:00","job":"j","svg_sha":"a","event":"shown",'
        '"pos":1,"ranker":null}\n\nnot json\n42\n', encoding="utf-8")
    events = triage_events.read_events()
    assert len(events) == 1
    assert events[0]["pos"] == 1


# --------------------------------------------------------------------------- #
# 6. Model persistence                                                         #
# --------------------------------------------------------------------------- #


def test_model_round_trips_and_a_missing_model_is_cold_start(learning):
    events, _starred, _other = prefer_colours_events()
    model = triage_ranker.learn_weights(events, cold_start_picks=4)

    path = triage_ranker.save_ranker_model(model)
    assert path == str(learning / "learned_ranker.json")
    # The model lands beside the event log, job-independent.
    assert os.path.dirname(path) == os.path.dirname(triage_events.events_path())

    back = triage_ranker.load_ranker_model()
    assert back == model
    assert back["version"] == triage_ranker.RANKER_VERSION
    assert back["direction"] == triage_ranker.SCORE_DIRECTION

    # JSON round trip keeps the ordering (weights are a dict, the FEATURES list
    # is the order the score is summed in).
    assert list(json.load(open(path, encoding="utf-8"))["features"]) == \
        model["features"]


def test_a_missing_or_corrupt_model_degrades_to_the_hand_order(learning, tmp_path):
    assert triage_ranker.load_ranker_model() is None
    assert triage_ranker.load_ranker_model(str(tmp_path / "nope.json")) is None

    learning.mkdir(parents=True, exist_ok=True)
    (learning / "learned_ranker.json").write_text("{ broken", encoding="utf-8")
    assert triage_ranker.load_ranker_model() is None

    clean = candidate(SHA_A, hard=0, advisory=0)
    poorer = candidate(SHA_B, hard=1, advisory=0)
    assert triage_ranker.score(clean, None) > triage_ranker.score(poorer, None)
    assert [entry["id"] for entry in triage_ranker.rank([poorer, clean], None)] == \
        [SHA_A, SHA_B]


def test_an_unwritable_learning_dir_is_not_fatal(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file, not a directory", encoding="utf-8")
    monkeypatch.setenv("PIPELINE_LEARNING_DIR", str(blocker / "learning"))

    assert triage_ranker.save_ranker_model(triage_ranker.cold_model("x")) is None
    assert triage_ranker.learn_weights()["kind"] == triage_ranker.KIND_COLD_START
    assert triage_ranker.load_ranker_model() is None
    assert triage_ranker.save_bucket_stats(
        triage_ranker.load_bucket_stats()) is None
    assert triage_ranker.load_bucket_stats() == triage_events.default_learned_buckets()


# --------------------------------------------------------------------------- #
# 7. Phase 5: the bucket bandit                                                #
# --------------------------------------------------------------------------- #


def test_bandit_chooses_different_buckets_over_repeated_samples(learning):
    stats = triage_ranker.load_bucket_stats()
    buckets = ["bw|s0|c0|", "poster|s1|c2|", "photo|s4|c4|"]

    rng = random.Random(20260101)
    draws = [triage_ranker.choose_bucket(buckets, stats, rng=rng)
             for _ in range(60)]

    assert set(draws) == set(buckets), "the bandit never explored"
    assert all(draw in buckets for draw in draws)


def test_bandit_prefers_the_bucket_that_keeps_succeeding(learning):
    stats = triage_ranker.load_bucket_stats()
    # Bucket A: 40/40 successes.  Bucket B: 0/40.  Epsilon off, so this is the
    # posterior talking and not the explore draw.
    for _ in range(40):
        triage_ranker.record_outcome("good|s1|c2|", True, stats)
        triage_ranker.record_outcome("bad|s1|c2|", False, stats)

    rng = random.Random(11)
    draws = [triage_ranker.choose_bucket(["good|s1|c2|", "bad|s1|c2|"], stats,
                                         rng=rng, epsilon=0.0)
             for _ in range(50)]
    assert draws.count("good|s1|c2|") >= 49


def test_bandit_records_a_beta_update(learning):
    stats = triage_ranker.load_bucket_stats()
    bucket = "poster|s1|c2|"

    first = triage_ranker.record_outcome(bucket, True, stats, t="2026-01-01T00:00:00+00:00")
    assert first["attempts"] == 1
    assert first["successes"] == 1
    assert first["last_t"] == "2026-01-01T00:00:00+00:00"

    second = triage_ranker.record_outcome(bucket, False, stats,
                                          t="2026-01-02T00:00:00+00:00")
    assert second["attempts"] == 2
    assert second["successes"] == 1
    assert second["last_t"] == "2026-01-02T00:00:00+00:00"

    # The bandit's counters are its own; the Phase 2 human counters are untouched.
    assert second["shown"] == 0 and second["chosen"] == 0 and second["rejected"] == 0


def test_bandit_honours_a_veto(learning):
    stats = triage_ranker.load_bucket_stats()
    buckets = ["bw|s0|c0|", "poster|s1|c2|", "photo|s4|c4|"]

    triage_ranker.set_veto(stats, "poster|s1|c2|", True)
    assert triage_ranker.vetoed_buckets(stats) == ["poster|s1|c2|"]

    rng = random.Random(5)
    draws = [triage_ranker.choose_bucket(buckets, stats, rng=rng)
             for _ in range(40)]
    assert "poster|s1|c2|" not in draws

    # A veto passed in for this call only is honoured the same way.
    draws = [triage_ranker.choose_bucket(buckets, stats, rng=random.Random(6),
                                         veto=["bw|s0|c0|"])
             for _ in range(40)]
    assert "bw|s0|c0|" not in draws

    # Every bucket vetoed: the bandit declines to choose rather than quietly
    # overriding the human.
    assert triage_ranker.choose_bucket(["poster|s1|c2|"], stats) is None
    assert triage_ranker.choose_bucket([], stats) is None
    assert triage_ranker.bucket_decision([], stats)["mode"] == "none"

    # A veto can be lifted again.
    triage_ranker.set_veto(stats, "poster|s1|c2|", False)
    assert triage_ranker.vetoed_buckets(stats) == []


def test_bandit_re_explores_expired_statistics(learning):
    stats = triage_ranker.load_bucket_stats()
    for _ in range(20):
        triage_ranker.record_outcome("stale|s1|c2|", True, stats,
                                     t="2020-01-01T00:00:00+00:00")
    for _ in range(20):
        triage_ranker.record_outcome("fresh|s1|c2|", True, stats)

    assert triage_ranker.expired(stats, "stale|s1|c2|") is True
    assert triage_ranker.expired(stats, "fresh|s1|c2|") is False
    # No attempts is the prior, not a stale statistic.
    assert triage_ranker.expired(stats, "untouched|s0|c0|") is False
    # An undateable statistic is re-explored rather than trusted.
    assert triage_ranker.expired({"buckets": {"junk|s0|c0|": {"last_t": "whenever"}}},
                                 "junk|s0|c0|") is True
    # A TTL the caller sets short expires a fresh record too.
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    assert triage_ranker.expired(stats, "fresh|s1|c2|", now=later,
                                 ttl_seconds=0.0) is True

    decision = triage_ranker.bucket_decision(["stale|s1|c2|", "fresh|s1|c2|"],
                                             stats, rng=random.Random(9),
                                             epsilon=0.0)
    assert decision["expired"] == ["stale|s1|c2|"]
    # The stale bucket is sampled from the prior, so across draws the live one
    # wins -- a single draw from a flat prior can always come out on top, which is
    # exactly why the staleness rule re-explores instead of trusting the count.
    wins = 0
    rng = random.Random(17)
    for _ in range(20):
        drawn = triage_ranker.bucket_decision(
            ["stale|s1|c2|", "fresh|s1|c2|"], stats, rng=rng, epsilon=0.0)
        wins += drawn["bucket"] == "fresh|s1|c2|"
    assert wins >= 16


def test_bucket_decision_explains_an_epsilon_explore(learning):
    stats = triage_ranker.load_bucket_stats()
    for _ in range(10):
        triage_ranker.record_outcome("good|s1|c2|", True, stats)

    decision = triage_ranker.bucket_decision(["good|s1|c2|", "other|s0|c0|"],
                                             stats, rng=random.Random(2),
                                             epsilon=1.0)
    assert decision["mode"] == "explore"
    assert "epsilon" in decision["reason"]
    assert decision["bucket"] in ("good|s1|c2|", "other|s0|c0|")
    # The sampled thetas are reported for every live bucket, vetoes excluded.
    assert sorted(decision["sampled"]) == ["good|s1|c2|", "other|s0|c0|"]

    cold = triage_ranker.bucket_decision(["new|s0|c0|"], stats,
                                         rng=random.Random(3), epsilon=0.0)
    assert cold["mode"] == "exploit"
    assert "prior" in cold["reason"]


def test_learned_buckets_round_trips_through_the_existing_store(learning):
    empty = triage_ranker.load_bucket_stats()
    assert empty["version"] == triage_events.LEARNED_BUCKETS_VERSION
    assert empty["buckets"] == {}

    triage_ranker.record_outcome("poster|s1|c2|", True, empty,
                                 t="2026-01-01T00:00:00+00:00")
    triage_ranker.set_veto(empty, "bw|s0|c0|", True)

    written = triage_ranker.save_bucket_stats(empty)
    assert written == str(learning / "learned_buckets.json")
    assert written == triage_events.learned_buckets_path()

    back = triage_ranker.load_bucket_stats()
    assert back["buckets"]["poster|s1|c2|"]["successes"] == 1
    assert back["buckets"]["poster|s1|c2|"]["attempts"] == 1
    assert back["buckets"]["poster|s1|c2|"]["last_t"] == "2026-01-01T00:00:00+00:00"
    assert back["buckets"]["bw|s0|c0|"]["veto"] is True

    # A skeleton written by Phase 2 (no bandit fields) still reads: the missing
    # keys are filled from the defaults on the way out of the store.
    (learning / "learned_buckets.json").write_text(json.dumps(
        {"version": 1, "buckets": {"poster|s1|c2|": triage_events.empty_bucket_stats()}}),
        encoding="utf-8")
    legacy = triage_ranker.load_bucket_stats()
    assert legacy["buckets"]["poster|s1|c2|"]["attempts"] == 0
    assert legacy["buckets"]["poster|s1|c2|"]["veto"] is False


def test_a_corrupt_bucket_store_degrades_to_the_prior(learning):
    learning.mkdir(parents=True, exist_ok=True)
    (learning / "learned_buckets.json").write_text("{ not json", encoding="utf-8")

    stats = triage_ranker.load_bucket_stats()
    assert stats == triage_events.default_learned_buckets()
    # ...and the bandit still answers, from the prior.
    assert triage_ranker.choose_bucket(["a|s0|c0|", "b|s0|c0|"], stats) in \
        ("a|s0|c0|", "b|s0|c0|")


def test_beta_sample_is_bounded_and_leans_with_the_counts(learning):
    rng = random.Random(4)
    cold = [triage_ranker.beta_sample(rng, 0, 0) for _ in range(50)]
    warm = [triage_ranker.beta_sample(rng, 200, 200) for _ in range(50)]

    assert all(0.0 <= value <= 1.0 for value in cold + warm)
    assert sum(cold) / len(cold) < sum(warm) / len(warm)


def test_bucket_stats_helpers_never_write_and_normalise_a_partial_record(learning):
    stats = triage_ranker.load_bucket_stats()
    record = triage_ranker.bucket_stats(stats, "poster|s1|c2|")
    assert record["attempts"] == 0 and record["veto"] is False
    # A read must not conjure a bucket into the store.
    assert stats["buckets"] == {}

    assert triage_ranker.normalise_bucket_stats({"successes": 3})["attempts"] == 0
    assert triage_ranker.normalise_bucket_stats(None) == triage_ranker.default_bucket_stats()
    assert triage_ranker.bucket_bins("not a bucket key") == (None, None, None, None)
