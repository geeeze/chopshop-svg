"""Make the project root importable from the test suite.

The tests live in tests/ per the v4.0 layout, but validate_svg.py and
preflight.py sit at the project root next to the pipeline, so the root has to
be on sys.path.
"""

import os
import shutil
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# -------------------------------------------------------------------------- #
# Skip markers for system tools the tests shell out to.                       #
# -------------------------------------------------------------------------- #
# Without these, a fresh checkout without inkscape/gs/qpdf/poppler looks       #
# broken (23 fail, 5 error).  Tests that need a tool skip cleanly instead.    #

_TOOLS = ("inkscape", "gs", "qpdf", "pdfinfo", "pdfimages")
_missing = [t for t in _TOOLS if shutil.which(t) is None]

needs_render = pytest.mark.skipif(
    bool(_missing),
    reason="missing render tools: " + ", ".join(_missing))


# -------------------------------------------------------------------------- #
# Learning-store isolation                                                    #
# -------------------------------------------------------------------------- #
# The Phase 2 shown-event log and the learned-bucket store live at a
# JOB-INDEPENDENT path on purpose -- they must survive deletion of the job they
# describe (see scripts/triage_events.py). That is right in production and
# wrong under test: without this, every run of the suite that exercises the
# comparison stage appends "shown" events for synthetic candidates into the
# developer's real learning store, and a future learner would be training on
# that noise. Point the store at a throwaway dir; a test that wants a specific
# location overrides the env var itself with monkeypatch.

@pytest.fixture(autouse=True)
def _isolated_learning_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("PIPELINE_LEARNING_DIR", str(tmp_path / "learning"))

