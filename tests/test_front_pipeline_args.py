#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Argument-handling tests for front_pipeline.sh (the FRONT half's shell entry).

Why a shell-level test: the bug was in the shell's own argument loop, so no
Python module can catch it.  The script is exercised for real -- no venv, no
tracer, no Inkscape -- by running a copy of it in a tmp dir whose
``.venv/bin/python`` is a stub that APPENDS the argv of every invocation to a log
and exits 0.  That makes the tracer's argv inspectable, which is the whole point:
``--sweep "<path with spaces>"`` must arrive as ONE argv element, and that is only
true when the option is passed as quoting-preserving argv (a bash array) instead
of through an unquoted ``$SWEEP_ARG`` string.

Two behaviours are pinned:
  1. a trailing ``--sweep`` with no value is a usage error (exit 2, a message
     naming the option) instead of a bare ``$2: unbound variable`` death under
     ``set -u``;
  2. a sweep path containing spaces survives to the tracer's argv intact.
"""

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONT = ROOT / "front_pipeline.sh"

STUB = (
    "#!/usr/bin/env bash\n"
    # One block per invocation: a marker line, then the argv, one element per line.
    # Append, never truncate -- the script calls its interpreter several times.
    '{\n'
    '  printf \'=== invoke\\n\'\n'
    '  for a in "$@"; do printf \'%s\\n\' "$a"; done\n'
    '} >> "$ARGV_LOG"\n'
    "exit 0\n"
)


def _stub_project(tmp_path):
    """A throwaway project the copy of front_pipeline.sh can believe in.

    Only the pieces the script touches before and around the sweep stage: an
    executable ``.venv/bin/python`` stub, a spec file, the prepped PNG that
    ``--skip-prep`` requires, and an input raster.
    """
    project = tmp_path / "proj"
    (project / "scripts").mkdir(parents=True)
    (project / "01_prepped").mkdir(parents=True)
    shutil.copy2(FRONT, project / FRONT.name)

    argv_log = tmp_path / "argv.log"
    stub = project / ".venv" / "bin" / "python"
    stub.parent.mkdir(parents=True)
    stub.write_text(STUB, encoding="utf-8")
    stub.chmod(0o755)

    (project / "spec.json").write_text("{}", encoding="utf-8")
    raster = tmp_path / "art.png"
    raster.write_bytes(b"\x89PNG\r\n\x1a\n")
    (project / "01_prepped" / "art.prepped.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return project, raster, argv_log


def _invocations(log_text):
    """Split the stub's log into one argv list per invocation."""
    calls = []
    for line in log_text.splitlines():
        if line == "=== invoke":
            calls.append([])
        elif calls:
            calls[-1].append(line)
    return calls


def _run(tmp_path, *args):
    project, raster, argv_log = _stub_project(tmp_path)
    env = dict(os.environ, ARGV_LOG=str(argv_log))
    result = subprocess.run(
        ["bash", str(project / FRONT.name), str(raster), *args],
        capture_output=True, text=True, env=env, timeout=120)
    calls = _invocations(argv_log.read_text()) if argv_log.is_file() else []
    return result, calls


def test_trailing_sweep_reports_usage_not_unbound_variable(tmp_path):
    """A bare trailing --sweep is a usage error, never a set -u crash."""
    result, _ = _run(tmp_path, "--skip-prep", "--sweep")

    assert result.returncode == 2, (result.returncode, result.stderr)
    assert "--sweep needs a path argument" in result.stderr
    assert "unbound variable" not in result.stderr
    assert "unbound variable" not in result.stdout
    # The usage block was printed -- so the operator is told how to call it.
    assert "Usage:" in result.stdout


def test_sweep_path_with_spaces_arrives_as_one_argv_element(tmp_path):
    """The sweep path is one argv element, spaces and all (no word splitting)."""
    sweeps = tmp_path / "sweep dir with spaces" / "sweep.json"
    sweeps.parent.mkdir(parents=True)
    sweeps.write_text('{"axes": {}}', encoding="utf-8")

    result, calls = _run(tmp_path, "--skip-prep", "--sweep", str(sweeps))

    trace_calls = [argv for argv in calls
                   if any(a.endswith("trace_sweep.py") for a in argv)]
    assert len(trace_calls) == 1, calls
    argv = trace_calls[0]

    assert "--sweep" in argv, argv
    assert argv[argv.index("--sweep") + 1] == str(sweeps), argv
    assert argv.count(str(sweeps)) == 1, argv
    # The splitting bug showed up as path fragments becoming their own arguments.
    assert "dir" not in argv
    assert "with" not in argv
    assert "sweep.json" not in argv
    # The run stops after compare (the stub produces no comparison report).
    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    assert "compare produced no report" in result.stderr


def test_sweep_without_a_value_is_rejected_before_any_stage_runs(tmp_path):
    """Rejecting a valueless --sweep must not run prep or the tracer first."""
    result, calls = _run(tmp_path, "--skip-prep", "--sweep")
    assert all(not any(a.endswith("trace_sweep.py") for a in argv)
               for argv in calls), calls
    # The disk-capacity gate runs before the output dirs are created; nothing
    # after it may have produced artefacts for a rejected invocation.
    assert all(not any(a.endswith("prep_raster.py") for a in argv)
               for argv in calls), calls
