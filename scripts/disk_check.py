#!/usr/bin/env python3
"""Disk-capacity gate for the print pipeline.

Runs early in front_pipeline.sh and pipeline.sh, and again before the runner
builds a 7z archive. A pipeline needs room for prepped PNGs, a full candidate
sweep, rendered preflight layers, proofs, PDFs and manifests; a disk that fills
mid-run fails the job halfway through, which is worse than refusing up front.

Exit codes:
  0  enough space (a warning goes to stderr when free < 2 GiB)
  2  free < 1 GiB -- refuse to run the pipeline or build an archive

Usage:
  disk_check.py [path]     # measure the filesystem holding `path` (cwd default)
"""
import shutil
import sys

WARN_BYTES = 2 * 1024 ** 3  # 2 GiB -- warn only
MIN_BYTES = 1 * 1024 ** 3   # 1 GiB -- refuse


def human(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024.0
    return f"{value:.1f} TiB"


def check(path: str) -> str | None:
    """Return an error message when the run must be refused, else None.

    Warnings are printed to stderr directly; the caller only needs to act on a
    non-None return (refusal)."""
    usage = shutil.disk_usage(path)
    free = usage.free
    if free < MIN_BYTES:
        return (f"disk check: FAILED -- only {human(free)} free on {path!r}; "
                f"need at least {human(MIN_BYTES)} to run the pipeline or archive")
    if free < WARN_BYTES:
        print(f"disk check: WARNING -- {human(free)} free on {path!r} "
              f"(below {human(WARN_BYTES)}); the run may fill the disk",
              file=sys.stderr)
    return None


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "."
    err = check(path)
    if err is not None:
        print(err, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
