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

# --------------------------------------------------------------------------
# disk capacity gate: warn below 2GB, refuse below 1GB (disk_check.py exits 2)
# --------------------------------------------------------------------------
if ! "$PY" "$PROJECT/scripts/disk_check.py" "$PROJECT"; then
  echo "disk capacity gate refused the run (free < 1GiB) -- free space first" >&2
  exit 2
fi

STAGE_A_DIR="$PROJECT/04_validated"
STAGE_B_DIR="$PROJECT/05_final"
mkdir -p "$STAGE_A_DIR" "$STAGE_B_DIR"
STEM="$(basename "$INPUT" .svg)"

# --------------------------------------------------------------------------
# artwork identity: every published artefact names (artwork, candidate, run)
# --------------------------------------------------------------------------
# Why this block exists: STEM is the INPUT FILE's stem.  When the back half is
# run per candidate -- 02_traced/<artwork>/candidate_NN.svg, exactly how
# runner.py invokes it -- that stem is "candidate_NN", the same string for every
# artwork that ever traced.  05_final/candidate_NN.manifest.json is therefore
# shared state: a later artwork's run record could read an earlier artwork's
# manifest, proof and human pick.  The canonical ${STEM}.* names below stay
# exactly as they are (runner.py:620-636 reads them, scripts/orphan_sweep.py
# classifies them); everything added here is ADDITIVE -- an artwork-scoped set,
# so an artefact is always traceable back to the artwork it came from.
#
# artwork-stem-resolution
# The containing directory names the artwork (runner.py copies the candidate
# into 02_traced/<artwork-stem>/ and runs the back half there).  A path handed
# straight to this script -- 00_source/art.svg -- names its own artwork, so the
# shared output columns and a dir-less path fall back to $STEM.
PARENT_STEM="$(basename "$(dirname "$INPUT")")"
case "$PARENT_STEM" in
  ""|.|00_source|01_prepped|02_traced|04_validated|05_final|06_run)
    ARTWORK_STEM="$STEM" ;;
  *)
    ARTWORK_STEM="$PARENT_STEM" ;;
esac
#label-pipeline-artwork-stem-resolution
# Artwork-scoped column inside 05_final.  05_final is NOT swept by
# orphan_sweep.py (its STEM_DIRS are 01_prepped, 02_traced, 04_validated), so a
# subdirectory here is never classified as an orphan, let alone deleted.
SCALED_B_DIR="$STAGE_B_DIR/$ARTWORK_STEM"
mkdir -p "$SCALED_B_DIR"
# artwork-scoped preflight workdir: one per (artwork, candidate).  The former
# shared top-level .preflight-work let two runs of the same candidate stem race
# on <stem>.proof.png / <stem>.print.pdf / flattened.pdf and on the stale
# sep*.tif cleanup inside it.  preflight.py creates the directory itself
# (os.makedirs(workdir, exist_ok=True)) and only ever touches files inside it,
# so a nested path is equivalent to the flat one -- see preflight.py:1370-1375.
PREFLIGHT_WORK="$PROJECT/.preflight-work/$ARTWORK_STEM/$STEM"

# Honour validation.run_preflight from the spec (v4.0 stage 0 contract).
# The except branch below fails OPEN (an unparseable/missing spec runs Layer B).
# That is safe: a spec that cannot be parsed is not a spec a candidate can pass
# Layer A against, so validate_svg.py fails the run on its own and the render is
# only wasted time, never a false PASS.
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
[ "$ARTWORK_STEM" != "$STEM" ] && echo "                artwork: $ARTWORK_STEM  candidate: $STEM"
echo "$BANNER"

echo
echo "--- stage 4: source validation (Layer A) ---"
# The canonical <candidate-stem>.layer_a.txt stays (orphan_sweep's stem-prefix
# rule and the run record read it); the artwork-scoped <artwork>.<candidate>.
# layer_a.txt is added so the report is attributable when the same candidate
# stem appears under two artworks.  Both are artwork-stem-prefixed, so neither
# is ever mistaken for a stem-keyed orphan.
LAYER_A_TEES=("$STAGE_A_DIR/${STEM}.layer_a.txt")
[ "$ARTWORK_STEM" != "$STEM" ] && \
    LAYER_A_TEES+=("$STAGE_A_DIR/${ARTWORK_STEM}.${STEM}.layer_a.txt")
"$PY" "$PROJECT/validate_svg.py" "$INPUT" "$SPEC" 2>&1 | tee "${LAYER_A_TEES[@]}"
#label-pipeline-artwork-scoped-layer-a
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
  # separations out of 05_final.  The workdir is per (artwork, candidate) so two
  # runs of the same candidate name under different artworks cannot race.
  DPI_FLAG=""
  [ -n "$DPI" ] && DPI_FLAG="--dpi=$DPI"
  LAYER_B_TEES=("$STAGE_B_DIR/${STEM}.layer_b.txt")
  [ "$ARTWORK_STEM" != "$STEM" ] && LAYER_B_TEES+=("$SCALED_B_DIR/${STEM}.layer_b.txt")
  "$PY" "$PROJECT/preflight.py" "$INPUT" "$SPEC" \
      --workdir "$PREFLIGHT_WORK" $DPI_FLAG \
      --json "$STAGE_B_DIR/${STEM}.manifest.json" 2>&1 | tee "${LAYER_B_TEES[@]}"
  #label-pipeline-preflight-workdir-scope
  CODE_B="${PIPESTATUS[0]}"

  # Publish the artefacts per job, plus a run-unique copy so consecutive runs
  # never overwrite each other.  Three names per artefact, all additive:
  #   <candidate>.x                          canonical (runner.py reads these)
  #   <artwork>/<candidate>.x                artwork-scoped, no collision
  #   <artwork>.<candidate>.<RUN_ID>.x       run-unique, attributable to both
  RUN_ID="$(date +%Y%m%d-%H%M%S)-$(od -An -N4 -tx1 </dev/urandom | tr -d ' \n')"
  for produced in proof.png print.pdf; do
    src="$PREFLIGHT_WORK/${STEM}.${produced}"
    if [ -f "$src" ]; then
      cp -f "$src" "$STAGE_B_DIR/${STEM}.${produced}"
      cp -f "$src" "$SCALED_B_DIR/${STEM}.${produced}"
      cp -f "$src" "$STAGE_B_DIR/${ARTWORK_STEM}.${STEM}.${RUN_ID}.${produced}"
    fi
  done
  if [ -f "$STAGE_B_DIR/${STEM}.manifest.json" ]; then
    cp -f "$STAGE_B_DIR/${STEM}.manifest.json" "$SCALED_B_DIR/${STEM}.manifest.json"
    cp -f "$STAGE_B_DIR/${STEM}.manifest.json" \
        "$STAGE_B_DIR/${ARTWORK_STEM}.${STEM}.${RUN_ID}.manifest.json"
  fi
  #label-pipeline-artwork-scoped-publish
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
  [ "$ARTWORK_STEM" != "$STEM" ] && [ -f "$SCALED_B_DIR/${STEM}.manifest.json" ] && \
      echo "  scoped:   $SCALED_B_DIR/${STEM}.manifest.json"
  exit 0
fi
echo "PIPELINE FAILED -- $STEM"
[ "$CODE_A" -ne 0 ] && echo "  layer A (source validation) exited $CODE_A: $STAGE_A_DIR/${STEM}.layer_a.txt"
[ "$CODE_B" -ne 0 ] && echo "  layer B (render preflight) exited $CODE_B: $STAGE_B_DIR/${STEM}.layer_b.txt"
[ -f "$STAGE_B_DIR/${STEM}.manifest.json" ] && echo "  manifest: $STAGE_B_DIR/${STEM}.manifest.json  (see \"findings\" for severities)"
echo "  A source-level failure is usually cheaper to fix first: it often clears the render result too."
exit 1