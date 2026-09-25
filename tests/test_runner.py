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
