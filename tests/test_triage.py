#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for scripts/triage.py (Phase 0 candidate triage: gate + bucket + dedupe).

Nothing here reads real artwork: every image is synthesized in-test with Pillow.
The two things worth pinning:

* identity is stable under FORMATTING.  A trace that is regenerated and reformatted
  (attributes reordered, comments added, ids rewritten, coordinates re-emitted with
  more decimals) is not new work, and a dedupe layer that thinks it is will report
  phantom candidates forever.
* similarity is measured on the RENDERED thumbnail, not the SVG text.  Two
  documents that share no bytes can be the same picture, and the near-dupe metric
  has to survive the harmless per-pixel noise of a re-render while still separating
  a shifted or flipped image.

The gate tests set HARD/WEAK to stub rules and restore them afterwards
(``monkeypatch.setattr`` does the restore) to pin the evaluation order; the phase 1
tests below drive the real rules through ``make_candidate``, whose shape mirrors
what ``scripts/run_record.py`` assembles.  The tf-idf tests pin the two things
that make a corpus scorer trustworthy here: near-duplicate intent groups, and
DIGITS survive normalisation (``filter_speckle=8`` is not ``filter_speckle=16``).
"""

import hashlib
import os
import random
import sys

import pytest
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
for path in (ROOT, SCRIPTS):
    if path not in sys.path:
        sys.path.insert(0, path)

import triage  # noqa: E402

SVG_NS = "http://www.w3.org/2000/svg"

# --------------------------------------------------------------------------- #
# Image fixtures -- synthesized, deterministic, no artwork on disk             #
# --------------------------------------------------------------------------- #


def _block_image(size=64):
    """Deterministic gradient with three bright blocks in it.

    High-frequency enough that a 3px shift changes the difference hash, and
    non-uniform enough that the cosine metrics are not degenerate.
    """
    img = Image.new("L", (size, size), 0)
    pixels = img.load()
    for y in range(size):
        for x in range(size):
            pixels[x, y] = (x * 3 + y * 2) % 256
    for x0, y0, x1, y1 in ((4, 4, 20, 30), (28, 10, 50, 40), (50, 44, 62, 60)):
        for y in range(y0, min(y1, size)):
            for x in range(x0, min(x1, size)):
                pixels[x, y] = 250
    return img


def _noisy_copy(img, amount=6, seed=11):
    """Same artwork with a little per-pixel noise -- the near-dupe case."""
    out = img.copy()
    pixels = out.load()
    rng = random.Random(seed)
    for y in range(out.height):
        for x in range(out.width):
            value = pixels[x, y] + rng.randint(-amount, amount)
            pixels[x, y] = max(0, min(255, value))
    return out


def _shifted_copy(img, dx=3, dy=0):
    """The same artwork moved by a few pixels on a black canvas."""
    out = Image.new("L", img.size, 0)
    out.paste(img, (dx, dy))
    return out


def _checkerboard(size=32, cell=4):
    img = Image.new("L", (size, size), 0)
    pixels = img.load()
    for y in range(size):
        for x in range(size):
            pixels[x, y] = 255 if (x // cell + y // cell) % 2 == 0 else 0
    return img


def _inverted(img):
    return Image.eval(img, lambda v: 255 - v)


# --------------------------------------------------------------------------- #
# canonical_svg / svg_sha                                                      #
# --------------------------------------------------------------------------- #


def test_canonical_svg_is_idempotent():
    text = ('<svg xmlns="{0}" width="10" height="10">'
            '<path id="p1" d="M1.23456 2 L3 4" fill="#000000"/></svg>').format(SVG_NS)
    once = triage.canonical_svg(text)
    assert triage.canonical_svg(once) == once


def test_formatting_only_differences_hash_equal():
    # Same document: attribute order, whitespace, indentation, comma separators
    # and a comment all differ; ids and coordinate noise differ too.
    one = ('<svg xmlns="{ns}" width="10" height="10">'
           '<!-- traced by hand -->'
           '<path id="path1" fill="#000000" d="M1.23456 2 L3 4"/>'
           '</svg>').format(ns=SVG_NS)
    two = ('<svg height="10" xmlns="{ns}" width="10">\n'
           '  <path d="M 1.2349, 2.0 L 3.00 4.000" fill="#000000"/>\n'
           '</svg>').format(ns=SVG_NS)
    assert one != two
    assert triage.canonical_svg(one) == triage.canonical_svg(two)
    assert triage.svg_sha(triage.canonical_svg(one)) == \
        triage.svg_sha(triage.canonical_svg(two))


def test_canonical_svg_strips_ids_and_comments():
    text = ('<svg xmlns="{0}"><!-- SecretTrace -->'
            '<path id="layer-1" d="M0 0"/></svg>').format(SVG_NS)
    canon = triage.canonical_svg(text)
    assert "id=" not in canon
    assert "layer-1" not in canon
    assert "SecretTrace" not in canon


def test_coordinates_round_to_two_decimals():
    text = '<svg xmlns="{0}"><path d="M1.23456 2 L3 4"/></svg>'.format(SVG_NS)
    canon = triage.canonical_svg(text)
    assert "1.23" in canon
    assert "1.23456" not in canon
    # Sub-cent noise is absorbed: the same path traced twice must agree.
    noisy = '<svg xmlns="{0}"><path d="M1.2349 2.004 L3.001 4.00"/></svg>'.format(SVG_NS)
    assert triage.canonical_svg(text) == triage.canonical_svg(noisy)


def test_whitespace_normalization_keeps_path_separators_meaningful():
    # "M1 2 3 4" is a moveto with an implicit lineto; canonicalizing separators
    # must not drop the 3/4 pair or change the command.
    sparse = '<svg xmlns="{0}"><path d="M1 2 3 4"/></svg>'.format(SVG_NS)
    canon = triage.canonical_svg(sparse)
    assert "M 1.00 2.00 3.00 4.00" in canon


def test_child_order_is_preserved():
    # Paint order is meaningful, so reordering children IS a different document.
    a = '<svg xmlns="{0}"><rect id="r1"/><circle id="c1"/></svg>'.format(SVG_NS)
    b = '<svg xmlns="{0}"><circle id="c1"/><rect id="r1"/></svg>'.format(SVG_NS)
    assert triage.canonical_svg(a) != triage.canonical_svg(b)


def test_transform_rounds_without_breaking_syntax():
    text = ('<svg xmlns="{0}"><g transform=" translate( 2.506 )  scale(1, 2)">'
            '</g></svg>').format(SVG_NS)
    canon = triage.canonical_svg(text)
    assert 'transform="translate(2.51) scale(1.00,2.00)"' in canon


def test_svg_sha_is_sha256_of_the_canonical_text():
    canon = triage.canonical_svg('<svg xmlns="{0}"><path d="M0 0"/></svg>'.format(SVG_NS))
    expected = hashlib.sha256(canon.encode("utf-8")).hexdigest()
    assert triage.svg_sha(canon) == expected
    assert len(triage.svg_sha(canon)) == 64


# --------------------------------------------------------------------------- #
# file_sha256                                                                  #
# --------------------------------------------------------------------------- #


def test_file_sha256_known_value(tmp_path):
    path = tmp_path / "artifact.txt"
    path.write_bytes(b"chopshop triage\n")
    assert triage.file_sha256(path) == \
        "7503f6cefcb31d068739fe40af03b3ef16a15d0f1ff9180639494e27b43457f5"


def test_file_sha256_empty_and_streamed(tmp_path):
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    assert triage.file_sha256(empty) == \
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    # Larger than the read chunk, to exercise the streaming loop rather than a
    # single read.
    blob = tmp_path / "blob.bin"
    payload = bytes(range(256)) * 8192
    blob.write_bytes(payload)
    assert triage.file_sha256(blob) == hashlib.sha256(payload).hexdigest()


# --------------------------------------------------------------------------- #
# dhash / gray_cosine                                                          #
# --------------------------------------------------------------------------- #


def test_dhash_identical_images_match():
    img = _block_image()
    assert triage.dhash(img) == triage.dhash(img.copy())
    assert isinstance(triage.dhash(img), int)


def test_dhash_shifted_image_differs():
    img = _block_image()
    assert triage.dhash(_shifted_copy(img, dx=3)) != triage.dhash(img)
    assert triage.dhash(_shifted_copy(img, dy=2)) != triage.dhash(img)


def test_dhash_flipped_image_differs():
    img = _block_image()
    assert triage.dhash(img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)) != \
        triage.dhash(img)


def test_dhash_of_flat_image_is_zero():
    # No pixel is darker than its right neighbour, so no bit is set.  This is the
    # documented low-information case, not a bug.
    assert triage.dhash(Image.new("L", (32, 32), 128)) == 0


def test_dhash_accepts_colour_images():
    assert triage.dhash(Image.new("RGB", (40, 40), "#ffffff")) == 0
    assert isinstance(triage.dhash(Image.new("RGB", (40, 40), "#ffffff")), int)


def test_gray_cosine_identical_is_one():
    img = _block_image()
    assert triage.gray_cosine(img, img) == pytest.approx(1.0, abs=1e-9)


def test_gray_cosine_near_dupe_stays_high():
    # A re-render is not pixel-identical; the metric has to tolerate that or it
    # will treat every re-render as a new candidate.
    img = _block_image()
    assert triage.gray_cosine(img, _noisy_copy(img)) > 0.95


def test_gray_cosine_unrelated_is_low():
    # Contrast-inverted checkerboard: the two vectors share no structure.
    board = _checkerboard()
    assert triage.gray_cosine(board, _inverted(board)) < 0.2


def test_gray_cosine_black_thumbnail_is_zero():
    black = Image.new("L", (32, 32), 0)
    assert triage.gray_cosine(black, black) == 0.0
    assert triage.gray_cosine(black, _block_image()) == 0.0


# --------------------------------------------------------------------------- #
# bucket_of                                                                    #
# --------------------------------------------------------------------------- #


def test_same_bin_and_preset_share_a_bucket():
    assert triage.bucket_of("bw", 3, 2) == triage.bucket_of("bw", 5, 3)
    assert triage.bucket_of("bw", 6, 4) == triage.bucket_of("bw", 20, 5)


def test_different_preset_is_a_different_bucket():
    assert triage.bucket_of("bw", 3, 2) != triage.bucket_of("poster", 3, 2)


def test_speckle_and_colour_edges():
    speckles = [triage.bucket_of("bw", n, 1) for n in (0, 5, 6, 20, 21, 40, 41)]
    assert [b.split("|")[1] for b in speckles] == \
        ["s0", "s1", "s2", "s2", "s3", "s3", "s4"]
    colours = [triage.bucket_of("bw", 1, n) for n in (0, 1, 3, 4, 5, 8, 9, 20)]
    assert [b.split("|")[2] for b in colours] == \
        ["c0", "c0", "c1", "c2", "c2", "c3", "c4", "c4"]
    # A bin boundary is where a family stops collapsing: 40 and 41 speckles are
    # different buckets, so they can be judged separately.
    assert triage.bucket_of("bw", 40, 5) != triage.bucket_of("bw", 41, 5)


def test_prompt_family_is_part_of_the_bucket():
    assert triage.bucket_of("bw", 3, 2, "portrait") != \
        triage.bucket_of("bw", 3, 2, "landscape")
    assert triage.bucket_of("bw", 3, 2) == "bw|s1|c1|"


# --------------------------------------------------------------------------- #
# gate -- evaluation order, with stub rule lists                               #
# --------------------------------------------------------------------------- #


def test_rule_lists_hold_named_rules():
    # Phase 1 lands the rules; phase 0 shipped the lists empty.
    for rules in (triage.HARD, triage.WEAK):
        assert rules, "the rule lists are populated as of phase 1"
        for name, fn in rules:
            assert isinstance(name, str) and name == name.upper()
            assert callable(fn)
    names = [name for name, _ in triage.HARD + triage.WEAK]
    assert len(names) == len(set(names)), "a reason string names exactly one rule"
    assert set(names) >= {
        "ARTWORK_LOST", "SILHOUETTE_LOST", "GLOW_AREA_HIGH",
        "COLOR_CAP_EXCEEDED", "GEOMETRY_HARD_FAIL",
        "SPECKLE_HEAVY", "COLORS_NEAR_CAP", "LOW_LUMA_CONTRAST",
        "NODE_OVERLOAD", "BORDERLINE_FIDELITY"}


def test_candidate_without_run_record_fields_passes():
    # No sweep, no comparison -> no reading to fire on, and no bucket to key on.
    verdict = triage.gate({"file": "candidate_01.svg"})
    assert verdict.disposition is triage.Disp.OK
    assert verdict.reasons == []
    assert verdict.bucket is None


def test_hard_hit_drops(monkeypatch):
    monkeypatch.setattr(triage, "HARD", [("EMPTY_PATH", lambda c: True)])
    verdict = triage.gate({"file": "candidate_01.svg"})
    assert verdict.disposition is triage.Disp.DROP
    assert "EMPTY_PATH" in verdict.reasons


def test_hard_hit_wins_over_weak_hits(monkeypatch):
    monkeypatch.setattr(triage, "HARD", [("EMPTY_PATH", lambda c: True)])
    monkeypatch.setattr(triage, "WEAK", [
        ("A", lambda c: True), ("B", lambda c: True), ("C", lambda c: True)])
    verdict = triage.gate({"file": "candidate_01.svg"})
    assert verdict.disposition is triage.Disp.DROP
    assert verdict.reasons == ["EMPTY_PATH"]


def test_weak_rules_that_do_not_fire_are_ok(monkeypatch):
    monkeypatch.setattr(triage, "WEAK", [("NEVER", lambda c: False)])
    assert triage.gate({}).disposition is triage.Disp.OK


def test_one_weak_hit_is_ok(monkeypatch):
    monkeypatch.setattr(triage, "WEAK", [("TINY_ISLANDS", lambda c: True)])
    verdict = triage.gate({})
    assert verdict.disposition is triage.Disp.OK
    assert verdict.reasons == ["TINY_ISLANDS"]


def test_three_weak_hits_demote(monkeypatch):
    monkeypatch.setattr(triage, "WEAK", [
        ("TINY_ISLANDS", lambda c: True),
        ("LOW_CONTRAST", lambda c: True),
        ("BLEED_RISK", lambda c: True),
        ("NEVER", lambda c: False),
    ])
    verdict = triage.gate({})
    assert verdict.disposition is triage.Disp.DEMOTE
    assert verdict.reasons == ["TINY_ISLANDS", "LOW_CONTRAST", "BLEED_RISK"]


def test_weak_threshold_is_configurable(monkeypatch):
    monkeypatch.setattr(triage, "WEAK", [("TINY_ISLANDS", lambda c: True)])
    assert triage.gate({}, weak_needed=1).disposition is triage.Disp.DEMOTE
    assert triage.gate({}, weak_needed=2).disposition is triage.Disp.OK


def test_gate_is_deterministic(monkeypatch):
    monkeypatch.setattr(triage, "WEAK", [
        ("A", lambda c: True), ("B", lambda c: True), ("C", lambda c: True)])
    first = triage.gate({})
    second = triage.gate({})
    assert first == second
    assert first.reasons == ["A", "B", "C"]


def test_verdict_carries_all_three_fields():
    verdict = triage.gate({})
    assert isinstance(verdict, triage.Verdict)
    assert verdict.disposition in (triage.Disp.OK, triage.Disp.DEMOTE, triage.Disp.DROP)
    assert isinstance(verdict.reasons, list)
    assert verdict.bucket is None


# --------------------------------------------------------------------------- #
# gate -- the phase 1 rules, driven through a run-record candidate             #
# --------------------------------------------------------------------------- #


def make_candidate(comparison=None, metrics=None, fidelity=None, sweep=None,
                   triage_block=None, **top):
    """A CLEAN candidate: every reading present, no rule fires.

    Shape follows ``scripts/run_record.py``'s per-candidate record (id, sweep,
    comparison with Layer A/B + fidelity + verdict).  ``comparison.metrics`` is the
    optional block the gate reads for the observations Layer A/B does not record
    (speckle count, glow area, luma separation, silhouette overlap); pass
    ``comparison={"metrics": None}`` to model a run that never measured them.

    Each keyword moves one reading so a test can state exactly what it changed:
    ``comparison=``/``metrics=``/``fidelity=`` merge one level down, ``sweep=``
    merges into the sweep params, ``triage_block=`` attaches the run's merged
    ``triage`` thresholds (``load_triage(spec)``).
    """
    candidate = {
        "id": "candidate_01",
        "file": "candidate_01.svg",
        "sweep": {"preset": "poster", "filter_speckle": 8,
                  "hierarchical": "stacked", "use_palette": False},
        "comparison": {
            "passed": True,
            "hard": 0,
            "advisory": 1,
            "node_count_max": 120,
            "node_count_total": 900,
            "rendered_ink_colors": 4,
            "declared_colors": 3,
            "layer_a": {"hard": 0, "advisory": 1},
            "layer_b": {"hard": 0, "advisory": 0},
            "fidelity": {"measured": True, "mae": 0.5, "mae_art": 0.5,
                         "p95": 2.0, "within10": 0.99},
            "fidelity_verdict": "faithful",
            "metrics": {"speckle_count": 4, "glow_area_pct": 1.0,
                        "luma_delta": 30.0, "silhouette_iou": 0.97},
        },
    }
    candidate.update(top)
    if sweep:
        candidate["sweep"] = dict(candidate["sweep"], **sweep)
    block = candidate["comparison"]
    if comparison:
        block.update(comparison)
    if fidelity:
        block["fidelity"] = dict(block["fidelity"], **fidelity)
    if metrics:
        block["metrics"] = dict(block["metrics"] or {}, **metrics)
    if triage_block is not None:
        candidate["triage"] = triage_block
    return candidate


def test_the_clean_candidate_passes_every_rule():
    verdict = triage.gate(make_candidate())
    assert verdict.disposition is triage.Disp.OK
    assert verdict.reasons == []


def test_artwork_lost_verdict_drops():
    # What compare_candidates.py records for a bw/binary trace of a colour design:
    # both layers pass, and the artwork is gone.
    verdict = triage.gate(make_candidate(comparison={
        "fidelity_verdict": "artwork_lost"}))
    assert verdict.disposition is triage.Disp.DROP
    assert verdict.reasons == ["ARTWORK_LOST"]


def test_artwork_mae_past_the_cliff_drops_without_a_verdict():
    # 104.2 is the measured artwork MAE of the six bw candidates against 0.005 for
    # the faithful ones; the gate must not need the verdict string to catch them.
    verdict = triage.gate(make_candidate(fidelity={"mae_art": 104.214}))
    assert verdict.disposition is triage.Disp.DROP
    assert verdict.reasons == ["ARTWORK_LOST"]


def test_unmeasured_fidelity_is_not_a_lost_artwork():
    # measured=False means the render was never diffed; the number beside it is
    # meaningless, and "not measured" is not "the artwork is gone".
    verdict = triage.gate(make_candidate(
        comparison={"fidelity_verdict": None},
        fidelity={"measured": False, "mae_art": 104.214}))
    assert verdict.disposition is triage.Disp.OK
    assert verdict.reasons == []


def test_silhouette_overlap_below_the_floor_drops():
    verdict = triage.gate(make_candidate(metrics={"silhouette_iou": 0.6}))
    assert verdict.disposition is triage.Disp.DROP
    assert verdict.reasons == ["SILHOUETTE_LOST"]


def test_glow_area_over_the_ceiling_drops():
    verdict = triage.gate(make_candidate(metrics={"glow_area_pct": 14.0}))
    assert verdict.disposition is triage.Disp.DROP
    assert verdict.reasons == ["GLOW_AREA_HIGH"]


def test_colour_cap_exceeded_drops():
    verdict = triage.gate(make_candidate(comparison={"declared_colors": 9}))
    assert verdict.disposition is triage.Disp.DROP
    assert verdict.reasons == ["COLOR_CAP_EXCEEDED"]


def test_a_recorded_layer_hard_failure_drops():
    verdict = triage.gate(make_candidate(comparison={
        "passed": False, "hard": 1,
        "layer_b": {"hard": 1, "advisory": 0}}))
    assert verdict.disposition is triage.Disp.DROP
    assert verdict.reasons == ["GEOMETRY_HARD_FAIL"]


def test_a_layer_that_never_ran_is_not_a_geometry_failure():
    # passed=False with zero recorded findings is "not checked", not "failed".
    verdict = triage.gate(make_candidate(comparison={"passed": False, "hard": 0,
                                                     "layer_b": {"hard": 0}}))
    assert verdict.disposition is triage.Disp.OK


def test_every_missing_reading_stays_silent():
    # Nothing measured at all: no metric block, no fidelity, no colour counts.
    bare = make_candidate(comparison={
        "metrics": None, "fidelity": None, "declared_colors": None,
        "rendered_ink_colors": None, "node_count_max": None})
    verdict = triage.gate(bare)
    assert verdict.disposition is triage.Disp.OK
    assert verdict.reasons == []


def test_a_hard_signal_hides_the_weak_signals():
    # A doomed candidate's advisories are noise: only the hard reason is reported.
    verdict = triage.gate(make_candidate(
        comparison={"declared_colors": 8},
        metrics={"speckle_count": 55, "luma_delta": 6, "glow_area_pct": 14.0}))
    assert verdict.disposition is triage.Disp.DROP
    assert verdict.reasons == ["GLOW_AREA_HIGH"]


def test_weak_needed_minus_one_signals_stay_ok():
    verdict = triage.gate(make_candidate(
        metrics={"speckle_count": 55, "luma_delta": 6}))
    assert verdict.disposition is triage.Disp.OK
    assert verdict.reasons == ["SPECKLE_HEAVY", "LOW_LUMA_CONTRAST"]


def test_weak_needed_signals_demote():
    verdict = triage.gate(make_candidate(
        comparison={"declared_colors": 8},
        metrics={"speckle_count": 55, "luma_delta": 6}))
    assert verdict.disposition is triage.Disp.DEMOTE
    assert verdict.reasons == ["SPECKLE_HEAVY", "COLORS_NEAR_CAP",
                              "LOW_LUMA_CONTRAST"]


def test_node_overload_is_a_weak_signal():
    verdict = triage.gate(make_candidate(
        comparison={"node_count_max": 900},
        metrics={"speckle_count": 60, "luma_delta": 3}))
    assert verdict.disposition is triage.Disp.DEMOTE
    assert verdict.reasons == ["SPECKLE_HEAVY", "LOW_LUMA_CONTRAST",
                              "NODE_OVERLOAD"]


def test_borderline_fidelity_is_weak_not_hard():
    # 6.0 is drift -- off the source, still the design. One weak signal: OK.
    drifted = triage.gate(make_candidate(fidelity={"mae_art": 6.0}))
    assert drifted.disposition is triage.Disp.OK
    assert drifted.reasons == ["BORDERLINE_FIDELITY"]
    # Three weak signals demote; the loss cliff for a drop is 8.0.
    demoted = triage.gate(make_candidate(
        fidelity={"mae_art": 6.0},
        metrics={"speckle_count": 55, "luma_delta": 6}))
    assert demoted.disposition is triage.Disp.DEMOTE


def test_threshold_boundaries_are_inclusive_where_documented():
    # At the cap is allowed (and flagged); over it is a drop.
    assert triage.gate(make_candidate(comparison={"declared_colors": 8})).reasons \
        == ["COLORS_NEAR_CAP"]
    assert triage.gate(make_candidate(comparison={"declared_colors": 6})).reasons == []
    # At max_speckles is at the limit; one over is the weak hit.
    assert triage.gate(make_candidate(metrics={"speckle_count": 40})).reasons == []
    assert triage.gate(make_candidate(metrics={"speckle_count": 41})).reasons == \
        ["SPECKLE_HEAVY"]
    # min_luma_delta is a floor: 12 is fine, 11 is not.
    assert triage.gate(make_candidate(metrics={"luma_delta": 12})).reasons == []
    assert triage.gate(make_candidate(metrics={"luma_delta": 11})).reasons == \
        ["LOW_LUMA_CONTRAST"]
    # max_nodes_per_path likewise: 500 is the documented cap.
    assert triage.gate(make_candidate(comparison={"node_count_max": 500})).reasons == []
    assert triage.gate(make_candidate(comparison={"node_count_max": 501})).reasons == \
        ["NODE_OVERLOAD"]


def test_gate_carries_named_reasons_and_the_bucket_key():
    verdict = triage.gate(make_candidate(
        sweep={"preset": "bw"},
        comparison={"declared_colors": 8},
        metrics={"speckle_count": 55, "luma_delta": 6},
        prompt_family="portrait"))
    assert verdict.disposition is triage.Disp.DEMOTE
    assert verdict.reasons == ["SPECKLE_HEAVY", "COLORS_NEAR_CAP",
                              "LOW_LUMA_CONTRAST"]
    # The bucket is the family a single rejection retires: preset, speckle bin,
    # colour bin, prompt family.
    assert verdict.bucket == triage.bucket_of("bw", 55, 8, "portrait")
    assert verdict.bucket == "bw|s4|c3|portrait"
    # Every reason is a rule that actually exists, in the list order documented.
    names = dict(triage.HARD + triage.WEAK)
    assert all(reason in names for reason in verdict.reasons)
    order = [name for name, _ in triage.WEAK]
    assert verdict.reasons == [name for name in order if name in verdict.reasons]


def test_bucket_is_none_when_the_candidate_keyed_on_nothing():
    assert triage.gate({}).bucket is None
    assert triage.gate({"id": "c1", "file": "c1.svg", "metrics": {}}).bucket is None


def test_gate_honours_a_candidate_carried_triage_block():
    # The candidate carries its run's merged triage block, so a spec override
    # reaches the rules themselves -- raising the caps turns a demote into a pass.
    candidate = make_candidate(
        comparison={"declared_colors": 8},
        metrics={"speckle_count": 55, "luma_delta": 6},
        triage_block=triage.load_triage({"triage": {
            "max_speckles": 100, "min_luma_delta": 2}}))
    verdict = triage.gate(candidate)
    assert verdict.disposition is triage.Disp.OK
    assert verdict.reasons == ["COLORS_NEAR_CAP"]
    # The colour cap moves the same way: 10 colours under a cap of 12 is fine.
    raised = make_candidate(
        comparison={"declared_colors": 10},
        triage_block=triage.load_triage({"triage": {"max_unique_colors": 12}}))
    assert triage.gate(raised).disposition is triage.Disp.OK
    # ... and the artwork cliff too, so a spec can trade fidelity for colour.
    lenient = make_candidate(
        fidelity={"mae_art": 20.0},
        triage_block=triage.load_triage({"triage": {"max_mae_art": 30.0}}))
    assert triage.gate(lenient).disposition is triage.Disp.OK


def test_gate_uses_the_defaults_when_no_block_is_carried():
    thresholds = triage.thresholds_of(make_candidate())
    assert thresholds == triage.triage_defaults()


# --------------------------------------------------------------------------- #
# norm / grams / cosine / text_sim                                             #
# --------------------------------------------------------------------------- #


def test_norm_lowercases_and_collapses():
    assert triage.norm("Poster Trace  #3") == "poster trace 3"
    assert triage.norm("  mixed\twhitespace\n") == "mixed whitespace"


def test_norm_keeps_digits():
    # The whole reason digits survive normalization: dropping them makes these
    # two parameter strings score as identical text.
    a = triage.norm("filter_speckle=8")
    b = triage.norm("filter_speckle=16")
    assert a == "filter speckle 8"
    assert b == "filter speckle 16"
    assert a != b
    assert triage.text_sim("filter_speckle=8", "filter_speckle=16") < 1.0


def test_norm_iterates_runes_not_bytes():
    # Non-ASCII letters stay whole characters: byte-wise handling would either
    # split them or drop them.
    assert triage.norm("Résumé ÄÖÜ") == "résumé äöü"
    assert len(triage.norm("é")) == 1


def test_grams_counts_char_bigrams():
    assert triage.grams("abc") == {"ab": 1, "bc": 1}
    assert triage.grams("aaa") == {"aa": 2}
    # Shorter than n still yields something rather than silently scoring empty.
    assert triage.grams("a") == {"a": 1}
    assert triage.grams("") == {}


def test_cosine_edge_cases():
    assert triage.cosine({}, {"ab": 1}) == 0.0
    assert triage.cosine({}, {}) == 0.0
    assert triage.cosine({"ab": 1}, {"xy": 1}) == 0.0
    assert triage.cosine({"ab": 1}, {"ab": 1}) == pytest.approx(1.0)


def test_text_sim_identical_and_disjoint_and_clamped():
    assert triage.text_sim("Poster trace 3", "poster TRACE 3") == 1.0
    assert triage.text_sim("abc", "xyz") == 0.0
    assert triage.text_sim("", "anything") == 0.0
    assert 0.0 <= triage.text_sim("a b c", "a b d") <= 1.0


def test_text_sim_is_symmetric():
    a, b = "tracer bw cutout", "tracer poster stacked"
    assert triage.text_sim(a, b) == pytest.approx(triage.text_sim(b, a))


# --------------------------------------------------------------------------- #
# tf-idf intent grouping (phase 3)                                             #
# --------------------------------------------------------------------------- #

# Same ask, retraced at a different speckle number, then one unrelated document.
CORPUS = [
    "tracer bw cutout 8",
    "tracer bw cutout 16",
    "poster sunset landscape",
]


def test_tfidf_vectors_are_sparse_normalised_dicts():
    idf, vectors = triage.tfidf_vectors(CORPUS)
    assert len(vectors) == len(CORPUS)
    assert all(isinstance(gram, str) for gram in idf)
    for vector in vectors:
        assert isinstance(vector, dict) and vector
        assert all(isinstance(gram, str) for gram in vector)
        length = sum(weight * weight for weight in vector.values()) ** 0.5
        assert length == pytest.approx(1.0)


def test_tfidf_query_scores_every_document_in_corpus_order():
    idf, vectors = triage.tfidf_vectors(CORPUS)
    scores = triage.tfidf_query("tracer bw cutout 8", idf, vectors)
    assert len(scores) == len(CORPUS)
    assert scores[0] == pytest.approx(1.0)
    # Its own document first, the sibling retrace second, the unrelated one last.
    assert scores[0] > scores[1] > scores[2]
    assert scores[2] < 0.2


def test_tfidf_groups_near_duplicate_intent():
    idf, vectors = triage.tfidf_vectors(CORPUS)
    assert triage.intent_groups(vectors) == [[0, 1], [2]]


def test_intent_groups_never_hides_an_index():
    # Every index lands in exactly one group, in order, even when nothing matches.
    idf, vectors = triage.tfidf_vectors(CORPUS)
    loose = triage.intent_groups(vectors, threshold=1.5)
    assert loose == [[0], [1], [2]]
    assert sorted(i for group in loose for i in group) == [0, 1, 2]


def test_intent_grouping_is_deterministic():
    first = triage.intent_groups(triage.tfidf_vectors(CORPUS)[1])
    second = triage.intent_groups(triage.tfidf_vectors(CORPUS)[1])
    assert first == second


def test_tfidf_keeps_digits():
    # THE digit case: normalisation that strips numbers makes these one document.
    docs = ["tracer bw cutout 8", "tracer bw cutout 16"]
    idf, vectors = triage.tfidf_vectors(docs)
    eight = triage.tfidf_query("tracer bw cutout 8", idf, vectors)
    sixteen = triage.tfidf_query("tracer bw cutout 16", idf, vectors)
    assert eight[0] == pytest.approx(1.0)
    assert sixteen[1] == pytest.approx(1.0)
    # Each query prefers its own document, so the digits carry real weight ...
    assert eight[0] > eight[1]
    assert sixteen[1] > sixteen[0]
    # ... while the two still group as one intent, which is the point of the phase.
    assert triage.intent_groups(vectors) == [[0, 1]]


def test_tfidf_handles_empty_corpus_and_empty_documents():
    idf, vectors = triage.tfidf_vectors([])
    assert vectors == []
    assert triage.tfidf_query("anything", idf, vectors) == []

    idf, vectors = triage.tfidf_vectors(["", "poster sunset"])
    assert triage.tfidf_query("", idf, vectors) == [0.0, 0.0]
    # An empty document has no direction: it matches nothing, not everything.
    assert triage.tfidf_query("poster sunset", idf, vectors)[0] == 0.0
    assert triage.tfidf_query("poster sunset", idf, vectors)[1] == pytest.approx(1.0)


def test_tfidf_query_tolerates_grams_the_corpus_never_saw():
    idf, vectors = triage.tfidf_vectors(CORPUS)
    assert triage.tfidf_query("zzzz qqqq", idf, vectors) == [0.0, 0.0, 0.0]


def test_doc_text_flattens_mappings_and_iterables_deterministically():
    assert triage.doc_text(None) == ""
    assert triage.doc_text("text") == "text"
    # A tag set has no meaningful order, so it is sorted ...
    assert triage.doc_text(["b", "a"]) == "a b"
    assert triage.doc_text({"z": 1, "a": "x"}) == "a x z 1"
    # ... but a sequence of non-strings keeps the order it was given.
    assert triage.doc_text(["a", ["b"]]) == "a b"


def test_doc_text_keeps_runes_whole():
    # Normalising by bytes would split the accented letters apart.
    assert triage.doc_text("Résumé") == "Résumé"
    assert len(triage.norm(triage.doc_text("é"))) == 1


def test_candidate_text_is_stable_under_tag_order():
    one = triage.candidate_text({"prompt": "Engraved floral",
                                 "tags": ["bw", "cutout", "shirt"]})
    two = triage.candidate_text({"prompt": "Engraved floral",
                                 "tags": ["shirt", "cutout", "bw"]})
    assert one == two
    assert one.startswith("Engraved floral")
    assert "cutout" in one


def test_candidate_text_carries_finding_text():
    text = triage.candidate_text({
        "prompt": "floral",
        "tags": ["poster"],
        "findings": [{"rule": "NODE_BUDGET", "detail": "path 3 has 900 nodes"}]})
    assert "floral" in text
    assert "poster" in text
    # The number is what makes the finding a distinct document.
    assert "900" in text


def test_candidate_intent_groups_scores_candidates_against_each_other():
    candidates = [
        {"id": "c1", "prompt": "tracer bw cutout 8", "tags": ["shirt"]},
        {"id": "c2", "prompt": "tracer bw cutout 16", "tags": ["shirt"]},
        {"id": "c3", "prompt": "poster sunset landscape", "tags": ["mug"]},
    ]
    idf, vectors, groups = triage.candidate_intent_groups(candidates)
    assert groups == [[0, 1], [2]]
    # The near-duplicates are one family while still being distinguishable.
    scores = triage.tfidf_query(triage.candidate_text(candidates[0]),
                                idf, vectors)
    assert scores[0] > scores[1] > scores[2]


def test_candidate_with_no_text_matches_nothing():
    idf, vectors, groups = triage.candidate_intent_groups(
        [{}, {"prompt": "poster sunset"}])
    assert groups == [[0], [1]]
    assert triage.tfidf_query("poster sunset", idf, vectors)[0] == 0.0


# --------------------------------------------------------------------------- #
# triage_defaults / load_triage                                                #
# --------------------------------------------------------------------------- #

EXPECTED_DEFAULTS = {
    "max_unique_colors": 8,
    "max_speckles": 40,
    "max_glow_area_pct": 8,
    "min_silhouette_iou": 0.85,
    "min_luma_delta": 12,
    "weak_needed": 3,
    "bucket_cap": 2,
    "learn_min_rejections": 3,
    "learn_min_jobs": 2,
    # Phase 1 gate thresholds: the artwork-MAE cliff and noise floor the
    # comparison already sorts on, plus the geometry cap the gate mirrors.
    "max_nodes_per_path": 500,
    "max_mae_art": 8.0,
    "borderline_mae_art": 1.0,
}


def test_triage_defaults_exact_values():
    assert triage.triage_defaults() == EXPECTED_DEFAULTS


def test_triage_defaults_cover_every_gate_threshold():
    # A threshold with no default is a threshold no spec can override, which is
    # how a gate silently becomes a constant.
    defaults = triage.triage_defaults()
    for key in ("max_unique_colors", "max_speckles", "max_glow_area_pct",
                "min_silhouette_iou", "min_luma_delta", "max_nodes_per_path",
                "max_mae_art", "borderline_mae_art", "weak_needed"):
        assert key in defaults


def test_load_triage_overrides_the_phase1_thresholds():
    merged = triage.load_triage({"triage": {"max_mae_art": 20.0,
                                            "borderline_mae_art": 0.5,
                                            "max_nodes_per_path": 100}})
    assert merged["max_mae_art"] == 20.0
    assert merged["borderline_mae_art"] == 0.5
    assert merged["max_nodes_per_path"] == 100
    # Untouched thresholds keep their documented values.
    assert merged["max_unique_colors"] == 8
    assert merged["min_luma_delta"] == 12


def test_triage_defaults_returns_a_copy():
    first = triage.triage_defaults()
    first["max_speckles"] = 999
    assert triage.triage_defaults() == EXPECTED_DEFAULTS


def test_load_triage_defaults_when_absent():
    assert triage.load_triage(None) == EXPECTED_DEFAULTS
    assert triage.load_triage({}) == EXPECTED_DEFAULTS
    assert triage.load_triage({"max_colors": 6}) == EXPECTED_DEFAULTS
    # An explicit null block behaves like an omitted one.
    assert triage.load_triage({"triage": None}) == EXPECTED_DEFAULTS
    assert triage.load_triage({"triage": {}}) == EXPECTED_DEFAULTS


def test_load_triage_override_wins_per_key():
    merged = triage.load_triage({"triage": {"max_speckles": 7, "weak_needed": 1}})
    assert merged["max_speckles"] == 7
    assert merged["weak_needed"] == 1
    # Untouched keys keep their defaults.
    assert merged["max_unique_colors"] == 8
    assert merged["bucket_cap"] == 2
    assert merged["learn_min_jobs"] == 2


def test_load_triage_keeps_unknown_keys():
    # A newer spec may add a knob; this module must not silently drop it.
    merged = triage.load_triage({"triage": {"future_knob": "on"}})
    assert merged["future_knob"] == "on"
    assert merged["max_speckles"] == 40


def test_load_triage_does_not_mutate_the_spec():
    spec = {"triage": {"max_speckles": 7}}
    triage.load_triage(spec)
    assert spec == {"triage": {"max_speckles": 7}}


def test_spec_example_block_loads():
    # The block the pipeline will actually read has to round-trip through the
    # defaults without special-casing.
    spec = {"triage": {"max_unique_colors": 6, "bucket_cap": 3}}
    merged = triage.load_triage(spec)
    assert merged["max_unique_colors"] == 6
    assert merged["bucket_cap"] == 3
    assert set(merged) >= set(EXPECTED_DEFAULTS)
