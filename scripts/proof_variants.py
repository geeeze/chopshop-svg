#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
proof_variants.py -- derive a deterministic family of "similar copies" of one
chosen proof, packaged as a training-dataset set.

The print check validates ONE SVG the operator picked and emits a proof PNG, a
print PDF and a JSON manifest.  This stage is the opt-in tail of that: given the
proof raster and the selected candidate SVG it writes every requested transform
of BOTH halves -- `raster/<transform>.png` next to `vector/<transform>.svg` -- so
what an augmented training set for this pipeline looks like can be eyeballed
(and used) without a build step in between.

It is invoked by `runner.py`'s `run_back`, gated on `validation.variants` in the
spec (see the `# dataset-seam` block there) -- NOT from `pipeline.sh`, which is a
frozen, tested deliverable.  This file also runs standalone; nothing in the
pipeline calls into it.

Two laws carry over from the rest of the repo:

* **It never picks a winner.**  Every requested transform is emitted; nothing is
  ranked, filtered or recommended.  The report lists variants in the vocabulary's
  own order, which is a reading order, not a quality judgement (same law as
  `palette_variants.py` and `tune_sweep.py`).
* **Nothing is synthesised.**  A spatial transform is a matrix/attribute rewrite
  of the root's frame; a colour transform is a `fill` / `stroke` / `stop-color`
  rewrite.  No path data is generated, ever -- there is no tracer and no model in
  this file.  A colour transform leaves geometry, node count and path data
  untouched, and a spatial transform leaves the colour set untouched; both
  directions are pinned by tests.

Determinism is a hard promise: every transform is a pure function of the input
bytes with no random component, so running the stage twice on the same inputs
produces byte-identical files (including `dataset.json` / `dataset.md`, which is
why neither carries a timestamp -- a clock in there would break the promise the
moment a caller compared two runs).

Honesty about the approximate transform: `hue-*` rotates the hue band through
PIL's 8-bit HSV, so a 90-degree turn is a 64-step offset and the result is
approximate at the last bit of each channel.  It is stated here rather than
rounded up to "exact".  A rotation (`rot90` / `rot270`) is a CLOCKWISE quarter
turn on both halves, so the raster and the vector name the same rotation --
Pillow calls the counter-clockwise turn `ROTATE_90`, which is why the raster side
maps the vocabulary onto Pillow's opposite constant.

Honesty about `palette-cycle`: an exact-match cycle can only permute pixels (or
declared fills) that EQUAL a palette colour.  On a real proof -- tens of
thousands of rendered colours against a six-colour declared palette -- almost
nothing matches exactly, and the first version of this stage therefore emitted a
file that was visually the proof: a duplicate, hashed and counted as an
augmentation.  The exact pass is now MEASURED, and below
:data:`MIN_VARIANT_CHANGE` (0.5% of the pixel mass, or of the paint values) the
cycle is redone through PIL's nearest-colour search over the palette and
recorded in the dataset notes as a REQUANTISATION rather than a permutation; if
even that cannot move the image the transform is refused, not emitted.  More
generally no colour transform may emit a duplicate silently: every transform
reports what it moved when the caller passes a ``notes`` list, and says plainly
when its output is visually its input.

Usage
-----
::

    python3 scripts/proof_variants.py --proof 05_final/art.proof.png \\
        --svg 02_traced/art/candidate_03.svg --out 05_final/candidate_03.dataset
    python3 scripts/proof_variants.py --proof p.png --svg c.svg --out D \\
        --transforms flip-h,invert --sheet

Exit codes: 0 = every requested variant written, 2 = bad usage, 5 = a transform
failed (the message names the transform).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from lxml import etree

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _path in (HERE, ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import palette_variants as pv                      # noqa: E402
import snap_colors                                 # noqa: E402
import validate_svg                                # noqa: E402

# The vocabulary is ORDERED and CLOSED.  `dataset.json` lists variants in this
# order whatever order the caller asked in, so two runs of the same request are
# comparable; and a name outside the list is refused with the list quoted, never
# skipped (a silently missing variant reads as a broken stage).
TRANSFORMS = (
    "flip-h", "flip-v", "rot180", "rot90", "rot270", "transpose",
    "invert", "hue-90", "hue-180", "hue-270", "channel-swap", "palette-cycle",
)
SPATIAL_TRANSFORMS = frozenset(
    ("flip-h", "flip-v", "rot180", "rot90", "rot270", "transpose"))
COLOUR_TRANSFORMS = frozenset(t for t in TRANSFORMS
                              if t not in SPATIAL_TRANSFORMS)
HUE_DEGREES = {"hue-90": 90, "hue-180": 180, "hue-270": 270}

SVG_NS = "http://www.w3.org/2000/svg"
PROOF_SUFFIX = ".proof.png"
DATASET_SUFFIX = ".dataset"
DEFAULT_COLUMNS = 4
DEFAULT_TILE = 360
# Raster colours a palette-less `palette-cycle` will touch.  Flat printed art has
# a handful; a photographic source has millions, and cycling those is neither
# meaningful nor cheap, so the count is capped and reported.
NO_PALETTE_CYCLE_LIMIT = 32
MAX_DISTINCT_COLOURS = 1 << 20


class TransformError(RuntimeError):
    """A transform could not be applied to this input (the CLI exits 5)."""


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=False)
        handle.write("\n")


def as_hex(raw):
    """A plain ``#rrggbb`` or None -- same contract palette_variants uses."""
    return pv.as_hex(raw)


def hex_of(rgb) -> str:
    red, green, blue = (int(round(value)) for value in tuple(rgb)[:3])
    return "#%02x%02x%02x" % (max(0, min(255, red)),
                              max(0, min(255, green)),
                              max(0, min(255, blue)))


def invert_hex(colour: str) -> str:
    """The full negative of one colour."""
    red, green, blue = snap_colors.hex_to_rgb(colour)
    return "#%02x%02x%02x" % (255 - red, 255 - green, 255 - blue)


def swap_channels_hex(colour: str) -> str:
    """R <-> B, leaving G where it is."""
    red, green, blue = snap_colors.hex_to_rgb(colour)
    return "#%02x%02x%02x" % (blue, green, red)


def hue_offset(degrees: int) -> int:
    """The 8-bit hue step for a turn of ``degrees``.

    PIL's HSV mode keeps hue in ONE 8-bit channel spanning a full 360 turn, so
    90 degrees is 255/4 = 63.75, rounded to 64 steps.  The shift is therefore
    approximate at the last bit of each channel -- stated, not hidden.  Both
    halves of a pair go through this same function, so a raster and a vector
    variant of one colour agree with each other.
    """
    return int(round(degrees / 360.0 * 255.0)) % 256


def cycle_mapping(colours) -> dict:
    """``{colour: the next colour}`` over an explicit, ordered colour list.

    The order is whatever the caller supplies -- the manifest's screens/palette
    in that order, or the artwork's own colours sorted for a palette-less run.
    Fewer than two colours cannot cycle, so the mapping is empty and the caller
    records why.
    """
    items = [colour for colour in colours if colour]
    if len(items) < 2:
        return {}
    return {items[index]: items[(index + 1) % len(items)]
            for index in range(len(items))}


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------

def variant_plan(requested=None) -> list:
    """Resolve a request against the vocabulary; raise on an unknown name.

    ``None`` / ``True`` mean "the whole vocabulary".  A string is split on
    commas, a list/tuple/set is taken as given, and a spec block
    (``{"enabled": bool, "transforms": [...]}``) has its ``transforms`` read.
    ``False`` means "nothing" -- that is what an opted-out spec resolves to.

    The result is always in :data:`TRANSFORMS` order and de-duplicated, so the
    same request always produces the same plan (and therefore the same bytes).
    """
    if requested is None or requested is True:
        return list(TRANSFORMS)
    if requested is False:
        return []
    if isinstance(requested, dict):
        # A spec block is a REQUEST: one that says enabled=false asks for
        # nothing, whatever it lists.
        if not requested.get("enabled", True):
            return []
        names = requested.get("transforms")
        if names is None:
            return list(TRANSFORMS)
    elif isinstance(requested, str):
        names = requested.split(",")
    elif isinstance(requested, (list, tuple, set, frozenset)):
        names = list(requested)
    else:
        raise ValueError("cannot read a transform request from %r -- give a "
                         "list of names, or true/false" % (requested,))

    wanted = set()
    unknown = set()
    for name in names:
        name = str(name).strip()
        if not name:
            continue
        if name in TRANSFORMS:
            wanted.add(name)
        else:
            unknown.add(name)
    if unknown:
        raise ValueError(
            "unknown transform(s): %s -- the vocabulary is: %s"
            % (", ".join(sorted(unknown)), ", ".join(TRANSFORMS)))
    return [name for name in TRANSFORMS if name in wanted]


# --------------------------------------------------------------------------
# Raster half -- PIL only
# --------------------------------------------------------------------------

def _spatial_raster(image, transform: str):
    """Flips and rotations are lossless in the PIXELS: no resampling.

    The FILE is re-encoded, and that is not the same claim. Pillow's PNG encoder
    is not Inkscape's, so the bytes differ from the proof even when the pixels do
    not -- which is why the round-trip test asserts pixel identity plus
    determinism rather than byte identity with an upstream encoder this pipeline
    does not control. What the re-encode must NOT lose is the container's own
    metadata: `transform_raster` carries the source's dpi, text chunks and ICC
    profile through, because for a print artifact the physical resolution is part
    of the content.

    The vocabulary's quarter turns are CLOCKWISE on both halves of a pair, and
    Pillow's ``ROTATE_90`` is the counter-clockwise one, so ``rot90`` uses
    Pillow's ``ROTATE_270``.  Naming the same turn on both sides matters more
    than matching Pillow's constant names.
    """
    from PIL import Image, ImageOps

    if transform == "flip-h":
        return ImageOps.mirror(image)
    if transform == "flip-v":
        return ImageOps.flip(image)
    if transform == "rot180":
        return image.transpose(Image.Transpose.ROTATE_180)
    if transform == "rot90":
        return image.transpose(Image.Transpose.ROTATE_270)
    if transform == "rot270":
        return image.transpose(Image.Transpose.ROTATE_90)
    if transform == "transpose":                      # main-diagonal mirror
        return image.transpose(Image.Transpose.TRANSPOSE)
    raise TransformError("no spatial raster rule for %r" % transform)


def hue_shift_image(image_rgb, degrees: int):
    """Rotate the hue band of an RGB image; saturation and value are kept."""
    from PIL import Image

    offset = hue_offset(degrees)
    table = [(value + offset) % 256 for value in range(256)]
    hue, sat, val = image_rgb.convert("HSV").split()
    return Image.merge("HSV", (hue.point(table), sat, val)).convert("RGB")


def hue_shift_hex(colour: str, degrees: int) -> str:
    """One colour through the SAME 8-bit path the raster half uses."""
    from PIL import Image

    probe = Image.new("RGB", (1, 1), snap_colors.hex_to_rgb(colour))
    return hex_of(hue_shift_image(probe, degrees).getpixel((0, 0)))


def remap_exact(image_rgb, mapping: dict):
    """Replace exact colour matches, PIL only.

    A pixel is replaced when ALL THREE channels match, which needs the three
    per-channel masks multiplied together -- a single ``difference(...).convert(
    "L")`` collapses channels into a weighted sum and would also match a colour
    that merely *looks* close.  Every mask is taken from the ORIGINAL channels,
    so a chain (a->b, b->c) cannot cascade: a pixel that was ``a`` is not
    treated as ``b`` by the second step.
    """
    from PIL import Image, ImageChops

    channels = image_rgb.split()
    size = image_rgb.size
    out = image_rgb
    for source, target in mapping.items():
        if source == target:
            continue
        wanted = snap_colors.hex_to_rgb(source)
        mask = Image.new("L", size, 255)
        for index, channel in enumerate(channels):
            flat = Image.new("L", size, wanted[index])
            hit = ImageChops.difference(channel, flat).point(
                lambda value: 255 if value == 0 else 0)
            mask = ImageChops.multiply(mask, hit)
        out = Image.composite(
            Image.new("RGB", size, snap_colors.hex_to_rgb(target)), out, mask)
    return out


def distinct_colours(image_rgb, limit: int = MAX_DISTINCT_COLOURS):
    """(colours, overflowed) -- the image's own colours, most-used first.

    Deterministic: ties are broken by the colour value itself, so the list does
    not depend on dictionary or pixel order.
    """
    entries = image_rgb.getcolors(maxcolors=limit)
    if entries is None:
        return [], True
    ranked = sorted(entries, key=lambda item: (-item[0], item[1]))
    return [hex_of(rgb) for _count, rgb in ranked], False


def norm_hex(colour: str) -> str:
    """Uppercase ``#RRGGBB``, so a cycle key and a nearest lookup cannot miss.

    ``hex_of`` (from a pixel) and ``as_hex`` (from a manifest) both produce a
    canonical form but not the SAME one; a mapping keyed on one and looked up
    with the other would silently miss every entry.
    """
    return colour.upper() if colour.startswith("#") else colour


# --------------------------------------------------------------------------
# Did this variant actually become a different image?
# --------------------------------------------------------------------------
#
# A variant that is visually identical to its input is worse than a missing one:
# it is hashed, counted and shipped as an augmentation, and it teaches a
# training set that the same picture is a different picture.  Every transform
# therefore MEASURES what it moved (this file's callers pass a `notes` list to
# have that recorded), and `palette-cycle` -- the one transform that can fail
# this way on real art without any bug at all -- re-runs itself through a
# nearest-colour requantisation before it is allowed to emit a near-duplicate.

# Below this share of the proof's pixels a variant is a duplicate, not a
# training example.
#
# Measured on TWO real proofs, and the figure depends on the proof so the
# artifacts are named rather than one number presented as universal:
#
#   * 3200x3200, ~69k rendered colours vs a six-colour palette -> an exact-match
#     cycle moved 0.069% of the pixel mass;
#   * 4688x2694, 87880 rendered colours, 581 paint values -> 0.013% of the pixel
#     mass and 0 of 581 paint values.
#
# Both are negligible against this floor, which is the point: on a
# continuous-tone proof the exact-match cycle is effectively a no-op whatever its
# size, while invert/channel-swap/hue move ~100%. A flat, few-colour proof is the
# opposite case, where the exact cycle does the work and no requantisation fires.
MIN_VARIANT_CHANGE = 0.005
PALETTE_LIMIT = 256          # PIL palettes are 256 entries; a screen set is ~6


def changed_fraction(before_rgb, after_rgb) -> float:
    """Share of pixels that differ AT ALL between two same-size RGB images."""
    from PIL import ImageChops

    diff = ImageChops.difference(before_rgb, after_rgb)
    red, green, blue = diff.split()
    # Per channel, then the maximum: `diff.convert("L")` collapses the three
    # into a weighted sum, so a pixel differing by 1 in blue alone would read as
    # unchanged and a small-but-real change could disappear.
    biggest = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    histogram = biggest.point(lambda value: 255 if value else 0).histogram()
    total = before_rgb.width * before_rgb.height
    return (histogram[255] / float(total)) if total else 0.0


def note(notes, text: str) -> None:
    """Record a plain-language note when the caller asked for notes."""
    if notes is not None:
        notes.append(text)


def report_raster_change(source_rgb, out_rgb, transform: str, notes) -> float:
    """Measure a finished variant against its source and note a duplicate."""
    share = changed_fraction(source_rgb, out_rgb)
    if share < MIN_VARIANT_CHANGE:
        if transform in SPATIAL_TRANSFORMS:
            note(notes, "%s changed %.3f%% of the pixels: the art is symmetric "
                        "under this transform, so this variant duplicates the "
                        "proof" % (transform, share * 100.0))
        else:
            note(notes, "%s changed %.3f%% of the pixels: this variant is "
                        "visually the proof (the transform had nothing to "
                        "move), so it is not a usable augmentation"
                 % (transform, share * 100.0))
    return share


def palette_image(palette):
    """A P-mode image carrying the palette, for PIL's nearest-colour search."""
    from PIL import Image

    entries = []
    for colour in list(palette)[:PALETTE_LIMIT]:
        entries.extend(snap_colors.hex_to_rgb(colour))
    if not entries:
        raise TransformError("palette-cycle needs a colour to cycle")
    entries.extend([0, 0, 0] * (PALETTE_LIMIT - len(entries) // 3))
    image = Image.new("P", (1, 1))
    image.putpalette(entries)
    return image


def requantise(image_rgb, palette):
    """Collapse an RGB image onto the palette by nearest colour, PIL only.

    PIL's own search, in C, deterministic, no dithering (error diffusion would
    break flatness and make the result depend on scan order).
    """
    from PIL import Image

    return image_rgb.quantize(palette=palette_image(palette),
                              dither=Image.Dither.NONE).convert("RGB")


def nearest_palette_map(colours, palette) -> dict:
    """{colour: its nearest palette entry}, by PIL's nearest-colour search.

    Both halves go through PIL, so the raster and the vector make the SAME
    nearest decision for the same colour and a pair stays a pair; a hand-rolled
    distance could disagree exactly where it matters (a colour equidistant
    between two screens).  The probes are one strip, so the lookup is a single
    call rather than one per colour.
    """
    from PIL import Image

    ordered = list(colours)
    if not ordered:
        return {}
    strip = Image.new("RGB", (len(ordered), 1))
    strip.putdata([snap_colors.hex_to_rgb(colour) for colour in ordered])
    reduced = strip.quantize(palette=palette_image(palette),
                             dither=Image.Dither.NONE).convert("RGB")
    # Raw bytes rather than getdata(): PIL deprecates the pixel-sequence API.
    raw = reduced.tobytes()
    return {colour: hex_of(raw[index * 3:index * 3 + 3])
            for index, colour in enumerate(ordered)}


def cycle_colours(palette, colours=None, notes=None, transform="palette-cycle"):
    """(cycle order, where it came from) for ``palette-cycle``.

    A manifest/explicit palette is the honest order.  With none, the artwork's
    own colours (sorted and capped) are all there is to cycle.  Every entry is
    returned as uppercase ``#RRGGBB``: a palette from a manifest is uppercase
    while the colours read out of an SVG or a raster are canonical lowercase, and
    a mapping keyed on one case and looked up with the other misses EVERY entry
    -- which is a silent no-op, the one failure this stage must not have.
    """
    wanted = [norm_hex(colour) for colour in (as_hex(c) for c in (palette or []))
              if colour]
    if wanted:
        return wanted, "the declared palette (%d colours)" % len(wanted)
    colours = list(colours or [])
    if len(colours) > NO_PALETTE_CYCLE_LIMIT:
        note(notes, "%s: no declared palette, so the cycle walks the artwork's "
                    "own %d colours (the %d most used, sorted)"
             % (transform, len(colours), NO_PALETTE_CYCLE_LIMIT))
    return [norm_hex(colour) for colour in
            sorted(colours[:NO_PALETTE_CYCLE_LIMIT])], \
        "the artwork's own colours (sorted, %d)" % min(
            len(colours), NO_PALETTE_CYCLE_LIMIT)


def cycle_raster(rgb, palette, notes=None, colours=None):
    """``palette-cycle`` on an RGB image: permute if it can, requantise if not.

    An exact-match cycle only touches pixels that EQUAL a palette colour.  On a
    real proof -- tens of thousands of rendered colours against a six-colour
    declared palette -- almost no pixel matches exactly, so the exact pass
    produced a file that was visually the proof: a duplicate in the training set
    and the worst possible output for this stage.  The exact pass is therefore
    measured, and below :data:`MIN_VARIANT_CHANGE` the cycle is redone through a
    nearest-colour requantisation onto the palette -- the honest reading of
    "cycle the screens" for continuous-tone art, recorded as a note because it
    is a requantisation, not a permutation of flats.  If even that cannot move
    the image (a palette with nothing to cycle, e.g. two identical entries) the
    transform is REFUSED rather than emitted as a duplicate.
    """
    wanted, source = cycle_colours(palette, colours=colours, notes=notes)
    if len(set(wanted)) < 2:
        raise TransformError(
            "palette-cycle has nothing to cycle: %s gives %s -- it needs two "
            "different colours to make a permutation"
            % (source, ", ".join(wanted) or "none"))
    mapping = cycle_mapping(wanted)
    out = remap_exact(rgb, mapping)
    share = changed_fraction(rgb, out)
    if share >= MIN_VARIANT_CHANGE:
        note(notes, "palette-cycle permuted %s: %.2f%% of the pixel mass "
                    "changed" % (source, share * 100.0))
        return out

    requantised = remap_exact(requantise(rgb, wanted), mapping)
    share2 = changed_fraction(rgb, requantised)
    if share2 < MIN_VARIANT_CHANGE:
        raise TransformError(
            "palette-cycle cannot change this raster: an exact cycle of %s "
            "moved %.3f%% of the pixel mass and a nearest-colour "
            "requantisation onto it moved %.3f%% -- emitting it would put a "
            "duplicate of the proof in the dataset"
            % (source, share * 100.0, share2 * 100.0))
    note(notes, "palette-cycle could not permute this proof: %s does not appear as "
                "exact pixel values here (an exact cycle "
                "moved only %.3f%% of the pixel mass), so the cycle was applied "
                "through PIL's nearest-colour search — this variant is a "
                "REQUANTISATION onto the palette (%.2f%% of the pixel mass "
                "changed), not a permutation of existing flats"
         % (source, share * 100.0, share2 * 100.0))
    return requantised


def transform_raster(image_bytes: bytes, transform: str, *, palette=None,
                     notes=None) -> bytes:
    """Apply one transform to a raster; return PNG bytes.

    ``palette`` (the manifest's screen colours, in order) is what
    ``palette-cycle`` walks.  Without it the cycle falls back to the raster's
    own most-used colours, capped at :data:`NO_PALETTE_CYCLE_LIMIT` -- a real
    palette is the honest order, the fallback exists only so a manifest-less run
    still produces a variant instead of a silent identity.

    ``notes`` (optional list) collects what the transform actually did: the
    share of the proof it moved, and plainly-worded warnings when the result is
    a duplicate of the proof rather than a new view of it.
    """
    if transform not in TRANSFORMS:
        raise ValueError("unknown transform %r -- the vocabulary is: %s"
                         % (transform, ", ".join(TRANSFORMS)))
    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as opened:
        image = opened.copy()                      # detach from the stream
        # THE PROOF'S OWN CONTAINER METADATA, captured before the pixels are
        # transformed. A proof is a PRINT artifact: the render's physical
        # resolution (pHYs, "300 dpi") is the difference between a production
        # file and a picture, and re-encoding with no pnginfo silently dropped it
        # from every one of the 12 variants. Measured before this was carried:
        # the source declares (299.9994, 299.9994) and each variant declared
        # NOTHING. The text chunks are provenance of the render the pixels came
        # from, so they travel with them; an ICC profile is not optional for
        # colour-managed print work, so it is carried too when the source has one.
        source_info = dict(opened.info)
    source_dpi = source_info.get("dpi")
    source_icc = source_info.get("icc_profile")
    source_text = {}
    for key, value in source_info.items():
        # Only the text chunks: `dpi` (a tuple) and `icc_profile` (bytes) are
        # handled above, and PngInfo.add_text takes strings.
        if isinstance(value, str) and key not in ("dpi", "icc_profile"):
            source_text[key] = value

    if transform in SPATIAL_TRANSFORMS:
        out = _spatial_raster(image, transform)
    else:
        # Colour work happens on RGB; an alpha channel is carried across rather
        # than flattened, so a transparent proof keeps its transparency.
        alpha = None
        if image.mode in ("RGBA", "LA") or "transparency" in image.info:
            alpha = image.convert("RGBA").getchannel("A")
        rgb = image.convert("RGB")

        if transform == "invert":
            from PIL import ImageOps
            out = ImageOps.invert(rgb)
        elif transform == "channel-swap":
            red, green, blue = rgb.split()
            out = Image.merge("RGB", (blue, green, red))
        elif transform in HUE_DEGREES:
            out = hue_shift_image(rgb, HUE_DEGREES[transform])
        elif transform == "palette-cycle":
            colours, _overflow = distinct_colours(rgb)
            out = cycle_raster(rgb, palette, notes=notes, colours=colours)
        else:                                       # pragma: no cover - guarded
            raise TransformError("no colour raster rule for %r" % transform)

        if alpha is not None:
            out = out.convert("RGBA")
            out.putalpha(alpha)

    # Measure only when someone asked for the measurement: the diff costs a pass
    # over the whole proof and a caller that passes no notes has not asked.
    if notes is not None:
        with Image.open(io.BytesIO(image_bytes)) as opened:
            report_raster_change(opened.convert("RGB"), out.convert("RGB"),
                                 transform, notes)

    buffer = io.BytesIO()
    # Re-attach what the container carried. See the capture above: without these
    # the variant is pixel-identical to the proof and metadata-identical to a
    # screenshot, which for a print dataset is the wrong half to keep.
    save_kwargs = {}
    if source_dpi:
        save_kwargs["dpi"] = source_dpi
    if source_icc:
        save_kwargs["icc_profile"] = source_icc
    if source_text:
        from PIL import PngImagePlugin
        info = PngImagePlugin.PngInfo()
        for key, value in source_text.items():
            info.add_text(key, value)
        save_kwargs["pnginfo"] = info
    out.save(buffer, format="PNG", **save_kwargs)
    return buffer.getvalue()


# --------------------------------------------------------------------------
# Vector half -- lxml, attribute rewrites only
# --------------------------------------------------------------------------

def _parser():
    return etree.XMLParser(resolve_entities=False, no_network=True,
                           recover=False, huge_tree=True)


def parse_svg_bytes(svg_bytes: bytes):
    """(tree, root, styles) for SVG bytes, same parser contract as the rest."""
    tree = etree.parse(io.BytesIO(svg_bytes), _parser())
    root = tree.getroot()
    return tree, root, validate_svg.StyleContext(root)


def _number(value: float) -> str:
    """Shortest honest spelling of a frame coordinate ('120', '-240.5')."""
    if float(value).is_integer():
        return "%d" % int(value)
    return ("%.4f" % value).rstrip("0").rstrip(".")


def _frame(root):
    """(minx, miny, width, height) of the root's own user-unit frame."""
    viewbox = root.get("viewBox")
    if viewbox:
        parts = [part for part in viewbox.replace(",", " ").split() if part]
        if len(parts) == 4:
            try:
                minx, miny, width, height = (float(part) for part in parts)
                if width > 0 and height > 0:
                    return minx, miny, width, height
            except ValueError:
                pass
    width = _svg_length(root.get("width"))
    height = _svg_length(root.get("height"))
    if width and height:
        return 0.0, 0.0, width, height
    raise TransformError(
        "cannot frame the document: the root has no usable viewBox and no "
        "numeric width/height")


def _svg_length(raw):
    """A numeric length, units stripped; None when it is missing/percentage."""
    if not raw:
        return None
    text = str(raw).strip().rstrip("%")
    for unit in ("px", "pt", "mm", "cm", "in", "pc", "q", "em", "ex"):
        if text.lower().endswith(unit):
            text = text[: -len(unit)]
            break
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value > 0 else None


def _length_text(value, template):
    """Keep the source's unit suffix when swapping width and height."""
    if template:
        for unit in ("px", "pt", "mm", "cm", "in", "pc", "q", "em", "ex"):
            if str(template).lower().endswith(unit):
                return "%s%s" % (_number(value), str(template)[-len(unit):])
    return _number(value)


def spatial_rules(transform: str, minx, miny, width, height):
    """(transform attribute, new viewBox or None, swap width/height?).

    Mirrors and half turns keep the frame exactly as it was; the three diagonal
    moves map the frame onto a rectangle with the axes exchanged, so the viewBox
    is rewritten (and width/height swapped with it) rather than cropping the art.
    Every case keeps the artwork inside the frame -- nothing is clipped and
    nothing is padded.
    """
    if transform == "flip-h":
        return ("translate(%s 0) scale(-1 1)" % _number(2 * minx + width),
                None, False)
    if transform == "flip-v":
        return ("translate(0 %s) scale(1 -1)" % _number(2 * miny + height),
                None, False)
    if transform == "rot180":
        return ("translate(%s %s) rotate(180)"
                % (_number(2 * minx + width), _number(2 * miny + height)),
                None, False)
    if transform == "transpose":                      # x,y -> y,x
        return ("matrix(0 1 1 0 0 0)",
                " ".join(_number(v) for v in (miny, minx, height, width)), True)
    if transform == "rot90":                          # x,y -> -y,x
        return ("rotate(90)",
                " ".join(_number(v) for v in (-(miny + height), minx,
                                              height, width)), True)
    if transform == "rot270":                         # x,y -> y,-x
        return ("rotate(270)",
                " ".join(_number(v) for v in (miny, -(minx + width),
                                              height, width)), True)
    raise TransformError("no spatial vector rule for %r" % transform)


def _wrap(root, transform_attr: str):
    """Wrap the root's content in ONE group carrying the transform.

    The transform goes on a group rather than on the root element itself: SVG
    1.1 (which every renderer, Inkscape included, still implements) does not
    apply `transform` to the outermost svg, so a root attribute is silently
    ignored in the very renderer this pipeline prints through.  A group is
    honoured everywhere and it leaves path data, node count and colours exactly
    as they were -- the only thing that changes is the frame the content sits in.
    """
    ns = None
    if isinstance(root.tag, str) and root.tag.startswith("{"):
        ns = root.tag[1:].split("}", 1)[0]
    group = etree.Element("{%s}g" % ns if ns else "g")
    group.set("transform", transform_attr)
    for child in list(root):
        root.remove(child)
        group.append(child)
    root.append(group)
    return group


def _ensure_frame(root) -> None:
    """Guarantee the result carries width, height and viewBox.

    A transform needs a frame, and so does every consumer downstream (the print
    check included), so a document that declares only a viewBox gets its pixel
    size from it and one that declares only width/height gets a matching
    viewBox.  On a real tracer candidate all three are already there, which
    makes this a no-op; it exists so the output of EVERY transform satisfies the
    same contract instead of depending on what the input happened to carry.
    """
    minx, miny, width, height = _frame(root)
    if not root.get("viewBox"):
        root.set("viewBox", " ".join(_number(v) for v in (minx, miny,
                                                          width, height)))
    if not root.get("width"):
        root.set("width", _number(width))
    if not root.get("height"):
        root.set("height", _number(height))


def cycle_vector(svg_bytes, colour_counts, palette, notes=None):
    """(tree, ``palette-cycle`` applied) -- permute if it can, nearest if not.

    Same reasoning as :func:`cycle_raster`, one level up: an exact cycle of a
    declared palette only rewrites elements whose paint VALUE is in that
    palette.  A traced continuous-tone candidate declares hundreds of colours
    that are not, so on two real candidates the exact pass rewrote 0 of 339 and
    0 of 581 paint values respectively and emitted a copy of the source as a
    "variant" (the counts differ because the artifacts do -- name the one you
    measured rather than quoting a figure as if it were universal). So it is
    measured, and below :data:`MIN_VARIANT_CHANGE` the cycle goes through PIL's
    nearest-colour search over the palette (recorded as a note), or the transform
    is refused.
    """
    colours = sorted(colour_counts)
    wanted, source = cycle_colours(palette, colours=colours, notes=notes)
    if len(set(wanted)) < 2:
        raise TransformError(
            "palette-cycle has nothing to cycle: %s gives %s -- it needs two "
            "different colours to make a permutation"
            % (source, ", ".join(wanted) or "none"))

    total = sum(colour_counts.values()) or 1
    # The declared colours come back in canonical lowercase while a palette from
    # the manifest is uppercase, so both sides are normalised before the lookup:
    # keyed on one case and looked up with the other, EVERY entry misses and the
    # exact permutation rewrites nothing even on flat art.  (That was a second,
    # independent cause of the real no-op.)
    base = cycle_mapping(wanted)
    exact = {}
    for colour in colours:
        target = base.get(norm_hex(colour))
        if target and norm_hex(target) != norm_hex(colour):
            exact[colour] = target

    tree, root, styles = parse_svg_bytes(svg_bytes)
    changes, _where = pv.apply_mapping(root, styles, exact)
    exact_changed = sum(changes.values())
    share = exact_changed / float(total)
    if share >= MIN_VARIANT_CHANGE:
        note(notes, "palette-cycle permuted %s: %d of %d paint values changed"
             % (source, exact_changed, total))
        return tree, root

    nearest = nearest_palette_map(colours, wanted)
    mapping = {}
    for colour in colours:
        target = base.get(norm_hex(nearest.get(colour, "")))
        if target and norm_hex(target) != norm_hex(colour):
            mapping[colour] = target
    # Re-parsed, not re-mapped in place: the first pass already rewrote some
    # fills, and applying a second mapping to that tree would cascade (a fill
    # moved to a palette colour would then be moved again by the next entry).
    tree, root, styles = parse_svg_bytes(svg_bytes)
    changes, _where = pv.apply_mapping(root, styles, mapping)
    nearest_changed = sum(changes.values())
    if nearest_changed / float(total) < MIN_VARIANT_CHANGE:
        raise TransformError(
            "palette-cycle cannot change this SVG: an exact cycle of %s "
            "rewrote %d of %d paint values and a nearest-colour remap onto it "
            "rewrote %d -- emitting it would put a copy of the candidate in "
            "the dataset"
            % (source, exact_changed, total, nearest_changed))
    note(notes, "palette-cycle could not permute this candidate: %s is not what this "
                "SVG declares (an exact cycle "
                "rewrote only %d of %d paint values), so every declared colour "
                "was sent to the cycle target of its NEAREST screen (PIL's "
                "nearest-colour search) — a requantisation onto the palette, not "
                "a permutation of the declared colours (%d of %d paint values "
                "changed)" % (source, exact_changed, total, nearest_changed,
                              total))
    return tree, root


def transform_svg(svg_bytes: bytes, transform: str, *, palette=None,
                  notes=None) -> bytes:
    """Apply one transform to an SVG; return well-formed SVG bytes.

    A spatial transform rewrites the root's frame (viewBox/width/height) and
    wraps the content in one transformed group; the colours and the path data
    are untouched.  A colour transform rewrites ONLY ``fill``/``stroke``/
    ``stop-color`` through the CSS cascade, reusing palette_variants' tested
    mapping machinery; geometry, node count and path data are untouched.

    ``notes`` (optional list) collects what the transform did, including a plain
    statement when the colour result is visually the input (nothing to move).
    """
    if transform not in TRANSFORMS:
        raise ValueError("unknown transform %r -- the vocabulary is: %s"
                         % (transform, ", ".join(TRANSFORMS)))
    tree, root, styles = parse_svg_bytes(svg_bytes)

    if transform in SPATIAL_TRANSFORMS:
        minx, miny, width, height = _frame(root)
        attribute, viewbox, swap = spatial_rules(transform, minx, miny,
                                                 width, height)
        if viewbox is not None:
            root.set("viewBox", viewbox)
        if swap:
            old_width, old_height = root.get("width"), root.get("height")
            if old_width and old_height:
                root.set("width", _length_text(height, old_width))
                root.set("height", _length_text(width, old_height))
        _wrap(root, attribute)
    else:
        colour_counts = pv.declared_colours(root, styles)
        if transform == "palette-cycle":
            # Returns its own (tree, root): it may have had to re-parse, and the
            # frame fix-up below must run on the tree that actually gets written
            # (a re-parsed tree's root is a different object).
            tree, root = cycle_vector(svg_bytes, colour_counts, palette,
                                      notes=notes)
        else:
            if transform == "invert":
                mapping = {colour: invert_hex(colour)
                           for colour in colour_counts}
            elif transform == "channel-swap":
                mapping = {colour: swap_channels_hex(colour)
                           for colour in colour_counts}
            elif transform in HUE_DEGREES:
                mapping = {colour: hue_shift_hex(colour, HUE_DEGREES[transform])
                           for colour in colour_counts}
            else:                                   # pragma: no cover - guarded
                raise TransformError("no colour vector rule for %r" % transform)
            changes, _where = pv.apply_mapping(root, styles, mapping)
            total = sum(colour_counts.values()) or 1
            share = sum(changes.values()) / float(total)
            if notes is not None and share < MIN_VARIANT_CHANGE:
                note(notes, "%s rewrote %d of %d paint values: the vector half "
                            "is visually the candidate (the transform had "
                            "nothing to move), so it is not a usable "
                            "augmentation" % (transform, sum(changes.values()),
                                              total))

    _ensure_frame(root)
    buffer = io.BytesIO()
    tree.write(buffer, encoding="utf-8", xml_declaration=True)
    data = buffer.getvalue()
    verify_svg(data)
    return data


def verify_svg(svg_bytes: bytes) -> None:
    """Refuse a result that is not well-formed XML with a usable frame.

    The frame is not decoration: every consumer downstream (the print check
    included) needs width/height/viewBox to know what it is looking at, so this
    is part of the transform rather than a step a caller might skip.
    """
    try:
        root = etree.parse(io.BytesIO(svg_bytes), _parser()).getroot()
    except etree.XMLSyntaxError as exc:
        raise TransformError("result is not well-formed XML: %s" % exc) from exc
    missing = [name for name in ("width", "height", "viewBox")
               if not root.get(name)]
    if missing:
        raise TransformError("result carries no %s" % ", ".join(missing))


# --------------------------------------------------------------------------
# Provenance -- a training set is useless without knowing what it came from
# --------------------------------------------------------------------------

def _default_stem(proof_path: Path, svg_path: Path) -> str:
    if proof_path.name.endswith(PROOF_SUFFIX):
        return proof_path.name[: -len(PROOF_SUFFIX)]
    if svg_path:
        return svg_path.stem
    return proof_path.stem


def default_manifest_path(proof_path: Path) -> Path:
    """Where the print check put this proof's manifest."""
    name = proof_path.name
    if name.endswith(PROOF_SUFFIX):
        return proof_path.with_name(name[: -len(PROOF_SUFFIX)] + ".manifest.json")
    return proof_path.with_name(proof_path.stem + ".manifest.json")


def load_manifest(manifest, proof_path: Path):
    """(payload, path, source) for this proof's manifest.

    ``source`` is one of ``file`` (read from ``path``), ``supplied`` (a dict was
    handed in by the caller) or ``absent``.  The distinction is not cosmetic: a
    supplied manifest has no path of its own, and reporting that as "no manifest
    found beside the proof" would be a false statement in `dataset.json` about
    what this set was derived from.  A missing manifest is never an error -- the
    stage is still useful, it just knows less.
    """
    if isinstance(manifest, dict):
        # The caller supplied the content; still record the conventional sibling
        # path when that file exists, so provenance points at something a reader
        # can open.
        sibling = default_manifest_path(proof_path)
        return manifest, (sibling if sibling.is_file() else None), "supplied"
    path = Path(manifest) if manifest is not None else default_manifest_path(
        proof_path)
    if not path.is_file():
        return None, None, "absent"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None, "absent"
    if not isinstance(payload, dict):
        return None, None, "absent"
    return payload, path, "file"


def palette_from_manifest(manifest, spec=None) -> list:
    """The screen colours, in the order the manifest (or spec) declares them.

    The substrate/garment colour is dropped when the manifest knows which one it
    is: it is not an ink and cycling it would recolour the fabric.
    """
    manifest = manifest or {}
    substrate = manifest.get("substrate") or {}
    declared = substrate.get("screens")
    if not declared:
        declared = substrate.get("palette")
    if not declared:
        declared = (spec or {}).get("palette")
    colours = []
    for raw in declared or []:
        colour = as_hex(raw)
        if colour and colour not in colours:
            colours.append(colour)
    return colours


def provenance_of(manifest, manifest_path, proof_path, svg_path, palette,
                  manifest_source: str = "file"):
    """What this set was derived FROM, recorded in dataset.json."""
    manifest = manifest or {}
    substrate = manifest.get("substrate") or {}
    stats = manifest.get("stats") or {}
    record = {
        "manifest": str(manifest_path) if manifest_path else None,
        "manifest_source": manifest_source,
        "manifest_tool": manifest.get("tool"),
        "manifest_generated": manifest.get("generated"),
        "dpi": stats.get("dpi"),
        "palette": palette,
        "screens": substrate.get("screens"),
        "substrate": substrate.get("colour"),
        "proof": {"file": str(proof_path)},
        "candidate": {"file": str(svg_path)},
    }
    if manifest_source == "absent":
        record["note"] = ("no manifest found for the proof (beside it or given): "
                          "provenance is limited to the two input files")
    elif manifest_source == "supplied" and not manifest_path:
        record["note"] = ("the manifest was supplied to the stage in memory, so "
                          "no manifest path is recorded")
    return record


# --------------------------------------------------------------------------
# The package
# --------------------------------------------------------------------------

def _check_destination(out: Path) -> None:
    """Refuse a destination this stage must not take over.

    `build_dataset` swaps a freshly built tree into place (see below), so it
    REPLACES whatever is at `out`.  A directory that carries no dataset.json is
    somebody else's, and replacing it would be a silent delete of their files --
    the same class of mistake as a silently transformed source.  Refuse loudly
    instead.
    """
    if not out.exists():
        return
    if not out.is_dir():
        raise TransformError("%s exists and is not a directory" % out)
    if (out / "dataset.json").is_file() or not any(out.iterdir()):
        return
    raise TransformError(
        "%s is not empty and carries no dataset.json: refusing to replace a "
        "directory this stage does not own (point --out at a new path, or "
        "remove it yourself)" % out)


def build_dataset(proof_png, candidate_svg, out_dir, *, transforms=None,
                  manifest=None, stem="") -> dict:
    """Write the dataset tree and return its description.

    The returned dict is what lands in ``dataset.json`` AND what the caller
    copies into the validation manifest's ``dataset`` key (the first six keys of
    it are that key), so the description of a set and the set itself can never
    disagree.  Both halves of every transform are written; the proof, the print
    PDF, the manifest and the selected SVG are read only, never touched.

    ALL OR NOTHING: the tree is built in a staging directory beside the target
    and swapped into place only once every requested transform has been written.
    A refused transform (an impossible `palette-cycle`, a document with no
    frame) therefore leaves no half-built set behind -- a partial `.dataset/`
    would be read by the archive route as a real dataset and would ship an empty
    package.
    """
    proof_path = Path(proof_png)
    svg_path = Path(candidate_svg)
    out = Path(out_dir)
    plan = variant_plan(transforms)

    proof_bytes = proof_path.read_bytes()
    svg_bytes = svg_path.read_bytes()
    stem = stem or _default_stem(proof_path, svg_path)
    manifest_payload, manifest_path, manifest_source = load_manifest(
        manifest, proof_path)
    palette = palette_from_manifest(manifest_payload)

    notes = []
    if plan and "palette-cycle" in plan and not palette:
        notes.append("palette-cycle has no manifest palette: it cycles the "
                     "artwork's own colours (sorted) instead")
    if any(name in HUE_DEGREES for name in plan):
        notes.append("hue-90/hue-180/hue-270 rotate hue through PIL's 8-bit HSV "
                     "band (a 90-degree turn is a 64-step offset), so the last "
                     "bit of each channel is approximate")
    if manifest_source == "absent":
        notes.append("no manifest found for the proof: provenance is limited to "
                     "the two input files")
    elif manifest_source == "supplied" and not manifest_path:
        notes.append("the manifest was supplied in memory with no path, so "
                     "dataset.json records no manifest file")

    _check_destination(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="%s." % out.name, suffix=".partial",
                                    dir=str(out.parent)))
    try:
        description = _build_into(staging, out.name, plan, proof_bytes,
                                  svg_bytes, proof_path, svg_path, stem, palette,
                                  manifest_payload, manifest_path,
                                  manifest_source, notes)
        if out.exists():
            shutil.rmtree(out)
        os.replace(staging, out)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return description


def _build_into(out, dir_name, plan, proof_bytes, svg_bytes, proof_path,
                svg_path, stem, palette, manifest_payload, manifest_path,
                manifest_source, notes) -> dict:
    """Build one dataset tree at ``out``; the caller stages and swaps it.

    ``dir_name`` is the FINAL directory name, which is what goes into the report
    -- the staging directory's random suffix must never reach dataset.json, or
    two runs of the same request would disagree and the set would stop being
    byte-deterministic.  (It is NOT called ``name``: the transform loop below
    binds ``name`` to each transform.)
    """
    (out / "raster").mkdir(parents=True, exist_ok=True)
    (out / "vector").mkdir(parents=True, exist_ok=True)

    from PIL import Image

    with Image.open(io.BytesIO(proof_bytes)) as opened:
        proof_size = list(opened.size)

    proof_sha = sha256_bytes(proof_bytes)
    files = []
    for name in plan:
        # A per-transform notes list: what the transform measured, and a plain
        # statement whenever it produced something that duplicates its input
        # (a training set must not carry a variant that teaches nothing).
        transform_notes = []
        try:
            raster = transform_raster(proof_bytes, name, palette=palette,
                                      notes=transform_notes)
            vector = transform_svg(svg_bytes, name, palette=palette,
                                   notes=transform_notes)
        except TransformError:
            raise
        except Exception as exc:                    # noqa: BLE001 - name it
            raise TransformError("transform %s failed: %s: %s"
                                 % (name, type(exc).__name__, exc)) from exc
        raster_name = "%s.png" % name
        vector_name = "%s.svg" % name
        (out / "raster" / raster_name).write_bytes(raster)
        (out / "vector" / vector_name).write_bytes(vector)
        with Image.open(io.BytesIO(raster)) as opened:
            size = list(opened.size)
        entry = {
            "transform": name,
            "kind": "spatial" if name in SPATIAL_TRANSFORMS else "colour",
            "source_proof_sha256": proof_sha,
            "raster": {"name": raster_name, "sha256": sha256_bytes(raster),
                       "bytes": len(raster), "width": size[0], "height": size[1]},
            "vector": {"name": vector_name, "sha256": sha256_bytes(vector),
                       "bytes": len(vector)},
        }
        if transform_notes:
            entry["notes"] = transform_notes
        if size != proof_size:
            # Only the diagonal moves do this, and only for a non-square proof:
            # the axes are exchanged rather than the art resampled.
            entry["note"] = ("axes exchanged: %dx%d -> %dx%d"
                             % (proof_size[0], proof_size[1], size[0], size[1]))
        files.append(entry)

    # Lift the per-transform notes into the dataset's own notes, named by the
    # transform: that list is what a consumer reads, and a note that only lives
    # inside a file entry is a note nobody sees.  A note that already opens with
    # its own transform name is not renamed twice.
    for entry in files:
        for text in entry.get("notes") or []:
            prefix = entry["transform"]
            if text.startswith(prefix):
                text = text[len(prefix):].lstrip(": ").strip()
            notes.append("%s: %s" % (prefix, text))

    description = {
        "dir": dir_name,
        "manifest": "%s%s.json" % (stem, DATASET_SUFFIX),
        "sheet": "%s%s-contact-sheet.png" % (stem, DATASET_SUFFIX),
        "package": "%s%s.7z" % (stem, DATASET_SUFFIX),
        "count": len(plan),
        "transforms": list(plan),
        "tool": "proof_variants.py",
        "vocabulary": list(TRANSFORMS),
        "source": {
            "proof": {"file": str(proof_path), "sha256": proof_sha,
                      "bytes": len(proof_bytes),
                      "width": proof_size[0], "height": proof_size[1]},
            "vector": {"file": str(svg_path),
                       "sha256": sha256_bytes(svg_bytes),
                       "bytes": len(svg_bytes)},
        },
        "provenance": provenance_of(manifest_payload, manifest_path, proof_path,
                                    svg_path, palette, manifest_source),
        "files": files,
        "notes": notes,
    }

    write_json(out / "dataset.json", description)
    (out / "dataset.md").write_text(format_markdown(description),
                                    encoding="utf-8")
    return description


def format_markdown(description: dict) -> str:
    """The human half of dataset.json -- same facts, readable."""
    source = description.get("source") or {}
    proof = source.get("proof") or {}
    vector = source.get("vector") or {}
    provenance = description.get("provenance") or {}
    out = []
    out.append("# Proof variants -- %d similar copies" % description["count"])
    out.append("")
    out.append("Every requested transform, both halves. This is a set, not a "
               "ranking: nothing here says which copy is better.")
    out.append("")
    out.append("- proof: `%s` (%sx%s, sha256 `%s`)"
               % (proof.get("file"), proof.get("width"), proof.get("height"),
                  str(proof.get("sha256"))[:16]))
    out.append("- selected vector: `%s` (sha256 `%s`)"
               % (vector.get("file"), str(vector.get("sha256"))[:16]))
    out.append("- derived from manifest: `%s`%s"
               % (provenance.get("manifest") or "(none recorded)",
                  "" if provenance.get("manifest") else
                  " -- the manifest was supplied in memory"
                  if provenance.get("manifest_source") == "supplied" else
                  " -- provenance is limited to the two input files"))
    if provenance.get("dpi"):
        out.append("- dpi: %s" % provenance["dpi"])
    if provenance.get("screens"):
        out.append("- screens (cycle order for `palette-cycle`): %s"
                   % ", ".join("`%s`" % c for c in provenance["screens"]))
    out.append("")
    out.append("| transform | kind | file | sha256 | bytes | size |")
    out.append("| --- | --- | --- | --- | --- | --- |")
    for entry in description["files"]:
        raster = entry["raster"]
        out.append("| `%s` | %s | `raster/%s` | `%s` | %d | %dx%d |"
                   % (entry["transform"], entry["kind"], raster["name"],
                      raster["sha256"][:16], raster["bytes"],
                      raster["width"], raster["height"]))
        out.append("| `%s` | %s | `vector/%s` | `%s` | %d | - |"
                   % (entry["transform"], entry["kind"], entry["vector"]["name"],
                      entry["vector"]["sha256"][:16], entry["vector"]["bytes"]))
    out.append("")
    out.append("## Caveats")
    out.append("")
    out.append("- `hue-90` / `hue-180` / `hue-270` rotate the hue band through "
               "PIL's 8-bit HSV: a 90-degree turn is a 64-step offset, so each "
               "channel is approximate in the last bit.")
    out.append("- `rot90` / `rot270` are clockwise quarter turns on both halves, "
               "and for a non-square proof they exchange the raster's pixel "
               "dimensions (the art is not resampled or cropped).")
    out.append("- A colour transform touches only `fill` / `stroke` / "
               "`stop-color`; a spatial transform touches only the frame. "
               "Neither rewrites path data.")
    out.append("- `palette-cycle` permutes the declared palette when the art "
               "actually uses those colours as exact values. When it does not "
               "(the usual case for traced continuous-tone art) the cycle is "
               "applied through PIL's nearest-colour search, so the variant is "
               "a **requantisation onto the palette**, not a permutation -- "
               "read this list to see which happened.")
    out.append("- A variant that came out visually identical to its input says "
               "so below, per transform. A duplicate is not an augmentation.")
    out.append("- Deterministic: no clock and no random component is recorded, so "
               "re-running on the same inputs reproduces these bytes exactly.")
    for note in description.get("notes") or []:
        out.append("- %s" % note)
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------
# Contact sheet -- the same dark-tile grid the other benches produce
# --------------------------------------------------------------------------

def contact_sheet(pngs, out_path, *, columns: int = DEFAULT_COLUMNS,
                  tile: int = DEFAULT_TILE):
    """Tile one labelled raster per variant into one PNG; returns the path.

    ``pngs`` items are a path, or a ``(path, label)`` pair.  Missing/unreadable
    tiles are skipped rather than raising, so a partial set still gets a sheet.
    """
    from PIL import Image, ImageDraw

    out_path = Path(out_path)
    labels = []
    for item in pngs:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            path, label = Path(item[0]), str(item[1])
        else:
            path, label = Path(item), Path(str(item)).stem
        labels.append((path, label))

    tiles = []
    for path, label in labels:
        if not path.is_file():
            continue
        try:
            with Image.open(path) as opened:
                image = opened.convert("RGB")
            image.thumbnail((tile, tile), Image.LANCZOS)
        except Exception:                           # noqa: BLE001 - skip it
            continue
        canvas = Image.new("RGB", (tile, tile + 26), "#101018")
        canvas.paste(image, ((tile - image.width) // 2,
                             (tile - image.height) // 2))
        draw = ImageDraw.Draw(canvas)
        draw.text((5, tile + 6), label[:40], fill="#c8c8d8")
        tiles.append(canvas)

    if not tiles:
        raise TransformError("no readable rasters to tile into %s" % out_path)

    columns = max(1, int(columns))
    rows = (len(tiles) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * tile, rows * (tile + 26)), "#08080c")
    for index, canvas in enumerate(tiles):
        sheet.paste(canvas, ((index % columns) * tile,
                             (index // columns) * (tile + 26)))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path)
    return out_path


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def banner(step: int, title: str) -> None:
    """Same stage banner shape pipeline.sh uses, so logs read alike."""
    print("")
    print("--- stage %d: %s ---" % (step, title), flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="proof_variants.py",
        description="Derive deterministic similar copies (flipped / rotated / "
                    "colour-shifted) of one chosen proof, both the raster and "
                    "the vector, packaged as a training-dataset set. Emits every "
                    "requested transform and never picks a winner.")
    parser.add_argument("--proof", required=True,
                        help="the print check's proof PNG")
    parser.add_argument("--svg", required=True,
                        help="the selected candidate SVG the proof renders")
    parser.add_argument("--out", required=True,
                        help="dataset directory to write (<stem>.dataset)")
    parser.add_argument("--transforms", default=None,
                        help="comma-separated subset of: %s (default: all)"
                             % ",".join(TRANSFORMS))
    parser.add_argument("--sheet", action="store_true",
                        help="also write a labelled contact sheet")
    parser.add_argument("--columns", type=int, default=DEFAULT_COLUMNS,
                        help="contact-sheet columns (default: %(default)s)")
    parser.add_argument("--manifest", default=None,
                        help="the proof's manifest for provenance "
                             "(default: <proof stem>.manifest.json beside it)")
    parser.add_argument("--stem", default="",
                        help="dataset stem (default: from the proof filename)")
    args = parser.parse_args(argv)

    proof_path = Path(args.proof)
    svg_path = Path(args.svg)
    if not proof_path.is_file():
        print("no such proof: %s" % proof_path, file=sys.stderr)
        return 2
    if not svg_path.is_file():
        print("no such SVG: %s" % svg_path, file=sys.stderr)
        return 2

    try:
        plan = variant_plan(args.transforms)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not plan:
        print("no transforms requested -- nothing to derive", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    stem = args.stem or _default_stem(proof_path, svg_path)

    banner(1, "variant plan")
    print("proof:  %s" % proof_path)
    print("vector: %s" % svg_path)
    print("output: %s/" % out_dir)
    print("transforms (%d): %s" % (len(plan), ", ".join(plan)))

    try:
        description = build_dataset(proof_path, svg_path, out_dir,
                                    transforms=plan, manifest=args.manifest,
                                    stem=stem)
    except TransformError as exc:
        print("transform failed: %s" % exc, file=sys.stderr)
        return 5
    except (OSError, ValueError) as exc:
        print("could not build the dataset: %s: %s"
              % (type(exc).__name__, exc), file=sys.stderr)
        return 5

    banner(2, "variants written (raster + vector)")
    for entry in description["files"]:
        print("  %-14s %-7s raster/%-16s %7.1f KB"
              % (entry["transform"], entry["kind"], entry["raster"]["name"],
                 entry["raster"]["bytes"] / 1024.0))
        # A transform's own report on itself: what it moved, and a plain warning
        # when its output is visually its input (a duplicate is not a variant).
        for text in entry.get("notes") or []:
            print("      %s" % text)

    if args.sheet:
        banner(3, "contact sheet")
        try:
            sheet = out_dir.parent / description["sheet"]
            contact_sheet(
                [(out_dir / "raster" / entry["raster"]["name"], entry["transform"])
                 for entry in description["files"]],
                sheet, columns=args.columns)
            print("wrote %s" % sheet)
        except TransformError as exc:
            print("contact sheet skipped: %s" % exc, file=sys.stderr)

    banner(4, "dataset manifest")
    print("wrote %s" % (out_dir / "dataset.json"))
    print("wrote %s" % (out_dir / "dataset.md"))
    print("")
    print("%d variant(s) of %s -- every one emitted; which copy is useful is "
          "your call, not this tool's." % (description["count"], stem))
    return 0


if __name__ == "__main__":
    sys.exit(main())
