import importlib.util
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
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


# ------------------------------------------------------------ recolour seam --
#
# The studio's "cycle colours" preview recolours a candidate SVG without a
# raster round-trip. These pin the pure functions the /recolour routes delegate
# to: the map validator refuses bad input, the colour lister names the distinct
# paints, and the rewrite changes paint only (geometry, nodes, dimensions stay).

def _min_svg():
    # Two red shapes (so #c1272d is the most-declared colour) + one green.
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        '<rect x="0" y="0" width="4" height="4" fill="#c1272d"/>'
        '<rect x="5" y="5" width="4" height="4" fill="#c1272d"/>'
        '<circle cx="2" cy="2" r="1" fill="#3a5a32"/>'
        "</svg>"
    )


def test_recolour_map_refuses_a_non_hex_value():
    try:
        runner.recolour_map({"#c1272d": "not-a-colour"})
    except ValueError:
        pass
    else:
        raise AssertionError("a non-#rrggbb target must be refused, not dropped")


def test_recolour_map_refuses_an_empty_map():
    try:
        runner.recolour_map({})
    except ValueError:
        pass
    else:
        raise AssertionError("an empty map is a client error, not a silent no-op")


def test_recolour_map_accepts_hex_pairs():
    assert runner.recolour_map({"#c1272d": "#5b2c6f"}) == {"#c1272d": "#5b2c6f"}


def test_svg_colours_lists_distinct_paints_most_used_first(tmp_path):
    svg = tmp_path / "art.svg"
    svg.write_text(_min_svg(), encoding="utf-8")
    assert runner.svg_colours(svg) == ["#c1272d", "#3a5a32"]


def test_recolour_rewrites_paint_only(tmp_path):
    svg = tmp_path / "art.svg"
    svg.write_text(_min_svg(), encoding="utf-8")
    out = runner.recolour_svg(svg, {"#c1272d": "#5b2c6f"}).decode("utf-8")
    assert "#5b2c6f" in out
    assert "#c1272d" not in out, "the mapped source colour must be gone"
    assert "#3a5a32" in out, "an unmapped colour must be left alone"
    assert 'width="10"' in out, "geometry/dimensions must be untouched"


# --------------------------------------------------------------- compose ---
#
# POST /compose is the COMPOSITION operation: the studio hands over the whole
# layer stack inline and gets one merged SVG back with the vectors intact. These
# pin the route's two jobs -- refusing a spec it cannot merge with the
# invalid_spec envelope the studio switches on, and answering image/svg+xml
# rather than JSON -- plus the fact that layer content is inline only, so a
# compose request cannot read the runner's disk.

def _compose_body(spec):
    return {"op": "compose", "spec": spec}


def _inline_layer(width="300", height="200", x=0, y=0, w=300, h=200):
    return {
        "type": "svg",
        "src": ('<svg xmlns="http://www.w3.org/2000/svg" width="%s" height="%s">'
                '<rect width="10" height="10" fill="#111111"/></svg>'
                % (width, height)),
        "x": x, "y": y, "w": w, "h": h, "opacity": 1.0, "hue": 0,
    }


def _post_json(path, payload):
    """POST to a private Handler instance; returns (status, headers, body)."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), runner.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (server.server_address[1], path),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()
    finally:
        server.shutdown()
        server.server_close()


def test_compose_problem_names_the_missing_piece():
    assert "width" in runner.compose_problem({"height": 10, "layers": []})
    assert "height" in runner.compose_problem({"width": 10, "layers": []})
    assert "layers" in runner.compose_problem({"width": 10, "height": 10})
    assert "list" in runner.compose_problem(
        {"width": 10, "height": 10, "layers": {}})
    assert runner.compose_problem("not a spec") == "compose needs a spec object"
    assert runner.compose_problem(
        {"width": 10, "height": 10, "layers": []}) is None


def test_compose_route_returns_one_merged_svg_inline(monkeypatch):
    monkeypatch.setattr(runner, "TOKEN", "")
    status, headers, body = _post_json("/compose", _compose_body({
        "width": 600, "height": 400, "background": "#ffffff",
        "layers": [_inline_layer(x=10, y=20, w=300, h=200),
                   _inline_layer("100", "100", x=0, y=0, w=100, h=100)],
    }))
    assert status == 200
    assert headers.get("Content-Type") == "image/svg+xml"
    text = body.decode("utf-8")
    # The merged document, with each vector layer as a transformed group --
    # proof it did not rasterise and re-trace.
    assert 'viewBox="0 0 600 400"' in text
    assert 'transform="translate(10,20) scale(1,1)"' in text
    assert text.count("<g ") == 2


def test_compose_route_refuses_a_spec_with_no_canvas(monkeypatch):
    monkeypatch.setattr(runner, "TOKEN", "")
    status, _headers, body = _post_json(
        "/compose", _compose_body({"layers": [_inline_layer()]}))
    assert status == 422
    payload = json.loads(body)
    assert payload["kind"] == "invalid_spec"
    assert "width" in payload["error"]


def test_compose_route_refuses_an_unmergeable_layer(monkeypatch):
    """A layer the merge cannot size is a client error, not a 500."""
    monkeypatch.setattr(runner, "TOKEN", "")
    layer = _inline_layer()
    layer["src"] = '<svg xmlns="http://www.w3.org/2000/svg"/>'   # no size
    status, _headers, body = _post_json("/compose", _compose_body({
        "width": 100, "height": 100, "layers": [layer],
    }))
    assert status == 422
    assert json.loads(body)["kind"] == "invalid_spec"


def test_compose_route_refuses_a_raster_layer_that_is_a_path(monkeypatch):
    """Layer content is inline: the route must not read the runner's disk."""
    monkeypatch.setattr(runner, "TOKEN", "")
    status, _headers, body = _post_json("/compose", _compose_body({
        "width": 100, "height": 100,
        "layers": [{"type": "raster", "src": "/etc/hostname", "x": 0, "y": 0,
                    "w": 10, "h": 10}],
    }))
    assert status == 422
    assert json.loads(body)["kind"] == "invalid_spec"
    assert b"data:" in body
