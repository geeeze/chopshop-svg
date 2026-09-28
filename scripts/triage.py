#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
triage.py -- candidate-triage primitives: quality gates + bucket + dedupe + text-sim.

Phase 0 landed the shapes (canonical form, hashes, thumbnail metrics, bucket key,
rule-list plumbing, text scoring).  Phase 1 fills the rule lists with the actual
quality gates -- drop / demote / pass, always with named reasons.  Phase 3 adds
tf-idf intent grouping over prompt/tag/finding text.

This module is deliberately pure and side-effect free.  It holds the deterministic
building blocks a later stage needs in order to answer, for every trace candidate
the pipeline produces, three questions that are cheap to ask and expensive to get
wrong:

1. **Is it the same file?** -- ``canonical_svg`` / ``svg_sha`` / ``file_sha256``.
   Formatting-only differences (attribute order, indentation, editor comments,
   regenerated ``id`` attributes) must not read as new work, and every artifact the
   pipeline writes records a checksum taken at creation time.
2. **Is it the same picture?** -- ``dhash`` / ``gray_cosine``.  Two candidates can
   differ in every byte of SVG text and still be the same visual result, and two
   candidates can be byte-identical in structure and differ in the artwork.  The
   rendered thumbnail is the signal here, never the SVG text.
3. **Is it worth keeping?** -- ``gate`` / ``bucket_of``.  A rejection must be able
   to collapse a whole family of near-identical candidates, not just the one file
   that was rejected by hand.  The gate drops, demotes or passes a candidate and
   names every rule that fired; it never picks a winner, and a later stage is what
   turns a bucket into a decision a human reviews.

Nothing in this module reads the spec file or writes anything: ``load_triage``
merges the ``triage`` block of an already-parsed spec over the documented
defaults, and everything else takes its inputs as arguments.

Canonicalization (``canonical_svg``)
------------------------------------
Approach: ``xml.etree.ElementTree`` (stdlib) parses the document, then the tree is
rewritten in place -- comments are dropped by the parser, ``id`` attributes are
removed, every attribute is whitespace-normalized and re-inserted in sorted order,
path/``points`` data is tokenized and re-emitted with single-space separators, and
every number in path data, ``transform`` and the positional attributes (``x`` ``y``
``x1`` ``y1`` ``x2`` ``y2`` ``cx`` ``cy`` ``r`` ``rx`` ``ry``) is rounded to 2
decimal places.  A regex tokenizer does the numbers; ElementTree does the structure
and the serialization.  No external tool is invoked at any point, and no node is
moved between elements.

Why 2 decimal places: traced path data carries 5-8 decimals of float noise that is
invisible at print resolution, and the noise is what makes two reruns of the same
trace produce different bytes.  2dp (0.01 user units) is finer than any plottable
feature and coarse enough to absorb that noise, so a regenerate-and-reformat cycle
hashes equal without changing which points the path visits.

Caveats, documented because they are load-bearing:
* Child order is NOT sorted.  Paint order is meaningful in SVG, so reordering
  children really is a different document.
* ``id`` removal means internal references (``fill="url(#grad1)"``, ``clip-path``,
  ``mask``, ``use href``) become dangling in the canonical form.  The canonical
  form is a dedupe/fingerprint aid, not a claim of semantic equivalence: use it to
  decide "have I already seen this file", not to substitute one file for another.
* Whitespace-only text/tail nodes are dropped (that is how pretty-printing is
  absorbed) and runs of whitespace inside text collapse to one space.  A ``<text>``
  element that relies on leading/trailing whitespace for spacing will canonicalize
  to the collapsed content.
* A document with an SVG namespace declaration and one without are different
  documents; the namespace is preserved rather than normalized away.

Text similarity (``norm`` / ``grams`` / ``cosine`` / ``text_sim``)
-----------------------------------------------------------------
This is for prompts, filenames and finding text -- never for SVG geometry.
``norm`` lowercases, keeps letters, DIGITS and spaces (iterating runes, not bytes),
and turns every other character into a space, then collapses whitespace.  Digits
are kept on purpose: dropping them makes ``filter_speckle=8`` and
``filter_speckle=16`` score as identical strings.  Numeric trace parameters are
compared as numbers, by the ranker, and never through this scorer.

Phase 3 extends this section with a small tf-idf: ``tfidf_vectors`` turns a corpus
of candidate documents (prompt, tags, finding text) into one sparse,
L2-normalised vector each, ``tfidf_query`` scores a query against all of them, and
``intent_groups`` buckets the near-duplicates.  Pure Python -- no numpy, scipy or
sklearn -- because the scorer has to run anywhere the pipeline runs.  It is a
grouping aid, not a ranker: any ordering it produces keeps every index visible so
the uncertain members of a group can be seen and judged by a human.

Bucket keys
-----------
``bucket_of`` returns ``"{preset}|s{bin}|c{bin}|{prompt_family}"``.  The whole point
of the key is that one human rejection of one candidate can retire every candidate
that shares it, so the bins are deliberately coarse -- and their widest edge lines
up with the corresponding spec default (speckles up to ``max_speckles`` 40, colours
up to ``max_unique_colors`` 8).  ``gate`` fills the key from the candidate's
run-record fields -- sweep preset, measured speckle count, unique colour count,
prompt family -- and leaves it ``None`` for a candidate carrying none of them, so
the gate stays usable on partial data instead of inventing a family for it.

Gate skeleton
-------------
This section is now Phase 1: see ``Quality gates (phase 1)`` below.

``HARD`` and ``WEAK`` hold ``(name, fn)`` pairs with ``fn(candidate) -> bool``.  A
hard hit drops the candidate immediately; otherwise ``weak_needed`` weak hits
demote it.  ``Verdict.reasons`` carries rule NAMES -- one per fired signal, in
list order -- so a decision is explainable from the JSON alone, and ``bucket``
carries the family key a rejection can be applied to.

Hard signals: artwork lost (the ``artwork_lost`` fidelity verdict, or artwork MAE
past ``max_mae_art``), silhouette overlap below ``min_silhouette_iou``, glow area
past ``max_glow_area_pct``, more than ``max_unique_colors`` unique colours, and a
hard geometry finding already recorded by Layer A/B.  Weak signals: speckle count
past ``max_speckles``, a colour count at or one under the cap, luma separation
below ``min_luma_delta``, a path over ``max_nodes_per_path``, and borderline
artwork MAE (``borderline_mae_art`` < ``mae_art`` <= ``max_mae_art``).

Every rule reads the candidate and returns False when its reading is missing:
"not measured" and "measured bad" must never collapse into the same verdict.  The
observations the Layer A/B records do not carry (``speckle_count``,
``glow_area_pct``, ``luma_delta``, ``silhouette_iou``) arrive under
``comparison.metrics``; everything else is read from the run-record shape the
comparison already writes.

Thresholds come from ``thresholds_of(candidate)``, which is ``load_triage``
applied to the candidate: a candidate carrying its run's merged ``triage`` block
overrides any threshold the spec set, and a candidate carrying none gets the
documented defaults.

Usage
-----
::

    python3 -c "import sys; sys.path.insert(0,'scripts'); import triage; \
print(triage.svg_sha(triage.canonical_svg(open('art.svg').read())))"

No CLI: this slice is a library.  ``python3 scripts/triage.py --selftest`` runs a
small deterministic self-check and exits 0.
"""

from __future__ import annotations

import hashlib
import math
import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum

from PIL import Image

# --------------------------------------------------------------------------- #
# Constants                                                                   #
# --------------------------------------------------------------------------- #

#: Decimal places used for every coordinate in the canonical form.  See the module
#: docstring for why 2dp is the chosen granularity.
COORD_PLACES = 2

#: Attributes holding path/point data: tokenized, numbers rounded, separators
#: rewritten to a single canonical space.
PATH_ATTRS = frozenset(("d", "points"))

#: Attributes holding CSS transform functions: numbers rounded, whitespace and
#: comma spacing normalized without touching the function syntax.
TRANSFORM_ATTRS = frozenset(("transform",))

#: Attributes holding a single coordinate each: numbers rounded.  Width/height are
#: deliberately excluded -- they are sizes that may carry a unit (``100%``,
#: ``12pt``) and rounding them buys nothing for the byte-stability of path data.
NUMBER_ATTRS = frozenset(("x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry"))

#: Speckle-count bin edges (inclusive upper bound per bin, ascending):
#: 0 -> bin 0, 1-5 -> bin 1, 6-20 -> bin 2, 21-40 -> bin 3, 41+ -> bin 4.
#: The top edge matches the ``max_speckles`` default of 40, so "at the limit"
#: and "over the limit" cannot land in the same bucket.
SPECKLE_EDGES = (0, 5, 20, 40)

#: Unique-colour bin edges (inclusive upper bound per bin, ascending):
#: 0-1 -> bin 0 (single ink), 2-3 -> bin 1, 4-5 -> bin 2, 6-8 -> bin 3,
#: 9+ -> bin 4.  The 8 edge matches the ``max_unique_colors`` default.
COLOR_EDGES = (1, 3, 5, 8)

#: Character n-gram width used by ``text_sim``.
GRAM_N = 2

#: SVG namespace, registered as the default namespace so that serializing a parsed
#: SVG back out cannot turn ``<svg>`` into ``<ns0:svg>``.
_SVG_NS = "http://www.w3.org/2000/svg"

_NUM_RE = re.compile(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")
_TOKEN_RE = re.compile(r"[A-Za-z]|[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")
_WS_RE = re.compile(r"\s+")
_COMMA_RE = re.compile(r"\s*([,])\s*")
_OPEN_RE = re.compile(r"\(\s+")
_CLOSE_RE = re.compile(r"\s+\)")

# --------------------------------------------------------------------------- #
# 1-3. Identity: canonical form, its hash, and the file bytes hash             #
# --------------------------------------------------------------------------- #


def _fmt_num(value):
    """Format one number to ``COORD_PLACES`` decimals, without a sign surprise.

    ``-0.00`` is normalized to ``0.00`` so that a value that rounds to zero has
    one spelling.
    """
    out = "{0:.{1}f}".format(value, COORD_PLACES)
    if out.startswith("-") and float(out) == 0.0:
        out = out[1:]
    return out


def _round_numbers(value):
    """Round every number inside an attribute value, leaving separators alone."""

    def repl(match):
        try:
            return _fmt_num(float(match.group(0)))
        except ValueError:  # pragma: no cover - regex only matches numbers
            return match.group(0)

    return _NUM_RE.sub(repl, value)


def _canon_path_data(value):
    """Tokenize path/``points`` data and re-emit it with canonical separators.

    Command letters and numbers are the tokens; commas and every run of whitespace
    are only separators, so replacing them with single spaces makes ``"M1.23 2"``
    and ``"M 1.23, 2"`` identical.  Tokens are never dropped, added or reordered,
    which is what keeps the implicit-repeat forms (``"M1 2 3 4"``) meaning what
    they meant.
    """
    tokens = _TOKEN_RE.findall(value)
    if not tokens:
        return _norm_ws(value)
    out = []
    for token in tokens:
        if token[0].isalpha():
            out.append(token)
        else:
            try:
                out.append(_fmt_num(float(token)))
            except ValueError:  # pragma: no cover - regex only matches numbers
                out.append(token)
    return " ".join(out)


def _canon_transform(value):
    """Round transform numbers and normalize spacing without breaking the syntax.

    ``translate( 2.5 )`` and ``translate(2.5)`` collapse to the same spelling, but
    the function name and its parentheses are never split apart -- a space between
    ``translate`` and ``(`` would not be a valid transform.
    """
    value = _round_numbers(_norm_ws(value))
    value = _COMMA_RE.sub(r"\1", value)
    value = _OPEN_RE.sub("(", value)
    return _CLOSE_RE.sub(")", value)


def _norm_ws(value, empty_to_none=False):
    """Collapse whitespace runs to a single space and strip the edges.

    ``None`` stays ``None``.  With ``empty_to_none`` a value that collapses to the
    empty string becomes ``None`` -- that is how pretty-printing whitespace
    disappears from the canonical form.
    """
    if value is None:
        return None
    out = _WS_RE.sub(" ", value).strip()
    if empty_to_none and not out:
        return None
    return out


def _is_id_attr(key):
    """True for ``id`` and any namespace-qualified ``{ns}id``."""
    return key == "id" or key.endswith("}id")


def canonical_svg(text):
    """Return the canonical text form of an SVG document.

    Comments are dropped, ``id`` attributes removed, attribute order sorted,
    geometry numbers rounded to ``COORD_PLACES``, and whitespace normalized --
    all in pure stdlib text/XML handling, with no external tool.  Two documents
    that differ only by formatting therefore return the same string, and
    ``svg_sha`` of that string is the file's stable fingerprint.
    """
    parser = ET.XMLParser()
    parser.feed(text)
    root = parser.close()

    # Only needed when the document actually uses the SVG namespace; harmless for
    # a fragment without one.
    try:
        ET.register_namespace("", _SVG_NS)
    except ValueError:  # pragma: no cover - malformed prefix, never for ""
        pass

    for elem in root.iter():
        for key in list(elem.attrib):
            if _is_id_attr(key):
                del elem.attrib[key]

        cleaned = {}
        for key, value in elem.attrib.items():
            value = _norm_ws(value)
            if key in PATH_ATTRS:
                value = _canon_path_data(value)
            elif key in TRANSFORM_ATTRS:
                value = _canon_transform(value)
            elif key in NUMBER_ATTRS:
                value = _round_numbers(value)
            cleaned[key] = value
        # Sorted insertion: ElementTree serializes attributes in dict order, so
        # this is what makes attribute reordering a no-op.
        elem.attrib.clear()
        for key in sorted(cleaned):
            elem.attrib[key] = cleaned[key]

        elem.text = _norm_ws(elem.text, empty_to_none=True)
        elem.tail = _norm_ws(elem.tail, empty_to_none=True)

    return ET.tostring(root, encoding="unicode")


def svg_sha(canonical_text):
    """sha256 hex digest of canonical SVG text."""
    return hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()


def file_sha256(path):
    """Streamed sha256 hex digest of a file's bytes.

    This is the checksum-at-creation primitive: every artifact the pipeline writes
    records one so a later stage can tell whether the bytes it is looking at are
    the bytes that were produced.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# 4. Near-duplicate detection on rendered thumbnails                          #
# --------------------------------------------------------------------------- #


#: Grayscale thumbnail the near-duplicate metrics work on.
THUMB = 32


def _gray32(img):
    """Flatten a PIL Image to a 32x32 grayscale byte list.

    The input is a render of the candidate (a thumbnail), not the SVG: this is
    the "do these two look the same" signal, and it is intentionally blind to
    what the vector source says.  A same-size image passes through unchanged,
    which is what makes the metrics reproducible in tests.
    """
    return list(img.convert("L").resize((THUMB, THUMB),
                                        Image.Resampling.LANCZOS).tobytes())


def dhash(img):
    """Difference hash of a rendered thumbnail as an int bitmask.

    Grayscale, resized to 32x32; for each pixel the bit is set when it is darker
    than its right neighbour, one bit per comparison in row-major order.  The last
    column of each row has no right neighbour and contributes no bit, so the
    bitmask is 992 bits wide and fits any Python int.  Identical images give an
    identical hash; a 1-2px shift at full size (roughly a half pixel at 32x32) is
    usually absorbed, a 3px shift is not -- see the tests for the measured
    tolerance.
    """
    gray = _gray32(img)
    bits = 0
    index = 0
    for row_start in range(0, len(gray), 32):
        for col in range(31):
            if gray[row_start + col] > gray[row_start + col + 1]:
                bits |= 1 << index
            index += 1
    return bits


def gray_cosine(a, b):
    """Cosine similarity in [0, 1] between two 32x32 grayscale thumbnails.

    Both images are flattened to 1024 gray values and compared as vectors.  The
    result is clamped into [0, 1] (gray values are non-negative, so the raw cosine
    already is).  An all-black thumbnail has a zero vector and therefore scores
    0.0 against everything, including another all-black one; two identical
    non-black images score exactly 1.0.  Near-dupes are not penalised: the same
    artwork re-rendered with a few units of per-pixel noise still scores >0.99,
    which is the tolerance this metric is meant to have.
    """
    va = _gray32(a)
    vb = _gray32(b)
    dot = 0
    sq_a = 0
    sq_b = 0
    for x, y in zip(va, vb):
        dot += x * y
        sq_a += x * x
        sq_b += y * y
    if sq_a == 0 or sq_b == 0:
        return 0.0
    cos = dot / ((sq_a ** 0.5) * (sq_b ** 0.5))
    return max(0.0, min(1.0, cos))


# --------------------------------------------------------------------------- #
# 5. Bucket key                                                               #
# --------------------------------------------------------------------------- #


def _bin_(value, edges):
    """Coarse bin index for a value against ascending inclusive upper bounds.

    Values below the first edge (including negatives) land in bin 0; a value above
    the last edge lands in ``len(edges)``.
    """
    for index, upper in enumerate(edges):
        if value <= upper:
            return index
    return len(edges)


def bucket_of(preset, speckles, colors, prompt_family=""):
    """Family key for a candidate: ``"{preset}|s{speckle_bin}|c{color_bin}|{family}"``.

    One rejection recorded against this key retires every candidate that shares it,
    which is why the bins are coarse.  Edges are ``SPECKLE_EDGES`` and
    ``COLOR_EDGES``; both top edges match the corresponding spec default.
    """
    return "{0}|s{1}|c{2}|{3}".format(
        preset,
        _bin_(speckles, SPECKLE_EDGES),
        _bin_(colors, COLOR_EDGES),
        prompt_family,
    )


# --------------------------------------------------------------------------- #
# 6. Quality gates (phase 1)                                                  #
# --------------------------------------------------------------------------- #


class Disp(Enum):
    """What the gate decided about a candidate."""

    OK = "ok"
    DEMOTE = "demote"
    DROP = "drop"


@dataclass
class Verdict:
    """A gate decision: what happened, why, and which family it belonged to.

    ``bucket`` carries ``bucket_of``'s key for the candidate so a rejection can be
    applied to the whole family.  It is ``None`` only for a candidate that carries
    none of the fields the key is built from -- a bare stub dict, not a run-record
    candidate.
    """

    disposition: Disp
    reasons: list = field(default_factory=list)
    bucket: "str | None" = None


# --------------------------------------------------------------------------- #
# 6a. Reading a candidate                                                     #
# --------------------------------------------------------------------------- #

# The gate reads ONE shape: the candidate records the front half already writes
# (see scripts/run_record.py).  Two of those fields live in nested blocks that may
# legitimately be missing -- a candidate that was traced but never compared has
# comparison None -- and the observations the gate needs beyond what Layer A/B
# records (speckle count, glow area, luma separation, silhouette overlap) arrive
# under ``comparison["metrics"]``.
#
# Every accessor below therefore returns None rather than raising or defaulting to
# a zero, and every rule treats None as "did not fire".  A threshold that fires on a
# missing reading would silently drop every candidate from a run that was measured
# slightly differently, which is the one failure mode a gate must not have.


def _comparison(candidate):
    """The candidate's ``comparison`` block, or an empty dict when absent."""
    if not isinstance(candidate, dict):
        return {}
    comp = candidate.get("comparison")
    return comp if isinstance(comp, dict) else {}


def _metrics(candidate):
    """The candidate's ``comparison.metrics`` block, or an empty dict.

    This is where a run records the measurements the Layer A/B result does not
    carry: ``speckle_count``, ``glow_area_pct``, ``luma_delta``,
    ``silhouette_iou``.  All optional, all read by name.
    """
    metrics = _comparison(candidate).get("metrics")
    return metrics if isinstance(metrics, dict) else {}


def _number(value):
    """The value as a number, or None when it is not a real reading.

    ``bool`` is an ``int`` in Python, so a True where a measurement belongs would
    otherwise read as exactly 1 and fire a threshold.  It is rejected here: that
    is a broken record upstream, not a measurement.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _metric(candidate, name):
    """A named reading from ``comparison.metrics``, or None."""
    return _number(_metrics(candidate).get(name))


def _mae_art(candidate):
    """Artwork-only MAE from ``comparison.fidelity``, or None when unmeasured.

    ``fidelity.measured`` False means the render was never diffed (no inkscape, no
    source raster); the field is present but meaningless, so it does not count.
    """
    fidelity = _comparison(candidate).get("fidelity")
    if not isinstance(fidelity, dict) or not fidelity.get("measured"):
        return None
    return _number(fidelity.get("mae_art"))


def _colour_count(candidate):
    """The candidate's unique-colour count: declared first, then rendered ink.

    ``declared_colors`` is what the SVG says it paints; ``rendered_ink_colors`` is
    what the render produced and includes the background, so the declared count is
    the better reading when both exist.  Either way it is one number, so the hard
    cap, the near-cap weak rule and the bucket bin cannot disagree about it.
    """
    comp = _comparison(candidate)
    for key in ("declared_colors", "rendered_ink_colors"):
        value = _number(comp.get(key))
        if value is not None:
            return int(value)
    return None


def _speckle_count(candidate):
    """Measured speckle (tiny-island) count, or None when the run did not count one.

    Note the difference from ``sweep.filter_speckle``: that is VTracer's minimum
    ISLAND AREA parameter, not a count, and the two run in opposite directions (a
    larger ``filter_speckle`` removes more, so it yields FEWER speckles).  Feeding
    the parameter into a count-shaped bin would invert the bucket, so the bucket
    uses the measured count and nothing else.
    """
    count = _metric(candidate, "speckle_count")
    return None if count is None else int(count)


# --------------------------------------------------------------------------- #
# 6b. HARD rules -- any single hit drops the candidate                        #
# --------------------------------------------------------------------------- #


def _rule_artwork_lost(candidate):
    """HARD: the trace does not reproduce the design (silhouette/artwork lost).

    Two readings, either one fires.  Either ``comparison.fidelity_verdict`` is
    already ``"artwork_lost"`` -- what compare_candidates.py recorded for this
    candidate -- or artwork MAE is past ``max_mae_art``, the same cliff that
    verdict is derived from, so the report and the gate cannot disagree about
    which candidates are silhouettes.

    Why this is hard and not advisory: a bw/binary trace of a colour design
    scores hard=0 on both layers and can still throw the artwork away entirely
    (mae_art 104 against 0.005 for the faithful ones).  The comparison sorts those
    candidates by artwork survival for exactly this reason; the triage gate is
    where such a family leaves the list.  The fidelity verdict stays advisory in
    compare_candidates' gate counts -- that is a print decision -- but a candidate
    whose artwork did not survive has nothing for a human to review.
    """
    comp = _comparison(candidate)
    if comp.get("fidelity_verdict") == "artwork_lost":
        return True
    # rules-thresholds-of: the spec's triage block reaches the rule here.
    mae_art = _mae_art(candidate)
    return mae_art is not None and mae_art > thresholds_of(candidate)["max_mae_art"]


def _rule_silhouette_lost(candidate):
    """HARD: the rendered silhouette covers too little of the source's.

    ``min_silhouette_iou`` (0.85) is the overlap floor between the traced
    silhouette and the source's.  It fires only when the run measured an overlap:
    a missing reading is not a lost silhouette.
    """
    iou = _metric(candidate, "silhouette_iou")
    return iou is not None and iou < thresholds_of(candidate)["min_silhouette_iou"]


def _rule_glow_area(candidate):
    """HARD: too much of the canvas is halo/glow rather than printed area.

    ``max_glow_area_pct`` (8%) is the ceiling on the soft fringe the trace picked
    up around the artwork.  Past it the design does not stop at an edge; it fades
    into the garment, which is a screen-print defect, not a style.
    """
    pct = _metric(candidate, "glow_area_pct")
    return pct is not None and pct > thresholds_of(candidate)["max_glow_area_pct"]


def _rule_colour_cap_exceeded(candidate):
    """HARD: more unique colours than the job can print.

    ``max_unique_colors`` (8) is the documented cap.  A candidate over it needs
    screens the job does not have, so no amount of fidelity redeems it.  Counts
    via ``_colour_count`` (declared, else rendered ink), which is the same reading
    the near-cap weak rule and the bucket bin use.
    """
    count = _colour_count(candidate)
    return count is not None and count > thresholds_of(candidate)["max_unique_colors"]


def _rule_geometry_hard_fail(candidate):
    """HARD: an unprintable-geometry failure is already on the record.

    Layer A (source validation) and Layer B (render preflight) grade the geometry
    itself -- stroke width, node counts, open paths, embedded rasters.  A hard
    finding there is a fact about the file, not a measurement to weigh against
    other measurements, so this gate does not re-derive it: it reads the counts
    the comparison recorded.

    ``passed`` is deliberately NOT used on its own: a layer that never ran reports
    passed=False too, and "not checked" is not "failed".  Only recorded hard
    findings count.
    """
    comp = _comparison(candidate)
    for layer in ("layer_a", "layer_b"):
        block = comp.get(layer)
        if isinstance(block, dict) and (_number(block.get("hard")) or 0) > 0:
            return True
    return (_number(comp.get("hard")) or 0) > 0


#: Hard rules: ``(name, fn)`` with ``fn(candidate) -> bool``.  A single hit drops
#: the candidate.  Order is the documented evaluation order and the order reasons
#: are reported in.
HARD = [
    ("ARTWORK_LOST", _rule_artwork_lost),
    ("SILHOUETTE_LOST", _rule_silhouette_lost),
    ("GLOW_AREA_HIGH", _rule_glow_area),
    ("COLOR_CAP_EXCEEDED", _rule_colour_cap_exceeded),
    ("GEOMETRY_HARD_FAIL", _rule_geometry_hard_fail),
]


# --------------------------------------------------------------------------- #
# 6c. WEAK rules -- enough of them demote the candidate                       #
# --------------------------------------------------------------------------- #


def _rule_speckle_heavy(candidate):
    """WEAK: more tiny islands than ``max_speckles`` (40) allows.

    Speckles are the noise the tracer kept: thousands of pinprick regions that
    print as grit.  Weak rather than hard because a speckled candidate is still
    the artwork -- it is the retrace-with-a-higher-filter case, which a human may
    well want to look at.
    """
    count = _speckle_count(candidate)
    return count is not None and count > thresholds_of(candidate)["max_speckles"]


def _rule_colours_near_cap(candidate):
    """WEAK: the colour count is at, or one under, the cap.

    One screen away from ``max_unique_colors`` is a cost decision for a human, not
    a reason for the machine to hide the candidate.  Over the cap is
    ``COLOR_CAP_EXCEEDED`` (hard); this rule only covers the last two steps up to
    it.
    """
    cap = thresholds_of(candidate)["max_unique_colors"]
    count = _colour_count(candidate)
    return count is not None and cap - 1 <= count <= cap


def _rule_low_luma_contrast(candidate):
    """WEAK: adjacent inks are closer in luma than ``min_luma_delta`` (12).

    Below that separation two distinct inks read as one shape on a shirt: the
    design loses a layer without losing a colour, which no colour-count check can
    see.
    """
    delta = _metric(candidate, "luma_delta")
    return delta is not None and delta < thresholds_of(candidate)["min_luma_delta"]


def _rule_node_overload(candidate):
    """WEAK: a single path carries more nodes than the geometry cap allows.

    ``max_nodes_per_path`` mirrors ``spec.geometry.max_nodes_per_path`` (500), so
    the spec can retune either without the gate drifting from Layer A.  Over it is
    a redraw cost and a symptom of a trace that followed noise instead of edges.
    """
    nodes = _number(_comparison(candidate).get("node_count_max"))
    return nodes is not None and nodes > thresholds_of(candidate)["max_nodes_per_path"]


def _rule_borderline_fidelity(candidate):
    """WEAK: artwork MAE is off, but not destroyed.

    ``borderline_mae_art`` (1.0) and ``max_mae_art`` (8.0) are the thresholds
    compare_candidates.py uses to separate ``faithful`` from ``drift`` from
    ``artwork_lost``.  A candidate its report called "drift" is exactly the one
    this flags: measurably departed from the source, still recognisably the
    design, worth a human look rather than a drop.
    """
    thresholds = thresholds_of(candidate)
    mae_art = _mae_art(candidate)
    return (mae_art is not None
            and thresholds["borderline_mae_art"] < mae_art
            <= thresholds["max_mae_art"])


#: Weak rules: ``(name, fn)`` with ``fn(candidate) -> bool``.  ``weak_needed`` of
#: them demote the candidate.  Same ordering rules as ``HARD``.
WEAK = [
    ("SPECKLE_HEAVY", _rule_speckle_heavy),
    ("COLORS_NEAR_CAP", _rule_colours_near_cap),
    ("LOW_LUMA_CONTRAST", _rule_low_luma_contrast),
    ("NODE_OVERLOAD", _rule_node_overload),
    ("BORDERLINE_FIDELITY", _rule_borderline_fidelity),
]


# --------------------------------------------------------------------------- #
# 6d. The decision                                                            #
# --------------------------------------------------------------------------- #


def _bucket_for(candidate):
    """The bucket key for a candidate, or None when there is nothing to key on.

    ``bucket_of`` needs a preset, a speckle count, a colour count and a prompt
    family.  A run-record candidate carries all four (two of them as optional
    measurements); a bare stub dict carries none, and inventing a family for it
    would silently merge unrelated candidates into one bucket -- a rejection of
    one would then retire the others.  So: no fields, no bucket, ``None``.

    ``prompt_family`` is taken from the candidate as written (a later phase fills
    it from the prompt/tag text -- see ``intent_groups``); nothing here guesses it,
    because a guessed family is a rejection applied to candidates the human never
    saw.
    """
    if not isinstance(candidate, dict):
        return None
    if "sweep" not in candidate and "comparison" not in candidate:
        return None
    sweep = candidate.get("sweep")
    sweep = sweep if isinstance(sweep, dict) else {}
    speckles = _speckle_count(candidate)
    colors = _colour_count(candidate)
    # gate-bucket-of
    return bucket_of(sweep.get("preset") or "",
                     speckles if speckles is not None else 0,
                     colors if colors is not None else 0,
                     candidate.get("prompt_family") or "")


def gate(candidate, weak_needed=3):
    """Evaluate ``candidate`` against the HARD and WEAK rule lists.

    Hard hit -> ``Disp.DROP`` (weak rules are not even consulted: a doomed
    candidate's advisories are noise).  Otherwise ``weak_needed`` or more weak hits
    -> ``Disp.DEMOTE``, fewer -> ``Disp.OK``.  ``reasons`` holds the names of the
    rules that fired, in list order, so the same candidate always yields the same
    verdict and the same reason order, and ``bucket`` carries ``bucket_of``'s key
    for the family (``None`` for a candidate carrying none of the run-record
    fields).

    Thresholds are per-candidate, from ``thresholds_of(candidate)``: a candidate
    carrying its run's merged ``triage`` block is gated with the spec's values, a
    candidate carrying none with the documented defaults.  ``weak_needed`` stays a
    parameter because the caller may want a stricter or looser run than the spec
    asks for; to honour a spec's own ``triage.weak_needed``, pass it --
    ``gate(candidate, weak_needed=thresholds_of(candidate)["weak_needed"])``.

    This function decides nothing about a print job and chooses no winner: it
    returns a disposition and the names of the rules behind it.
    """
    # gate-hard-rules
    hard_hits = [name for name, fn in HARD if fn(candidate)]
    if hard_hits:
        return Verdict(Disp.DROP, hard_hits, _bucket_for(candidate))

    # gate-weak-rules
    weak_hits = [name for name, fn in WEAK if fn(candidate)]
    if len(weak_hits) >= weak_needed:
        return Verdict(Disp.DEMOTE, weak_hits, _bucket_for(candidate))

    return Verdict(Disp.OK, weak_hits, _bucket_for(candidate))


# --------------------------------------------------------------------------- #
# 7. Text similarity (prompts, filenames, finding text -- never geometry)     #
# --------------------------------------------------------------------------- #


def norm(s):
    """Normalize text for similarity scoring: lowercase, alphanumeric + spaces.

    Iterates runes (``str`` characters), not bytes, so a non-ASCII char is one
    character and does not get split into its UTF-8 bytes.  Every non-alphanumeric
    character becomes a space; runs of whitespace collapse.  DIGITS ARE KEPT: a
    scorer that strips them cannot tell ``filter_speckle=8`` from
    ``filter_speckle=16``.  Numeric trace parameters are compared as numbers by the
    ranker; this scorer only ever sees text.
    """
    out = []
    for ch in s.lower():
        out.append(ch if (ch.isalnum() or ch.isspace()) else " ")
    return _WS_RE.sub(" ", "".join(out)).strip()


def grams(tokens, n=GRAM_N):
    """Counter of overlapping character n-grams.

    Takes a single string (already ``norm``-ed or not -- callers that want stable
    scoring should normalize first) or an iterable of strings, which are joined
    with a space.  A string shorter than ``n`` yields one whole-string gram, so a
    short input still scores rather than silently behaving as empty.
    """
    if not isinstance(tokens, str):
        tokens = " ".join(tokens)
    if not tokens:
        return Counter()
    if len(tokens) < n:
        return Counter({tokens: 1})
    counts = Counter()
    for i in range(len(tokens) - n + 1):
        counts[tokens[i:i + n]] += 1
    return counts


def cosine(a, b):
    """Cosine similarity of two Counters; 0.0 if either side is empty."""
    if not a or not b:
        return 0.0
    dot = sum(count * b.get(gram, 0) for gram, count in a.items())
    if dot == 0:
        return 0.0
    sq_a = sum(count * count for count in a.values())
    sq_b = sum(count * count for count in b.values())
    return dot / ((sq_a ** 0.5) * (sq_b ** 0.5))


def text_sim(a, b):
    """Similarity in [0, 1] between two text strings (char bigrams, digits kept).

    Clamped into [0, 1]: identical strings give exactly 1.0 rather than
    1.0000000000000002 from floating-point noise.
    """
    sim = cosine(grams(norm(a)), grams(norm(b)))
    return max(0.0, min(1.0, sim))


# --------------------------------------------------------------------------- #
# 7b. TF-IDF intent grouping (phase 3)                                        #
# --------------------------------------------------------------------------- #

# The char-bigram scorer above answers "how similar are these two strings".  What
# a candidate list needs is one step up: score every candidate against every other
# and group the ones that came from the same ask, so a prompt repeated with a
# different speckle number (or the same tags on a retrace) does not read as new
# intent.  That is a corpus problem, so a corpus scorer: idf over the whole set of
# documents, tf-idf vectors per document, cosine between them.
#
# Pure Python on purpose -- no numpy/scipy/sklearn.  The scorer runs wherever the
# pipeline runs (the comparison stage already has no guarantee of a scientific
# stack) and a few hundred short documents is nothing for dicts of floats.
#
# What this section deliberately does NOT do, because an earlier attempt at the
# same job (a go crawler) got each of them wrong:
#   * it never matches on substrings of a title -- an n-gram overlap is a score,
#     not a "contains" test that a single shared word can win;
#   * it never writes a boolean from one weak phrase -- one common bigram between
#     two documents moves a cosine a little, never from 0 to 1, and the grouping
#     threshold is high;
#   * it never lets iteration order into the result -- mappings and tag lists are
#     sorted before they are joined, and every loop is over a list or a range;
#   * it never measures length in bytes -- ``norm`` walks code points, so an
#     accented word is not two characters long;
#   * it never drops digits -- ``norm`` keeps them on purpose.

#: Similarity at which two documents count as the same intent in
#: ``intent_groups``.  High on purpose: this is for NEAR-duplicates (a prompt
#: repeated with a different numeric parameter, the same tags on a retrace).  A
#: mid-range cut would merge genuinely different asks into one family, and a
#: family is what a single rejection retires.
INTENT_THRESHOLD = 0.8


def doc_text(value):
    """Flatten one document source (text, tag list, finding list, mapping) to text.

    A mapping and an all-string iterable are emitted in SORTED order: a tag set
    arrives in whatever order the run record happened to write it, and a corpus
    that reorders itself between runs would group the same candidates
    differently.  An iterable of non-strings (a list of finding dicts, say) keeps
    the order it was given -- a sequence is a sequence, and findings are ordered.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join("{0} {1}".format(key, doc_text(value[key]))
                        for key in sorted(value))
    if isinstance(value, (list, tuple, set, frozenset)):
        parts = list(value)
        if all(isinstance(part, str) for part in parts):
            parts = sorted(parts)
        return " ".join(part for part in (doc_text(part) for part in parts) if part)
    return str(value)


def candidate_text(candidate):
    """The document string for one candidate: prompt, tags, then finding text.

    Field order is fixed and non-string values are flattened deterministically
    (``doc_text``), so two candidates carrying the same intent produce the same
    document however their records were written.  A candidate with no prompt and
    no tags yields ``""``, which scores 0.0 against everything -- the honest
    reading, rather than raising or matching on noise.
    """
    if not isinstance(candidate, dict):
        return ""
    parts = []
    for key in ("prompt", "tags", "findings", "finding"):
        text = doc_text(candidate.get(key))
        if text:
            parts.append(text)
    return " ".join(parts)


def _unit(vector):
    """L2-normalise a sparse vector; the empty vector stays empty.

    Normalising means length alone cannot separate two documents.  A document with
    no grams has no direction, so it stays ``{}`` and scores 0.0 against
    everything.
    """
    squared = sum(weight * weight for weight in vector.values())
    if squared == 0:
        return {}
    norm_ = squared ** 0.5
    return {gram: weight / norm_ for gram, weight in vector.items()}


def _tfidf_vector(counts, idf):
    """One document's sparse tf-idf vector, L2-normalised.

    ``tf = 1 + log(count)``: a repeated gram counts, and repeating it twice again
    does not double its weight.  ``idf`` supplies the corpus weighting; a gram the
    corpus never saw (only reachable for a query) falls back to 1.0.
    """
    vector = {}
    for gram, count in counts.items():
        weight = (1.0 + math.log(count)) * idf.get(gram, 1.0)
        if weight:
            vector[gram] = weight
    return _unit(vector)


def tfidf_vectors(docs, n=GRAM_N):
    """TF-IDF vectors for a corpus of documents -- pure Python, no numpy.

    Returns ``(idf, vectors)``: ``idf`` maps every gram in the corpus to its
    weight, and ``vectors`` holds one sparse ``{gram: weight}`` dict per document
    in input order, L2-normalised so a long document cannot outrank a short one on
    length alone.

    Weighting is the smoothed textbook form
    ``idf = log((1 + N) / (1 + df)) + 1``.  The ``+1`` inside the log is
    load-bearing here: a corpus of near-duplicates has many grams present in every
    document, and unsmoothed idf would zero exactly those -- the grams that carry
    the shared intent.  Documents go through ``norm`` first, so digits survive
    (``filter_speckle=8`` and ``filter_speckle=16`` stay different documents) and a
    non-ASCII character counts as one code point.  Gram keys keep first-appearance
    order, which is deterministic for a given corpus.

    This is a grouping aid, not a ranker: it picks no winner, and every input
    document keeps its index in the returned list.
    """
    prepared = [grams(norm(doc_text(doc)), n) for doc in docs]
    n_docs = len(prepared)

    # Document frequency, accumulated in corpus order so the idf mapping's key
    # order is a function of the input and nothing else.
    df = {}
    for counts in prepared:
        for gram in counts:
            df[gram] = df.get(gram, 0) + 1
    idf = {gram: math.log((1.0 + n_docs) / (1.0 + df[gram])) + 1.0
           for gram in df}
    return idf, [_tfidf_vector(counts, idf) for counts in prepared]


def tfidf_query(query, idf, vectors):
    """Similarity in [0, 1] of ``query`` text against every vector, in order.

    The query is vectorised with the corpus's own ``idf``; a gram the corpus never
    saw keeps weight 1.0 and merely dilutes the query a little.  An empty query, an
    empty corpus or an empty vector scores 0.0 rather than raising, so a candidate
    with no prompt and no tags matches nothing.
    """
    query_vector = _tfidf_vector(grams(norm(doc_text(query))), idf)
    return [max(0.0, min(1.0, cosine(query_vector, vector)))
            for vector in vectors]


def intent_groups(vectors, threshold=INTENT_THRESHOLD):
    """Group near-duplicate intent: indices of ``vectors`` in greedy families.

    Walks the vectors in index order; the lowest unassigned index seeds a group,
    and every later unassigned vector whose similarity to the SEED is >=
    ``threshold`` joins it.  Membership is measured against the seed and never
    member-to-member, so a chain of loose matches cannot drag an unrelated
    document in one step at a time.

    Deterministic by construction: the seed is always the lowest free index, every
    loop is over a list or a ``range``, and nothing iterates a set or an unordered
    view.  Ties at the threshold are IN (``>=``).

    Every index appears in exactly one group and nothing is dropped: the members
    that joined near the cut are the uncertain ones, and they stay visible here
    and in ``tfidf_query``'s raw similarities rather than being hidden by a
    ranking.  This groups; it never picks a winner and never consumes its own
    output.
    """
    groups = []
    assigned = [False] * len(vectors)
    for index, vector in enumerate(vectors):
        if assigned[index]:
            continue
        assigned[index] = True
        group = [index]
        for other in range(index + 1, len(vectors)):
            if assigned[other]:
                continue
            if cosine(vector, vectors[other]) >= threshold:
                assigned[other] = True
                group.append(other)
        groups.append(group)
    return groups


def candidate_intent_groups(candidates, threshold=INTENT_THRESHOLD):
    """Group candidates by intent: ``candidate_text`` -> tf-idf -> ``intent_groups``.

    Returns ``(idf, vectors, groups)`` so the caller can also score one candidate's
    text against the whole corpus with ``tfidf_query`` without re-vectorising it.
    The groups are index-based, so they line up with the ``candidates`` list as
    given -- ordering it differently is the caller's decision, not this function's.
    """
    # text-candidate-text
    idf, vectors = tfidf_vectors([candidate_text(c) for c in candidates])
    return idf, vectors, intent_groups(vectors, threshold=threshold)


# --------------------------------------------------------------------------- #
# 8. spec.json ``triage`` defaults                                             #
# --------------------------------------------------------------------------- #

#: Defaults for the ``triage`` block of spec.json.  These are the canonical values:
#: a spec that omits the block gets exactly these, and a spec that sets one key
#: overrides only that key.  Every threshold the gate reads appears here, so the
#: gate has no constants of its own a spec cannot reach.
TRIAGE_DEFAULTS = {
    "max_unique_colors": 8,
    "max_speckles": 40,
    "max_glow_area_pct": 8,
    "min_silhouette_iou": 0.85,
    "min_luma_delta": 12,
    "weak_needed": 3,
    "bucket_cap": 2,
    "learn_min_rejections": 3,
    "learn_min_jobs": 2,
    # Phase 1 gate thresholds with no counterpart in spec.example.json, plus one
    # that deliberately mirrors the geometry block so the gate and Layer A cannot
    # drift apart.  ``max_mae_art`` and ``borderline_mae_art`` are the artwork-MAE
    # cliff and noise floor compare_candidates.py already sorts on (MAE_ART_FAIL
    # 8.0, drift above 1.0); keeping them here means a spec can retune the gate
    # without editing that script.
    "max_nodes_per_path": 500,
    "max_mae_art": 8.0,
    "borderline_mae_art": 1.0,
}


def triage_defaults():
    """Return a fresh copy of the ``triage`` block defaults."""
    return dict(TRIAGE_DEFAULTS)


def thresholds_of(candidate):
    """The effective ``triage`` thresholds for one candidate.

    A candidate may carry its run's merged ``triage`` block -- the assembler fills
    it with ``load_triage(spec)`` -- in which case those values win, so a spec.json
    override reaches the gate rules themselves and not just the config table.  A
    candidate carrying no block, or none at all, gets the documented defaults.

    This is why the rules take a candidate rather than a threshold dict: one
    candidate in, one complete set of thresholds out, with no hidden global state,
    so gating a list of candidates from two runs with different specs is safe.
    """
    return load_triage(candidate if isinstance(candidate, dict) else None)


def load_triage(spec):
    """Merge a spec's ``triage`` block over the defaults.

    ``spec`` is an already-parsed spec dict (or None).  A missing or null
    ``triage`` key yields the defaults; present keys win, one at a time.  Keys the
    defaults do not know are carried through untouched, so a newer spec can add a
    knob without this module discarding it.

    Also used per candidate by ``thresholds_of``: the same merge applies to the
    candidate's own ``triage`` block, which is how a spec's thresholds reach the
    gate.
    """
    merged = triage_defaults()
    if isinstance(spec, dict):
        block = spec.get("triage") or {}
        for key, value in block.items():
            merged[key] = value
    return merged


# --------------------------------------------------------------------------- #
# Self-check                                                                  #
# --------------------------------------------------------------------------- #


def _selftest():
    """Deterministic smoke check; no test framework needed."""
    a = '<svg xmlns="http://www.w3.org/2000/svg" width="10">' \
        '<!-- note --><path id="p1" d="M1.23456 2 L3 4"/></svg>'
    b = '<svg width="10" xmlns="http://www.w3.org/2000/svg">' \
        '<path d="M 1.2349  2 L3 4"/></svg>'
    assert canonical_svg(a) == canonical_svg(b), "canonical form unstable"
    assert svg_sha(canonical_svg(a)) == svg_sha(canonical_svg(b))
    assert "id=" not in canonical_svg(a)
    assert text_sim("filter_speckle=8", "filter_speckle=16") < 1.0
    assert bucket_of("bw", 3, 2) == bucket_of("bw", 5, 3)
    assert bucket_of("bw", 3, 2) != bucket_of("poster", 3, 2)
    assert gate({}).disposition is Disp.OK
    assert load_triage({"triage": {"max_speckles": 7}})["max_speckles"] == 7

    # Phase 1 gates: a hard signal drops, weak signals need weak_needed of them.
    lost = {"sweep": {"preset": "bw"},
            "comparison": {"fidelity": {"measured": True, "mae_art": 104.2}}}
    assert gate(lost).disposition is Disp.DROP, "artwork lost must drop"
    weak = {"sweep": {"preset": "poster"},
            "comparison": {"rendered_ink_colors": 5, "node_count_max": 120,
                           "metrics": {"speckle_count": 55, "luma_delta": 6}}}
    assert gate(weak).disposition is Disp.OK, "two weak hits must not demote"
    assert gate(weak).reasons == ["SPECKLE_HEAVY", "LOW_LUMA_CONTRAST"]
    weak["comparison"]["declared_colors"] = 8
    demoted = gate(weak)
    assert demoted.disposition is Disp.DEMOTE
    assert demoted.reasons == ["SPECKLE_HEAVY", "COLORS_NEAR_CAP",
                              "LOW_LUMA_CONTRAST"]
    assert demoted.bucket == bucket_of("poster", 55, 8)

    # Phase 3: tf-idf groups near-duplicate intent and keeps digits.
    idf, vectors = tfidf_vectors(["tracer bw cutout 8", "tracer bw cutout 16",
                                  "poster sunset landscape"])
    assert intent_groups(vectors) == [[0, 1], [2]], "intent grouping unstable"
    scores = tfidf_query("tracer bw cutout 8", idf, vectors)
    assert scores[0] > scores[1] > scores[2], scores
    assert tfidf_query("filter_speckle=8", idf, vectors) != \
        tfidf_query("filter_speckle=16", idf, vectors)
    print("triage selftest OK")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised manually
    import sys

    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print("triage.py -- importable library; run with --selftest to self-check.")
    raise SystemExit(0)
