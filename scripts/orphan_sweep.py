#!/usr/bin/env python3
"""Orphan sweep: delete pipeline artifacts left behind by jobs that no longer exist.

The studio's job delete removes the runner's job directory only. Everything the
pipeline wrote into the shared output dirs (`01_prepped/`, `02_traced/`,
`04_validated/`) is keyed by the INPUT STEM, not by the job id, so it survives
the delete -- as does the studio's own copy of the upload. Measured on the live
box: 5 orphan stems held ~215 MiB of traces/validation work plus 14 orphan
uploads (~25 MiB).

This script is deliberately HOST-AGNOSTIC (this repo is public): it never asks
the database anything. The caller supplies the authoritative list of live stems
-- one per line, or a JSON list -- and the wrapper that knows where this box
keeps its Rails app produces it. See `references/orphan-sweep.md` in the skill
or the host-local wrapper for the Rails one-liner.

Safety properties, in order of importance:

* **Refuses to run on an empty live set.** An empty list would mark every
  artifact as an orphan and delete the lot. A failed database query must abort
  the sweep, never widen it: exit 2 and delete nothing.
* **Dry-run by default.** Deletion requires an explicit `--apply`.
* **Grace period.** Anything modified within `--grace-hours` is kept, so an
  upload the operator just dropped on the new-job form is never swept.
* **Prefix collision safe.** A stem is matched as `name == stem` or
  `name.startswith(stem + ".")`, so stem `a` never matches `ab.svg`.
* **Symlinks are never followed.** A symlinked entry is skipped and reported;
  the sweep cannot be talked into deleting outside the tree it was given.
* **`--max-delete` valve.** If the plan exceeds the cap, everything is aborted
  and nothing is deleted.

Exit codes: 0 success, 2 guard refusal (bad or empty live set), 3 error.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Columns of the repo's shared output dirs. Entries are keyed by the job INPUT
# stem, but NOT every file in those dirs is: `04_validated/candidate_01.layer_a.txt`
# is written by pipeline.sh under the CANDIDATE's stem, which has no relationship
# to the upload stem. A bare `<stem>.` prefix match therefore classified live
# layer-A reports as orphans (caught by a dry run against the live box). Each
# column is matched against an explicit whitelist of the names the pipeline
# actually writes; anything else is reported as not-stem-keyed and left alone.
STEM_DIRS = ("01_prepped", "02_traced", "04_validated")

ALLOWED_SUFFIXES = {
    "01_prepped": ("prep.json", "prepped.png", "prepped.inverse.png"),
    "04_validated": ("work", "comparison.json", "comparison.md"),
}

# Never deleted, and not because of a live job: these are tracked in git as the
# repo's reference output, so removing them would dirty the working tree.
DEFAULT_KEEP = ("00-example",)


def load_live_stems(path: Path) -> list[str]:
    """Read the authoritative live-stem list. JSON list or one stem per line.

    Returns [] for an unreadable/newline-free file; the caller treats that as a
    refusal, never as "nothing is live".
    """
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return []
    if not raw:
        return []
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        return [str(item).strip() for item in parsed if str(item).strip()]
    stems = []
    for line in raw.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            stems.append(line)
    return stems


def entry_stem(name: str) -> str:
    """The pipeline's stem for an artifact name.

    First-dot split, because prep writes `upload_x.prepped.inverse.png` and the
    stem is `upload_x`, not `upload_x.prepped.inverse`.
    """
    return name.split(".", 1)[0]


def matches_stem(name: str, stem: str) -> bool:
    return name == stem or name.startswith(stem + ".")


def dir_size(path: Path) -> int:
    """Bytes held by a tree, without following symlinks."""
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                else:
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total


def newest_mtime(path: Path) -> float:
    """Newest mtime in the tree rooted at path (the path itself for a file)."""
    try:
        if path.is_symlink():
            return path.lstat().st_mtime
        if not path.is_dir():
            return path.stat().st_mtime
    except OSError:
        return 0.0
    newest = 0.0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                else:
                    newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
            except OSError:
                continue
    try:
        newest = max(newest, path.stat().st_mtime)
    except OSError:
        pass
    return newest


class Plan:
    """Collects candidates, then applies them only if the whole plan is safe."""

    def __init__(self, apply: bool, grace_hours: float, max_delete: int, now: float):
        self.apply = apply
        self.grace_seconds = grace_hours * 3600.0
        self.max_delete = max_delete
        self.now = now
        self.targets: list[dict] = []
        self.skipped_protected: list[str] = []
        self.skipped_grace: list[str] = []
        self.skipped_symlink: list[str] = []
        self.skipped_unknown: list[str] = []
        self.errors: list[str] = []

    def consider(self, path: Path, kind: str, reason: str = "") -> None:
        """Offer one candidate path for deletion."""
        try:
            if path.is_symlink():
                self.skipped_symlink.append(str(path))
                return
        except OSError:
            self.errors.append(f"cannot stat {path}")
            return
        age = self.now - newest_mtime(path)
        if age < self.grace_seconds:
            self.skipped_grace.append(f"{path} (modified {age / 3600.0:.1f}h ago)")
            return
        size = dir_size(path) if path.is_dir() else _file_size(path)
        self.targets.append({"path": str(path), "kind": kind, "bytes": size, "reason": reason})

    def apply_all(self) -> tuple[int, list[str]]:
        """Delete everything planned. Returns (entries deleted, failures)."""
        deleted = 0
        failures: list[str] = []
        for target in self.targets:
            path = Path(target["path"])
            try:
                if path.is_symlink():
                    value = path.lstat().st_size
                    path.unlink()
                    target["bytes"] = value
                elif path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                deleted += 1
            except FileNotFoundError:
                target["bytes"] = 0
            except OSError as exc:
                failures.append(f"{path}: {exc}")
        return deleted, failures


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def plan_repo_root(plan: Plan, root: Path, protected: set[str]) -> None:
    for sub in STEM_DIRS:
        column = root / sub
        if not column.is_dir():
            continue
        try:
            entries = list(os.scandir(column))
        except OSError as exc:
            plan.errors.append(f"cannot list {column}: {exc}")
            continue
        for entry in entries:
            stem = stem_of_entry(sub, entry)
            if stem is None:
                plan.skipped_unknown.append(
                    f"{entry.path} (not a stem-keyed {sub} output; left alone)")
                continue
            if stem in protected:
                plan.skipped_protected.append(entry.path)
                continue
            plan.consider(Path(entry.path), f"{sub}/stem", f"orphan stem {stem}")


def stem_of_entry(column: str, entry: os.DirEntry) -> str | None:
    """The input stem an output dir entry belongs to, or None if it is not
    stem-keyed at all.

    * `02_traced/<stem>/` -- directories with a dotless name (candidate files
      live INSIDE those dirs).
    * `01_prepped/<stem>.<suffix>`, `04_validated/<stem>.<suffix>` -- only the
      suffixes the pipeline writes, so a candidate-keyed sibling such as
      `04_validated/candidate_01.layer_a.txt` is never mistaken for an upload
      stem and deleted.
    """
    if column == "02_traced":
        if entry.is_dir(follow_symlinks=False) and "." not in entry.name:
            return entry.name
        return None
    allowed = ALLOWED_SUFFIXES.get(column, ())
    if "." not in entry.name:
        return None
    stem, suffix = entry.name.split(".", 1)
    return stem if suffix in allowed else None


def plan_uploads(plan: Plan, uploads: Path, protected: set[str]) -> None:
    if not uploads.is_dir():
        return
    try:
        entries = list(os.scandir(uploads))
    except OSError as exc:
        plan.errors.append(f"cannot list {uploads}: {exc}")
        return
    for entry in entries:
        stem = entry_stem(entry.name)
        if stem in protected:
            plan.skipped_protected.append(entry.path)
            continue
        plan.consider(Path(entry.path), "uploads/source", f"orphan stem {stem}")


def plan_mock_root(plan: Plan, mock_root: Path, runner_url: str) -> None:
    """Runner-side dirs are keyed by JOB ID, and the runner's index is memory-only.

    Delete a dir only when the runner itself no longer knows the id: if it can
    still serve the job, the files stay. That keeps this rule independent of the
    studio's live stems and safe across a runner restart (which re-indexes every
    dir on disk).
    """
    if not mock_root.is_dir():
        return
    try:
        entries = list(os.scandir(mock_root))
    except OSError as exc:
        plan.errors.append(f"cannot list {mock_root}: {exc}")
        return
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        if not runner_forgot(runner_url, entry.name):
            plan.skipped_protected.append(entry.path)
            continue
        plan.consider(Path(entry.path), "runner/job-dir", f"runner forgot {entry.name}")


def runner_forgot(runner_url: str, job_id: str) -> bool:
    """True when the runner answers 404 for this job id (it no longer holds it)."""
    url = f"{runner_url.rstrip('/')}/jobs/{job_id}"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            response.read(1)
        return False
    except urllib.error.HTTPError as exc:
        return exc.code == 404
    except Exception:
        # Unreachable runner proves nothing: keep the files.
        return False


def human_report(plan: Plan, live_count: int, root_label: str, applied: bool) -> str:
    lines = []
    total = sum(target["bytes"] for target in plan.targets)
    lines.append(f"live stems: {live_count}  scope: {root_label}")
    lines.append(f"mode: {'APPLY' if applied else 'dry-run'}")
    lines.append(
        f"planned: {len(plan.targets)} entries / {total / 1048576.0:.1f} MiB"
        f"   protected: {len(plan.skipped_protected)}"
        f"   inside grace: {len(plan.skipped_grace)}"
        f"   not stem-keyed: {len(plan.skipped_unknown)}"
        f"   symlinks skipped: {len(plan.skipped_symlink)}"
    )
    for target in sorted(plan.targets, key=lambda item: -item["bytes"])[:20]:
        lines.append(f"  {target['bytes'] / 1048576.0:8.2f} MiB  {target['path']}  [{target['reason']}]")
    if len(plan.targets) > 20:
        lines.append(f"  ... {len(plan.targets) - 20} more")
    for note in plan.skipped_grace[:5]:
        lines.append(f"  kept (grace): {note}")
    for note in plan.skipped_unknown[:3]:
        lines.append(f"  left alone: {note}")
    for error in plan.errors:
        lines.append(f"  ERROR: {error}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Delete pipeline artifacts left behind by jobs that no longer exist.")
    parser.add_argument("--live-stems-file", required=True, type=Path,
                        help="file listing live job input stems (one per line, or a JSON list)")
    parser.add_argument("--root", type=Path,
                        help="repo root; sweeps 01_prepped/, 02_traced/, 04_validated/ inside it")
    parser.add_argument("--uploads-dir", type=Path,
                        help="studio upload dir; sweeps source copies whose stem is not live")
    parser.add_argument("--runner-root", type=Path,
                        help="a runner's job root; sweeps dirs the runner can no longer serve")
    parser.add_argument("--runner-url", default="",
                        help="base URL of the runner owning --runner-root (required with it)")
    parser.add_argument("--keep", action="append", default=[],
                        help="extra stem to protect (repeatable)")
    parser.add_argument("--grace-hours", type=float, default=24.0,
                        help="keep anything modified more recently than this (default 24)")
    parser.add_argument("--max-delete", type=int, default=2000,
                        help="abort the whole run if the plan exceeds this many entries")
    parser.add_argument("--apply", action="store_true", help="delete; without it the run is a dry-run")
    parser.add_argument("--json-out", type=Path, help="write the plan/report as JSON here")
    args = parser.parse_args(argv)

    windows = [args.root, args.uploads_dir, args.runner_root]
    if not any(windows):
        print("nothing to sweep: pass --root, --uploads-dir or --runner-root", file=sys.stderr)
        return 2
    if args.runner_root and not args.runner_url:
        print("--runner-root needs --runner-url (the runner that owns those dirs)", file=sys.stderr)
        return 2

    stems = load_live_stems(args.live_stems_file)
    if not stems:
        print(f"REFUSING: live-stem list is empty or unreadable ({args.live_stems_file}). "
              "An empty list would treat every artifact as an orphan.", file=sys.stderr)
        return 2
    protected = set(stems) | set(DEFAULT_KEEP) | set(args.keep)

    now = time.time()
    plan = Plan(apply=args.apply, grace_hours=args.grace_hours,
                max_delete=args.max_delete, now=now)
    if args.root:
        plan_repo_root(plan, args.root, protected)
    if args.uploads_dir:
        plan_uploads(plan, args.uploads_dir, protected)
    if args.runner_root:
        plan_mock_root(plan, args.runner_root, args.runner_url)

    if len(plan.targets) > args.max_delete:
        print(f"REFUSING: plan holds {len(plan.targets)} entries, over --max-delete {args.max_delete}. "
              "Nothing deleted; inspect the plan before raising the cap.", file=sys.stderr)
        return 2

    freed = sum(target["bytes"] for target in plan.targets)
    scope = ", ".join(str(path) for path in windows if path)
    print(human_report(plan, len(stems), scope, args.apply))

    deleted = 0
    failures: list[str] = []
    if args.apply:
        deleted, failures = plan.apply_all()
        print(f"deleted {deleted} entries, freed {freed / 1048576.0:.1f} MiB"
              + (f", {len(failures)} failures" if failures else ""))
        for failure in failures:
            print(f"  FAILED: {failure}", file=sys.stderr)

    if args.json_out:
        report = {
            "when": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "scope": scope,
            "mode": "apply" if args.apply else "dry-run",
            "live_stems": len(stems),
            "planned_entries": len(plan.targets),
            "planned_bytes": freed,
            "deleted_entries": deleted,
            "failures": failures,
            "protected": len(plan.skipped_protected),
            "inside_grace": len(plan.skipped_grace),
            "not_stem_keyed": len(plan.skipped_unknown),
            "symlinks_skipped": len(plan.skipped_symlink),
            "errors": plan.errors,
            "targets": plan.targets,
        }
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    return 3 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
