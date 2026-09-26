#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
node_reduce.py -- deterministic, TARGETED node reduction for flat-colour print.

WHY THIS EXISTS
    `geometry_overload` (Layer A `NODE_COUNT`, gate `geometry.max_nodes_per_path`)
    is the failure mode nothing in the repo could remedy. Measured routes, all
    rejected on this repo's own candidates:

      * SVGO / scour / svgcleaner -- byte optimizers. Their own docs: "does not
        remove anchor points. A path that arrives with 4,000 points leaves with
        4,000 points." Useless against a NODE_COUNT gate, and they *look* like
        they worked because the file got smaller.
      * svg-simplifier 1.0.2 -- raises `TypeError: object of type 'CubicBezier'
        has no len()` on 40.7% of paths in real VTracer output (deterministically,
        83 of 204 in one candidate). Would need forking, plus a GEOS dep.
      * Inkscape `path-simplify` -- works, but the threshold is a *preference*,
        not a flag; it is non-monotonic (0.001 -> 204 nodes, 0.002 -> 234); the
        default loses ~18% of ink; and a single threshold does not transfer
        between files (4x spread), leaving a 0.2% margin where it does.

    This implements the reduction directly with dependencies the project already
    requires (svgpathtools, numpy, lxml). No Node, no preference-file hacks, no
    new pip installs.

DESIGN: TARGET, DO NOT SIMPLIFY THE WHOLE FILE
    The failure is a handful of outliers -- 1 path of 204 here, 3 of 2 491 there,
    19 of 22 244 on the worst candidate. Simplifying everything spends fidelity
    on thousands of paths that were already fine. On the repo's own candidates,
    targeted simplification reached the *same* max node count as whole-file
    simplification while touching one path instead of 204, losing 5.6x less ink.

    Measured on the tonal reference image (`00_source/00-example-tonal-
    reference.png`, kept for exactly this purpose -- a 6-colour gate cannot be
    satisfied by tonal art, so node weight shows up hardest there):
        whole file, tol 0.5 : 204 paths touched, ink lost 6.615%
        targeted,  tol 0.5 :   1 path touched, ink lost 1.192%

WHAT IS DIFFERENT FROM THE PROTOTYPE
    The prototype measured fit quality by comparing the fitted curve and the
    original at the SAME chord-length parameter, which is not a geometric
    distance. That is why its whole-file mode perturbed the render ~8x more than
    Inkscape at lower node reduction. Here deviation is a true nearest-point
    distance in BOTH directions, after Schneider-style re-parameterisation:

      1. sample the run densely            -> Q
      2. fit a fixed-endpoint cubic by LS  -> C
      3. re-parameterise: each Q_i gets the t of the nearest point on C
      4. refit with those t, repeat (2-3 passes)
      5. deviation = max( max_i |Q_i - C(t_i)|,      original -> fit
                          max_j |C(u_j) - Q(u_j)| )  fit -> original

    Step 5's second term is what catches a fitted curve that BULGES away from
    the original between samples. Measuring only the first term under-reports.

ALGORITHM
    Greedy run-fitting. Walk a path's segments; grow the longest run replaceable
    by ONE cubic whose two-sided deviation stays within tolerance. Endpoints are
    held fixed, so continuity and closure are exact by construction. A run of
    near-collinear segments collapses to a single Line.

REGRESSION REJECTION
    A simplifier that makes a path worse is worse than no simplifier (Inkscape
    was measured raising the worst node count 2364 -> 2752 at a low threshold
    while reporting success). Every run compares against its own input and
    rejects any individual path whose node count did not fall. The whole file
    is then rejected -- leaving the input untouched on disk -- if ANY path grew,
    not merely the maximal one: a damaged small path used to be committed as
    long as the worst path happened to improve. Fits are STAGED and only applied
    to the tree once every path has passed, so a later rejection cannot leave a
    half-rewritten file behind. Fits whose path data does not re-parse are
    refused outright, since their node count came from a command-letter regex
    rather than a real parse and could read as a reduction when it is not.

RENDER VERIFICATION
    A node count clearing the gate says nothing about whether the artwork
    survived. `--verify` renders before/after and reports ink lost / ink gained
    and the largest background-coloured blob, which is the shared-boundary gap
    signature. Ink-vs-white is only valid when the artwork has a light
    background; on full-bleed art that metric is degenerate (it reports 0.000%
    change for a file where 19 paths were rewritten), so a pixel-difference
    metric is used as well and the ink fraction is reported for honesty.

SHARED BOUNDARIES (the honest remaining risk)
    VTracer traces each colour region as its own path and adjacent regions
    SHARE boundaries: on one candidate, 36.4% of occupied grid cells were covered
    by more than one path. If a fit moves a shared edge on one side only, the
    ground shows through between two flat inks -- invisible in the SVG, a white
    hairline on a shirt. Targeting limits which edges can move; it does not
    eliminate the risk. `--verify`'s gap-blob measurement is the check that
    catches it. Nothing here welds shared edges.

NEVER PICKS A WINNER. Like every tool here, it transforms and reports.

Usage::

    python3 scripts/node_reduce.py art.svg --out reduced.svg
    python3 scripts/node_reduce.py art.svg --only-over --verify --json r.json
    python3 scripts/node_reduce.py art.svg --max-nodes 500 --tolerance-cap 4.0

Exit codes: 0 = wrote a reduced file (or nothing was over the gate), 1 = usage
or I/O error, 3 = the run was rejected as a regression.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time

import numpy as np
from lxml import etree

from svgpathtools import (CubicBezier, Line, Path as SvgPath,
                         QuadraticBezier, parse_path)

SVG_NS = "http://www.w3.org/2000/svg"
VERSION = "0.2.0"

SAMPLES_PER_SEG = 16      # samples taken per original segment
CURVE_DENSITY = 3         # fitted-curve samples per original sample
REFIT_PASSES = 3          # Schneider re-parameterisation passes
MAX_RUN = 96              # cap on segments one fit may absorb
CMD_RE = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]")


class UnsupportedSegmentError(Exception):
    """A path holds a segment type that cannot be written back exactly.

    Raised rather than approximated: the alternative silently changes artwork
    the node gate was never complaining about.
    """


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- fitting

def _sample_run(segments, per_seg=SAMPLES_PER_SEG):
    """Dense samples along a run of segments, endpoints included once."""
    pts = []
    for k, seg in enumerate(segments):
        for j in range(per_seg + 1):
            if k and j == 0:
                continue          # shared with the previous segment's end
            p = seg.point(j / per_seg)
            pts.append(complex(p.real, p.imag))
    return pts


def _chord_params(pts):
    p = np.asarray(pts, dtype=complex)
    d = np.abs(np.diff(p))
    cum = np.concatenate([[0.0], np.cumsum(d)])
    total = cum[-1] if cum[-1] > 0 else 1.0
    return cum / total


def _basis(t):
    omt = 1.0 - t
    return omt ** 3, 3 * omt ** 2 * t, 3 * omt * t ** 2, t ** 3


def _fit_with_params(p, t):
    """Least-squares cubic with FIXED endpoints at the given parameters."""
    b0, b1, b2, b3 = _basis(t)
    p0, p3 = p[0], p[-1]
    r = p - b0 * p0 - b3 * p3
    A = np.stack([b1, b2], axis=1)
    try:
        sol, *_ = np.linalg.lstsq(A, r, rcond=None)
    except np.linalg.LinAlgError:
        return None
    p1, p2 = sol[0], sol[1]
    if not (np.isfinite(p1.real) and np.isfinite(p1.imag)
            and np.isfinite(p2.real) and np.isfinite(p2.imag)):
        return None
    return CubicBezier(p0, p1, p2, p3)


def _cubic_samples(seg, count):
    t = np.linspace(0.0, 1.0, count)
    b0, b1, b2, b3 = _basis(t)
    p = np.asarray([complex(seg.start), complex(seg.control1),
                    complex(seg.control2), complex(seg.end)], dtype=complex)
    return b0 * p[0] + b1 * p[1] + b2 * p[2] + b3 * p[3]


def _nearest_params(seg, Q, count):
    """For each Q point, the t of the nearest point on the fitted curve."""
    C = _cubic_samples(seg, count)
    # (n, m) squared distances; n,m are modest (few hundred) so this is cheap.
    d2 = (np.abs(Q[:, None] - C[None, :]) ** 2)
    idx = np.argmin(d2, axis=1)
    t = idx / float(count - 1)
    # Pull each t onto its exact local minimum rather than the sample grid.
    lo = np.clip(t - 1.0 / (count - 1), 0.0, 1.0)
    hi = np.clip(t + 1.0 / (count - 1), 0.0, 1.0)
    for _ in range(12):           # ternary search: unimodal per point
        m1 = lo + (hi - lo) / 3.0
        m2 = hi - (hi - lo) / 3.0
        d1 = np.abs(Q - _eval(seg, m1))
        d2 = np.abs(Q - _eval(seg, m2))
        left = d1 < d2
        hi = np.where(left, m2, hi)
        lo = np.where(left, lo, m1)
    return (lo + hi) / 2.0


def fit_cubic(pts, passes=REFIT_PASSES):
    """Fixed-endpoint least-squares cubic, Schneider-reparameterised."""
    p = np.asarray(pts, dtype=complex)
    if len(p) < 3:
        return None
    t = _chord_params(pts)
    seg = None
    for _ in range(max(1, passes)):
        seg = _fit_with_params(p, t)
        if seg is None:
            return None
        t = _nearest_params(seg, p, len(p) * CURVE_DENSITY)
    return seg


def _point_to_polyline(p, pts):
    """Distance from each complex point to the POLYLINE through pts.

    Nearest-*sample* distance would report a discretisation artifact: on a curve
    sampled 3x denser than the original points, the worst sample sits about
    half a sample spacing from its nearest neighbour even when the fit is
    exact. Measured at 1.27 units on a perfectly straight 100-unit line.

    The polyline is a 16x-per-segment CHORD approximation of the original, not
    the original itself. Chords cut inside every curve, so the true error is
    systematically LARGER than reported: measured 79.65 reported vs 82.77 true
    on a pathological loop, a 3.9% under-report. Treat the tolerance as a
    slightly optimistic bound; the render-level `--verify` is the real check.
    """
    a = pts[:-1]
    b = pts[1:]
    ab = b - a
    L2 = (ab.real ** 2 + ab.imag ** 2)
    # Work in real (x, y) rather than complex: np.abs(z)**2 already collapses to
    # a real array, so there is no third axis to reduce over.
    P = np.stack([p.real, p.imag], axis=1)[:, None, :]
    A = np.stack([a.real, a.imag], axis=1)[None, :, :]
    V = np.stack([ab.real, ab.imag], axis=1)[None, :, :]
    L2s = np.where(L2 == 0.0, 1.0, L2)[None, :]
    t = np.clip(((P - A) * V).sum(axis=2) / L2s, 0.0, 1.0)
    proj = A + t[:, :, None] * V
    return np.sqrt(((P - proj) ** 2).sum(axis=2).min(axis=1))


def two_sided_deviation(seg, pts):
    """Max geometric distance, original->fit AND fit->original.

    Measuring only original->fit under-reports: a curve can bulge away from the
    polyline between samples and still pass every forward check. That is the
    prototype's blind spot and the reason its whole-file mode perturbed the
    render several times more than a comparable reduction here.
    """
    p = np.asarray(pts, dtype=complex)
    count = len(p) * CURVE_DENSITY

    # original -> fit. Use the REFINED nearest-point search, not the raw sample
    # grid: snapping t to the nearest of `count` samples leaves up to half a
    # sample spacing of error (0.41 units at CURVE_DENSITY=3) on a curve that is
    # geometrically exact, which would understate the fit and let a real bulge
    # through the tolerance gate.
    t = _nearest_params(seg, p, count)
    fwd = float(np.abs(_eval(seg, t) - p).max())

    # fit -> original (nearest point on the original polyline)
    C = _cubic_samples(seg, count)
    bwd = float(_point_to_polyline(C, p).max())
    return max(fwd, bwd)


def _eval(seg, t):
    b0, b1, b2, b3 = _basis(t)
    p = np.asarray([complex(seg.start), complex(seg.control1),
                    complex(seg.control2), complex(seg.end)], dtype=complex)
    return b0 * p[0] + b1 * p[1] + b2 * p[2] + b3 * p[3]


def _line_deviation(line, pts):
    p = np.asarray(pts, dtype=complex)
    v = line.end - line.start
    L2 = (v.real ** 2 + v.imag ** 2)
    if L2 <= 0.0:
        return float(np.abs(p - line.start).max())
    t = ((p - line.start).real * v.real + (p - line.start).imag * v.imag) / L2
    t = np.clip(t, 0.0, 1.0)
    proj = line.start + t * v
    return float(np.abs(p - proj).max())


def split_subpaths(path):
    """Split a parsed path into its subpaths, at every discontinuity.

    A single `d` attribute may hold several `M`-separated subpaths, and
    svgpathtools represents those as one continuous segment list with a
    discontinuity where a new subpath began. Simplifying across such a
    discontinuity WELDS two disjoint shapes into one -- a real corruption, and
    one that can be accepted because the welded result is shorter. So each
    subpath is fitted on its own and the boundaries are restored.

    Returns a list of segment lists.
    """
    segs = list(path)
    if not segs:
        return []
    subs = [[segs[0]]]
    for prev, seg in zip(segs, segs[1:]):
        if abs(complex(seg.start) - complex(prev.end)) > 1e-9:
            subs.append([seg])
        else:
            subs[-1].append(seg)
    return subs


def _fit_one_subpath(segs, tolerance, max_run):
    """Greedy two-sided run-fitting over a single (continuous) subpath."""
    n = len(segs)
    if n < 3:
        return segs
    out = []
    i = 0
    while i < n:
        # --- grow the longest cubic run within tolerance
        best_j = best_seg = None
        limit = min(n, i + max_run)
        for j in range(i + 2, limit + 1):
            pts = _sample_run(segs[i:j])
            cand = fit_cubic(pts)
            if cand is None:
                break
            if two_sided_deviation(cand, pts) <= tolerance:
                best_j, best_seg = j, cand
            elif j - i > 4:
                break        # well past tolerance: growing will not recover
        if best_seg is not None:
            out.append(best_seg)
            i = best_j
            continue
        # --- otherwise try a straight line over the run ahead
        j = min(n, i + max_run)
        pts = _sample_run(segs[i:j])
        line = Line(pts[0], pts[-1])
        if j - i >= 2 and _line_deviation(line, pts) <= tolerance:
            out.append(line)
            i = j                    # consume the WHOLE run, not the samples
            continue
        out.append(segs[i])
        i += 1
    return out


def simplify_path(path, tolerance, max_run=MAX_RUN):
    """Greedy two-sided run-fitting, per subpath. Returns (new_path, n_out).

    Paths containing a segment type this tool cannot re-serialise faithfully
    (an elliptical arc, say) are returned UNCHANGED with a flag, because
    serialising them approximately would silently alter artwork that the gate
    was never complaining about. See `is_serialisable`.
    """
    segs = list(path)
    if len(segs) < 3:
        return path, len(segs)
    if not is_serialisable(segs):
        return path, len(segs)

    out = []
    for sub in split_subpaths(path):
        out.extend(_fit_one_subpath(sub, tolerance, max_run))
    if not out:
        return path, len(segs)
    return SvgPath(*out), len(out)


def is_serialisable(segs):
    """True when every segment can be written back as exact path commands.

    Only Line and CubicBezier are. QuadraticBezier converts exactly (a
    quadratic IS a cubic with the control points at the 2/3 points), so it is
    included. An Arc cannot: it would have to be approximated, and an
    approximation that then gets fed to a fitter is how artwork quietly
    changes. Such paths are skipped instead.
    """
    return all(isinstance(s, (Line, CubicBezier, QuadraticBezier))
               for s in segs)


# ---------------------------------------------------------------- serialise

def _num(x) -> str:
    """Format a coordinate for a path `d`.

    Guards non-finite values: `%.4f` renders them as `nan` / `inf`, neither of
    which is legal in path data. `parse_path` then throws, `path_nodes` falls
    back to counting regex command letters, and a corrupted path can be counted
    as having FEWER nodes -- so a `nan` would read as a successful reduction.
    Raise instead; the caller abandons the path.
    """
    v = float(x)
    if not math.isfinite(v):
        raise UnsupportedSegmentError(
            "non-finite coordinate (%r) cannot be written to path data" % v)
    if v == 0.0:
        return "0"                      # normalises -0.0, which %.4f renders "-0"
    s = ("%.4f" % v).rstrip("0").rstrip(".")
    return s if s else "0"


def _is_degenerate(seg) -> bool:
    """True for a zero-length segment (start == end).

    Such a segment is not a closure: a fit can leave a collapsed point sitting
    exactly on the subpath's start, and treating that as a `Z` would delete a
    real vertex and change the fill.
    """
    return abs(complex(seg.start) - complex(seg.end)) < 1e-12


def path_to_d(path) -> str:
    """Serialise a path back to `d`, exactly, or raise.

    Every segment type this tool accepts is written as its own exact command.
    Nothing is approximated and nothing is skipped:

    * a QuadraticBezier is emitted as `Q` (exact -- it is stored as a
      quadratic, so re-emitting `Q` loses nothing);
    * a subpath discontinuity emits its own `M`, so disjoint subpaths in one
      `d` stay disjoint instead of being welded into a single shape;
    * ANY other type (notably `Arc`) RAISES. The previous version fell back to
      `seg.d() if hasattr(seg, "d") else ""`, and svgpathtools' QuadraticBezier
      and Arc have no `.d()` at all -- so the fallback was `""`, the trailing
      `if p` filter dropped it, and a path made entirely of unsupported
      segments serialised to the empty string. That DELETED the artwork while
      reporting `reduced, nodes 30 -> 0`, i.e. it looked like a triumph.

    Raising is the correct failure: the caller must leave such a path alone.
    """
    parts = []
    sub = []                       # segments of the subpath being written

    def flush():
        """Emit the accumulated subpath, restoring its Z if it had one."""
        nonlocal sub
        if not sub:
            return
        # A `Z` is parsed into an explicit segment running back to the subpath's
        # start (a Line when the source ended in a line, a Cubic when the fitter
        # replaced it), and svgpathtools' own `path.closed` is unreliable once
        # there is more than one subpath. So detect closure per subpath,
        # GEOMETRICALLY: a trailing segment that ends exactly where the subpath
        # started is the parsed Z.
        #
        # The segment TYPE must not be part of the test. An earlier version
        # required `isinstance(tail, Line)`, which silently dropped the Z from
        # every subpath the fitter had closed with a cubic -- losing the fill
        # closure on exactly the paths this tool rewrites.
        #
        # There is deliberately NO fallback to path.closed: it is wrong for
        # multi-subpath paths, and on a genuinely open path it would invent a Z
        # and add a node. Geometry is the only reliable signal here.
        # A subpath may be fitted down to a SINGLE closed segment (its two ends
        # coincide), and that still carries the original `Z`. Requiring
        # `len(sub) > 1` dropped the closure from every such subpath -- 7 of the
        # 13 rewritten paths in candidate_08, i.e. the majority of them.
        # A single segment is only ambiguous when it is a genuine arc-like
        # closing move, and a one-segment subpath whose end IS its start is
        # precisely the closure we must keep.
        tail = sub[-1]
        start = complex(sub[0].start)
        # Degeneracy means the segment has no extent, i.e. a real vertex was
        # lost. For a MULTI-segment subpath that is a good reason to distrust
        # the geometric match. For a SINGLE-segment subpath it is not: the one
        # segment runs from a point back to itself, which is precisely what a
        # fully-collapsed closed loop looks like, and treating it as degenerate
        # dropped the `Z` from 3 of 6 subpaths in candidate_08.
        closed = (abs(complex(tail.end) - start) < 1e-9
                  and (len(sub) == 1 or not _is_degenerate(tail)))
        # Only drop the tail when there is something left to draw. A
        # single-segment closed subpath IS the whole shape (one loop drawn by
        # one curve), so dropping its only segment would emit a bare `Z` and
        # delete the artwork -- the exact failure mode this file guards.
        body = sub[:-1] if (closed and len(sub) > 1) else sub
        for k, seg in enumerate(body):
            if k == 0:
                parts.append("M %s %s" % (_num(seg.start.real),
                                          _num(seg.start.imag)))
            parts.append(_seg_command(seg))
        if closed:
            parts.append("Z")
        sub = []

    for seg in path:
        start = complex(seg.start)
        if not sub or abs(start - complex(sub[-1].end)) > 1e-9:
            flush()                  # discontinuity: close the previous subpath
        sub.append(seg)
    flush()
    return " ".join(p for p in parts if p)


def _seg_command(seg) -> str:
    """Serialise ONE segment, exactly, or raise.

    `QuadraticBezier` and `Arc` in svgpathtools have NO `.d()` method, so an
    `hasattr` fallback silently yields "" and a path made of them serialises to
    the empty string -- DELETING the artwork while reporting a node reduction
    (and a deleted path clears any node gate). Arcs cannot be written exactly,
    so they raise and the caller leaves the path alone.
    """
    if isinstance(seg, CubicBezier):
        return "C %s %s %s %s %s %s" % (
            _num(seg.control1.real), _num(seg.control1.imag),
            _num(seg.control2.real), _num(seg.control2.imag),
            _num(seg.end.real), _num(seg.end.imag))
    if isinstance(seg, Line):
        return "L %s %s" % (_num(seg.end.real), _num(seg.end.imag))
    if isinstance(seg, QuadraticBezier):
        return "Q %s %s %s %s" % (_num(seg.control.real), _num(seg.control.imag),
                                  _num(seg.end.real), _num(seg.end.imag))
    raise UnsupportedSegmentError(
        "cannot serialise %s exactly; leaving this path unchanged"
        % type(seg).__name__)


# ---------------------------------------------------------------- counting

def path_nodes(d: str):
    """Node count the way validate_svg.py counts it: len(parse_path(d)).

    The regex fallback exists so a malformed path still yields a number instead
    of raising. It is NOT equivalent: a `d` containing `nan` (or any token
    parse_path rejects) falls through, and command-letter counting can report
    FEWER nodes than the path really has. So the fallback is flagged, and
    `reduce_svg` refuses to accept a fit whose count came from it -- otherwise a
    corrupted path would read as a successful reduction.
    """
    try:
        return len(parse_path(d))
    except Exception:
        return len(CMD_RE.findall(d or ""))


def path_nodes_exact(d: str):
    """(count, is_exact). False means parse_path rejected the path data."""
    try:
        return len(parse_path(d)), True
    except Exception:
        return len(CMD_RE.findall(d or "")), False


def node_counts(root):
    out = []
    for el in root.iter("{%s}path" % SVG_NS):
        d = el.get("d")
        if d:
            out.append(path_nodes(d))
    return out


# ---------------------------------------------------------------- verify

def _inkscape(src_svg, out_png, width, height):
    import subprocess
    env = dict(os.environ, DBUS_SESSION_BUS_ADDRESS="disabled:")
    argv = ["inkscape", src_svg,
            "--export-filename=%s" % out_png,
            "--export-width=%d" % width, "--export-height=%d" % height,
            "--export-background=#ffffff", "--export-background-opacity=255"]
    subprocess.run(argv, check=True, capture_output=True, timeout=300, env=env)


def verify(svg_before, svg_after, workdir, dpi=96):
    """Render both, report ink drift and the largest background blob.

    The ink metric is degenerate on full-bleed artwork (100% ink -> no
    reference -> reports 0.000% change), so a plain pixel-difference metric is
    always reported too, and the ink fraction is printed for honesty.
    """
    from PIL import Image
    import numpy as np

    os.makedirs(workdir, exist_ok=True)
    before_png = os.path.join(workdir, "_verify_before.png")
    after_png = os.path.join(workdir, "_verify_after.png")
    _inkscape(svg_before, before_png, dpi * 8, dpi * 8)
    _inkscape(svg_after, after_png, dpi * 8, dpi * 8)

    a = np.asarray(Image.open(before_png).convert("RGB")).astype(np.int16)
    b = np.asarray(Image.open(after_png).convert("RGB")).astype(np.int16)
    n = min(a.shape[0], b.shape[0]), min(a.shape[1], b.shape[1])
    a, b = a[:n[0], :n[1]], b[:n[0], :n[1]]

    total = a.shape[0] * a.shape[1]
    changed = np.abs(a - b).max(axis=2) > 8
    px_changed = float(changed.sum()) / total * 100.0

    # light-vs-dark reference; report the fraction so a degenerate case is visible
    light = (a.sum(axis=2) > 3 * 200)
    ink_frac = float((~light).sum()) / total * 100.0
    ink_lost = float((light & changed).sum()) / total * 100.0
    ink_gained = float((~light & changed).sum()) / total * 100.0

    # largest connected run of newly-background pixels = the gap signature
    try:
        from scipy import ndimage  # optional; only sharpens the gap number
        lab, cnt = ndimage.label(light & changed)
        gap = int(ndimage.sum(np.ones_like(lab), lab, range(1, cnt + 1)).max()) if cnt else 0
    except Exception:
        gap = None

    return {
        "render_size": [int(a.shape[1]), int(a.shape[0])],
        "px_changed_percent": round(px_changed, 4),
        "ink_fraction_percent": round(ink_frac, 3),
        "ink_metric_valid": 1.0 <= ink_frac <= 99.0,
        "ink_lost_percent": round(ink_lost, 4),
        "ink_gained_percent": round(ink_gained, 4),
        "largest_gap_blob_px": gap,
    }


# ---------------------------------------------------------------- main

def reduce_svg(svg_path, out_path, max_nodes, tol_start, tol_cap,
               only_over=True, report=None):
    tree = etree.parse(svg_path)
    root = tree.getroot()

    before = node_counts(root)
    if not before:
        return {"status": "no_paths", "paths": 0}
    max_before = max(before)

    targets = []
    for el in root.iter("{%s}path" % SVG_NS):
        d = el.get("d")
        if not d:
            continue
        if only_over and path_nodes(d) <= max_nodes:
            continue
        targets.append(el)

    if not targets:
        return {"status": "nothing_over_gate", "paths": len(before),
                "path_nodes_max": max_before, "max_nodes_limit": max_nodes,
                "paths_touched": 0}

    per_path = []
    staged = []            # (element, new_d) -- applied only after every path passes
    for el in targets:
        d = el.get("d")
        n_before = path_nodes(d)
        try:
            path = parse_path(d)
        except Exception as exc:
            per_path.append({"status": "parse_error", "detail": str(exc)[:120],
                             "nodes_before": n_before})
            continue
        tol = tol_start
        best = None
        while tol <= tol_cap + 1e-9:
            try:
                new_path, n_out = simplify_path(path, tol)
                new_d = path_to_d(new_path)
            except UnsupportedSegmentError as exc:
                # Not a failure: the path is simply one this tool will not
                # touch. Recorded distinctly so a reader can tell "declined"
                # from "crashed".
                per_path.append({"status": "unsupported_segment",
                                 "detail": str(exc)[:160],
                                 "nodes_before": n_before})
                best = None
                break
            except Exception as exc:  # noqa: BLE001
                per_path.append({"status": "fit_error", "detail": str(exc)[:120],
                                 "nodes_before": n_before})
                best = None
                break
            n_after, exact = path_nodes_exact(new_d)
            # A fit that emptied the path is not a reduction, it is a deletion.
            # `nodes 30 -> 0` used to be reported as a success. Refuse it, and
            # refuse any result that lost a subpath (a welded/disappeared
            # subpath shows up as a jump in geometry, not just node count).
            if not new_d.strip():
                per_path.append({"status": "fit_empty", "nodes_before": n_before,
                                 "detail": "fit produced an empty path; refused"})
                best = None
                break
            if not exact:
                # The count came from the regex fallback, so it is not
                # comparable to a parse_path count. Accepting it would let a
                # corrupt path read as a reduction.
                per_path.append({"status": "unparseable_result",
                                 "nodes_before": n_before,
                                 "detail": "fitted path data does not parse; refused"})
                best = None
                break
            # REGRESSION REJECTION: only accept a strict improvement, and only
            # if it clears the gate.
            if n_after < n_before and n_after <= max_nodes:
                best = (new_d, n_after, tol)
                break
            if n_after < n_before and best is None:
                best = (new_d, n_after, tol)   # improved, still over: keep going
            tol *= 2.0
        if best is None:
            per_path.append({"status": "no_fit", "nodes_before": n_before})
            continue
        new_d, n_after, tol_used = best
        # STAGE, do not mutate. `el.set` used to run here, so the whole tree was
        # already altered and the file-level rejection below depended entirely
        # on a single `if` returning before `tree.write` -- any future path that
        # wrote earlier, or any per-path check that was bypassed, would commit
        # a partially-corrupted file. Now the tree is only touched once every
        # path has passed its own checks.
        staged.append((el, new_d))
        per_path.append({"status": "reduced", "nodes_before": n_before,
                         "nodes_after": n_after, "tolerance": round(tol_used, 6)})

    for el, new_d in staged:
        el.set("d", new_d)

    after = node_counts(root)
    max_after = max(after) if after else 0

    result = {
        "status": "reduced",
        "tool": "node_reduce.py",
        "version": VERSION,
        "input": svg_path,
        "output": out_path,
        "paths": len(before),
        "paths_touched": len(targets),
        "path_nodes_max_before": max_before,
        "path_nodes_max_after": max_after,
        "path_nodes_total_before": int(sum(before)),
        "path_nodes_total_after": int(sum(after)),
        "max_nodes_limit": max_nodes,
        "gate_cleared": max_after <= max_nodes,
        "per_path": per_path,
    }

    if max_after > max_before:
        result["status"] = "rejected_regression"
        return result

    # File-level guard: reject if ANY path ended up with more nodes, not just
    # the maximum one. The old check only looked at `max`, so a damaged
    # non-maximal path was committed as long as the worst path happened to
    # improve. `after` is positional: path_nodes() walks the same elements in
    # the same order as `before`, so the indices line up.
    grew = [(i, b, a) for i, (b, a) in enumerate(zip(before, after)) if a > b]
    if grew:
        result["status"] = "rejected_regression"
        result["note"] = ("%d path(s) increased in node count (e.g. path #%d: "
                          "%d -> %d); the file was left unchanged"
                          % (len(grew), grew[0][0], grew[0][1], grew[0][2]))
        return result

    # Nothing was actually rewritten (every target declined or found no fit).
    # Still write the output, so a caller that asked for a file gets an
    # unmodified copy rather than a missing file it has to special-case.
    if out_path:
        tree.write(out_path, xml_declaration=True, encoding="UTF-8")
    if not any(p.get("status") == "reduced" for p in per_path):
        result["status"] = "no_change"
        result["note"] = ("no path could be reduced; the output is a "
                          "byte-equivalent copy of the input")
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="node_reduce.py",
        description="Deterministic targeted node reduction for print SVGs.")
    ap.add_argument("svg")
    ap.add_argument("--out", default=None, help="output SVG (default: in place)")
    ap.add_argument("--max-nodes", type=int, default=500,
                    help="gate from spec.geometry.max_nodes_per_path (default 500)")
    ap.add_argument("--tolerance", type=float, default=0.5,
                    help="starting tolerance in path units (default 0.5)")
    ap.add_argument("--tolerance-cap", type=float, default=8.0,
                    help="max tolerance after doubling (default 8.0)")
    ap.add_argument("--all-paths", action="store_true",
                    help="simplify every path, not just those over the gate "
                         "(measurably worse: spends fidelity on paths that "
                         "were already fine)")
    ap.add_argument("--verify", action="store_true",
                    help="render before/after and report ink drift + gap blob")
    ap.add_argument("--json", dest="json_path", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.svg):
        log("node_reduce: no such file: %s" % args.svg)
        return 2

    t0 = time.time()
    log("--- node_reduce %s ---" % VERSION)
    log("input     : %s" % args.svg)
    log("gate      : %d nodes/path   mode: %s" % (
        args.max_nodes, "whole file" if args.all_paths else "targeted"))

    out = None if args.dry_run else (args.out or args.svg)

    # In the default in-place mode `out` IS the input, so by the time --verify
    # rendered "before", the file had already been overwritten and the two
    # renders were identical -- producing a perfect, meaningless 0.000% report
    # from the very check meant to catch geometry damage. Keep a copy of the
    # original whenever verification is on and the run is in place.
    verify_before = args.svg
    verify_tmp = None
    if args.verify and out == args.svg:
        import shutil
        import tempfile
        verify_tmp = tempfile.NamedTemporaryFile(
            suffix=".svg", delete=False, prefix="node_reduce_before_")
        verify_tmp.close()
        shutil.copy2(args.svg, verify_tmp.name)
        verify_before = verify_tmp.name

    result = reduce_svg(args.svg, out, args.max_nodes, args.tolerance,
                        args.tolerance_cap, only_over=not args.all_paths)

    log("status    : %s" % result["status"])
    if result["status"] in ("reduced", "rejected_regression"):
        log("paths     : %d  touched: %d" % (result["paths"], result["paths_touched"]))
        log("max nodes : %d -> %d   (gate %s)" % (
            result["path_nodes_max_before"], result["path_nodes_max_after"],
            "CLEARED" if result["gate_cleared"] else "still over"))
        log("total     : %d -> %d" % (
            result["path_nodes_total_before"], result["path_nodes_total_after"]))
        touched = [p for p in result["per_path"] if p.get("status") == "reduced"]
        if touched:
            log("tolerances: %s" % ", ".join(
                "%g" % p["tolerance"] for p in touched[:12]))
        if result["status"] == "rejected_regression":
            log("REJECTED  : max node count did not fall; input left unchanged")
            if args.json_path:
                with open(args.json_path, "w") as fh:
                    json.dump(result, fh, indent=2)
            return 3

    if args.verify and result["status"] == "reduced" and out:
        try:
            result["verify"] = verify(verify_before, out,
                                      out + ".verify")
            v = result["verify"]
            log("verify    : %.3f%% pixels changed, ink lost %.3f%%, gained %.3f%%"
                % (v["px_changed_percent"], v["ink_lost_percent"],
                   v["ink_gained_percent"]))
            if not v["ink_metric_valid"]:
                log("            (ink metric DEGENERATE: ink fraction %.1f%% -- "
                    "use px_changed)" % v["ink_fraction_percent"])
            if v.get("largest_gap_blob_px"):
                log("            largest background blob: %d px  <-- gap check"
                    % v["largest_gap_blob_px"])
        except Exception as exc:
            result["verify"] = {"error": "%s: %s" % (type(exc).__name__, exc)}
            log("verify    : FAILED %s" % result["verify"]["error"])
        finally:
            if verify_tmp is not None:
                try:
                    os.unlink(verify_tmp.name)
                except OSError:
                    pass

    result["seconds"] = round(time.time() - t0, 2)
    if args.json_path:
        with open(args.json_path, "w") as fh:
            json.dump(result, fh, indent=2)
        log("wrote     : %s" % args.json_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
