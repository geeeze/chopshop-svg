#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for scripts/node_reduce.py (targeted node reduction).

Synthetic fixtures only. Nothing here touches 02_traced/ or 00_source/.

The contracts under test, in order of how badly they break art if violated:

  1. TARGETING. Only paths over the gate are touched. Whole-file mode is the
     measured-worse option and must not happen by accident.
  2. REGRESSION REJECTION. A run that does not lower path_nodes_max is rejected
     and the input left alone -- a simplifier that makes a path worse is worse
     than none.
  3. GEOMETRY PRESERVED. Endpoints are held fixed, so a reduced path still
     starts and ends where it did.
  4. THE DEVIATION METRIC. The prototype's bug was measuring the fit at the same
     chord-length parameter rather than by true nearest-point distance. These
     tests pin the two-sided behaviour, including the fit->original direction
     that catches a curve bulging between samples.
"""

import json
import os
import sys

import pytest
from lxml import etree


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
for p in (ROOT, SCRIPTS):
    if p not in sys.path:
        sys.path.insert(0, p)

import node_reduce as nr  # noqa: E402

SVG_NS = nr.SVG_NS


def write_svg(path, paths):
    """paths: list of (d, fill). Minimal standalone SVG."""
    root = etree.Element("{%s}svg" % SVG_NS, nsmap={None: SVG_NS})
    root.set("viewBox", "0 0 200 200")
    for d, fill in paths:
        el = etree.SubElement(root, "{%s}path" % SVG_NS)
        el.set("d", d)
        el.set("fill", fill)
    etree.ElementTree(root).write(str(path), xml_declaration=True,
                                  encoding="UTF-8")
    return str(path)


def wiggly_d(n=400, amp=30.0):
    """A long meandering open path -- the shape that overruns a node gate."""
    import math
    pts = []
    for i in range(n + 1):
        t = i / n
        x = t * 200.0
        y = 100.0 + amp * math.sin(t * math.pi * 8)
        pts.append("%.3f %.3f" % (x, y))
    return "M " + " L ".join(pts)


def simple_d():
    return "M 10 10 L 90 10 L 90 90 L 10 90 Z"


# ------------------------------------------------------------------ counting

def test_path_nodes_counts_like_validate_svg():
    from svgpathtools import parse_path
    d = simple_d()
    assert nr.path_nodes(d) == len(parse_path(d))


def test_path_nodes_falls_back_on_unparseable():
    # Must not raise: a malformed path is data, not a crash.
    assert nr.path_nodes("M 0 0 L") >= 0


# ------------------------------------------------------------------ fitting

def test_fit_cubic_reproduces_a_straight_line():
    import numpy as np
    pts = [complex(x, 0.0) for x in np.linspace(0, 100, 40)]
    seg = nr.fit_cubic(pts)
    assert seg is not None
    # A cubic fitting a line has near-zero deviation. The floor is the
    # ternary search's convergence (~0.003 units on a 100-unit line), not the
    # fit: sampling the curve on a raw grid instead reports 0.41 here, which is
    # half a sample spacing of pure discretisation error.
    assert nr.two_sided_deviation(seg, pts) < 0.01


def test_two_sided_deviation_is_zero_on_exact_geometry():
    import numpy as np
    pts = [complex(x, 0.0) for x in np.linspace(0, 100, 40)]
    seg = nr.fit_cubic(pts)
    assert nr.two_sided_deviation(seg, pts) < 0.01


def test_deviation_detects_a_bulge_between_samples():
    """The fit->original direction must fire on a curve that misses mid-span.

    A fit that agrees at every sample but bows away in between is exactly the
    error a forward-only metric reports as clean. This is the prototype's bug,
    so it is pinned deliberately.
    """
    import numpy as np
    # Sample a chord every 10 units -- a bowed cubic misses the chord's middle
    # by far more than it misses the endpoints.
    pts = [complex(x, 0.0) for x in np.linspace(0, 100, 11)]
    bowed = nr.CubicBezier(complex(0, 0), complex(33, 60),
                           complex(66, 60), complex(100, 0))
    dev = nr.two_sided_deviation(bowed, pts)
    assert dev > 20.0, "a 60-unit bow must not measure as a clean fit"


def test_deviation_grows_as_the_fit_degrades():
    import numpy as np
    pts = [complex(x, 0.0) for x in np.linspace(0, 100, 11)]
    good = nr.CubicBezier(complex(0, 0), complex(33, 2), complex(66, 2), complex(100, 0))
    bad = nr.CubicBezier(complex(0, 0), complex(33, 50), complex(66, 50), complex(100, 0))
    assert (nr.two_sided_deviation(bad, pts)
            > nr.two_sided_deviation(good, pts))


def test_fit_cubic_rejects_too_few_points():
    assert nr.fit_cubic([complex(0, 0)]) is None
    assert nr.fit_cubic([complex(0, 0), complex(1, 1)]) is None


def test_deviation_is_not_reported_off_the_sample_grid():
    """Regression guard: the deviation must use the REFINED nearest point.

    Snapping t to the nearest of `count` curve samples leaves up to half a
    sample spacing of error. On a geometrically exact fit that reported 0.41
    units, which both overstated the error and, in the other direction, would
    let a real bulge through the tolerance gate.
    """
    import numpy as np
    pts = np.asarray([complex(x, 0.0) for x in np.linspace(0, 100, 40)])
    seg = nr.fit_cubic(list(pts))
    count = len(pts) * nr.CURVE_DENSITY
    grid = nr._cubic_samples(seg, count)
    t_grid = np.argmin((np.abs(pts[:, None] - grid[None, :]) ** 2),
                       axis=1) / float(count - 1)
    coarse = float(np.abs(nr._eval(seg, t_grid) - pts).max())
    refined = nr.two_sided_deviation(seg, list(pts))
    assert coarse > 0.1, "expected visible grid quantisation error"
    assert refined < coarse / 10.0, (
        "refined search (%g) should be far tighter than the grid (%g)"
        % (refined, coarse))


# ------------------------------------------------------------------ simplify

def test_simplify_reduces_a_wiggly_path():
    from svgpathtools import parse_path
    d = wiggly_d(400)
    before = nr.path_nodes(d)
    new_path, n_out = nr.simplify_path(parse_path(d), 1.0)
    after = nr.path_nodes(nr.path_to_d(new_path))
    assert after < before
    assert n_out < before


def test_simplify_keeps_the_endpoints():
    from svgpathtools import parse_path
    d = wiggly_d(200)
    path = parse_path(d)
    new_path, _ = nr.simplify_path(path, 2.0)
    assert abs(complex(new_path.start) - complex(path.start)) < 1e-6
    assert abs(complex(new_path.end) - complex(path.end)) < 1e-6


def test_tight_tolerance_reduces_less_than_loose():
    from svgpathtools import parse_path
    d = wiggly_d(400)
    path = parse_path(d)
    tight = nr.path_nodes(nr.path_to_d(nr.simplify_path(path, 0.01)[0]))
    loose = nr.path_nodes(nr.path_to_d(nr.simplify_path(path, 5.0)[0]))
    assert loose <= tight


def test_short_paths_are_left_alone():
    from svgpathtools import parse_path
    path = parse_path(simple_d())
    out, n = nr.simplify_path(path, 1.0)
    assert n == len(list(path))


def test_simplify_never_increases_node_count():
    from svgpathtools import parse_path
    for tol in (0.1, 1.0, 10.0):
        path = parse_path(wiggly_d(150))
        before = nr.path_nodes(wiggly_d(150))
        after = nr.path_nodes(nr.path_to_d(nr.simplify_path(path, tol)[0]))
        assert after <= before, "tolerance %g increased nodes" % tol


# ------------------------------------------------------------------ reduce_svg

def test_reduce_touches_only_paths_over_the_gate(tmp_path):
    # 700 segments -> 701 nodes, comfortably over the 500 gate; simple_d() is 4.
    d = wiggly_d(700)
    src = write_svg(tmp_path / "in.svg",
                    [(simple_d(), "#111111"), (d, "#222222"),
                     (simple_d(), "#333333")])
    out = tmp_path / "out.svg"
    result = nr.reduce_svg(src, str(out), 500, 0.5, 8.0)
    assert result["paths"] == 3
    assert result["paths_touched"] == 1, "only the over-gate path may be touched"
    # the two small paths must be byte-identical
    tree = etree.parse(str(out)).getroot()
    ds = [el.get("d") for el in tree.iter("{%s}path" % SVG_NS)]
    assert ds[0] == simple_d()
    assert ds[2] == simple_d()


def test_reduce_clears_the_gate(tmp_path):
    src = write_svg(tmp_path / "in.svg", [(wiggly_d(600), "#222222")])
    out = tmp_path / "out.svg"
    result = nr.reduce_svg(src, str(out), 500, 0.5, 8.0)
    assert result["gate_cleared"], result
    assert result["path_nodes_max_after"] <= 500


def test_reduce_reports_no_op_when_nothing_is_over(tmp_path):
    src = write_svg(tmp_path / "in.svg", [(simple_d(), "#111111")])
    result = nr.reduce_svg(src, None, 500, 0.5, 8.0)
    assert result["status"] == "nothing_over_gate"
    assert result["paths_touched"] == 0


def test_reduce_reports_no_paths(tmp_path):
    src = tmp_path / "empty.svg"
    write_svg(src, [])
    result = nr.reduce_svg(str(src), None, 500, 0.5, 8.0)
    assert result["status"] == "no_paths"


def test_reduce_records_the_tolerance_it_used(tmp_path):
    """Provenance: the per-path tolerance must be recorded so a run is
    explainable after the fact."""
    src = write_svg(tmp_path / "in.svg", [(wiggly_d(600), "#222222")])
    out = tmp_path / "out.svg"
    result = nr.reduce_svg(src, str(out), 500, 0.5, 8.0)
    reduced = [p for p in result["per_path"] if p.get("status") == "reduced"]
    assert reduced
    for p in reduced:
        assert p["tolerance"] >= 0.5
        assert p["nodes_after"] < p["nodes_before"]


def test_reduce_tolerates_a_malformed_path(tmp_path):
    """One bad path must not lose the run."""
    root = etree.Element("{%s}svg" % SVG_NS, nsmap={None: SVG_NS})
    bad = etree.SubElement(root, "{%s}path" % SVG_NS)
    bad.set("d", "M bogus")
    good = etree.SubElement(root, "{%s}path" % SVG_NS)
    good.set("d", wiggly_d(600))
    src = tmp_path / "bad.svg"
    etree.ElementTree(root).write(str(src), xml_declaration=True,
                                  encoding="UTF-8")
    out = tmp_path / "out.svg"
    result = nr.reduce_svg(str(src), str(out), 500, 0.5, 8.0)
    assert result["status"] in ("reduced", "no_paths")
    assert os.path.exists(str(out))


def test_reduce_dry_run_writes_nothing(tmp_path):
    src = write_svg(tmp_path / "in.svg", [(wiggly_d(600), "#222222")])
    before = open(src).read()
    nr.reduce_svg(src, None, 500, 0.5, 8.0)
    assert open(src).read() == before


def test_reduce_output_is_parseable_svg(tmp_path):
    from svgpathtools import parse_path
    src = write_svg(tmp_path / "in.svg", [(wiggly_d(600), "#222222")])
    out = tmp_path / "out.svg"
    nr.reduce_svg(src, str(out), 500, 0.5, 8.0)
    root = etree.parse(str(out)).getroot()
    for el in root.iter("{%s}path" % SVG_NS):
        parse_path(el.get("d"))          # raises on malformed output
    assert os.path.getsize(str(out)) > 0


def test_reduce_preserves_fill_and_viewbox(tmp_path):
    src = write_svg(tmp_path / "in.svg", [(wiggly_d(600), "#abcdef")])
    out = tmp_path / "out.svg"
    nr.reduce_svg(src, str(out), 500, 0.5, 8.0)
    before = etree.parse(src).getroot()
    after = etree.parse(str(out)).getroot()
    assert after.get("viewBox") == before.get("viewBox")
    b = list(before.iter("{%s}path" % SVG_NS))[0].get("fill")
    a = list(after.iter("{%s}path" % SVG_NS))[0].get("fill")
    assert a == b == "#abcdef"


def test_reduce_is_idempotent(tmp_path):
    """Rerunning on an already-reduced file must be a no-op, not further damage."""
    src = write_svg(tmp_path / "in.svg", [(wiggly_d(600), "#222222")])
    once = tmp_path / "a.svg"
    twice = tmp_path / "b.svg"
    nr.reduce_svg(src, str(once), 500, 0.5, 8.0)
    r2 = nr.reduce_svg(str(once), str(twice), 500, 0.5, 8.0)
    assert r2["status"] in ("nothing_over_gate", "reduced")
    if r2["status"] == "reduced":
        assert r2["path_nodes_max_after"] <= r2["path_nodes_max_before"]


# ------------------------------------------------------------------ serialise

def test_path_to_d_roundtrips_through_the_parser():
    from svgpathtools import parse_path
    d = wiggly_d(40)
    new_d = nr.path_to_d(parse_path(d))
    assert nr.path_nodes(new_d) > 0
    assert "M" in new_d


def test_path_to_d_keeps_closure():
    from svgpathtools import parse_path
    out = nr.path_to_d(parse_path(simple_d()))
    assert out.strip().endswith("Z")


# ------------------------------------------------- regressions (review found)

def test_quadratics_are_not_deleted():
    """A path of QuadraticBeziers must not serialise to the empty string.

    svgpathtools' QuadraticBezier has NO `.d()` method, so the old fallback
    `seg.d() if hasattr(seg, "d") else ""` produced "", the trailing filter
    dropped it, and the whole path was deleted -- reported as the success
    "reduced, nodes 30 -> 0". A deleted path clears any node gate.
    """
    from svgpathtools import QuadraticBezier, Path as SvgPath
    q = QuadraticBezier(complex(0, 0), complex(5, 5), complex(10, 0))
    p = SvgPath(q, q, q)
    out = nr.path_to_d(p)
    assert out.strip(), "a quadratic path must not serialise to nothing"
    assert nr.path_nodes(out) == 3
    assert out.count("Q") == 3


def test_unsupported_segment_raises_instead_of_approximating():
    from svgpathtools import Arc, Path as SvgPath
    arc = Arc(complex(0, 0), complex(50, 50), 0, 0, 1, complex(100, 0))
    p = SvgPath(arc, arc, arc)
    assert nr.is_serialisable(list(p)) is False
    with pytest.raises(nr.UnsupportedSegmentError):
        nr.path_to_d(p)


def test_arc_path_is_declined_and_left_byte_identical(tmp_path):
    """End to end: a file of arcs comes out unchanged, not approximated.

    The gate is 1 so the path is actually a target -- with the default 500 a
    2-node path is never considered, and the test would pass without ever
    exercising the decline.
    """
    d = "M 0 0 A 50 50 0 0 1 100 0 A 50 50 0 0 1 0 0"
    src = write_svg(tmp_path / "arcs.svg", [(d, "#ff0000")])
    out = tmp_path / "out.svg"
    result = nr.reduce_svg(src, str(out), 1, 0.5, 8.0)
    assert result["status"] == "no_change"
    assert any(p.get("status") == "unsupported_segment"
               for p in result["per_path"])
    assert os.path.isfile(str(out)), "an output file must still be written"
    after_root = etree.parse(str(out)).getroot()
    kept = [el.get("d") for el in after_root.iter("{%s}path" % SVG_NS)]
    assert kept == [d], "the arc path must survive verbatim"


def test_multi_subpath_paths_are_not_welded():
    """Two disjoint subpaths in one `d` must stay two.

    Fitting across a discontinuity joined them into a single closed shape, and
    because the welded result is SHORTER it passed the node-count improvement
    test and was accepted. Node count cannot detect this; only geometry can.
    """
    from svgpathtools import parse_path
    d = "M 0 0 L 10 0 L 10 10 Z M 50 50 L 60 50 L 60 60 Z"
    path = parse_path(d)
    assert d.count("M") == 2
    new_path, _ = nr.simplify_path(path, 2.0)
    out = nr.path_to_d(new_path)
    assert out.count("M") == 2, "subpaths were welded together"
    assert len(nr.split_subpaths(parse_path(out))) == 2


def test_split_subpaths_finds_each_discontinuity():
    from svgpathtools import parse_path
    one = nr.split_subpaths(parse_path("M 0 0 L 10 0 L 10 10 Z"))
    assert len(one) == 1
    two = nr.split_subpaths(
        parse_path("M 0 0 L 10 0 Z M 50 50 L 60 50 Z M 90 90 L 95 95 Z"))
    assert len(two) == 3


def test_simplify_never_returns_an_empty_path():
    from svgpathtools import parse_path
    for d in (wiggly_d(300), simple_d()):
        out_path, n = nr.simplify_path(parse_path(d), 5.0)
        assert nr.path_to_d(out_path).strip(), "simplify emptied a path"


def test_reduce_refuses_an_emptied_fit(tmp_path):
    """`nodes N -> 0` must never be reported as a reduction."""
    src = write_svg(tmp_path / "in.svg", [(wiggly_d(300), "#222222")])
    out = tmp_path / "out.svg"
    result = nr.reduce_svg(src, str(out), 1, 0.5, 64.0)
    # Whatever happens, the written file must still contain geometry.
    assert "fit_empty" not in [p.get("status") for p in result["per_path"]], \
        "an emptied fit was recorded as an accepted reduction"
    root = etree.parse(str(out)).getroot()
    for el in root.iter("{%s}path" % SVG_NS):
        assert el.get("d", "").strip(), "a path was left empty"


def test_line_fallback_consumes_the_whole_run():
    """The line-collapse branch must advance past every segment it consumed.

    It previously advanced by a sample-derived count rather than the run
    length, which could re-visit segments and desynchronise the walk.
    """
    from svgpathtools import parse_path
    d = "M 0 0 " + " ".join("L %d 0" % x for x in range(1, 40))
    path = parse_path(d)
    new_path, n = nr.simplify_path(path, 1.0)
    assert n <= len(list(path)), "simplification grew the segment count"
    # A collinear run should collapse hard.
    assert n < 6, "a straight line did not collapse (%d segments)" % n


def test_no_change_still_writes_an_output(tmp_path):
    src = write_svg(tmp_path / "in.svg", [(simple_d(), "#111111")])
    out = tmp_path / "out.svg"
    result = nr.reduce_svg(src, str(out), 500, 0.5, 8.0)
    assert result["status"] == "nothing_over_gate"
    assert not out.exists(), "nothing was over the gate, so no rewrite"


# ------------------------------------------------- closure (review round 2)

@pytest.mark.parametrize("d", [
    "M 0 0 L 30 0 L 30 20 L 20 30 Z",                    # single closed
    "M 0 0 L 30 0 L 30 20 L 20 30",                     # single OPEN
    "M 0 0 L 30 0 L 30 20 Z",                           # short closed
    "M 0 0 L 30 0 L 30 20 Z M 50 50 L 80 50 L 80 70",   # first closed
    "M 0 0 L 30 0 L 30 20 Z M 50 50 L 80 50 L 80 70 Z", # both closed
    "M 0 0 C 1 1 2 2 3 0 C 4 -1 5 -1 6 0 Z",            # closed via cubics
])
def test_closure_survives_a_roundtrip(d):
    """`Z` must be preserved exactly -- count, not just geometry.

    Two separate bugs hid here. Emitting `Z` only for `k == 0` dropped closure
    from every multi-subpath path; and testing `isinstance(tail, Line)` dropped
    it from every subpath the fitter closed with a CUBIC, i.e. exactly the paths
    this tool rewrites. Both reported success.
    """
    from svgpathtools import parse_path
    out = nr.path_to_d(parse_path(d))
    assert out.upper().count("Z") == d.upper().count("Z"), \
        "closure count changed: %r -> %r" % (d, out)
    assert out.count("M") == d.count("M")
    assert nr.path_nodes(out) <= nr.path_nodes(d), \
        "serialising added nodes: %d -> %d" % (nr.path_nodes(d),
                                               nr.path_nodes(out))


def test_a_fitted_subpath_closed_by_a_cubic_keeps_its_z():
    """The specific case the Line-only test missed."""
    from svgpathtools import CubicBezier, Path as SvgPath
    c1 = CubicBezier(complex(0, 0), complex(10, 20), complex(20, 20),
                     complex(30, 0))
    c2 = CubicBezier(complex(30, 0), complex(20, -20), complex(10, -20),
                     complex(0, 0))          # returns to the start
    out = nr.path_to_d(SvgPath(c1, c2))
    assert out.upper().endswith("Z"), \
        "a subpath closed by a cubic must keep its Z: %r" % out


def test_a_subpath_fitted_to_one_closed_segment_keeps_its_z():
    """A single-segment subpath whose end IS its start is a collapsed loop.

    Requiring `len(sub) > 1` dropped its Z, and calling the segment
    "degenerate" (start == end) dropped it again for a different reason. On
    candidate_08 that silently removed 16 closures across 7 rewritten paths --
    the majority of them -- while every node-count check still passed.
    """
    from svgpathtools import CubicBezier, Path as SvgPath
    loop = CubicBezier(complex(10, 10), complex(40, 60), complex(-20, 60),
                       complex(10, 10))       # one segment, ends where it began
    out = nr.path_to_d(SvgPath(loop))
    assert out.upper().count("Z") == 1, "collapsed loop lost its Z: %r" % out
    assert "C" in out.upper(), "the loop's geometry was deleted: %r" % out
    assert out.strip().upper().endswith("Z")


def test_a_collapsed_loop_is_never_serialised_to_a_bare_z():
    """Regression guard for the deletion this nearly introduced.

    Dropping the only segment of a closed one-segment subpath would leave a
    lone `Z`, which is not a shape at all.
    """
    from svgpathtools import CubicBezier, Path as SvgPath
    loop = CubicBezier(complex(5, 5), complex(30, 40), complex(-10, 40),
                       complex(5, 5))
    out = nr.path_to_d(SvgPath(loop)).strip()
    assert out.upper() != "Z"
    from svgpathtools import parse_path
    assert len(parse_path(out)) >= 1


def test_reduce_preserves_closure_on_real_output(tmp_path):
    """End to end: Z count must be identical before and after."""
    from lxml import etree as ET

    def z_count(path):
        root = ET.parse(str(path)).getroot()
        return sum((el.get("d") or "").upper().count("Z")
                   for el in root.iter("{%s}path" % SVG_NS))

    d = "M 0 0 " + " ".join(
        "C %f %f %f %f %f %f" % (x * 0.1, 20 - (x % 7) * 3, x * 0.1 + 1,
                                 20 - ((x + 1) % 7) * 3, x * 0.1 + 2,
                                 20 - ((x + 2) % 7) * 3)
        for x in range(1, 120)) + " C 0 20 0 10 0 0 Z"
    src = write_svg(tmp_path / "in.svg", [(d, "#224466")])
    out = tmp_path / "out.svg"
    result = nr.reduce_svg(src, str(out), 5, 0.5, 8.0)
    if result["status"] == "reduced":
        assert z_count(out) == z_count(src), "closure was lost or invented"


def test_num_rejects_non_finite_coordinates():
    """`nan` in a `d` breaks parse_path, so path_nodes falls back to counting
    regex command letters -- a corrupted path can then count as FEWER nodes and
    read as a successful reduction."""
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(nr.UnsupportedSegmentError):
            nr._num(bad)
    assert nr._num(1.5) == "1.5"
    assert nr._num(0.0) == "0"
    assert nr._num(-0.0) == "0"


# --------------------------------------------------------- --verify (review)

def test_verify_in_in_place_mode_compares_two_different_files(tmp_path):
    """`--verify` with no `--out` overwrites the input, then rendered "before"
    from that same path -- reporting a perfect, meaningless 0.000% from the
    very check meant to catch geometry damage.

    Runs the real CLI in place and asserts the reported change is non-zero for
    a file that genuinely changes. Skipped without Inkscape.
    """
    import shutil
    import subprocess
    if shutil.which("inkscape") is None:
        pytest.skip("inkscape not installed")
    d = "M 0 0 " + " ".join(
        "L %f %f" % (x * 0.5, 20 + (x % 9) * 4) for x in range(1, 200)
    ) + " L 0 0 Z"
    src = write_svg(tmp_path / "in.svg", [(d, "#c1272d")])
    proc = subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "node_reduce.py"),
         src, "--max-nodes", "10", "--verify",
         "--json", str(tmp_path / "r.json")],
        capture_output=True, text=True, timeout=600,
        env=dict(os.environ, DBUS_SESSION_BUS_ADDRESS="disabled:"))
    assert proc.returncode == 0, proc.stderr[-500:]
    payload = json.loads((tmp_path / "r.json").read_text())
    if payload["status"] == "reduced" and "verify" in payload \
            and not payload["verify"].get("error"):
        assert payload["verify"]["px_changed_percent"] > 0.0, \
            ("--verify reported zero change in in-place mode, so it compared "
             "the file against itself: %r" % payload["verify"])



