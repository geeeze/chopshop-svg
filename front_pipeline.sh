#!/usr/bin/env bash
#
# front_pipeline.sh -- the FRONT half of the print pipeline in one command.
#
#   raster image -> normalized PNG -> sweep of traced candidates -> comparison
#
# It stops at the comparison: a human (or GPT/Astra) reads the report, chooses
# a candidate, and runs pipeline.sh (the BACK half) on that candidate.  This
# script never calls pipeline.sh, and it never judges trace quality.
#
# Usage:
#   ./front_pipeline.sh artwork.png                 # prep -> sweep -> compare
#   ./front_pipeline.sh artwork.png --sweep s.json  # custom sweep
#   ./front_pipeline.sh artwork.png --skip-prep     # reuse an existing prep
#   ./front_pipeline.sh artwork.png --fix           # apply prep transforms
#   ./front_pipeline.sh artwork.png --loop          # then pick candidates interactively
#   SPEC=other.json ./front_pipeline.sh art.png     # different spec
#
# Exit codes:
#   0  at least one candidate passed both gates (paths printed)
#   1  candidates exist but none passed (comparison path printed)
#   2  a step failed: bad input, missing tool, or no candidates produced
#
set -uo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$PROJECT/.venv/bin/python"
SPEC="${SPEC:-$PROJECT/spec.json}"

usage() {
  sed -n '11,21p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

[ $# -ge 1 ] && case "$1" in -h|--help) usage ;; esac

INPUT=""
SWEEP=""
SKIP_PREP=0
FIX_PREP=0
LOOP=0
while [ $# -gt 0 ]; do
  case "$1" in
    --sweep)  SWEEP="$2"; shift 2 ;;
    --sweep=*) SWEEP="${1#--sweep=}"; shift ;;
    --skip-prep) SKIP_PREP=1; shift ;;
    --fix) FIX_PREP=1; shift ;;
    --loop) LOOP=1; shift ;;
    -h|--help) usage ;;
    -*) echo "unknown option: $1" >&2; usage ;;
    *) INPUT="$1"; shift ;;
  esac
done

if [ -z "$INPUT" ]; then
  echo "no raster input: pass a path to a PNG/JPG/TIFF" >&2
  usage
fi
if [ ! -f "$INPUT" ]; then
  echo "input not found: $INPUT" >&2
  exit 2
fi
if [ ! -f "$SPEC" ]; then
  echo "spec not found: $SPEC" >&2
  exit 2
fi
if [ ! -x "$PY" ]; then
  echo "python venv missing at $PY -- run: python3 -m venv .venv && .venv/bin/pip install lxml svgpathtools Pillow numpy pytest vtracer" >&2
  exit 2
fi

mkdir -p "$PROJECT/01_prepped" "$PROJECT/02_traced" "$PROJECT/04_validated"
STEM="$(basename "$INPUT")"
STEM="${STEM%.*}"

BANNER="$(printf '=%.0s' $(seq 1 72))"

echo "$BANNER"
echo "front pipeline   input: $INPUT"
echo "                 spec:  $SPEC"
echo "$BANNER"

# --------------------------------------------------------------------------
# stage 1: prep
# --------------------------------------------------------------------------
PREPPED="$PROJECT/01_prepped/$STEM.prepped.png"
echo
echo "--- stage 1: raster prep ---"
if [ "$SKIP_PREP" -eq 1 ]; then
  if [ ! -f "$PREPPED" ]; then
    echo "--skip-prep was given but $PREPPED does not exist" >&2
    exit 2
  fi
  echo "prep skipped (--skip-prep); using $PREPPED"
else
  FIX_ARG=""
  [ "$FIX_PREP" -eq 1 ] && FIX_ARG="--fix"
  "$PY" "$PROJECT/scripts/prep_raster.py" "$INPUT" "$SPEC" \
      --out-dir "$PROJECT/01_prepped" $FIX_ARG || {
    echo "prep_raster failed" >&2
    exit 2
  }
fi
if [ ! -f "$PREPPED" ]; then
  echo "prep produced no $PREPPED" >&2
  echo "if the input is already vector (.svg/.pdf), tracing does not apply -- run ./pipeline.sh on it directly" >&2
  exit 2
fi

# --------------------------------------------------------------------------
# stage 2: trace sweep
# --------------------------------------------------------------------------
TRACED="$PROJECT/02_traced/$STEM"
echo
echo "--- stage 2: trace sweep ---"
SWEEP_ARG=""
[ -n "$SWEEP" ] && SWEEP_ARG="--sweep $SWEEP"
"$PY" "$PROJECT/scripts/trace_sweep.py" "$PREPPED" "$SPEC" $SWEEP_ARG \
    --out-dir "$TRACED"
CODE_TRACE=$?
if [ "$CODE_TRACE" -eq 3 ]; then
  echo "trace_sweep could not find a tracer (exit 3); see the install commands above" >&2
  exit 2
fi
if [ "$CODE_TRACE" -ne 0 ]; then
  echo "trace_sweep failed (exit $CODE_TRACE)" >&2
  exit 2
fi

# --------------------------------------------------------------------------
# stage 3: compare
# --------------------------------------------------------------------------
echo
echo "--- stage 3: compare candidates (Layer A + Layer B) ---"
"$PY" "$PROJECT/scripts/compare_candidates.py" "$TRACED" "$SPEC" || {
  echo "compare_candidates failed" >&2
  exit 2
}
COMPJSON="$PROJECT/04_validated/$STEM.comparison.json"
if [ ! -f "$COMPJSON" ]; then
  echo "compare produced no report at $COMPJSON" >&2
  exit 2
fi

# --------------------------------------------------------------------------
# --loop: hand off to the interactive pick -> validate -> pick driver
# --------------------------------------------------------------------------
if [ "$LOOP" -eq 1 ]; then
  echo
  echo "--- interactive pick: run pipeline.sh on chosen candidates ---"
  "$PY" "$PROJECT/scripts/pick_finish.py" "$TRACED" "$SPEC"
  exit $?
fi

# --------------------------------------------------------------------------
# verdict (the human picks the candidate; this only reports the outcome)
# --------------------------------------------------------------------------
VERDICT="$("$PY" - "$COMPJSON" "$TRACED" <<'PYEOF'
import json, os, sys
data = json.load(open(sys.argv[1]))
traced = sys.argv[2]
cands = data.get("candidates", [])
passed = [c["file"] for c in cands if c.get("passed")]
if not cands:
    print("EMPTY")
elif passed:
    print("PASS")
    for f in passed:
        print(os.path.join(traced, f))
else:
    print("NOPASS")
PYEOF
)"

echo
echo "$BANNER"
case "$VERDICT" in
  EMPTY*)
    echo "front pipeline: no candidates were produced"
    exit 2
    ;;
  PASS*)
    echo "front pipeline: passing candidates below -- choose one, then run:"
    echo "passing candidates:"
    printf '%s\n' "$VERDICT" | tail -n +2 | sed 's/^/  /'
    echo "comparison: $COMPJSON"
    echo "            $PROJECT/04_validated/$STEM.comparison.md"
    echo "then:        ./pipeline.sh <chosen candidate>.svg"
    exit 0
    ;;
  NOPASS*)
    echo "front pipeline: candidates exist but none passed both gates"
    echo "comparison: $COMPJSON"
    echo "            $PROJECT/04_validated/$STEM.comparison.md"
    exit 1
    ;;
  *)
    echo "front pipeline: unexpected verdict: $VERDICT" >&2
    exit 2
    ;;
esac
