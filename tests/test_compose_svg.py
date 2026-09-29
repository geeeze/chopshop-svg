#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_compose_svg.py -- tests for the composition operation.

Composition turns several pieces into one printable artwork, and the whole point
of doing it here rather than in a raster editor is that THE VECTORS SURVIVE. So
the things pinned here are the ones that quietly stop being true if the merge
rasterises, loses a transform, or recolours something nobody asked it to:

* each vector layer lands as a ``<g>`` whose translate+scale comes from that
  layer's own natural size (``viewBox`` first, then ``width``/``height``),
* a ``hue`` survives as an ``feColorMatrix`` filter ELEMENT -- not a CSS filter,
  which a rasteriser would ignore while a browser preview looked right,
* a raster layer embeds as one inline ``<image>``, and a non-inline ``src`` is
  refused rather than resolved against the filesystem,
* the composite's palette snaps near misses and REPORTS far ones (forcing a
  nearest colour would silently recolour artwork), and
* a spec that cannot be merged is refused, not guessed at.
"""

import json
import os
import subprocess
import sys

import pytest
from lxml import etree

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
for path in (ROOT, SCRIPTS):
    if path not in sys.path:
        sys.path.insert(0, path)

import compose_svg  # noqa: E402

SVG_TAG = "{http://www.w3.org/2000/svg}"


# -------------------------------------------------------------------------- #
# Fixtures / helpers                                                         #
# -------------------------------------------------------------------------- #

def layer_svg(width="300", height="200", viewbox=None,
              body='<rect width="10" height="10" fill="#111111"/>'):
    """An inline layer SVG, with or without a viewBox or width/height."""
    attrs = ""
    if viewbox:
        attrs += ' viewBox="%s"' % viewbox
    if width:
        attrs += ' width="%s"' % width
    if height:
        attrs += ' height="%s"' % height
    return ('<svg xmlns="http://www.w3.org/2000/svg"%s>%s</svg>' % (attrs, body))


def vector(src=None, **over):
    layer = {"type": "svg", "src": src or layer_svg(), "x": 0, "y": 0,
             "w": 300, "h": 200, "opacity": 1.0, "hue": 0}
    layer.update(over)
    return layer


def raster(src="data:image/png;base64,iVBORw0KGgo=", **over):
    layer = {"type": "raster", "src": src, "x": 0, "y": 0, "w": 100, "h": 100,
             "opacity": 1.0, "hue": 0}
    layer.update(over)
    return layer


def spec(layers=None, **over):
    payload = {"width": 3000, "height": 3000, "background": "#ffffff",
               "layers": layers if layers is not None else [vector()]}
    payload.update(over)
    return payload


def parse(svg_text):
    """Parse compose's output -- proof it is well-formed XML."""
    return etree.fromstring(svg_text.encode("utf-8"))


def groups(root):
    return root.findall(SVG_TAG + "g")


def run_cli(argv, stdin=None):
    return subprocess.run(
        [sys.executable, os.path.join(SCRIPTS, "compose_svg.py")] + argv,
        input=stdin, capture_output=True, text=True, cwd=ROOT)


# -------------------------------------------------------------------------- #
# Natural size                                                               #
# -------------------------------------------------------------------------- #

class TestNaturalSize:
    def test_viewbox_wins_over_width_and_height(self):
        """A physical width ('100mm') says nothing about the drawing's units.

        The viewBox is the coordinate system the content was drawn in, so it is
        the size every layer is scaled by.
        """
        root = etree.fromstring(
            ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 5"'
             ' width="1000" height="500"/>').encode("utf-8"))
        assert compose_svg.natural_size(root) == (10.0, 5.0, 0.0, 0.0)

    def test_width_and_height_when_there_is_no_viewbox(self):
        root = etree.fromstring(
            ('<svg xmlns="http://www.w3.org/2000/svg" width="300"'
             ' height="200"/>').encode("utf-8"))
        assert compose_svg.natural_size(root) == (300.0, 200.0, 0.0, 0.0)

    def test_unit_suffix_is_accepted(self):
        root = etree.fromstring(
            ('<svg xmlns="http://www.w3.org/2000/svg" width="100mm"'
             ' height="50mm"/>').encode("utf-8"))
        assert compose_svg.natural_size(root) == (100.0, 50.0, 0.0, 0.0)

    def test_neither_viewbox_nor_size_is_a_spec_error(self):
        """There is nothing to scale by, and guessing a size is a print fault."""
        root = etree.fromstring(
            b'<svg xmlns="http://www.w3.org/2000/svg"/>')
        with pytest.raises(compose_svg.SpecError):
            compose_svg.natural_size(root)

    def test_non_positive_viewbox_is_a_spec_error(self):
        root = etree.fromstring(
            ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 0 10"/>')
            .encode("utf-8"))
        with pytest.raises(compose_svg.SpecError):
            compose_svg.natural_size(root)


# -------------------------------------------------------------------------- #
# Merging vector layers                                                      #
# -------------------------------------------------------------------------- #

class TestVectorLayers:
    def test_two_layers_become_two_groups_with_their_own_transforms(self):
        out = compose_svg.compose(spec(layers=[
            vector(layer_svg("300", "200"), x=100, y=50, w=600, h=400),
            vector(layer_svg("100", "100", viewbox="0 0 100 100"),
                   x=10, y=20, w=50, h=50, opacity=0.5),
        ]))
        merged = groups(parse(out))
        assert len(merged) == 2
        # 600/300 = 2, 400/200 = 2 -- the layer's natural size, not its w/h.
        assert merged[0].get("transform") == "translate(100,50) scale(2,2)"
        assert merged[1].get("transform") == "translate(10,20) scale(0.5,0.5)"
        # The layer's own content moved INTO the group, as vectors.
        for group in merged:
            assert group.find(SVG_TAG + "rect") is not None

    def test_transform_scales_by_natural_size_not_by_canvas(self):
        """A viewBox of 10x5 scaled to 100x50 is x10, not x(100/3000)."""
        out = compose_svg.compose(spec(layers=[
            vector(layer_svg("1000", "500", viewbox="0 0 10 5"),
                   w=100, h=50),
        ]))
        assert groups(parse(out))[0].get("transform") == \
            "translate(0,0) scale(10,10)"

    def test_viewbox_origin_is_moved_back_into_place(self):
        """Content drawn around (100, 100) must still land at the caller's x/y."""
        out = compose_svg.compose(spec(layers=[
            vector(layer_svg(viewbox="100 100 50 50"), w=50, h=50),
        ]))
        assert groups(parse(out))[0].get("transform") == \
            "translate(0,0) scale(1,1) translate(-100,-100)"

    def test_opacity_lands_on_the_group_not_the_shapes(self):
        out = compose_svg.compose(spec(layers=[
            vector(opacity=0.25)],
        ))
        group = groups(parse(out))[0]
        assert group.get("opacity") == "0.25"
        shape = group.find(SVG_TAG + "rect")
        assert shape.get("opacity") is None

    def test_opacity_is_clamped_to_the_unit_range(self):
        """1.5 is not a legal opacity; clipping it is friendlier than shipping it."""
        out = compose_svg.compose(spec(layers=[vector(opacity=1.5)]))
        assert groups(parse(out))[0].get("opacity") == "1"

    def test_layer_without_its_own_namespace_still_merges_as_svg(self):
        """A layer that omits xmlns must not become a foreign node in the tree."""
        out = compose_svg.compose(spec(layers=[
            vector('<svg width="10" height="10">'
                   '<rect width="5" height="5" fill="#111111"/></svg>'),
        ]))
        root = parse(out)
        group = groups(root)[0]
        assert group.find(SVG_TAG + "rect") is not None, (
            "the layer's content must be in the SVG namespace after the merge")
        assert SVG_TAG + "rect" in [child.tag for child in group]

    def test_layers_keep_their_paint_untouched_without_a_palette(self):
        src = layer_svg(body='<rect width="5" height="5" fill="#fe0101"/>')
        out = compose_svg.compose(spec(layers=[vector(src)]))
        assert "#fe0101" in out, "no palette in the spec means no snapping"


# -------------------------------------------------------------------------- #
# Hue                                                                        #
# -------------------------------------------------------------------------- #

class TestHue:
    def test_hue_becomes_a_fecolormatrix_filter_referenced_by_the_group(self):
        out = compose_svg.compose(spec(layers=[
            vector(hue=90),
            vector(hue=180),
        ]))
        root = parse(out)
        defs = root.find(SVG_TAG + "defs")
        assert defs is not None, "a hue must declare its filter in <defs>"
        filters = defs.findall(SVG_TAG + "filter")
        assert len(filters) == 2, "one filter per hue'd layer"
        ids = [filt.get("id") for filt in filters]
        assert len(set(ids)) == 2, "each layer needs its own filter id"
        matrices = [filt.find(SVG_TAG + "feColorMatrix") for filt in filters]
        assert [m.get("type") for m in matrices] == ["hueRotate", "hueRotate"]
        assert [m.get("values") for m in matrices] == ["90", "180"]
        # Referenced from the group: a filter a rasteriser can honour, unlike a
        # CSS-only 'filter:' declaration.
        assert [group.get("filter") for group in groups(root)] == \
            ["url(#%s)" % ids[0], "url(#%s)" % ids[1]]

    def test_hue_zero_or_absent_adds_no_filter(self):
        out = compose_svg.compose(spec(layers=[vector(hue=0)]))
        root = parse(out)
        assert root.find(SVG_TAG + "defs") is None
        assert groups(root)[0].get("filter") is None

    def test_a_full_turn_is_no_rotation(self):
        out = compose_svg.compose(spec(layers=[vector(hue=360)]))
        assert parse(out).find(SVG_TAG + "defs") is None

    def test_generated_filter_id_avoids_an_id_the_layer_already_uses(self):
        """Layers come from anywhere, so a generated id must be checked."""
        src = layer_svg(body='<rect id="compose-hue-0" width="5" height="5"'
                             ' fill="#111111"/>')
        out = compose_svg.compose(spec(layers=[vector(src, hue=45)]))
        root = parse(out)
        filter_id = root.find(SVG_TAG + "defs").find(
            SVG_TAG + "filter").get("id")
        assert filter_id != "compose-hue-0"
        assert groups(root)[0].get("filter") == "url(#%s)" % filter_id
        # The layer's own id is untouched; it is the GENERATED one that moved.
        assert groups(root)[0].find(SVG_TAG + "rect").get("id") == "compose-hue-0"


# -------------------------------------------------------------------------- #
# Raster layers                                                              #
# -------------------------------------------------------------------------- #

class TestRasterLayers:
    URI = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUg=="

    def test_raster_layer_embeds_as_one_inline_image(self):
        out = compose_svg.compose(spec(layers=[
            raster(self.URI, x=10, y=20, w=30, h=40, opacity=0.75),
        ]))
        root = parse(out)
        images = root.findall(SVG_TAG + "image")
        assert len(images) == 1
        image = images[0]
        assert image.get("href") == self.URI, "embedded verbatim, not re-encoded"
        assert image.get("x") == "10" and image.get("y") == "20"
        assert image.get("width") == "30" and image.get("height") == "40"
        assert image.get("opacity") == "0.75"
        assert groups(root) == [], "a raster layer is not wrapped in a group"

    def test_a_path_is_refused_because_layer_content_is_always_inline(self):
        """The merge must not read the filesystem for layer content."""
        with pytest.raises(compose_svg.SpecError) as info:
            compose_svg.compose(spec(layers=[raster("/tmp/layer.png")]))
        assert "data:" in str(info.value)

    def test_raster_hue_is_reported_not_silently_dropped(self):
        report = {}
        out = compose_svg.compose(
            spec(layers=[raster(self.URI, hue=90)]), report=report)
        assert parse(out) is not None
        assert parse(out).find(SVG_TAG + "defs") is None, "not applied yet"
        assert report["ignored"], "a hue it cannot apply must be reported"
        assert report["ignored"][0]["layer"] == 0
        assert "hue" in report["ignored"][0]["reason"]


# -------------------------------------------------------------------------- #
# Palette snapping                                                           #
# -------------------------------------------------------------------------- #

class TestPaletteSnapping:
    def test_near_miss_is_snapped(self):
        src = layer_svg(body='<rect width="5" height="5" fill="#fe0101"/>')
        report = {}
        out = compose_svg.compose(
            spec(layers=[vector(src)], palette=["#ff0000"]), report=report)
        assert "#ff0000" in out and "#fe0101" not in out
        assert report["snapped"] == [{"from": "#fe0101", "to": "#ff0000",
                                      "count": 1}]

    def test_far_colour_is_reported_and_left_alone(self):
        """Snapping a colour 100 units away silently recolours artwork."""
        src = layer_svg(body='<rect width="5" height="5" fill="#2e4a62"/>')
        report = {}
        out = compose_svg.compose(
            spec(layers=[vector(src)], palette=["#111111"]), report=report)
        assert "#2e4a62" in out, "a far colour must survive the merge"
        assert report["snapped"] == []
        finding = report["off_palette"][0]
        assert finding["colour"] == "#2e4a62"
        assert finding["nearest"] == "#111111"
        assert finding["distance"] > compose_svg.DEFAULT_TOLERANCE
        assert finding["count"] == 1

    def test_a_far_colour_in_one_layer_does_not_block_the_merge(self):
        """Off-palette is a finding, never a failure: the merge still returns."""
        src = layer_svg(body='<rect width="5" height="5" fill="#2e4a62"/>')
        out = compose_svg.compose(spec(layers=[vector(src)],
                                       palette=["#111111"]))
        assert parse(out).get("viewBox") == "0 0 3000 3000"

    def test_colour_declared_in_a_layer_stylesheet_is_snapped(self):
        """The snap runs through the CSS cascade, exactly as snap_colors does."""
        src = layer_svg(
            body='<rect class="band" width="5" height="5"/>',
        ).replace("<rect", "<style>.band { fill: #fe0101; }</style><rect")
        report = {}
        out = compose_svg.compose(
            spec(layers=[vector(src)], palette=["#ff0000"]), report=report)
        assert 'style="fill:#ff0000"' in out
        assert ".band { fill: #fe0101; }" in out, (
            "the layer's stylesheet rule must be left alone; the inline "
            "declaration out-ranks it")
        assert report["snapped"][0]["to"] == "#ff0000"

    def test_stroke_and_stop_color_are_snapped_too(self):
        src = layer_svg(body=(
            '<rect width="5" height="5" fill="none" stroke="#fe0101"/>'
            '<defs><linearGradient id="g"><stop offset="0"'
            ' stop-color="#fe0101"/></linearGradient></defs>'))
        out = compose_svg.compose(
            spec(layers=[vector(src)], palette=["#ff0000"]))
        assert "#fe0101" not in out
        root = parse(out)
        # Find the layer's own shapes, not the background rect compose added.
        stroked = [r for r in root.findall(".//" + SVG_TAG + "rect")
                   if r.get("stroke")]
        assert stroked and stroked[0].get("stroke") == "#ff0000"
        assert root.find(".//" + SVG_TAG + "stop").get("stop-color") == "#ff0000"

    def test_background_is_part_of_the_palette_pass(self):
        """The canvas is a fill: a palette that omits it reports it."""
        report = {}
        compose_svg.compose(
            spec(layers=[vector()], background="#123456", palette=["#ff0000"]),
            report=report)
        colours = [entry["colour"] for entry in report["off_palette"]]
        assert "#123456" in colours

    def test_palette_entries_must_be_real_colours(self):
        with pytest.raises(compose_svg.SpecError):
            compose_svg.compose(spec(layers=[vector()],
                                     palette=["not-a-colour"]))

    def test_absent_palette_is_not_an_error(self):
        report = {}
        compose_svg.compose(spec(layers=[vector()]), report=report)
        assert report["palette"] == []
        assert report["off_palette"] == []


# -------------------------------------------------------------------------- #
# The canvas and well-formedness                                             #
# -------------------------------------------------------------------------- #

class TestCanvas:
    def test_output_is_well_formed_with_the_requested_canvas(self):
        out = compose_svg.compose(spec(width=3000, height=2000))
        root = parse(out)
        assert root.tag == SVG_TAG + "svg"
        assert root.get("width") == "3000"
        assert root.get("height") == "2000"
        assert root.get("viewBox") == "0 0 3000 2000"

    def test_background_is_a_full_canvas_rect(self):
        root = parse(compose_svg.compose(
            spec(width=300, height=200, background="#ffffff")))
        rect = root.find(SVG_TAG + "rect")
        assert rect is not None
        assert rect.get("fill") == "#ffffff"
        assert (rect.get("width"), rect.get("height")) == ("300", "200")

    def test_transparent_background_adds_no_rect(self):
        root = parse(compose_svg.compose(spec(background="transparent")))
        assert root.find(SVG_TAG + "rect") is None

    def test_svg_root_declares_the_svg_namespace(self):
        out = compose_svg.compose(spec())
        assert 'xmlns="http://www.w3.org/2000/svg"' in out

    def test_layer_paint_verbatim_output_is_not_reencoded(self):
        data_uri = "data:image/png;base64,AAAA////"
        out = compose_svg.compose(spec(layers=[raster(data_uri)]))
        assert data_uri in out


# -------------------------------------------------------------------------- #
# Refusals                                                                   #
# -------------------------------------------------------------------------- #

class TestSpecErrors:
    @pytest.mark.parametrize("payload", [
        {"height": 10, "layers": []},                      # no width
        {"width": 10, "layers": []},                       # no height
        {"width": 10, "height": 10},                       # no layers
        {"width": 10, "height": 10, "layers": {}},         # layers not a list
        {"width": 0, "height": 10, "layers": []},          # non-positive canvas
        {"width": 10, "height": 10, "layers": [1]},        # layer not an object
        {"width": 10, "height": 10, "layers": [{"src": "<svg/>"}]},  # no type
        {"width": 10, "height": 10,
         "layers": [{"type": "gif", "src": "x"}]},         # unknown type
        {"width": 10, "height": 10,
         "layers": [{"type": "svg", "src": ""}]},          # no src
        {"width": 10, "height": 10,
         "layers": [{"type": "svg", "src": "<svg/>", "w": 1}]},  # no h
        {"width": 10, "height": 10,
         "layers": [{"type": "svg", "src": "<svg/>", "w": 0, "h": 1}]},
        {"width": 10, "height": 10,
         "layers": [{"type": "svg", "src": "<svg/>", "w": 1, "h": 1,
                     "opacity": "lots"}]},                 # non-numeric
        {"width": 10, "height": 10,
         "layers": [{"type": "svg", "src": "<svg width='1'/>", "w": 1,
                     "h": 1}]},                            # unparseable SVG
        {"width": 10, "height": 10,
         "layers": [{"type": "svg", "src": "<html/>", "w": 1, "h": 1}]},
        {"width": 10, "height": 10, "background": "not-a-colour",
         "layers": []},
        {"width": 10, "height": 10, "layers": [], "palette": "red"},
    ])
    def test_malformed_specs_are_refused(self, payload):
        with pytest.raises(compose_svg.SpecError):
            compose_svg.compose(payload)

    def test_a_spec_that_is_not_an_object_is_refused(self):
        with pytest.raises(compose_svg.SpecError):
            compose_svg.compose(["not", "a", "spec"])

    def test_spec_error_is_a_value_error(self):
        """Callers that only know ValueError still catch a refusal."""
        assert issubclass(compose_svg.SpecError, ValueError)

    def test_layer_svg_with_no_size_is_refused(self):
        """No viewBox and no width/height: nothing to scale by."""
        with pytest.raises(compose_svg.SpecError):
            compose_svg.compose(spec(layers=[
                vector('<svg xmlns="http://www.w3.org/2000/svg"/>')]))


# -------------------------------------------------------------------------- #
# CLI                                                                        #
# -------------------------------------------------------------------------- #

class TestCli:
    def test_spec_on_stdin_svg_on_stdout(self):
        proc = run_cli(["-"], stdin=json.dumps(spec()))
        assert proc.returncode == 0, proc.stderr
        assert parse(proc.stdout).get("viewBox") == "0 0 3000 3000"

    def test_out_path_receives_the_merge(self, tmp_path):
        out = tmp_path / "composite.svg"
        spec_file = tmp_path / "composite.spec.json"
        spec_file.write_text(json.dumps(spec()), encoding="utf-8")
        proc = run_cli([str(spec_file), "-o", str(out)])
        assert proc.returncode == 0, proc.stderr
        assert parse(out.read_text(encoding="utf-8")) is not None
        assert proc.stdout == "", "stdout stays empty when -o is given"

    def test_invalid_spec_is_exit_3(self):
        proc = run_cli(["-"], stdin=json.dumps({"width": 10}))
        assert proc.returncode == 3
        assert "invalid spec" in proc.stderr

    def test_malformed_json_is_exit_3(self):
        proc = run_cli(["-"], stdin="{not json")
        assert proc.returncode == 3
        assert "not valid JSON" in proc.stderr

    def test_unreadable_spec_file_is_exit_2(self, tmp_path):
        proc = run_cli([str(tmp_path / "nope.json")])
        assert proc.returncode == 2
        assert "cannot read" in proc.stderr

    def test_off_palette_colour_is_reported_without_failing(self, tmp_path):
        """Exit 0 with a report: the operator judges, the command does not fail."""
        src = layer_svg(body='<rect width="5" height="5" fill="#2e4a62"/>')
        spec_file = tmp_path / "s.json"
        spec_file.write_text(json.dumps(spec(layers=[vector(src)],
                                             palette=["#111111"])),
                             encoding="utf-8")
        proc = run_cli([str(spec_file)])
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "off palette: #2e4a62" in proc.stderr
        assert "#2e4a62" in proc.stdout, "the colour must survive in the SVG"

    def test_snapped_colour_is_reported_to_stderr_only(self, tmp_path):
        src = layer_svg(body='<rect width="5" height="5" fill="#fe0101"/>')
        proc = run_cli(["-"], stdin=json.dumps(spec(layers=[vector(src)],
                                                   palette=["#ff0000"])))
        assert proc.returncode == 0
        assert "#fe0101 -> #ff0000" in proc.stderr
        assert "#ff0000" in proc.stdout
