import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "runner.py"
spec = importlib.util.spec_from_file_location("chopshop_runner", MODULE_PATH)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_host_upload_path_maps_into_container_mount(tmp_path):
    upload = tmp_path / "public" / "uploads" / "upload_abc.png"
    upload.parent.mkdir(parents=True)
    upload.write_bytes(b"png")

    assert runner.container_input_path(
        str(upload),
        host_upload_root=str(tmp_path / "public" / "uploads"),
        container_upload_root="/data/uploads",
    ) == "/data/uploads/upload_abc.png"


# --------------------------------------------------------------- /health host

def test_health_host_comes_from_the_environment(monkeypatch):
    """The /health host label must be configuration, not a literal.

    This repo is PUBLIC, so a hardcoded machine name in a tracked file is
    published to the world. The response only needs a label for the studio's
    runner dropdown, which CHOPSHOP_RUNNER_LABEL supplies.
    """
    monkeypatch.setenv("CHOPSHOP_RUNNER_LABEL", "some-deployment")
    assert runner.health_payload()["host"] == "some-deployment"


def test_health_host_falls_back_to_the_machine_name(monkeypatch):
    """No label configured: report the host, never an empty or stale string."""
    import socket

    monkeypatch.delenv("CHOPSHOP_RUNNER_LABEL", raising=False)
    assert runner.health_payload()["host"] == socket.gethostname()


def test_no_machine_name_is_hardcoded_in_the_response(monkeypatch):
    """Guard the leak itself: a real hostname must never reach the wire.

    Catches the regression where the field was a literal, which looked fine
    locally and published the host name to anyone reading the repo. The
    forbidden names are built at runtime from this machine's own hostname
    rather than written out, so this test does not republish the very name it
    is guarding against.
    """
    import socket

    monkeypatch.setenv("CHOPSHOP_RUNNER_LABEL", "sentinel-label")
    payload = runner.health_payload()
    assert payload["host"] == "sentinel-label"

    source = MODULE_PATH.read_text().lower()
    machine = socket.gethostname().lower()
    if len(machine) > 3:          # skip trivial/placeholder hostnames
        assert machine not in source
    # No literal hostname assignment should reappear either.
    assert '"host": "' not in source




def test_oversized_input_is_downscaled_into_job_directory(tmp_path):
    from PIL import Image

    source = tmp_path / "source.png"
    Image.new("RGB", (400, 300), "white").save(source)
    destination = tmp_path / "job" / "input.png"

    result = runner.normalize_input(source, destination, max_dimension=200)

    assert result["original_pixels"] == 120_000
    assert result["width"] == 200
    assert result["height"] == 150
    assert destination.is_file()


def test_comparison_path_is_found_for_job_stem(tmp_path):
    report = tmp_path / "04_validated" / "jobby.comparison.json"
    report.parent.mkdir(parents=True)
    report.write_text('{"candidates": []}', encoding="utf-8")

    assert runner.comparison_path(tmp_path, "jobby") == report


def test_artifact_lookup_prefers_validation_output(tmp_path):
    output = tmp_path / "05_final" / "candidate_02.proof.png"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"proof")

    assert runner.artifact_path(
        tmp_path,
        stem="jobby",
        name="candidate_02.proof.png",
        job_dir=tmp_path / "jobs" / "abc",
    ) == output


# ------------------------------------------------------------- optional Jev --
#
# The annotator is a separate deliverable (chopshop-jev) mounted into the
# runner, and the key is operator-supplied. These tests pin the two things that
# actually bit: an unconfigured add-on must explain itself (never a 404 that
# reads like a missing route), and the envelope handed back to the studio must
# keep the annotator's answers verbatim rather than a re-derived copy.

def _fake_annotator(tmp_path, sidecar_body: str, *, exit_code: int = 0) -> Path:
    """A stand-in annotator that writes a sidecar to --output."""
    script = tmp_path / "jev_annotate.py"
    script.write_text(
        "import json, sys\n"
        "def arg(name):\n"
        "    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else None\n"
        f"code = {exit_code}\n"
        "out = arg('--output')\n"
        f"body = {sidecar_body!r}\n"
        "if out and code == 0:\n"
        "    open(out, 'w', encoding='utf-8').write(body)\n"
        "if code:\n"
        "    sys.stderr.write('boom: annotator exploded')\n"
        "sys.exit(code)\n",
        encoding="utf-8",
    )
    return script


def test_jev_reason_names_the_first_missing_piece(monkeypatch):
    monkeypatch.delenv("JEV_ANNOTATOR", raising=False)
    assert "JEV_ANNOTATOR" in runner.jev_unavailable_reason()

    monkeypatch.setenv("JEV_ANNOTATOR", "/nonexistent/jev_annotate.py")
    assert "not found" in runner.jev_unavailable_reason()

    # The script resolves, so the next missing piece is the key — presented
    # first, because that is the one the operator can actually fix.
    monkeypatch.setenv("JEV_ANNOTATOR", str(MODULE_PATH))
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert "JEV_API_KEY" in runner.jev_unavailable_reason()

    monkeypatch.setenv("JEV_API_KEY", "test-key")
    assert runner.jev_unavailable_reason() is None


def test_health_reports_jev_readiness(monkeypatch):
    monkeypatch.setenv("CHOPSHOP_RUNNER_LABEL", "some-deployment")
    monkeypatch.setenv("JEV_ANNOTATOR", str(MODULE_PATH))
    monkeypatch.setenv("JEV_API_KEY", "test-key")
    assert runner.health_payload()["tools"]["jev"] is True

    monkeypatch.delenv("JEV_API_KEY", raising=False)
    assert runner.health_payload()["tools"]["jev"] is False


def test_run_jev_passes_the_annotators_answers_through(tmp_path, monkeypatch):
    sidecar = (
        '{"schema": "chopshop-jev-annotation-0.4", "model": "jev-1.13.0",'
        ' "status": "ok",'
        ' "answers": {"failure_mode": {"type": "choice", "choice": "screen_explosion",'
        ' "confidence": 0.78}, "tonal_structure": {"type": "score", "score": 0.35,'
        ' "probabilities": {"0": 0.76, "3": 0.03}}},'
        ' "verdict": {"headline": "screen_explosion"}}'
    )
    script = _fake_annotator(tmp_path, sidecar)
    manifest = tmp_path / "candidate_11.manifest.json"
    manifest.write_text("{}", encoding="utf-8")

    monkeypatch.setenv("JEV_ANNOTATOR", str(script))
    monkeypatch.setenv("JEV_API_KEY", "test-key")
    monkeypatch.delenv("JEV_ENDPOINT", raising=False)

    result = runner.run_jev(manifest)

    assert result["model"] == "jev-1.13.0"
    assert result["endpoint"] == runner.JEV_DEFAULT_ENDPOINT
    assert result["answers"]["failure_mode"]["choice"] == "screen_explosion"
    assert result["answers"]["tonal_structure"]["score"] == 0.35
    assert result["verdict"] == {"headline": "screen_explosion"}
    assert isinstance(result["latency_ms"], int)


def test_run_jev_surfaces_an_annotator_failure_as_the_reason(tmp_path, monkeypatch):
    script = _fake_annotator(tmp_path, "{}", exit_code=3)
    manifest = tmp_path / "m.manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("JEV_ANNOTATOR", str(script))
    monkeypatch.setenv("JEV_API_KEY", "test-key")

    try:
        runner.run_jev(manifest)
    except runner.JevUnavailable as exc:
        assert "boom: annotator exploded" in str(exc)
    else:
        raise AssertionError("a failed annotator must not return an envelope")


def test_run_jev_reports_a_skipped_annotator_with_its_reason(tmp_path, monkeypatch):
    """--optional style skip (no key on the other side) must not read as success."""
    sidecar = '{"status": "skipped", "reason": "JEV_API_KEY is not set", "answers": {}}'
    script = _fake_annotator(tmp_path, sidecar)
    manifest = tmp_path / "m.manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("JEV_ANNOTATOR", str(script))
    monkeypatch.setenv("JEV_API_KEY", "test-key")

    try:
        runner.run_jev(manifest)
    except runner.JevUnavailable as exc:
        assert "JEV_API_KEY is not set" in str(exc)
    else:
        raise AssertionError("a skipped annotation must not return an envelope")
