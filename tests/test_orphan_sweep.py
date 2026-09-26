"""Tests for scripts/orphan_sweep.py.

Synthetic fixtures only (in-test tmp trees), per the repo rule that no test may
depend on the real 00_source/ batch or the live box's output dirs.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import orphan_sweep  # noqa: E402


def age(path: Path, hours: float) -> None:
    """Backdate a path (and its tree) so the grace period lets it through."""
    when = time.time() - hours * 3600.0
    for item in [path, *path.rglob("*")]:
        os.utime(item, (when, when))


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "01_prepped").mkdir(parents=True)
    (root / "02_traced").mkdir()
    (root / "04_validated").mkdir()
    (root / "05_final").mkdir()
    return root


def make_tree(repo: Path, stem: str, *, hours_old: float = 48.0) -> None:
    prepped = repo / "01_prepped" / f"{stem}.prepped.png"
    prepped.write_text("raster")
    prep_json = repo / "01_prepped" / f"{stem}.prep.json"
    prep_json.write_text("{}")
    traced = repo / "02_traced" / stem
    traced.mkdir()
    (traced / "candidate_01.svg").write_text("<svg/>")
    validated = repo / "04_validated" / f"{stem}.work"
    validated.mkdir()
    (validated / "candidate_01.proof.png").write_text("png")
    comparison = repo / "04_validated" / f"{stem}.comparison.json"
    comparison.write_text("{}")
    for item in (prepped, prep_json, traced, validated, comparison):
        age(item, hours_old)


def live_file(tmp_path: Path, stems: list[str]) -> Path:
    path = tmp_path / "live.txt"
    path.write_text("\n".join(stems) + "\n")
    return path


def run(argv: list[str]) -> int:
    return orphan_sweep.main(argv)


def test_orphan_stem_is_removed_and_live_stem_kept(repo: Path, tmp_path: Path) -> None:
    make_tree(repo, "upload_aaaa11112222")
    make_tree(repo, "upload_bbbb33334444")
    live = live_file(tmp_path, ["upload_bbbb33334444"])

    assert run(["--live-stems-file", str(live), "--root", str(repo), "--apply"]) == 0

    assert not (repo / "01_prepped" / "upload_aaaa11112222.prepped.png").exists()
    assert not (repo / "02_traced" / "upload_aaaa11112222").exists()
    assert not (repo / "04_validated" / "upload_aaaa11112222.work").exists()
    assert not (repo / "04_validated" / "upload_aaaa11112222.comparison.json").exists()
    # The live job's column is untouched.
    assert (repo / "01_prepped" / "upload_bbbb33334444.prepped.png").exists()
    assert (repo / "02_traced" / "upload_bbbb33334444" / "candidate_01.svg").exists()
    assert (repo / "04_validated" / "upload_bbbb33334444.work").exists()


def test_dry_run_deletes_nothing(repo: Path, tmp_path: Path) -> None:
    make_tree(repo, "upload_aaaa11112222")
    live = live_file(tmp_path, ["upload_live00000000"])

    assert run(["--live-stems-file", str(live), "--root", str(repo)]) == 0

    assert (repo / "01_prepped" / "upload_aaaa11112222.prepped.png").exists()
    assert (repo / "02_traced" / "upload_aaaa11112222").exists()


def test_empty_live_set_refuses(repo: Path, tmp_path: Path) -> None:
    make_tree(repo, "upload_aaaa11112222")
    empty = tmp_path / "live.txt"
    empty.write_text("")

    assert run(["--live-stems-file", str(empty), "--root", str(repo), "--apply"]) == 2
    assert (repo / "02_traced" / "upload_aaaa11112222").exists()


def test_missing_live_file_refuses(repo: Path, tmp_path: Path) -> None:
    make_tree(repo, "upload_aaaa11112222")
    missing = tmp_path / "nope.txt"

    assert run(["--live-stems-file", str(missing), "--root", str(repo), "--apply"]) == 2
    assert (repo / "02_traced" / "upload_aaaa11112222").exists()


def test_prefix_collision_is_not_a_match(repo: Path, tmp_path: Path) -> None:
    """Stem `ab` must not match a live `ab-long` entry, and vice versa."""
    make_tree(repo, "upload_aaaa")
    make_tree(repo, "upload_aaaa1111")
    live = live_file(tmp_path, ["upload_aaaa1111"])

    assert run(["--live-stems-file", str(live), "--root", str(repo), "--apply"]) == 0

    assert not (repo / "02_traced" / "upload_aaaa").exists()
    assert (repo / "02_traced" / "upload_aaaa1111").exists()


def test_grace_period_keeps_fresh_artifacts(repo: Path, tmp_path: Path) -> None:
    make_tree(repo, "upload_fresh0000000", hours_old=0.5)
    live = live_file(tmp_path, ["upload_live00000000"])

    assert run(["--live-stems-file", str(live), "--root", str(repo),
                "--grace-hours", "24", "--apply"]) == 0

    assert (repo / "02_traced" / "upload_fresh0000000").exists()

    # Inside the window it survives; widening nothing and waiting is not the
    # test's job -- backdate it and the same run takes it.
    age(repo / "02_traced" / "upload_fresh0000000", 30)
    age(repo / "01_prepped" / "upload_fresh0000000.prepped.png", 30)
    age(repo / "04_validated" / "upload_fresh0000000.work", 30)
    assert run(["--live-stems-file", str(live), "--root", str(repo),
                "--grace-hours", "24", "--apply"]) == 0
    assert not (repo / "02_traced" / "upload_fresh0000000").exists()


def test_default_keep_protects_reference_output(repo: Path, tmp_path: Path) -> None:
    make_tree(repo, "00-example")
    live = live_file(tmp_path, ["upload_live00000000"])

    assert run(["--live-stems-file", str(live), "--root", str(repo), "--apply"]) == 0

    assert (repo / "01_prepped" / "00-example.prepped.png").exists()
    assert (repo / "02_traced" / "00-example").exists()
    assert (repo / "04_validated" / "00-example.work").exists()


def test_first_dot_split_keeps_inverse_twin_with_its_stem(repo: Path, tmp_path: Path) -> None:
    """`<stem>.prepped.inverse.png` belongs to <stem>, not to `<stem>.prepped`."""
    assert orphan_sweep.entry_stem("upload_x.prepped.inverse.png") == "upload_x"
    make_tree(repo, "upload_orphan000000")
    live = live_file(tmp_path, ["upload_orphan000000"])
    twin = repo / "01_prepped" / "upload_orphan000000.prepped.inverse.png"
    twin.write_text("twinned")
    age(twin, 48)

    assert run(["--live-stems-file", str(live), "--root", str(repo), "--apply"]) == 0
    assert twin.exists()


def test_uploads_dir_sweeps_only_orphans(repo: Path, tmp_path: Path) -> None:
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    orphan = uploads / "upload_orphan000000.png"
    orphan.write_text("orphan")
    twin = uploads / "upload_orphan000000-inverse.png"  # a DIFFERENT stem, also orphan
    twin.write_text("orphan twin")
    live_one = uploads / "upload_live00000000.jpg"
    live_one.write_text("live")
    fresh = uploads / "upload_fresh0000000.png"
    fresh.write_text("just uploaded")
    for item in (orphan, twin, live_one):
        age(item, 48)
    live = live_file(tmp_path, ["upload_live00000000"])

    assert run(["--live-stems-file", str(live), "--uploads-dir", str(uploads), "--apply"]) == 0

    assert not orphan.exists()
    assert not twin.exists()
    assert live_one.exists()
    assert fresh.exists(), "an upload newer than the grace window must survive"


def test_symlink_is_never_followed_or_deleted(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    precious = outside / "precious.txt"
    precious.write_text("do not touch")
    link = repo / "02_traced" / "upload_symlink00000"
    link.symlink_to(outside)
    os.utime(outside, (time.time() - 48 * 3600, time.time() - 48 * 3600))
    live = live_file(tmp_path, ["upload_live00000000"])

    assert run(["--live-stems-file", str(live), "--root", str(repo), "--apply"]) == 0

    assert precious.exists()
    assert link.is_symlink()


def test_max_delete_valve_aborts_everything(repo: Path, tmp_path: Path) -> None:
    for index in range(3):
        make_tree(repo, f"upload_orphan{index:06d}")
    live = live_file(tmp_path, ["upload_live00000000"])

    assert run(["--live-stems-file", str(live), "--root", str(repo),
                "--max-delete", "2", "--apply"]) == 2

    assert (repo / "02_traced" / "upload_orphan000000").exists()
    assert (repo / "02_traced" / "upload_orphan000001").exists()


def test_candidate_keyed_files_are_never_swept(repo: Path, tmp_path: Path) -> None:
    """`04_validated/candidate_NN.layer_a.txt` is written by pipeline.sh under the
    CANDIDATE's stem. It is live data for a job in flight and must survive, even
    though no live job's input stem is `candidate_01` (caught by a dry run on the
    live box, hence this test).
    """
    make_tree(repo, "upload_orphan000000")
    layer_a = repo / "04_validated" / "candidate_01.layer_a.txt"
    layer_a.write_text("layer A report")
    age(layer_a, 48)
    unknown_prep = repo / "01_prepped" / "upload_orphan000000.something-else.png"
    unknown_prep.write_text("not a pipeline output name")
    age(unknown_prep, 48)
    live = live_file(tmp_path, ["upload_live00000000"])
    report_path = tmp_path / "report.json"

    assert run(["--live-stems-file", str(live), "--root", str(repo),
                "--apply", "--json-out", str(report_path)]) == 0

    assert layer_a.exists(), "a candidate-keyed layer A report was swept"
    assert unknown_prep.exists(), "an unrecognised prep-column name was swept"
    report = json.loads(report_path.read_text())
    assert report["not_stem_keyed"] == 2
    assert not any("candidate_01" in target["path"] for target in report["targets"])


def test_json_report_records_plan_and_outcome(repo: Path, tmp_path: Path) -> None:
    make_tree(repo, "upload_orphan000000")
    live = live_file(tmp_path, ["upload_live00000000"])
    report_path = tmp_path / "report.json"

    assert run(["--live-stems-file", str(live), "--root", str(repo),
                "--apply", "--json-out", str(report_path)]) == 0

    report = json.loads(report_path.read_text())
    assert report["mode"] == "apply"
    assert report["live_stems"] == 1
    assert report["deleted_entries"] == report["planned_entries"] > 0
    assert report["failures"] == []
    assert all(Path(target["path"]).exists() is False for target in report["targets"])


def test_runner_root_keeps_dirs_the_runner_still_knows(repo: Path, tmp_path: Path) -> None:
    """The runner's own index wins: an unknown dir is swept, a known one is kept."""
    runner_root = tmp_path / "jobs"
    known = runner_root / "known00000000"
    unknown = runner_root / "forgotten0000"
    for directory in (known, unknown):
        directory.mkdir(parents=True)
        (directory / "comparison.json").write_text("{}")
        age(directory, 48)
    live = live_file(tmp_path, ["upload_live00000000"])

    seen: list[str] = []
    original = orphan_sweep.runner_forgot

    def fake_forgot(url: str, job_id: str) -> bool:
        seen.append(job_id)
        return job_id == "forgotten0000"

    orphan_sweep.runner_forgot = fake_forgot
    try:
        code = run(["--live-stems-file", str(live), "--runner-root", str(runner_root),
                    "--runner-url", "http://127.0.0.1:1", "--apply"])
    finally:
        orphan_sweep.runner_forgot = original

    assert code == 0
    assert sorted(seen) == ["forgotten0000", "known00000000"]
    assert known.exists()
    assert not unknown.exists()


def test_runner_forgot_is_false_when_unreachable() -> None:
    """An unreachable runner must never read as 'forgot it' -- that deletes live jobs."""
    assert orphan_sweep.runner_forgot("http://127.0.0.1:1", "whatever") is False
