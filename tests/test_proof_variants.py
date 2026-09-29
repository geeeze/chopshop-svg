#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_proof_variants.py -- pytest suite for scripts/proof_variants.py.

The opt-in dataset stage: given the chosen proof's raster and the selected
candidate SVG it derives a deterministic family of similar copies (mirrored /
rotated / colour-shifted), emitting BOTH halves of each and packaging them with
a manifest and a contact sheet.  These tests pin the promises that make the
output usable as a training set:

1. **The vocabulary is fixed, ordered and closed.**  An unknown name is refused
   with the vocabulary quoted, never skipped.
2. **Determinism.**  Every transform is a pure function of the input bytes, so
   two runs write byte-identical files -- including the reports.
3. **The two halves stay in their lane.**  A colour transform leaves path data,
   node count and geometry untouched; a spatial transform leaves the colour set
   untouched.  Both directions are asserted, because a transform that quietly
   does both would make every downstream metric meaningless.
4. **It never picks a winner and never edits a source.**  Every requested
   transform that applies is emitted and the inputs are read only.
5. **A transform that cannot apply to THIS artwork costs only itself.**  It is
   SKIPPED, NAMED in the notes, and emits no file -- while `count` and
   `transforms` still describe exactly what is on disk.  A real fault (a broken
   input, a malformed document) still discards the whole set atomically.  The
   fixtures for this are single-colour on purpose: no multi-colour fixture can
   reach the path that made single-ink line art produce no dataset at all.

Fixtures are synthetic and built in ``tmp_path`` -- nothing here touches the
real ``00_source/`` batch or the repo's output directories.

Run with:  python3 -m pytest tests/test_proof_variants.py -v
"""

import hashlib
import io
import json
import os
import re
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
for _path in (ROOT, SCRIPTS):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import proof_variants as pv  # noqa: E402

from PIL import Image  # noqa: E402
from lxml import etree  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures -- synthetic, asymmetric, flat-coloured
# --------------------------------------------------------------------------

# Asymmetric on purpose: a symmetric fixture would make transpose and rot180
# look like the same operation and would let a sign error through.
PROOF_COLOURS = {
    (0, 0): (193, 68, 14),        # #c1440e, top-left block
    (30, 30): (106, 138, 63),     # #6a8a3f, bottom-right block
    (22, 5): (63, 124, 124),      # #3f7c7c, top-right bar
}

CANDIDATE_SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="40mm" height="40mm"
     viewBox="0 0 40 40">
  <rect id="ground" width="40" height="40" fill="#ffffff"/>
  <path id="motif" d="M2,2 L12,6 L4,18 Z" fill="#c1440e"/>
  <circle id="bud" cx="32" cy="32" r="5" fill="#6a8a3f"/>
  <path id="stem" d="M20,20 L36,24" stroke="#3f7c7c" stroke-width="2" fill="none"/>
</svg>
"""

SCREENS = ["#c1440e", "#6a8a3f", "#3f7c7c"]


def make_proof_bytes(size=(40, 40)):
    """A flat, asymmetric proof raster: one block per corner, nothing centred."""
    image = Image.new("RGB", size, "#ffffff")
    pixels = image.load()
    width, height = size
    for (x, y), colour in PROOF_COLOURS.items():
        if x < width and y < height:
            pixels[x, y] = colour
    for y in range(height):
        for x in range(width):
            if x < width // 4 and y < height // 8:
                pixels[x, y] = PROOF_COLOURS[(0, 0)]
            elif x >= width * 3 // 4 and y >= height * 3 // 4:
                pixels[x, y] = PROOF_COLOURS[(30, 30)]
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def write_proof(path, size=(40, 40)):
    path.write_bytes(make_proof_bytes(size))
    return path


def _png_bytes(image):
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def write_svg(path, text=CANDIDATE_SVG):
    path.write_text(text, encoding="utf-8")
    return path


def manifest_payload():
    return {
        "tool": "preflight.py (v4.0 two-layer pipeline)",
        "generated": "2026-01-01T00:00:00+00:00",
        "passed": True,
        "substrate": {"colour": "#FFFFFF",
                      "palette": ["#FFFFFF"] + SCREENS,
                      "screens": list(SCREENS)},
        "stats": {"dpi": 300},
        "notes": [],
    }


@pytest.fixture
def package(tmp_path):
    """(proof path, svg path, manifest path) for one synthetic proof."""
    proof = write_proof(tmp_path / "candidate_02.proof.png")
    svg = write_svg(tmp_path / "candidate_02.svg")
    manifest = tmp_path / "candidate_02.manifest.json"
    manifest.write_text(json.dumps(manifest_payload()), encoding="utf-8")
    return proof, svg, manifest


# --------------------------------------------------------------------------
# Reading the results
# --------------------------------------------------------------------------

def raster_size(data):
    with Image.open(io.BytesIO(data)) as image:
        return image.size


def raster_bytes_with(pixel):
    image = Image.new("RGB", (2, 2), pixel)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def path_data(text):
    """Every ``d`` attribute, order preserved -- the geometry fingerprint."""
    return re.findall(r'\sd="([^"]*)"', text)


def attr(text, name):
    """Every ``name="..."`` attribute value, order preserved."""
    return re.findall(r'\s%s="([^"]*)"' % name, text)


def colours_of(text):
    """The set of declared hex colours (lowercased)."""
    return sorted({value.lower() for value in re.findall(r"#[0-9a-fA-F]{6}", text)})


def node_count(text):
    root = etree.fromstring(text.encode("utf-8"))
    return sum(1 for _ in root.iter())


def frame_of(text):
    root = etree.fromstring(text.encode("utf-8"))
    return {"width": root.get("width"), "height": root.get("height"),
            "viewBox": root.get("viewBox")}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def tree_hashes(root):
    hashes = {}
    for base, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(base, name)
            with open(path, "rb") as handle:
                hashes[os.path.relpath(path, root)] = sha(handle.read())
    return hashes


# --------------------------------------------------------------------------
# The vocabulary and the plan
# --------------------------------------------------------------------------

def test_the_vocabulary_is_fixed_ordered_and_closed():
    assert pv.TRANSFORMS == (
        "flip-h", "flip-v", "rot180", "rot90", "rot270", "transpose",
        "invert", "hue-90", "hue-180", "hue-270", "channel-swap",
        "palette-cycle",
    )
    # Spatial and colour are a partition -- a name in both would make the
    # "colour transform leaves geometry alone" promise untestable.
    assert set(pv.SPATIAL_TRANSFORMS) | set(pv.COLOUR_TRANSFORMS) \
        == set(pv.TRANSFORMS)
    assert not set(pv.SPATIAL_TRANSFORMS) & set(pv.COLOUR_TRANSFORMS)


def test_variant_plan_defaults_to_the_whole_vocabulary():
    assert pv.variant_plan() == list(pv.TRANSFORMS)
    assert pv.variant_plan(True) == list(pv.TRANSFORMS)
    assert pv.variant_plan(None) == list(pv.TRANSFORMS)


def test_variant_plan_resolves_a_subset_in_canonical_order():
    # De-duplicated and re-ordered to the vocabulary's order, so a request
    # written in another order still produces the same (byte-identical) set.
    assert pv.variant_plan("invert,flip-h") == ["flip-h", "invert"]
    assert pv.variant_plan(["invert", "flip-h", "invert"]) == ["flip-h", "invert"]


def test_variant_plan_reads_a_spec_block():
    block = {"enabled": True, "transforms": ["rot90", "hue-90"]}
    assert pv.variant_plan(block) == ["rot90", "hue-90"]
    assert pv.variant_plan({"enabled": True}) == list(pv.TRANSFORMS)


def test_variant_plan_treats_false_as_nothing():
    assert pv.variant_plan(False) == []
    assert pv.variant_plan({"enabled": False}) == []


def test_variant_plan_refuses_an_unknown_name_and_names_the_vocabulary():
    with pytest.raises(ValueError) as caught:
        pv.variant_plan(["flip-h", "sideways"])
    message = str(caught.value)
    assert "sideways" in message
    for name in pv.TRANSFORMS:
        assert name in message, "the refusal must quote the whole vocabulary"


# --------------------------------------------------------------------------
# Every transform, both halves
# --------------------------------------------------------------------------

def test_every_transform_round_trips_at_the_same_dimensions(package):
    proof, svg, _manifest = package
    source = proof.read_bytes()
    svg_bytes = svg.read_bytes()
    for name in pv.TRANSFORMS:
        raster = pv.transform_raster(source, name, palette=SCREENS)
        assert raster_size(raster) == (40, 40), name
        vector = pv.transform_svg(svg_bytes, name, palette=SCREENS)
        frame = frame_of(vector.decode("utf-8"))
        assert frame["width"] and frame["height"] and frame["viewBox"], name


def test_a_flip_applied_twice_restores_the_PIXELS_and_stays_deterministic(package):
    """A double flip must restore the image, and the stage must be deterministic.

    This used to assert BYTE identity with the source (`transform_raster(once,
    name) == source`) and passed only because the fixture is PIL-authored: PIL
    writes the same bytes for the same pixels, so a re-encode of a PIL file looks
    like a no-op. Against a REAL proof it is false -- Inkscape's PNG and PIL's
    differ in compression, filters and chunk order, so the second flip can never
    reproduce the original file byte for byte. That does not matter, and asserting
    it hid something that does: the re-encode was dropping the container metadata
    (see the dpi/text test below). What must hold is the pixels, the metadata, and
    determinism.
    """
    proof, _svg, _manifest = package
    source = proof.read_bytes()
    for name in ("flip-h", "flip-v"):
        once = pv.transform_raster(source, name)
        assert once != source, "the fixture must be asymmetric for this to mean anything"
        twice = pv.transform_raster(once, name)

        # The image is restored...
        with Image.open(io.BytesIO(source)) as a, Image.open(io.BytesIO(twice)) as b:
            assert a.convert("RGB").tobytes() == b.convert("RGB").tobytes(), name
            assert a.size == b.size, name
        # ...and the SAME input still yields the SAME bytes. This is the property
        # the byte comparison was reaching for, stated so a real proof satisfies
        # it: determinism is about the stage, not about matching an upstream
        # encoder that is not part of this pipeline.
        assert pv.transform_raster(once, name) == twice, name


def test_the_proofs_own_container_metadata_survives_the_transform():
    """A proof is a print artifact, so its physical resolution has to survive.

    Every emitted raster used to be saved with no pnginfo at all: the proof
    declared 300 dpi and each of the 12 variants declared NOTHING. For a dataset
    derived from a print check that is the wrong half to keep -- the pixels were
    lossless while the file stopped saying what size it was. Measured on the real
    proof this was `dpi: (299.9994, 299.9994)` in and `None` out.

    Written to fail if the metadata is dropped again: the fixture carries dpi and
    a text chunk, and both are asserted on the OUTPUT.
    """
    from PIL import PngImagePlugin

    info = PngImagePlugin.PngInfo()
    info.add_text("Software", "written-by-the-test")
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(
        buffer, format="PNG", dpi=(300, 300), pnginfo=info)
    source = buffer.getvalue()

    with Image.open(io.BytesIO(source)) as opened:
        assert opened.info.get("dpi"), "the fixture must declare a dpi to test this"

    for name in pv.TRANSFORMS:
        try:
            raw = pv.transform_raster(source, name)
        except pv.TransformError as exc:
            # `palette-cycle` on a single-colour image legitimately REFUSES: a
            # cycle needs two colours to permute, and the module treats "cannot
            # move the image" as a refusal rather than a silent duplicate. That is
            # the documented behaviour, so it is asserted here rather than
            # tolerated -- anything else refusing is a real failure.
            assert name == "palette-cycle", \
                "%s raised TransformError: %s" % (name, exc)
            assert "nothing to cycle" in str(exc), exc
            continue
        out = Image.open(io.BytesIO(raw))
        assert out.info.get("dpi"), \
            "%s dropped the proof's physical resolution" % name
        assert abs(out.info["dpi"][0] - 300) < 1, name
        assert out.info.get("Software") == "written-by-the-test", \
            "%s dropped the container's text provenance" % name


def test_flip_h_mirrors_about_the_frames_own_width():
    """The mirror is expressed in the root's viewBox units, not in pixels."""
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="20mm" height="10mm" '
           'viewBox="5 0 20 10"><path d="M6,1 L9,2 Z" fill="#c1440e"/></svg>')
    out = pv.transform_svg(svg.encode("utf-8"), "flip-h").decode("utf-8")
    # 2*minx + width = 2*5 + 20 = 30: x -> 30 - x maps [5, 25] onto itself.
    assert 'transform="translate(30 0) scale(-1 1)"' in out, out
    assert 'viewBox="5 0 20 10"' in out, "a mirror must not move the frame"


def test_non_square_rotation_exchanges_the_axes(package, tmp_path):
    """rot90/rot270/transpose are the only ones that change the frame's shape."""
    proof = write_proof(tmp_path / "wide.proof.png", size=(48, 32))
    source = proof.read_bytes()
    for name in ("flip-h", "flip-v", "rot180", "invert", "hue-90",
                 "channel-swap"):
        assert raster_size(pv.transform_raster(source, name)) == (48, 32), name
    for name in ("rot90", "rot270", "transpose"):
        assert raster_size(pv.transform_raster(source, name)) == (32, 48), name


def test_transpose_and_rot180_are_not_the_same_operation(package):
    proof, svg, _manifest = package
    source = proof.read_bytes()
    svg_bytes = svg.read_bytes()
    transpose = pv.transform_raster(source, "transpose")
    rot180 = pv.transform_raster(source, "rot180")
    assert transpose != rot180, "a symmetric fixture would hide this"

    transpose_svg = pv.transform_svg(svg_bytes, "transpose").decode("utf-8")
    rot180_svg = pv.transform_svg(svg_bytes, "rot180").decode("utf-8")
    assert 'transform="matrix(0 1 1 0 0 0)"' in transpose_svg
    assert "rotate(180)" in rot180_svg
    assert transpose_svg != rot180_svg


# --------------------------------------------------------------------------
# The two halves stay in their lane
# --------------------------------------------------------------------------

def test_colour_transforms_leave_path_data_and_node_count_alone(package):
    _proof, svg, _manifest = package
    svg_bytes = svg.read_bytes()
    source_text = svg_bytes.decode("utf-8")
    for name in sorted(pv.COLOUR_TRANSFORMS):
        out = pv.transform_svg(svg_bytes, name, palette=SCREENS).decode("utf-8")
        assert path_data(out) == path_data(source_text), name
        assert node_count(out) == node_count(source_text), name
        # Geometry in the strict sense: the frame and every dimension survive.
        assert frame_of(out) == frame_of(source_text), name


def test_colour_transforms_change_the_declared_colours(package):
    _proof, svg, _manifest = package
    svg_bytes = svg.read_bytes()
    source_colours = colours_of(svg_bytes.decode("utf-8"))
    for name in ("invert", "hue-90", "hue-180", "hue-270", "channel-swap"):
        out = pv.transform_svg(svg_bytes, name, palette=SCREENS).decode("utf-8")
        assert colours_of(out) != source_colours, name


def test_palette_cycle_permutes_the_palette_colours(package):
    """The set is preserved BY CONSTRUCTION -- it is a rotation of the screens.

    So "changed colours" cannot be read off the set here; the motif's own paint
    is what must move, and the file must differ.
    """
    _proof, svg, _manifest = package
    svg_bytes = svg.read_bytes()
    out = pv.transform_svg(svg_bytes, "palette-cycle", palette=SCREENS).decode("utf-8")
    motif = re.search(r'<path id="motif"[^>]*fill="([^"]*)"', out)
    assert motif and motif.group(1).lower() == "#6a8a3f"
    assert out != svg_bytes.decode("utf-8")
    assert colours_of(out) == colours_of(svg_bytes.decode("utf-8"))


def test_spatial_transforms_leave_the_colour_set_identical(package):
    _proof, svg, _manifest = package
    svg_bytes = svg.read_bytes()
    source_colours = colours_of(svg_bytes.decode("utf-8"))
    for name in sorted(pv.SPATIAL_TRANSFORMS):
        out = pv.transform_svg(svg_bytes, name, palette=SCREENS).decode("utf-8")
        assert colours_of(out) == source_colours, name


def test_spatial_transforms_do_not_touch_path_data(package):
    _proof, svg, _manifest = package
    svg_bytes = svg.read_bytes()
    for name in sorted(pv.SPATIAL_TRANSFORMS):
        out = pv.transform_svg(svg_bytes, name).decode("utf-8")
        assert path_data(out) == path_data(svg_bytes.decode("utf-8")), name


def test_a_spatial_transform_carries_one_transformed_group(package):
    """The transform goes on a wrapping group, not on the root element.

    SVG 1.1 -- which the renderers this pipeline prints through still implement
    -- does not apply `transform` to the outermost svg, so a root attribute is
    silently ignored. The group is the form that renders.
    """
    _proof, svg, _manifest = package
    out = pv.transform_svg(svg.read_bytes(), "rot90").decode("utf-8")
    assert re.search(r'<g transform="rotate\(90\)">', out)
    root = etree.fromstring(out.encode("utf-8"))
    assert root.get("transform") is None
    assert len(root) == 1 and etree.QName(root[0]).localname == "g"


# --------------------------------------------------------------------------
# The colour transforms, one by one
# --------------------------------------------------------------------------

def test_invert_is_the_full_negative():
    source = raster_bytes_with((193, 68, 14))
    with Image.open(io.BytesIO(pv.transform_raster(source, "invert"))) as out:
        assert out.getpixel((0, 0)) == (62, 187, 241)
    text = pv.transform_svg(
        b'<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4" '
        b'viewBox="0 0 4 4"><rect width="4" height="4" fill="#c1440e"/></svg>',
        "invert").decode("utf-8")
    assert "#3ebbf1" in text.lower()


def test_channel_swap_is_red_blue():
    source = raster_bytes_with((193, 68, 14))
    with Image.open(io.BytesIO(pv.transform_raster(source, "channel-swap"))) as out:
        assert out.getpixel((0, 0)) == (14, 68, 193)
    text = pv.transform_svg(
        b'<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4" '
        b'viewBox="0 0 4 4"><rect width="4" height="4" fill="#3f7c7c"/></svg>',
        "channel-swap").decode("utf-8")
    assert "#7c7c3f" in text.lower()


def test_palette_cycle_walks_the_palette_in_the_given_order(package):
    proof, svg, _manifest = package
    # SCREENS order is c1440e -> 6a8a3f -> 3f7c7c -> back to c1440e.
    text = pv.transform_svg(svg.read_bytes(), "palette-cycle",
                            palette=SCREENS).decode("utf-8")
    assert "#6a8a3f" in text.lower(), "the first screen must take the second"
    assert "#3f7c7c" in text.lower()
    assert "#c1440e" in text.lower(), "the last screen wraps to the first"
    assert "#ffffff" in text.lower(), "a colour outside the palette is left alone"

    with Image.open(io.BytesIO(pv.transform_raster(
            proof.read_bytes(), "palette-cycle", palette=SCREENS))) as out:
        assert out.getpixel((0, 0)) == (106, 138, 63)


def test_palette_cycle_without_a_palette_cycles_the_artworks_own_colours(package):
    proof, svg, _manifest = package
    # Sorted, so the cycle is the same on every run regardless of pixel order.
    text = pv.transform_svg(svg.read_bytes(), "palette-cycle").decode("utf-8")
    # Sorted colours: #3f7c7c, #6a8a3f, #c1440e, #ffffff -> each takes the next.
    assert "#6a8a3f" in text.lower()
    assert colours_of(text) == colours_of(svg.read_text(encoding="utf-8"))
    cycled = pv.transform_raster(proof.read_bytes(), "palette-cycle")
    with Image.open(io.BytesIO(cycled)) as out:
        assert out.getpixel((0, 0)) == (255, 255, 255), \
            "the artwork's own colours cycle among themselves"


def test_hue_shift_agrees_between_the_two_halves_and_only_moves_hue():
    """The raster and the vector go through the SAME 8-bit HSV path.

    That is deliberate: a pair is only a pair if both halves of the same colour
    land on the same hex.  (The shift itself is approximate in the last bit --
    PIL's hue channel is 8 bits, so 90 degrees is a 64-step offset.)
    """
    colour = (193, 68, 14)
    hexed = "#c1440e"
    for name, degrees in (("hue-90", 90), ("hue-180", 180), ("hue-270", 270)):
        with Image.open(io.BytesIO(
                pv.transform_raster(raster_bytes_with(colour), name))) as out:
            shifted = pv.hex_of(out.getpixel((0, 0)))
        probe = pv.transform_svg(
            ('<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4" '
             'viewBox="0 0 4 4"><rect width="4" height="4" fill="%s"/></svg>'
             % hexed).encode("utf-8"), name).decode("utf-8")
        assert shifted in probe.lower(), name
        # The OFFSET itself, as a literal. This replaces an assertion that could
        # not fail -- `hue_offset(degrees) == hue_offset(degrees) % 256`, true by
        # construction because the function already returns a value in range.
        #
        # Literals rather than the formula, deliberately: restating
        # `round(degrees / 360 * 255)` would only prove the test agrees with the
        # code. These encode the documented REASONING instead -- PIL's 8-bit hue
        # channel spans 0..255, so a full turn is 255 steps and a quarter turn is
        # round(63.75) = 64. The 270 case is the one that matters: it is 191, not
        # 192, and 255-vs-256 is exactly the kind of off-by-one that would shift
        # every third colour wrongly. (This assertion caught my own wrong guess of
        # 192 when the tautology was removed.)
        assert pv.hue_offset(degrees) == {90: 64, 180: 128, 270: 191}[degrees], name


def test_hue_shift_leaves_an_unsaturated_colour_where_it_is():
    source = raster_bytes_with((128, 128, 128))
    for name in ("hue-90", "hue-180", "hue-270"):
        with Image.open(io.BytesIO(pv.transform_raster(source, name))) as out:
            assert out.getpixel((0, 0)) == (128, 128, 128), name


def test_an_unsupported_raster_mode_still_round_trips():
    image = Image.new("RGBA", (8, 8), (10, 20, 30, 128))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    out = pv.transform_raster(buffer.getvalue(), "hue-90")
    with Image.open(io.BytesIO(out)) as reopened:
        assert reopened.mode == "RGBA"
        assert reopened.getpixel((0, 0))[3] == 128, "alpha must survive"


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------

def test_an_unknown_transform_is_refused_by_both_halves():
    for call in (lambda: pv.transform_raster(b"", "sideways"),
                 lambda: pv.transform_svg(b"<svg/>", "sideways")):
        with pytest.raises(ValueError) as caught:
            call()
        assert "sideways" in str(caught.value)
        assert "palette-cycle" in str(caught.value)


def test_a_document_with_no_frame_is_refused():
    with pytest.raises(pv.TransformError) as caught:
        pv.transform_svg(b'<svg xmlns="http://www.w3.org/2000/svg"/>', "flip-h")
    assert "frame" in str(caught.value)


def test_verify_svg_refuses_a_result_without_a_frame():
    with pytest.raises(pv.TransformError):
        pv.verify_svg(b'<svg xmlns="http://www.w3.org/2000/svg"><g/></svg>')
    with pytest.raises(pv.TransformError):
        pv.verify_svg(b"not xml at all")


# --------------------------------------------------------------------------
# build_dataset
# --------------------------------------------------------------------------

def test_build_dataset_writes_a_pair_per_transform(package, tmp_path):
    proof, svg, manifest = package
    out = tmp_path / "candidate_02.dataset"
    description = pv.build_dataset(proof, svg, out, manifest=manifest)

    assert description["count"] == len(pv.TRANSFORMS)
    assert description["transforms"] == list(pv.TRANSFORMS)
    for name in pv.TRANSFORMS:
        assert (out / "raster" / ("%s.png" % name)).is_file()
        assert (out / "vector" / ("%s.svg" % name)).is_file()
    assert (out / "dataset.json").is_file()
    assert (out / "dataset.md").is_file()

    payload = json.loads((out / "dataset.json").read_text(encoding="utf-8"))
    assert payload == description, "the file and the returned description agree"
    for entry in payload["files"]:
        assert entry["source_proof_sha256"] == payload["source"]["proof"]["sha256"]
        assert entry["raster"]["sha256"] and entry["raster"]["bytes"]
        assert entry["vector"]["sha256"] and entry["vector"]["bytes"]
        assert entry["transform"] == entry["raster"]["name"].split(".")[0]


def test_the_description_is_the_manifest_key(package, tmp_path):
    """The first six keys are exactly the `dataset` block the contract names."""
    proof, svg, manifest = package
    description = pv.build_dataset(proof, svg, tmp_path / "candidate_02.dataset",
                                   manifest=manifest)
    assert {key: description[key] for key in
            ("dir", "manifest", "sheet", "package", "count", "transforms")} == {
        "dir": "candidate_02.dataset",
        "manifest": "candidate_02.dataset.json",
        "sheet": "candidate_02.dataset-contact-sheet.png",
        "package": "candidate_02.dataset.7z",
        "count": len(pv.TRANSFORMS),
        "transforms": list(pv.TRANSFORMS),
    }


def test_build_dataset_is_deterministic(package, tmp_path):
    proof, svg, manifest = package
    out = tmp_path / "candidate_02.dataset"
    first = pv.build_dataset(proof, svg, out, manifest=manifest)
    first_hashes = tree_hashes(out)
    second = pv.build_dataset(proof, svg, out, manifest=manifest)
    assert tree_hashes(out) == first_hashes, \
        "a second run must not move a single byte"
    assert second == first
    assert len(first_hashes) == 2 * len(pv.TRANSFORMS) + 2


def test_every_single_transform_is_a_pure_function(package):
    proof, svg, _manifest = package
    source, svg_bytes = proof.read_bytes(), svg.read_bytes()
    for name in pv.TRANSFORMS:
        assert (pv.transform_raster(source, name, palette=SCREENS)
                == pv.transform_raster(source, name, palette=SCREENS)), name
        assert (pv.transform_svg(svg_bytes, name, palette=SCREENS)
                == pv.transform_svg(svg_bytes, name, palette=SCREENS)), name


def test_build_dataset_records_provenance_from_the_manifest(package, tmp_path):
    proof, svg, manifest = package
    description = pv.build_dataset(proof, svg, tmp_path / "d", manifest=manifest,
                                   stem="candidate_02")
    provenance = description["provenance"]
    assert provenance["manifest"] == str(manifest)
    assert provenance["dpi"] == 300
    assert provenance["screens"] == SCREENS
    assert provenance["substrate"] == "#FFFFFF"
    assert provenance["manifest_tool"].startswith("preflight.py")
    assert description["source"]["vector"]["sha256"] == sha(svg.read_bytes())
    assert description["source"]["proof"]["sha256"] == sha(proof.read_bytes())
    assert "limited to the two input files" not in (provenance.get("note") or "")
    markdown = (tmp_path / "d" / "dataset.md").read_text(encoding="utf-8")
    assert "candidate_02.manifest.json" in markdown
    assert "`#c1440e`" in markdown


def test_build_dataset_finds_the_manifest_beside_the_proof(package, tmp_path):
    """No manifest argument: the print check's sibling naming is discovered."""
    proof, svg, manifest = package
    description = pv.build_dataset(proof, svg, tmp_path / "d")
    assert description["provenance"]["dpi"] == 300
    assert description["provenance"]["manifest"] == str(manifest)


def test_build_dataset_without_a_manifest_still_works_and_says_so(tmp_path):
    proof = write_proof(tmp_path / "lonely.proof.png")
    svg = write_svg(tmp_path / "lonely.svg")
    description = pv.build_dataset(proof, svg, tmp_path / "lonely.dataset",
                                   transforms=["flip-h"])
    assert description["count"] == 1
    assert description["provenance"]["manifest"] is None
    assert description["provenance"]["manifest_source"] == "absent"
    assert any("no manifest found" in note for note in description["notes"])


def test_a_supplied_manifest_is_not_reported_as_missing(tmp_path):
    """A dict handed in is provenance, not absence.

    `dataset.json` claiming "no manifest" while using one is a false statement
    about what the set was derived from -- and the runner hands the manifest in
    as a dict, so that was the ordinary path, not an edge case.
    """
    proof = write_proof(tmp_path / "given.proof.png")
    svg = write_svg(tmp_path / "given.svg")
    out = tmp_path / "given.dataset"
    description = pv.build_dataset(proof, svg, out, transforms=["flip-h"],
                                   manifest=manifest_payload())
    provenance = description["provenance"]
    assert provenance["manifest_source"] == "supplied"
    assert provenance["dpi"] == 300
    assert provenance["screens"] == SCREENS
    assert not any("no manifest found" in note for note in description["notes"])
    # No file beside the proof, so no path to record -- and it says exactly that.
    assert provenance["manifest"] is None
    assert "supplied to the stage in memory" in provenance["note"]
    assert "supplied in memory" in (out / "dataset.md").read_text(encoding="utf-8")


def test_a_supplied_manifest_records_the_sibling_file_when_there_is_one(tmp_path):
    proof = write_proof(tmp_path / "given2.proof.png")
    svg = write_svg(tmp_path / "given2.svg")
    sibling = tmp_path / "given2.manifest.json"
    sibling.write_text(json.dumps(manifest_payload()), encoding="utf-8")
    description = pv.build_dataset(proof, svg, tmp_path / "given2.dataset",
                                   transforms=["flip-h"],
                                   manifest=manifest_payload())
    assert description["provenance"]["manifest"] == str(sibling)
    assert "note" not in description["provenance"]


def test_build_dataset_only_reads_its_inputs(package, tmp_path):
    proof, svg, manifest = package
    before = (proof.read_bytes(), svg.read_bytes(), manifest.read_bytes())
    pv.build_dataset(proof, svg, tmp_path / "d", manifest=manifest)
    assert (proof.read_bytes(), svg.read_bytes(), manifest.read_bytes()) == before


def test_build_dataset_notes_that_the_hue_shift_is_approximate(package, tmp_path):
    proof, svg, manifest = package
    description = pv.build_dataset(proof, svg, tmp_path / "d",
                                   transforms=["hue-90"], manifest=manifest)
    assert any("8-bit HSV" in note for note in description["notes"])
    assert "8-bit HSV" in (tmp_path / "d" / "dataset.md").read_text(encoding="utf-8")


def test_rebuilding_a_dataset_replaces_the_previous_set(package, tmp_path):
    """A narrower second run leaves no variant the first run wrote."""
    proof, svg, manifest = package
    out = tmp_path / "d"
    pv.build_dataset(proof, svg, out, manifest=manifest)
    pv.build_dataset(proof, svg, out, transforms=["flip-h"], manifest=manifest)
    assert sorted(p.name for p in (out / "raster").iterdir()) == ["flip-h.png"]
    assert sorted(p.name for p in (out / "vector").iterdir()) == ["flip-h.svg"]
    payload = json.loads((out / "dataset.json").read_text(encoding="utf-8"))
    assert payload["transforms"] == ["flip-h"]


def test_build_dataset_refuses_a_directory_it_does_not_own(package, tmp_path):
    """Pointing --out at someone else's directory is refused, not deleted."""
    proof, svg, _manifest = package
    out = tmp_path / "elsewhere"
    (out / "raster").mkdir(parents=True)
    stranger = out / "raster" / "someone-elses.png"
    stranger.write_bytes(b"not ours")
    with pytest.raises(pv.TransformError) as caught:
        pv.build_dataset(proof, svg, out, transforms=["flip-h"])
    assert "refusing to replace" in str(caught.value)
    assert stranger.read_bytes() == b"not ours", "nothing may be deleted"


def test_a_refused_transform_leaves_no_partial_dataset(package, tmp_path):
    """All or nothing: a raise mid-loop must not leave a half-built tree.

    A partial `.dataset/` is worse than a missing one -- the archive route lists
    any such directory as a dataset and would stream an empty package.
    """
    proof, _svg, manifest = package
    broken = tmp_path / "broken.svg"
    broken.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>',
                      encoding="utf-8")
    out = tmp_path / "candidate_02.dataset"
    with pytest.raises(pv.TransformError):
        pv.build_dataset(proof, broken, out, manifest=manifest)
    assert not out.exists(), "a refused build must leave no dataset directory"
    assert not [p for p in tmp_path.iterdir() if "partial" in p.name]


def test_a_non_square_proof_notes_the_exchanged_axes(tmp_path):
    proof = write_proof(tmp_path / "wide.proof.png", size=(48, 32))
    svg = write_svg(tmp_path / "wide.svg")
    description = pv.build_dataset(proof, svg, tmp_path / "wide.dataset",
                                   transforms=["rot90", "flip-h"])
    entries = {entry["transform"]: entry for entry in description["files"]}
    assert entries["rot90"]["note"] == "axes exchanged: 48x32 -> 32x48"
    assert "note" not in entries["flip-h"]


# --------------------------------------------------------------------------
# A variant that is a duplicate of its input
# --------------------------------------------------------------------------
#
# The worst failure this stage can have, and the one that actually happened on a
# real proof: `palette-cycle` emitted a file that was visually the proof, hashed
# and counted as an augmentation.  The fixtures below are chosen so the exact
# permutation CANNOT work -- a smooth ramp whose pixels sit between the palette
# values, and an SVG whose declared colours are not palette colours -- because a
# flat 3-colour fixture makes exact remapping trivially succeed and therefore
# cannot catch this at all.

GRADIENT_PALETTE = ["#000000", "#ff0000", "#00ff00", "#0000ff"]


def make_gradient_proof(path, size=(48, 48)):
    """A ramp with NO pixel equal to a palette colour (or to white)."""
    image = Image.new("RGB", size)
    pixels = image.load()
    for y in range(size[1]):
        for x in range(size[0]):
            red = 1 + (x * 253) // (size[0] - 1)          # 1..254
            green = 1 + (y * 253) // (size[1] - 1)        # 1..254
            pixels[x, y] = (red, green, (red + green) // 2)
    path.write_bytes(_png_bytes(image))
    return path


def make_miss_svg(path):
    """Declared colours that appear in NO palette handed to the cycle."""
    return write_svg(path, '<svg xmlns="http://www.w3.org/2000/svg" width="10mm" '
                           'height="10mm" viewBox="0 0 10 10">'
                           '<path d="M0,0 L5,0 L5,5 Z" fill="#3fa2c1"/>'
                           '<circle cx="8" cy="8" r="2" fill="#e07a33"/>'
                           '</svg>')


def test_the_flat_fixture_proves_nothing_about_exact_matching():
    """Guards the guard: the ordinary fixture really does permute exactly.

    The flat fixture is the EASY case.  If this ever stops being true, the
    duplicate tests below stop meaning anything.
    """
    proof_bytes = make_proof_bytes()
    notes = []
    out = pv.transform_raster(proof_bytes, "palette-cycle",
                              palette=list(SCREENS), notes=notes)
    with Image.open(io.BytesIO(out)) as image:
        changed = pv.changed_fraction(
            Image.open(io.BytesIO(proof_bytes)).convert("RGB"),
            image.convert("RGB"))
    assert changed >= pv.MIN_VARIANT_CHANGE, (
        "flat art must cycle exactly; this fixture is the easy case")
    assert any("permuted" in text for text in notes), notes
    assert not any("REQUANTISATION" in text for text in notes), notes


def test_palette_cycle_requantises_when_no_pixel_matches_the_palette(tmp_path):
    """The measured defect: exact remap moved ~0% and shipped a duplicate."""
    proof = make_gradient_proof(tmp_path / "ramp.proof.png")
    proof_bytes = proof.read_bytes()
    palette = ["#FFFFFF"] + GRADIENT_PALETTE
    notes = []
    out = pv.transform_raster(proof_bytes, "palette-cycle", palette=palette,
                              notes=notes)
    with Image.open(io.BytesIO(proof_bytes)) as opened:
        source = opened.convert("RGB")
    with Image.open(io.BytesIO(out)) as opened:
        variant = opened.convert("RGB")
    changed = pv.changed_fraction(source, variant)
    assert changed >= pv.MIN_VARIANT_CHANGE, (
        "palette-cycle must not emit a near-duplicate: exact matching alone "
        "moves %.3f%% of this fixture" % (changed * 100.0))
    assert any("REQUANTISATION" in text for text in notes), notes
    assert any("0.000%" in text or "not exact pixel values" in text
               for text in notes), notes


def test_palette_cycle_requantises_a_vector_whose_colours_miss_the_palette(
        package, tmp_path):
    _proof, _svg, _manifest = package
    svg = make_miss_svg(tmp_path / "miss.svg")
    notes = []
    out = pv.transform_svg(svg.read_bytes(), "palette-cycle",
                           palette=GRADIENT_PALETTE, notes=notes).decode("utf-8")
    source_text = svg.read_text(encoding="utf-8")
    before = re.findall(r'\s(fill|stroke|stop-color)="([^"]*)"', source_text)
    after = re.findall(r'\s(fill|stroke|stop-color)="([^"]*)"', out)
    changed = sum(1 for a, b in zip(before, after) if a != b)
    assert changed == len(before), "an exact cycle rewrites none of these colours"
    assert any("NEAREST" in text for text in notes), notes
    assert path_data(out) == path_data(source_text), \
        "a requantisation of the palette must not touch path data"


def test_the_requantisation_is_recorded_in_the_dataset_notes(package, tmp_path):
    """The note is the deliverable: an unflagged duplicate is the failure."""
    proof = make_gradient_proof(tmp_path / "ramp.proof.png")
    svg = make_miss_svg(tmp_path / "ramp.svg")
    manifest = tmp_path / "ramp.manifest.json"
    manifest.write_text(json.dumps(dict(manifest_payload(),
                                        substrate={"colour": "#FFFFFF",
                                                   "palette": ["#FFFFFF"]
                                                   + GRADIENT_PALETTE,
                                                   "screens": list(GRADIENT_PALETTE)})),
                        encoding="utf-8")
    out = tmp_path / "ramp.dataset"
    description = pv.build_dataset(proof, svg, out,
                                   transforms=["palette-cycle", "invert"],
                                   manifest=manifest)
    entries = {entry["transform"]: entry for entry in description["files"]}
    assert any("REQUANTISATION" in text
               for text in entries["palette-cycle"]["notes"])
    assert any(text.startswith("palette-cycle: ") for text in description["notes"])
    payload = json.loads((out / "dataset.json").read_text(encoding="utf-8"))
    assert payload["notes"] == description["notes"]
    markdown = (out / "dataset.md").read_text(encoding="utf-8")
    assert "REQUANTISATION" in markdown


def test_palette_cycle_permutes_exactly_when_the_art_uses_palette_colours(
        package, tmp_path):
    """The other side of the same decision: no requantisation note here.

    Keyed on the wrong case, this passed vacuously for a long time -- the motif
    kept its own colour and the assertion only looked for colours that were
    already in the file.  The colour of a NAMED element is the claim that can
    actually fail.
    """
    _proof, svg, _manifest = package
    notes = []
    out = pv.transform_svg(svg.read_bytes(), "palette-cycle", palette=SCREENS,
                           notes=notes).decode("utf-8")
    motif = re.search(r'<path id="motif"[^>]*fill="([^"]*)"', out)
    assert motif and motif.group(1).lower() == "#6a8a3f"
    assert "#ffffff" in out.lower(), "a colour outside the palette is left alone"
    assert any("permuted" in text for text in notes), notes
    assert not any("REQUANTISATION" in text for text in notes), notes


def test_an_exact_permutation_is_reported_as_a_permutation(package, tmp_path):
    """The honest other half: say which one happened, not just "cycle"."""
    proof, svg, manifest = package
    notes = []
    pv.transform_raster(proof.read_bytes(), "palette-cycle", palette=SCREENS,
                        notes=notes)
    assert any("permuted" in text and "REQUANTISATION" not in text
               for text in notes), notes


def test_palette_cycle_refuses_a_palette_with_nothing_to_cycle(package):
    proof, svg, _manifest = package
    with pytest.raises(pv.TransformError) as caught:
        pv.transform_raster(proof.read_bytes(), "palette-cycle",
                            palette=["#ffffff", "#ffffff"])
    assert "nothing to cycle" in str(caught.value)
    with pytest.raises(pv.TransformError) as caught:
        pv.transform_svg(svg.read_bytes(), "palette-cycle",
                         palette=["#ffffff", "#ffffff"])
    assert "nothing to cycle" in str(caught.value)


# --------------------------------------------------------------------------
# The general invariant: never emit a duplicate silently
# --------------------------------------------------------------------------

def test_a_colour_transform_with_nothing_to_move_says_so(package):
    """hue-90 on an achromatic proof IS an identity -- and must be admitted."""
    grey = Image.new("RGB", (16, 16), (128, 128, 128))
    notes = []
    out = pv.transform_raster(_png_bytes(grey), "hue-90", notes=notes)
    assert pv.changed_fraction(grey, Image.open(io.BytesIO(out)).convert("RGB")) == 0
    assert any("visually the proof" in text for text in notes), notes


def test_a_spatial_variant_that_duplicates_a_symmetric_proof_says_so(tmp_path):
    image = Image.new("RGB", (12, 12), "#ffffff")
    pixels = image.load()
    for y in range(12):
        for x in range(12):
            if x < 3 or x > 8:                 # left/right symmetric
                pixels[x, y] = (193, 68, 14)
    notes = []
    out = pv.transform_raster(_png_bytes(image), "flip-h", notes=notes)
    with Image.open(io.BytesIO(out)) as image_out:
        assert image_out.convert("RGB").tobytes() == image.convert("RGB").tobytes()
    assert any("symmetric under this transform" in text for text in notes), notes


def test_the_duplicate_note_reaches_the_dataset_notes(tmp_path):
    grey = Image.new("RGB", (16, 16), (128, 128, 128))
    proof = tmp_path / "grey.proof.png"
    proof.write_bytes(_png_bytes(grey))
    svg = write_svg(tmp_path / "grey.svg")
    description = pv.build_dataset(proof, svg, tmp_path / "grey.dataset",
                                   transforms=["hue-90"])
    assert any(text.startswith("hue-90: ") and "visually the proof" in text
               for text in description["notes"]), description["notes"]


# --------------------------------------------------------------------------
# A transform that cannot apply to THIS artwork: skip it, name it, keep the rest
# --------------------------------------------------------------------------
#
# THE GAP THAT LET THIS SHIP: no fixture in this file was single-colour, so no
# test ever reached `palette-cycle`'s refusal on real single-ink line art -- an
# ordinary output of this pipeline's own trace stage.  Measured on a real proof
# through the deployed runner: that ONE refusal discarded the other ELEVEN
# transforms and `manifest["dataset"]` came back None.  The fixtures below are
# single-colour ON PURPOSE.
#
# Two invariants must survive the fix, and both are asserted here:
#   * no silent duplicate -- a skipped transform emits NO file at all, so it
#     cannot smuggle a copy of the proof into the set;
#   * all-or-nothing on a REAL fault -- a truncated proof or a transform that
#     raises a plain TransformError still discards every file, staging included.

SINGLE_INK = (0, 0, 0)

# One declared colour.  `circle` and `path` give the vector half real geometry,
# so a colour transform that rewrote something it should not would show up.
SINGLE_INK_SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="40mm" height="40mm"
     viewBox="0 0 40 40">
  <path id="ink" d="M2,2 L12,6 L4,18 Z" fill="#000000"/>
  <circle id="dot" cx="32" cy="32" r="5" fill="#000000"/>
</svg>
"""


def single_ink_proof(path, size=(40, 40)):
    """A one-colour raster: any second pixel value would be a second ink."""
    path.write_bytes(_png_bytes(Image.new("RGB", size, SINGLE_INK)))
    return path


@pytest.fixture
def single_ink(tmp_path):
    """(proof, svg) for one single-ink artwork, with NO manifest beside it.

    No manifest is the case that failed on the real proof: with no declared
    palette the cycle walks the ARTWORK'S OWN colours, and one colour is not a
    permutation.
    """
    proof = single_ink_proof(tmp_path / "candidate_05.proof.png")
    svg = write_svg(tmp_path / "candidate_05.svg", SINGLE_INK_SVG)
    return proof, svg


def test_a_single_colour_artwork_gets_every_other_transform(single_ink, tmp_path):
    """11 of 12, with the twelfth NAMED -- not twelve claimed and eleven held."""
    proof, svg = single_ink
    out = tmp_path / "candidate_05.dataset"
    emitted = [name for name in pv.TRANSFORMS if name != "palette-cycle"]
    assert len(pv.TRANSFORMS) == 12, \
        "11-of-12 is only meaningful against a 12-name vocabulary"

    description = pv.build_dataset(proof, svg, out, transforms=True)

    # The count describes the FILES, and the files are on disk.
    assert description["count"] == 11
    assert description["transforms"] == emitted
    assert sorted(p.name for p in (out / "raster").iterdir()) \
        == sorted("%s.png" % name for name in emitted)
    assert sorted(p.name for p in (out / "vector").iterdir()) \
        == sorted("%s.svg" % name for name in emitted)
    assert sorted(entry["transform"] for entry in description["files"]) \
        == sorted(emitted)
    assert not (out / "raster" / "palette-cycle.png").exists()
    assert not (out / "vector" / "palette-cycle.svg").exists()

    # The skipped transform is named, in the notes and in the report.
    notes = [text for text in description["notes"]
             if text.startswith("palette-cycle:")]
    assert notes and "not applicable" in notes[0], description["notes"]
    assert "nothing to cycle" in notes[0], notes
    assert not any(text.startswith(name + ":") and "not applicable" in text
                   for text in description["notes"] for name in emitted), \
        "only the transform that cannot apply may be skipped"
    assert [entry["transform"] for entry in description["skipped"]] \
        == ["palette-cycle"]

    # And the two written reports agree with the returned description.
    payload = json.loads((out / "dataset.json").read_text(encoding="utf-8"))
    assert payload["count"] == 11
    assert payload["transforms"] == emitted
    assert payload["skipped"] == description["skipped"]
    assert payload["notes"] == description["notes"]
    markdown = (out / "dataset.md").read_text(encoding="utf-8")
    assert "# Proof variants -- 11 similar copies" in markdown
    assert "not applicable" in markdown and "palette-cycle" in markdown


def test_the_refusal_on_single_ink_art_is_typed_not_applicable(single_ink):
    """The type is the fix's contract: TransformNotApplicable IS a TransformError.

    Both halves refuse, a caller that only knows TransformError keeps working
    (the CLI's exit code, the runner's note), and the reason travels with it.
    """
    proof, svg = single_ink
    for call in (lambda: pv.transform_raster(proof.read_bytes(), "palette-cycle"),
                 lambda: pv.transform_svg(svg.read_bytes(), "palette-cycle")):
        with pytest.raises(pv.TransformNotApplicable) as caught:
            call()
        assert isinstance(caught.value, pv.TransformError)
        assert "nothing to cycle" in str(caught.value)
        assert caught.value.reason, "the dataset note needs a reason to quote"


def test_a_real_fault_is_not_reclassified_as_inapplicable():
    """The boundary itself: a broken input stays a plain TransformError.

    A document with no usable frame is a fault -- every tracer candidate carries
    one -- and so is an unreadable image. Reclassifying those would turn a
    broken build into a quietly smaller dataset.
    """
    with pytest.raises(pv.TransformError) as caught:
        pv.transform_svg(b'<svg xmlns="http://www.w3.org/2000/svg"/>', "flip-h")
    assert not isinstance(caught.value, pv.TransformNotApplicable)
    assert "frame" in str(caught.value)


def test_a_dataset_of_only_inapplicable_transforms_leaves_no_dataset(
        single_ink, tmp_path):
    """Every requested transform skipped: there is nothing to write, so write it.

    An empty `.dataset/` is worse than none -- the archive route lists any such
    directory as a real dataset and would stream an empty package.
    """
    proof, svg = single_ink
    out = tmp_path / "candidate_05.dataset"
    with pytest.raises(pv.TransformNotApplicable) as caught:
        pv.build_dataset(proof, svg, out, transforms=["palette-cycle"])
    assert isinstance(caught.value, pv.TransformError)
    assert "palette-cycle" in str(caught.value)
    assert "nothing to cycle" in str(caught.value)
    assert not out.exists(), "no dataset directory may survive"
    assert not [p for p in tmp_path.iterdir() if "partial" in p.name]
    assert not [p for p in tmp_path.iterdir() if ".dataset" in p.name]


def test_cli_exits_five_when_no_requested_transform_applies(single_ink, tmp_path,
                                                           capsys):
    proof, svg = single_ink
    code = pv.main(["--proof", str(proof), "--svg", str(svg),
                    "--out", str(tmp_path / "d"),
                    "--transforms", "palette-cycle"])
    assert code == 5, "you asked for a variant and there is none"
    err = capsys.readouterr().err
    assert "no variant can be derived" in err and "palette-cycle" in err
    assert not (tmp_path / "d").exists()


def test_a_real_fault_still_discards_the_whole_set_atomically(single_ink, tmp_path,
                                                              monkeypatch):
    """All-or-nothing on a FAULT is untouched by the skip outcome.

    The fault is forced on the THIRD transform, so two pairs are already written
    into the staging tree when it fires -- and `palette-cycle`, which this
    artwork skips, is still in the plan behind it. A fault must neither be
    swallowed by the skip logic nor leave a half-built tree.
    """
    proof, svg = single_ink
    out = tmp_path / "candidate_05.dataset"
    real = pv.transform_raster
    calls = []

    def faulty(image_bytes, transform, **kwargs):
        calls.append(transform)
        if len(calls) == 3:
            raise pv.TransformError("forced fault on %s" % transform)
        return real(image_bytes, transform, **kwargs)

    monkeypatch.setattr(pv, "transform_raster", faulty)
    with pytest.raises(pv.TransformError) as caught:
        pv.build_dataset(proof, svg, out)
    assert calls[:3] == list(pv.TRANSFORMS[:3]), calls
    assert "forced fault" in str(caught.value)
    assert not isinstance(caught.value, pv.TransformNotApplicable)
    assert not out.exists()
    assert not [p for p in tmp_path.iterdir() if "partial" in p.name]


def test_a_truncated_proof_is_a_fault_not_an_inapplicability(single_ink, tmp_path):
    proof, svg = single_ink
    truncated = tmp_path / "truncated.proof.png"
    truncated.write_bytes(proof.read_bytes()[: len(proof.read_bytes()) // 3])
    out = tmp_path / "truncated.dataset"
    with pytest.raises(Exception) as caught:            # noqa: B017 - any type
        pv.build_dataset(truncated, svg, out)
    assert not isinstance(caught.value, pv.TransformNotApplicable), \
        "an unreadable image is a broken input, not a property of the art"
    assert not out.exists()


def test_a_multi_colour_artwork_skips_nothing(package, tmp_path):
    """The other side of the same decision: the ordinary case is unchanged."""
    proof, svg, manifest = package
    out = tmp_path / "candidate_02.dataset"
    description = pv.build_dataset(proof, svg, out,
                                   transforms=["palette-cycle", "flip-h",
                                               "invert"],
                                   manifest=manifest)
    assert description["count"] == 3
    assert description["transforms"] == ["flip-h", "invert", "palette-cycle"]
    assert description["skipped"] == []
    assert not any("not applicable" in text for text in description["notes"])
    assert (out / "raster" / "palette-cycle.png").is_file()
    assert (out / "vector" / "palette-cycle.svg").is_file()


def test_a_requantised_palette_cycle_is_emitted_not_skipped(tmp_path):
    """A continuous-tone proof still gets all twelve.

    `palette-cycle` on art whose colours miss the palette is a REQUANTISATION,
    not an inapplicability: the variant moves the image, so it is emitted with
    the note that says so. Only a cycle with nothing to move -- or one that
    could only move the image by duplicating it -- is skipped.
    """
    proof = make_gradient_proof(tmp_path / "ramp.proof.png")
    svg = make_miss_svg(tmp_path / "ramp.svg")
    out = tmp_path / "ramp.dataset"
    description = pv.build_dataset(
        proof, svg, out,
        manifest={"substrate": {"screens": list(GRADIENT_PALETTE)},
                  "stats": {"dpi": 300}})
    assert description["count"] == len(pv.TRANSFORMS)
    assert description["skipped"] == []
    entry = [item for item in description["files"]
             if item["transform"] == "palette-cycle"][0]
    assert any("REQUANTISATION" in text for text in entry["notes"]), entry["notes"]


# --------------------------------------------------------------------------
# The frame every consumer needs
# --------------------------------------------------------------------------

def test_every_transform_gives_a_viewboxless_document_a_frame(tmp_path):
    """A real tracer candidate carries width/height and may carry NO viewBox.

    Every consumer downstream needs all three, so each transform must leave the
    document with them -- including `palette-cycle`, which re-parses the file to
    make its second attempt.
    """
    svg = tmp_path / "noviewbox.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="40mm" height="40mm">'
        '<rect width="40" height="40" fill="#ffffff"/>'
        '<path d="M1,1 L20,4 L3,20 Z" fill="#3fa2c1"/></svg>',
        encoding="utf-8")
    for name in pv.TRANSFORMS:
        frame = frame_of(pv.transform_svg(svg.read_bytes(), name,
                                          palette=GRADIENT_PALETTE).decode("utf-8"))
        assert frame["width"] and frame["height"] and frame["viewBox"], name


# --------------------------------------------------------------------------
# Contact sheet
# --------------------------------------------------------------------------

def test_contact_sheet_tiles_one_labelled_cell_per_variant(package, tmp_path):
    proof, svg, manifest = package
    out = tmp_path / "candidate_02.dataset"
    description = pv.build_dataset(proof, svg, out, transforms=["flip-h",
                                                                "flip-v",
                                                                "rot180",
                                                                "invert"],
                                   manifest=manifest)
    sheet = tmp_path / "candidate_02.dataset-contact-sheet.png"
    pv.contact_sheet(
        [(out / "raster" / entry["raster"]["name"], entry["transform"])
         for entry in description["files"]], sheet, columns=2)
    with Image.open(sheet) as image:
        assert image.size == (2 * pv.DEFAULT_TILE, 2 * (pv.DEFAULT_TILE + 26))
        assert image.mode == "RGB"


def test_contact_sheet_labels_a_bare_path_with_its_stem(package, tmp_path):
    proof, _svg, _manifest = package
    sheet = tmp_path / "sheet.png"
    pv.contact_sheet([proof], sheet, columns=1)
    assert sheet.is_file()


def test_contact_sheet_refuses_an_empty_set(tmp_path):
    with pytest.raises(pv.TransformError):
        pv.contact_sheet([], tmp_path / "empty.png")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def cli(package, tmp_path, *extra, transforms="flip-h,invert"):
    proof, svg, _manifest = package
    argv = ["--proof", str(proof), "--svg", str(svg),
            "--out", str(tmp_path / "candidate_02.dataset")]
    if transforms is not None:
        argv += ["--transforms", transforms]
    return pv.main(argv + list(extra))


def test_cli_writes_the_package_and_exits_zero(package, tmp_path, capsys):
    code = cli(package, tmp_path, "--sheet")
    printed = capsys.readouterr().out
    assert code == 0
    assert "--- stage 1: variant plan ---" in printed
    assert "--- stage 2: variants written (raster + vector) ---" in printed
    assert (tmp_path / "candidate_02.dataset" / "dataset.json").is_file()
    assert (tmp_path / "candidate_02.dataset-contact-sheet.png").is_file()


def test_cli_writes_no_sheet_unless_asked(package, tmp_path):
    assert cli(package, tmp_path) == 0
    assert not (tmp_path / "candidate_02.dataset-contact-sheet.png").exists()


def test_cli_exits_two_on_an_unknown_transform(package, tmp_path, capsys):
    code = cli(package, tmp_path, transforms="sideways")
    assert code == 2
    assert "palette-cycle" in capsys.readouterr().err


def test_cli_exits_two_on_a_missing_input(package, tmp_path):
    _proof, svg, _manifest = package
    assert pv.main(["--proof", str(tmp_path / "nope.png"), "--svg", str(svg),
                    "--out", str(tmp_path / "d")]) == 2


def test_cli_exits_five_on_a_transform_failure(package, tmp_path, capsys):
    proof, _svg, _manifest = package
    broken = tmp_path / "broken.svg"
    broken.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>',
                      encoding="utf-8")
    code = pv.main(["--proof", str(proof), "--svg", str(broken),
                    "--out", str(tmp_path / "d")])
    assert code == 5
    assert "frame" in capsys.readouterr().err
