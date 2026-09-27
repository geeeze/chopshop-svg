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
# spec.print.prep_expand additionally runs the expanded prep benches at the two
# points where they belong -- raster filters and the tune sweep after prep, and
# node_reduce after the sweep, because it needs candidates to work on. All are
# off unless the key asks for them, so a spec without it behaves exactly as
# before. Their reports land in 06_run/<stem>/prep_expand (override with
# PREP_EXPAND_OUT), and they never change which candidate is chosen.
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

# --------------------------------------------------------------------------
# disk capacity gate: warn below 2GB, refuse below 1GB (disk_check.py exits 2)
# --------------------------------------------------------------------------
if ! "$PY" "$PROJECT/scripts/disk_check.py" "$PROJECT"; then
  echo "disk capacity gate refused the run (free < 1GiB) -- free space first" >&2
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
# stage 1b: expanded prep -- raster filters and the tune sweep (spec-gated)
# --------------------------------------------------------------------------
# Gated on the spec key rather than run-and-no-op, so a job that does not ask
# for these produces exactly the same log and artefacts as before they existed.
PREP_EXPAND_OUT="${PREP_EXPAND_OUT:-$PROJECT/06_run/$STEM/prep_expand}"
PREP_EXPAND_ENABLED="$("$PY" - "$SPEC" <<'PYEOF'
import json, sys
try:
    spec = json.load(open(sys.argv[1]))
except Exception:
    print("0"); raise SystemExit(0)
block = (spec.get("print") or {}).get("prep_expand") or {}
print("1" if isinstance(block, dict) and any(bool(v) for v in block.values()) else "0")
PYEOF
)"
if [ "$PREP_EXPAND_ENABLED" = "1" ]; then
  echo
  echo "--- stage 1b: expanded prep (filters, tune) ---"
  "$PY" "$PROJECT/scripts/prep_expand.py" --phase prep \
      "$PREPPED" "$SPEC" --out-dir "$PREP_EXPAND_OUT" || {
    # A bench that fails must not lose the job: the artefacts it would have
    # produced are advisory measurements, and the tracer does not read them.
    echo "prep_expand (prep) failed; continuing without it" >&2
  }
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
# stage 2b: node_reduce -- reduced COPIES of the over-gate candidates
# --------------------------------------------------------------------------
# Before compare, not after: the copies are written as fresh candidate_NN.svg
# files in the traced dir, and compare discovers candidates by that exact name,
# so they have to exist before it runs to be measured at all. The originals are
# never modified -- a run whose gate "clears" but whose artwork erodes is a real
# outcome (see scripts/NODE_REDUCTION.md), so both are kept and both are graded.
if [ "$PREP_EXPAND_ENABLED" = "1" ]; then
  echo
  echo "--- stage 2b: node_reduce ---"
  "$PY" "$PROJECT/scripts/prep_expand.py" --phase post-trace \
      "$STEM" "$SPEC" --out-dir "$PREP_EXPAND_OUT" \
      --traced-root "$PROJECT/02_traced" || {
    echo "prep_expand (node_reduce) failed; continuing with the traced candidates" >&2
  }
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
