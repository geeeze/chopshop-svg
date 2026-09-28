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
(``monkeypatch.setattr`` does the restore), because the rule lists ship EMPTY in
this phase -- the rules land in a later slice.
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
# gate                                                                         #
# --------------------------------------------------------------------------- #


def test_rules_start_empty():
    # Phase 0 ships the shape, not the rules.
    assert triage.HARD == []
    assert triage.WEAK == []


def test_empty_rule_lists_pass_everything():
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
}


def test_triage_defaults_exact_values():
    assert triage.triage_defaults() == EXPECTED_DEFAULTS


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
