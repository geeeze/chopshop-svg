# Path geometry with svgpathtools

## Recipes

```python
from svgpathtools import parse_path

path = parse_path(d)          # raises on malformed data: catch and report it
len(path)                      # number of segments; 0 for a moveto-only 'd'
path.isclosed()                # start == end (within tolerance) for the WHOLE path
path.continuous_subpaths()     # list[Path], one per continuous subpath
sub.isclosed()                 # per-subpath closure
path.length()                  # total drawn length
path.area()                    # signed enclosed area (closed paths)
path[0].start                  # complex x+yj of the first segment's start
```

## Behaviour to rely on

- **A `d` with only a moveto** (`"M10,10"`) parses successfully but yields an
  empty path: `len(path) == 0`. It draws nothing, so treat it as zero-area
  rather than skipping it.
- **A path can have length and zero area.** `"M10,10 L20,20 Z"` is closed,
  measurable, and encloses nothing — a collapsed hairline that prints as an
  invisible sliver. Check length and area as separate rules.
- **`isclosed()` on a multi-subpath path is all-or-nothing.** `"M0,0 L10,0
  L10,10 Z M20,20 L30,20"` can report closed for the path as a whole; call
  `continuous_subpaths()` and test each subpath, ignoring zero-length stubs, so
  a genuine open subpath is not hidden by a closed sibling.
- **Malformed data raises** — catch broadly (`except Exception`) and report the
  element plus the parser message. One bad `d` must not abort the other rules.

## Tolerances

Use explicit tolerances rather than exact equality, because floating-point path
arithmetic rarely lands exactly on the start point:

- closure: `abs(path.start - path.end) <= 1e-6`
- zero length: `length <= 1e-9`
- zero area: `abs(area) <= 1e-9`

## Defensive fallbacks

Library calls can fail on unusual segment types, so wrap each measurement and
fall back rather than letting the rule crash:

- length → sum `segment.length()`, then `abs(segment.end - segment.start)` per
  segment as a last resort
- area → if `path.area()` raises, report what you can and leave the shape
  unreported rather than guessing zero

## What this does not cover

Geometry is measured in **user units**: a `transform="scale(...)"` on the
element or an ancestor is not applied to stroke width or to a measured area.
Document that in the script header instead of pretending otherwise, and if exact
stroke-width compliance matters, flatten transforms or require the file to be
pre-scaled.
