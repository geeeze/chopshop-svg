"""Make the project root importable from the test suite.

The tests live in tests/ per the v4.0 layout, but validate_svg.py and
preflight.py sit at the project root next to the pipeline, so the root has to
be on sys.path.
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
