#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
triage.py -- Phase 0 candidate-triage primitives: gate + bucket + dedupe + text-sim.

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
   that was rejected by hand.

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

Bucket keys
-----------
``bucket_of`` returns ``"{preset}|s{bin}|c{bin}|{prompt_family}"``.  The whole point
of the key is that one human rejection of one candidate can retire every candidate
that shares it, so the bins are deliberately coarse -- and their widest edge lines
up with the corresponding spec default (speckles up to ``max_speckles`` 40, colours
up to ``max_unique_colors`` 8).

Gate skeleton
-------------
``HARD`` and ``WEAK`` start EMPTY: the rules themselves land in a later phase, the
shape and the evaluation order land here.  Each entry is ``(name, fn)`` with
``fn(candidate) -> bool``.  A hard hit drops the candidate immediately; otherwise
``weak_needed`` weak hits demote it.  ``Verdict.reasons`` always carries rule
NAMES so a decision is explainable from the JSON alone.

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
# 6. Gate skeleton                                                            #
# --------------------------------------------------------------------------- #


class Disp(Enum):
    """What the gate decided about a candidate."""

    OK = "ok"
    DEMOTE = "demote"
    DROP = "drop"


@dataclass
class Verdict:
    """A gate decision: what happened, why, and which family it belonged to.

    ``bucket`` is left ``None`` by ``gate`` (which sees one candidate at a time);
    a later stage fills it from ``bucket_of`` so a rejection can be applied to the
    whole bucket.
    """

    disposition: Disp
    reasons: list = field(default_factory=list)
    bucket: "str | None" = None


#: Hard rules: ``(name, fn)`` with ``fn(candidate) -> bool``.  A single hit drops.
#: EMPTY by design -- phase 1 lands the rules; this slice fixes the shape and the
#: evaluation order.
HARD = []

#: Weak rules: ``(name, fn)`` with ``fn(candidate) -> bool``.  Enough hits demote.
#: EMPTY by design, same as ``HARD``.
WEAK = []


def gate(candidate, weak_needed=3):
    """Evaluate ``candidate`` against the HARD and WEAK rule lists.

    Hard hit -> ``Disp.DROP`` (weak rules are not even consulted: a doomed
    candidate's advisories are noise).  Otherwise ``weak_needed`` or more weak hits
    -> ``Disp.DEMOTE``, fewer -> ``Disp.OK``.  ``reasons`` holds the names of the
    rules that fired, in list order, so the same candidate always yields the same
    verdict and the same reason order.
    """
    hard_hits = [name for name, fn in HARD if fn(candidate)]
    if hard_hits:
        return Verdict(Disp.DROP, hard_hits, None)

    weak_hits = [name for name, fn in WEAK if fn(candidate)]
    if len(weak_hits) >= weak_needed:
        return Verdict(Disp.DEMOTE, weak_hits, None)

    return Verdict(Disp.OK, weak_hits, None)


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
# 8. spec.json ``triage`` defaults                                             #
# --------------------------------------------------------------------------- #

#: Defaults for the ``triage`` block of spec.json.  These are the canonical values:
#: a spec that omits the block gets exactly these, and a spec that sets one key
#: overrides only that key.
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
}


def triage_defaults():
    """Return a fresh copy of the ``triage`` block defaults."""
    return dict(TRIAGE_DEFAULTS)


def load_triage(spec):
    """Merge a spec's ``triage`` block over the defaults.

    ``spec`` is an already-parsed spec dict (or None).  A missing or null
    ``triage`` key yields the defaults; present keys win, one at a time.  Keys the
    defaults do not know are carried through untouched, so a newer spec can add a
    knob without this module discarding it.
    """
    merged = triage_defaults()
    if spec:
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
    print("triage selftest OK")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised manually
    import sys

    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print("triage.py -- importable library; run with --selftest to self-check.")
    raise SystemExit(0)
