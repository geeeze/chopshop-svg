"""The shipped example must stay printable.

00_source/00-example.png is the first thing anyone runs, and the repo's own
front_pipeline/pipeline are the proof it works. This asserts the SOURCE half
of that statically -- colour count, no black, no gradients, no raster embed --
so a future edit to scripts/make_example_source.py cannot quietly produce an
example that the gates will reject, without waiting for a full pipeline run.

The runtime half (layers A and B) is covered by
test_example_passes_full_pipeline, which shells out to the real scripts.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "00_source" / "00-example.png"
TRACE = REPO / "02_traced" / "00-example" / "candidate_10.svg"
SPEC = REPO / "spec.json"

pytest.importorskip("PIL")

def _rgb(rng):
    return tuple(int(rng[i:i + 2], 16) for i in (1, 3, 5))


# spec.json's limits, read rather than duplicated, so these cannot drift.
_SPEC = json.loads(SPEC.read_text())
PALETTE_MAX = _SPEC["max_colors"]
ALLOWED = {_rgb(c) for c in _SPEC["palette"]}


@pytest.fixture(scope="module")
def source():
    if not SOURCE.exists():
        pytest.skip("00_source/00-example.png not built; "
                    "run .venv/bin/python scripts/make_example_source.py")
    from PIL import Image
    return Image.open(SOURCE).convert("RGB")


def test_example_is_inside_the_colour_gate(source):
    """A screen print has a hard colour ceiling. Tonal art cannot meet it.

    The example this replaced was an engraved floral: 245 608 distinct
    colours, where 256 colours covered only 34% of pixels. Every candidate
    that "passed" did so by collapsing to a single black silhouette
    (mae_art 114), which is not an example of anything useful.
    """
    colours = source.getcolors(maxcolors=1 << 24) or []
    assert len(colours) <= PALETTE_MAX, (
        "%d distinct colours exceeds the %d-colour gate; a source this tonal "
        "cannot pass, it can only pass by discarding the artwork"
        % (len(colours), PALETTE_MAX)
    )


def test_example_contains_no_black(source):
    """Solid black alone trips INK_COVERAGE.

    Layer B separates to CMYK, and #000000 comes out near 400% coverage by
    itself -- over the 300% limit. Measured: an identical design with a black
    ring reports 300% and 3.4% of the sheet over; with blue instead, no
    advisory fires at all.
    """
    dark = sum(n for n, rgb in (source.getcolors(maxcolors=1 << 24) or [])
               if max(rgb) < 40)
    assert dark == 0, (
        "%d near-black pixels: black ink alone exceeds the ink-coverage "
        "limit once separated to CMYK" % dark
    )


def test_every_colour_is_in_the_spec_palette(source):
    """Off-palette ink fails the PALETTE hard gate."""
    stray = {rgb for _, rgb in (source.getcolors(maxcolors=1 << 24) or [])
             if rgb not in ALLOWED}
    assert not stray, "colours outside the spec palette: %s" % sorted(stray)


@pytest.mark.skipif(not TRACE.exists(), reason="example not traced yet")
def test_traced_example_is_actually_printable():
    """End-to-end: the shipped trace must pass layer A and carry no raster."""
    d = TRACE.read_text()
    assert "<image" not in d and "data:image" not in d, (
        "allow_raster_embed is false: the trace embeds a raster"
    )
    assert not re.search(r"(?i)linearGradient|radialGradient", d), (
        "allow_gradients is false"
    )
    # 211 nodes for this design; the gate is 500/path. A large jump means the
    # example regressed into something the tracer cannot simplify.
    nodes = len(re.findall(r"[MLCZ]", d))
    assert nodes < 500, "%d nodes is unexpectedly heavy for the example" % nodes


def test_generator_is_deterministic():
    """Regenerating must not churn the committed PNG."""
    out = REPO / ".test-example-determinism.png"
    try:
        r = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "make_example_source.py"),
             str(out)],
            capture_output=True, text=True, cwd=REPO,
        )
        assert r.returncode == 0, r.stderr[-500:]
        assert out.read_bytes() == SOURCE.read_bytes(), (
            "scripts/make_example_source.py no longer reproduces the "
            "committed 00_source/00-example.png"
        )
    finally:
        out.unlink(missing_ok=True)
