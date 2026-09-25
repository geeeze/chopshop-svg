#!/usr/bin/env bash
#
# pipeline.sh -- the v4.0 two-layer print pipeline in one command.
#
#   Layer A (stage 4)   validate_svg.py  -- source-level, fast, deterministic
#   Layer B (stage 4b)  preflight.py     -- rendered, measured, slower
#
# Both gates run and both are reported. A job passes only if neither layer
# raises a HARD finding; advisories are reported and do not fail the run.
#
# Usage:
#   ./pipeline.sh                       # uses 00_source/artwork.svg
#   ./pipeline.sh path/to/art.svg       # explicit input
#   SPEC=other.json ./pipeline.sh x.svg # explicit spec
#   ./pipeline.sh x.svg --layer-a-only  # skip stage 4b
#
set -uo pipefail

PROJECT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$PROJECT/.venv/bin/python"
SPEC="${SPEC:-$PROJECT/spec.json}"

usage() {
  sed -n '3,17p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

[ $# -ge 1 ] && case "$1" in -h|--help) usage ;; esac

INPUT=""
LAYER_A_ONLY=0
DPI=""
for arg in "$@"; do
  case "$arg" in
    --layer-a-only) LAYER_A_ONLY=1 ;;
    --dpi=*) DPI="${arg#--dpi=}" ;;
    -*) echo "unknown option: $arg" >&2; usage ;;
    *) INPUT="$arg" ;;
  esac
done

if [ -z "$INPUT" ]; then
  INPUT="$(find "$PROJECT/00_source" -maxdepth 1 -name '*.svg' | sort | head -1)"
fi
if [ -z "$INPUT" ] || [ ! -f "$INPUT" ]; then
  echo "no input SVG: pass a path, or put one in 00_source/" >&2
  exit 2
fi
if [ ! -f "$SPEC" ]; then
  echo "spec not found: $SPEC" >&2
  exit 2
fi
if [ ! -x "$PY" ]; then
  echo "python venv missing at $PY -- run: python3 -m venv .venv && .venv/bin/pip install lxml svgpathtools Pillow numpy pytest" >&2
  exit 2
fi

STAGE_A_DIR="$PROJECT/04_validated"
STAGE_B_DIR="$PROJECT/05_final"
mkdir -p "$STAGE_A_DIR" "$STAGE_B_DIR"
STEM="$(basename "$INPUT" .svg)"

# Honour validation.run_preflight from the spec (v4.0 stage 0 contract).
RUN_PREFLIGHT="$("$PY" - "$SPEC" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1]) as fh:
        spec = json.load(fh)
    validation = spec.get("validation") or {}
    print("true" if validation.get("run_preflight", True) else "false")
except Exception:
    print("true")
PYEOF
)"
BANNER="$(printf '=%.0s' $(seq 1 72))"

echo "$BANNER"
echo "v4.0 pipeline   input: $INPUT"
echo "                spec:  $SPEC"
echo "$BANNER"

echo
echo "--- stage 4: source validation (Layer A) ---"
"$PY" "$PROJECT/validate_svg.py" "$INPUT" "$SPEC" 2>&1 | tee "$STAGE_A_DIR/${STEM}.layer_a.txt"
CODE_A="${PIPESTATUS[0]}"

CODE_B=0
LAYER_B_RAN=0
if [ "$LAYER_A_ONLY" -eq 1 ]; then
  echo
  echo "--- stage 4b: render preflight (Layer B) -- SKIPPED (--layer-a-only) ---"
elif [ "$RUN_PREFLIGHT" != "true" ]; then
  echo
  echo "--- stage 4b: render preflight (Layer B) -- SKIPPED (validation.run_preflight=false) ---"
else
  echo
  echo "--- stage 4b: render preflight (Layer B) ---"
  # preflight writes the single unified manifest; its workdir keeps the
  # separations out of 05_final.
  DPI_FLAG=""
  [ -n "$DPI" ] && DPI_FLAG="--dpi=$DPI"
  "$PY" "$PROJECT/preflight.py" "$INPUT" "$SPEC" \
      --workdir "$PROJECT/.preflight-work" $DPI_FLAG \
      --json "$STAGE_B_DIR/${STEM}.manifest.json" 2>&1 | tee "$STAGE_B_DIR/${STEM}.layer_b.txt"
  CODE_B="${PIPESTATUS[0]}"

  # Publish the artifacts per job, plus a run-unique copy so consecutive
  # runs never overwrite each other. The per-stem names are the canonical
  # outputs; the stamped copy carries a timestamp + 4 random bytes.
  RUN_ID="$(date +%Y%m%d-%H%M%S)-$(od -An -N4 -tx1 </dev/urandom | tr -d ' \n')"
  for produced in proof.png print.pdf; do
    src="$PROJECT/.preflight-work/${STEM}.${produced}"
    if [ -f "$src" ]; then
      cp -f "$src" "$STAGE_B_DIR/${STEM}.${produced}"
      cp -f "$src" "$STAGE_B_DIR/${RUN_ID}.${produced}"
    fi
  done
  [ -f "$STAGE_B_DIR/${STEM}.manifest.json" ] && \
      cp -f "$STAGE_B_DIR/${STEM}.manifest.json" "$STAGE_B_DIR/${RUN_ID}.manifest.json"
  LAYER_B_RAN=1
fi

echo
echo "$BANNER"
if [ "$CODE_A" -eq 0 ] && [ "$CODE_B" -eq 0 ]; then
  echo "PIPELINE PASSED -- $STEM"
  echo "  layer A (source): ok"
  if [ "$LAYER_B_RAN" -eq 1 ]; then
    echo "  layer B (render): ok"
  else
    echo "  layer B (render): not run"
  fi
  [ -f "$STAGE_B_DIR/${STEM}.manifest.json" ] && echo "  manifest: $STAGE_B_DIR/${STEM}.manifest.json"
  exit 0
fi
echo "PIPELINE FAILED -- $STEM"
[ "$CODE_A" -ne 0 ] && echo "  layer A (source validation) exited $CODE_A: $STAGE_A_DIR/${STEM}.layer_a.txt"
[ "$CODE_B" -ne 0 ] && echo "  layer B (render preflight) exited $CODE_B: $STAGE_B_DIR/${STEM}.layer_b.txt"
[ -f "$STAGE_B_DIR/${STEM}.manifest.json" ] && echo "  manifest: $STAGE_B_DIR/${STEM}.manifest.json  (see \"findings\" for severities)"
echo "  A source-level failure is usually cheaper to fix first: it often clears the render result too."
exit 1