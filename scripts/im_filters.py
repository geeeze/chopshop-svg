#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
im_filters.py -- ImageMagick filter passes that run BEFORE the tracer.

"chopshop-im": the auxiliary filter bench for the front half.  It takes a
raster and produces filtered readings of it, each one a candidate INPUT to
``prep_raster.py`` -> ``trace_sweep.py``.  Raster in, raster out; it never
traces, never judges art, and never edits the tracer's parameters.

Why this exists
---------------
``tune_sweep.py`` sweeps the TRACER's parameters against one raster.
This sweeps the RASTER against one tracer.  They are the two halves of the
same experiment, and the trace-stage notes are explicit that most of the
"too many nodes / too jagged" complaint is pixel-level noise rather than
colour structure: VTracer traces literal pixel boundaries, so grain and
compression artifacts become hundreds of corrective nodes.  Measured in this
repo's own skill: quantising the SOURCE does NOT reduce node count (max nodes
967 -> 912 -> 1415 across 6-48 colours), so the lever here is NOISE and SOFT
GRADIENTS, not palette size.  Read the report with that in mind.

Each pass is one filter (or a short chain) from the preset bank below.  The
bank is small and named; the report orders variants by ``colours_at_95pct``
as a MEASUREMENT ORDER and never recommends one -- which reading looks best
is a human call, exactly as in ``tune_sweep.py`` and ``palette_variants.py``.

A note on dither (measured on IM 7.1.1-43)
------------------------------------------
ImageMagick dithers BY DEFAULT and its default method is Riemersma, so a bare
``-colors 6`` is silently dithering.  Two consequences, both measured rather
than assumed -- verified at PIXEL level, not just by file hash, because two
images that differ only in metadata are not a real difference (ImageMagick's
PNG output turns out to be byte-stable -- it writes only ``dpi`` and
``chromaticity`` chunks, no dates -- so hashes are meaningful here too, but
pixels are the claim that matters):

* Naming the default changes nothing.  ``-dither Riemersma -colors 6`` against
  a bare ``-colors 6`` differs in **0 pixels**; ``-dither Riemersma -remap
  pal.png`` likewise matches ``-remap pal.png`` exactly.  A "dithered" preset
  built on Riemersma is a fake that reports success while emitting the
  undithered bytes.
* ``-dither None`` and ``-dither FloydSteinberg`` really do change the image:
  against a bare ``-colors 6``, FloydSteinberg moved 284 546 pixels (max delta
  248) and ``None`` moved 6 043.

This is not cosmetic for print: dithering sprays high-frequency speckle, which
is the same class of noise that makes VTracer emit corrective nodes, and at
6-16 colours the speckle IS the artwork.  Every reducing preset here therefore
states its dither explicitly, ``-dither None`` is the print reading (hard flat
edges, clean separation), and ``FloydSteinberg`` is offered as the
photographic reading for comparison.  The report carries a ``dither`` column
so a variant's reading is never ambiguous after the fact.

A note on colour type
---------------------
Every reducing operation -- ``-colors``, ``-posterize`` AND ``-remap`` --
writes an INDEXED (mode ``P``) PNG unless told otherwise.  The bench passes
``-define png:color-type=2`` on every preset except ``identity``, so the rest
of the chain sees one colour type.  This is not tidiness: on an indexed PNG
Pillow's ``getpixel`` returns a palette INDEX -- an ``int``, not a colour --
which made ``prep_raster.py``'s background check raise internally and degrade
to ``background: could not sample``, a silently MISSING measurement rather
than an error.

A note on the offset blend
--------------------------
The trace-stage notes propose "multi-source / negative-version blend" --
duplicate the image, offset it, blend at partial opacity -- as noise
cancellation by the dark-frame-subtraction analogy.  That analogy does NOT
hold and the preset is documented as what it actually is: averaging a shifted
copy of an image with itself is a DIRECTIONAL BOX BLUR.  Dark-frame
subtraction works because the two noise samples are independent; a shifted
copy carries the same noise, shifted, so nothing cancels -- it convolves.  It
is still useful (anisotropic smoothing of hair/fur along one axis) but it is
a blur, not a denoiser, and too much offset (>~2px) creates double edges that
VTracer traces as EXTRA regions.  ``offset-blend`` is capped at 2px for that
reason.

Usage
-----
::

    python3 scripts/im_filters.py art.png                 # every preset
    python3 scripts/im_filters.py art.png --list          # preset bank only
    python3 scripts/im_filters.py art.png --presets bilateral,flat8
    python3 scripts/im_filters.py art.png --contact-sheet
    python3 scripts/im_filters.py art.png --keep-alpha    # do not flatten
    python3 scripts/im_filters.py art.png --spec spec.json

Output lands in ``09_filters/<stem>/``: one PNG per preset, plus
``filters.json`` and ``filters.md``.  Feed a filtered PNG to the front half::

    ./front_pipeline.sh 09_filters/art/flat8.png

Exit codes: 0 = every variant written, 1 = a variant failed to write,
2 = bad usage, 3 = ImageMagick not installed.

Optional tool, per the repo's rule: with no ImageMagick on PATH this reports
the install command and exits 3.  It never crashes a run, and never
substitutes a Python filter written on the spot for the named one.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _path in (HERE, ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import front_common  # noqa: E402

DEFAULT_SPEC = os.path.join(ROOT, "spec.json")
DEFAULT_OUTDIR = os.path.join(ROOT, "09_filters")

# The engine.  ImageMagick 7 spells everything ``magick``; IM 6 has no
# ``magick`` binary at all (``convert``/``montage`` are the tools), so probe
# for the v7 driver and only then fall back.  ``tool_version`` is reused so
# the version string in the report is the same shape the rest of the front
# half records.
ENGINE = "magick"
ENGINE_FALLBACK = "convert"
INSTALL_HINT = ("ImageMagick is required: apt-get install imagemagick "
                "(or: podman run --rm chopshop-svg:0.1.0 ...)")

# --------------------------------------------------------------------------
# The preset bank.  Each value is an argv fragment appended between the input
# and the output path, so a preset can never smuggle in a shell.
#
# Kept small on purpose.  ``identity`` is not decoration: it is the control
# that proves a measured difference came from the filter and not from the
# bench -- which is why it is a byte copy and not an engine round-trip.
# --------------------------------------------------------------------------
PRESETS = {
    # -- controls ----------------------------------------------------------
    # ``identity`` is handled by the driver, not by ImageMagick: it copies the
    # source BYTE-FOR-BYTE.  An earlier version ran a no-op IM pass, which was
    # not a control at all -- the alpha flatten alone moved 36 931 pixels
    # (max delta 94, exactly the G channel of a red at alpha 161 composited
    # onto white).  A control that transforms the image cannot tell you
    # whether a measured difference came from the filter.
    "identity": [],
    # The alpha flatten on its own, so its cost is a reading rather than a
    # hidden step inside every other variant.
    "flatten": [],
    # -- noise removal -----------------------------------------------------
    "despeckle": ["-despeckle"],
    "median3": ["-median", "3"],
    "median5": ["-median", "5"],
    "kuwahara": ["-kuwahara", "3"],
    # Bilateral stand-in: smooths flat areas, respects edges -- the repo's
    # skill prefers a bilateral for skin/hair over a Gaussian.
    "bilateral": ["-selective-blur", "0x3+15%"],
    # -- soft gradients / colour depth -------------------------------------
    # Dither is stated EXPLICITLY on every reducing preset: IM 7's default
    # dither is Riemersma, so a bare ``-colors N`` was silently dithering --
    # spraying exactly the high-frequency speckle the tracer turns into nodes.
    # ``-dither None`` gives hard flat edges (the print reading);
    # ``FloydSteinberg`` is the photographic reading, offered for comparison.
    "flat8": ["-dither", "None", "-colors", "8"],
    "flat6": ["-dither", "None", "-colors", "6"],
    "flat6-dither": ["-dither", "FloydSteinberg", "-colors", "6"],
    "poster6": ["-posterize", "6"],
    # -- anisotropy (see the module docstring: this is a BLUR) -------------
    "offset-blend": ["-roll", "+2+0", "-evaluate-sequence", "mean"],
    # -- chains, in the order the notes recommend --------------------------
    "denoise-flat": ["-kuwahara", "3", "-dither", "None", "-colors", "8"],
    "denoise-then-edge": ["-kuwahara", "3", "-unsharp", "0x1+0.5+0"],
}

# Presets that need a generated palette strip: ``-remap`` takes an IMAGE, not
# a colour list, so the strip is written to disk before the pass runs.
#
# Dither is set explicitly here for the same reason as in PRESETS, and the
# method matters in BOTH paths (verified at pixel level on IM 7.1.1-43):
#
#   -dither Riemersma -colors 6  vs bare -colors 6 -> 0 px differ (inert)
#   -dither None      -colors 6  vs bare -colors 6 -> 6 043 px differ
#   -dither FloydSt.  -colors 6  vs bare -colors 6 -> 284 546 px differ
#   -dither Riemersma -remap pal vs -remap pal     -> identical
#
# So a "dithered" preset built on Riemersma is a fake that reports success and
# produces the undithered bytes.  Each entry maps a preset name to
# (palette key, dither method).
REMAPPED = {
    "remap-spec":        ("spec", "None"),
    "remap-spec-dither": ("spec", "FloydSteinberg"),
    "remap-auto":        ("auto", "None"),
    "remap-auto-dither": ("auto", "FloydSteinberg"),
}

# How many colours "auto" keeps: the screen-print ceiling this repo's gates
# work against (see the skill's "6-colour gate" section).
AUTO_PALETTE_COLOURS = 6

PALETTE_FILES = {"spec": "_palette-spec.png", "auto": "_palette-auto.png"}

# Gamma and sRGB chunks are how a GIMP/editor round-trip announces itself;
# recorded per file so a variant can be traced back to its editor.
_SIGNATURE_CHUNKS = ("srgb", "gamma", "chromaticity", "icc_profile")


def engine_argv():
    """(argv0, display_name) for the driver, or (None, None) when absent."""
    if front_common.which(ENGINE):
        return ENGINE, ENGINE
    if front_common.which(ENGINE_FALLBACK):
        return ENGINE_FALLBACK, ENGINE_FALLBACK
    return None, None


# --------------------------------------------------------------------------
# Measurement.  These are the numbers the trace stage actually cares about:
# how many colours a viewer can see, and how much high-frequency noise the
# tracer will mistake for edges.
# --------------------------------------------------------------------------

def colour_stats(png_path):
    """distinct colours, colours-at-95%, and top-colour share."""
    from PIL import Image

    with Image.open(png_path) as image:
        image = image.convert("RGB")
        entries = image.getcolors(maxcolors=1 << 24)
    if entries is None:
        entries = []
    total = sum(count for count, _ in entries) or 1
    counts = sorted((count for count, _ in entries), reverse=True)
    running, at95 = 0, len(counts)
    for index, count in enumerate(counts, start=1):
        running += count
        if running / total >= 0.95:
            at95 = index
            break
    return {
        "distinct_colours": len(entries),
        "colours_at_95pct": at95,
        "top_colour_share": round((counts[0] / total) if counts else 0.0, 4),
    }


def edge_energy(png_path):
    """Mean |Laplacian| -- a noise/edge proxy that needs no extra tools.

    High-frequency grain shows up here as a raised floor on flat areas, which
    is what the tracer turns into spurious regions.  Deliberately reported as
    a RELATIVE number between variants, never against a threshold: there is no
    absolute value that means "too noisy" independent of the artwork.
    """
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        return None
    with Image.open(png_path) as image:
        grey = image.convert("L")
        arr = np.asarray(grey, dtype=float)
    if arr.size < 9:
        return None
    lap = (arr[:-2, 1:-1] + arr[2:, 1:-1] + arr[1:-1, :-2] + arr[1:-1, 2:]
           - 4.0 * arr[1:-1, 1:-1])
    return round(float(abs(lap).mean()), 4)


def png_meta(png_path):
    """Size, mode and the editor-signature chunks of a written PNG."""
    from PIL import Image

    with Image.open(png_path) as image:
        info = {k: True for k in image.info if k in _SIGNATURE_CHUNKS}
        return {
            "bytes": os.path.getsize(png_path),
            "mode": image.mode,
            "size": list(image.size),
            "chunks": sorted(info),
        }


# --------------------------------------------------------------------------
# Running one preset
# --------------------------------------------------------------------------

def write_palette_strip(path, colours):
    """Write a ``-remap`` palette strip (one row, one pixel per colour).

    ``-remap`` takes an IMAGE, not a colour list, so the palette has to exist
    as a file.  Any existing file is replaced: a palette left from an earlier
    run would otherwise be reused silently when the spec changed.
    """
    from PIL import Image

    cleaned = []
    for colour in colours:
        rgb = front_common.hex_to_rgb(colour)
        if rgb is not None:
            cleaned.append(rgb)
    if not cleaned:
        return False
    image = Image.new("RGB", (len(cleaned), 1))
    image.putdata(cleaned)
    image.save(path)
    return True


def source_palette(src, count=AUTO_PALETTE_COLOURS):
    """The ``count`` most common colours in the source, most first.

    Deliberately coarse (median cut) -- this is the art's OWN reading, the
    thing a screen printer would actually separate to, not a palette we invent
    for it.
    """
    from PIL import Image

    with Image.open(src) as image:
        image = image.convert("RGB")
        reduced = image.quantize(colors=count)
        palette = reduced.getpalette() or []
    palette = palette[:count * 3]
    hexes = []
    for index in range(count):
        rgb = palette[index * 3:index * 3 + 3]
        if len(rgb) == 3:
            hexes.append("#%02X%02X%02X" % tuple(rgb))
    return hexes


def preset_dither(preset):
    """The dither method a preset uses, or None when it never reduces colour.

    Surfaced in the report because it is the quietest lever here: IM 7 dithers
    by DEFAULT (Riemersma), so a preset with no explicit dither is still
    dithering and still generating the speckle the tracer turns into nodes.
    """
    if preset in REMAPPED:
        return REMAPPED[preset][1]
    argv = PRESETS.get(preset) or []
    if "-dither" in argv:
        return argv[argv.index("-dither") + 1]
    return None


def filter_argv(driver, src, dst, preset, flatten_hex=None, palettes=None):
    """The full argv for one preset.  Pure -- unit-tested without ImageMagick."""
    argv = [driver, src]
    if preset in REMAPPED:
        key, dither = REMAPPED[preset]
        palette = (palettes or {}).get(key)
        if not palette:
            raise ValueError("preset %r needs the %r palette file" % (preset, key))
        if dither:
            argv += ["-dither", dither]
        argv += ["-remap", palette]
    else:
        argv += list(PRESETS[preset])
    if flatten_hex:
        # Match prep_raster's background_hex convention: alpha is flattened
        # onto the declared background before the tracer ever sees it, because
        # VTracer has no concept of a matte.
        argv += ["-background", flatten_hex, "-alpha", "remove", "-alpha", "off"]
    if preset != "identity":
        # Every reducing operation -- `-colors`, `-posterize` AND `-remap` --
        # writes an INDEXED (mode P) PNG by default.  Measured: flat6, flat8,
        # flat6-dither, denoise-flat, poster6 and all four remap presets came
        # out mode P.  Indexed output makes Pillow's getpixel return a palette
        # INDEX (an int) rather than a colour, which is how prep_raster's
        # background check silently degraded to "could not sample".  Forcing
        # truecolour gives the rest of the chain ONE colour type.  `identity`
        # is excluded because it is the byte-exact control.
        argv += ["-define", "png:color-type=2"]
    argv.append(dst)
    return argv


def run_preset(task):
    """One (preset -> filtered PNG).  Module-level so it is picklable.

    Returns a dict; never raises, because a single bad preset must not take
    down the bench (front_common.run_parallel maps this across processes).
    """
    driver, src, outdir, preset, flatten_hex, palettes = task
    dst = os.path.join(outdir, "%s.png" % preset)
    entry = {"preset": preset, "output": dst}
    if os.path.exists(dst):
        # Idempotent: a rerun must produce the same result, and a partial file
        # from an interrupted run must not be measured as if it were complete.
        os.unlink(dst)

    if preset == "identity":
        # The control: no engine involved, the source lands unmodified.
        try:
            shutil.copyfile(src, dst)
        except OSError as exc:
            entry["error"] = "could not copy the source: %s" % exc
            return entry
        entry["command"] = ["cp", src, dst]
    else:
        try:
            argv = filter_argv(driver, src, dst, preset, flatten_hex, palettes)
        except ValueError as exc:
            entry["error"] = str(exc)
            return entry
        entry["command"] = argv
        env = dict(os.environ)
        env.setdefault("DBUS_SESSION_BUS_ADDRESS", "disabled:")
        try:
            result = subprocess.run(argv, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env, timeout=900)
        except (OSError, subprocess.SubprocessError) as exc:
            entry["error"] = "could not run %s: %s" % (driver, exc)
            return entry
        if result.returncode != 0 or not os.path.exists(dst):
            entry["error"] = "%s exited %d" % (driver, result.returncode)
            entry["stderr_tail"] = result.stderr.decode("utf-8", "replace")[-800:]
            return entry

    try:
        entry.update(colour_stats(dst))
        entry["edge_energy"] = edge_energy(dst)
        entry["dither"] = preset_dither(preset)
        entry.update(png_meta(dst))
    except Exception as exc:                     # noqa: BLE001 - report it
        entry["error"] = "written but unmeasurable: %s" % exc
    return entry


def montage_argv(driver, pngs, dst):
    """``montage`` argv for the contact sheet, IM7 and IM6 spellings.

    IM7 subcommands the tool (``magick montage ...``); IM6 has a separate
    ``montage`` binary and no ``magick`` driver at all.  Using the IM7 form
    under IM6 fails silently into "no contact sheet", so the driver decides.

    ``-label`` / ``-background`` / ``-geometry`` are SETTINGS and must precede
    the input images.  Placed after them they apply to nothing and the sheet
    comes out unlabelled -- which looks like a font problem and is not one.
    """
    if os.path.basename(driver) == ENGINE:
        argv = [driver, "montage"]
    else:
        argv = ["montage"]
    argv += ["-label", "%f", "-tile", "4x", "-geometry", "+4+4",
             "-background", "#ffffff", "-fill", "#000000"]
    argv += list(pngs)
    argv.append(dst)
    return argv


def make_contact_sheet(driver, pngs, dst):
    """``magick montage`` the variants side by side.  Returns True on success."""
    argv = montage_argv(driver, pngs, dst)
    env = dict(os.environ)
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", "disabled:")
    try:
        result = subprocess.run(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, timeout=900)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and os.path.exists(dst)


# --------------------------------------------------------------------------
# Reporting.  Ordered by a measurement, never by "looks best".
# --------------------------------------------------------------------------

def sort_key(entry):
    """Reading order: fewest colours to 95% coverage, then least edge energy.

    A MEASUREMENT ORDER, not a ranking.  Low ``colours_at_95pct`` is what a
    screen print can actually hold; low ``edge_energy`` is what the tracer
    stops turning into nodes.  Neither says the art survived the filter -- look
    at the contact sheet for that.
    """
    if entry.get("error"):
        return (1, 0, 0, entry["preset"])
    return (0, entry.get("colours_at_95pct") or 0,
            entry.get("edge_energy") or 0, entry["preset"])


def format_markdown(result):
    out = ["# chopshop-im filter bench -- %s" % result["source"]["file"], ""]
    out.append("- source sha256: `%s`" % result["source"]["sha256"])
    out.append("- engine: %s" % result["engine"]["version"])
    out.append("- alpha: %s" % ("kept" if result["keep_alpha"]
                                else "flattened onto %s" % result["background"]))
    out.append("")
    out.append("| preset | dither | distinct colours | colours to 95% "
               "| edge energy | KB | ok |")
    out.append("| --- | --- | --- | --- | --- | --- | --- |")
    for entry in result["variants"]:
        out.append("| `%s` | %s | %s | %s | %s | %.1f | %s |" % (
            entry["preset"],
            entry.get("dither") or "-",
            entry.get("distinct_colours", "-"),
            entry.get("colours_at_95pct", "-"),
            "-" if entry.get("edge_energy") is None else entry["edge_energy"],
            entry.get("bytes", 0) / 1024.0,
            "no" if entry.get("error") else "yes"))
    out.append("")
    out.append("Ordered by colours-to-95%% then edge energy. This is a "
               "measurement order, **not** a recommendation: whether the art "
               "survived a filter is a human call. Compare the PNGs.")
    out.append("")
    out.append("These are INPUTS to the front half, not traces:")
    out.append("")
    out.append("```bash")
    for entry in result["variants"]:
        if not entry.get("error"):
            out.append("./front_pipeline.sh %s" % entry["output"])
    out.append("```")
    out.append("")
    for entry in result["variants"]:
        if entry.get("error"):
            out.append("- `%s` FAILED: %s" % (entry["preset"], entry["error"]))
    return "\n".join(out)


def format_report(result):
    lines = ["=" * 72]
    lines.append("chopshop-im filter bench   source: %s" % result["source"]["file"])
    lines.append("                           engine: %s" % result["engine"]["version"])
    lines.append("                           output: %s/" % result["outdir"])
    lines.append("=" * 72)
    header = "%-18s %-14s %8s %8s %8s %7s %5s" % (
        "preset", "dither", "colours", "to95%", "edges", "KB", "ok")
    lines.append(header)
    lines.append("-" * len(header))
    for entry in result["variants"]:
        lines.append("%-18s %-14s %8s %8s %8s %7.1f %5s" % (
            entry["preset"],
            entry.get("dither") or "-",
            entry.get("distinct_colours", "-"),
            entry.get("colours_at_95pct", "-"),
            "-" if entry.get("edge_energy") is None else entry["edge_energy"],
            entry.get("bytes", 0) / 1024.0,
            "no" if entry.get("error") else "yes"))
    lines.append("")
    lines.append("Measurement order, not a recommendation. Read the PNGs.")
    lines.append("Feed any of them to the front half, e.g.:")
    first = next((e for e in result["variants"] if not e.get("error")), None)
    if first:
        lines.append("  ./front_pipeline.sh %s" % first["output"])
    if result.get("contact_sheet"):
        lines.append("contact sheet: %s" % result["contact_sheet"])
    lines.append("")
    lines.append("wrote %s" % os.path.join(result["outdir"], "filters.json"))
    lines.append("wrote %s" % os.path.join(result["outdir"], "filters.md"))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def build(src, outdir, presets, keep_alpha, background, workers, contact_sheet,
          spec_palette=None):
    driver, _name = engine_argv()
    if driver is None:
        print(INSTALL_HINT, file=sys.stderr)
        return 3
    if not os.path.exists(src):
        print("no such raster: %s" % src, file=sys.stderr)
        return 2
    unknown = [p for p in presets if p not in PRESETS and p not in REMAPPED]
    if unknown:
        print("unknown preset(s): %s -- see --list" % ", ".join(unknown),
              file=sys.stderr)
        return 2

    stem = os.path.splitext(os.path.basename(src))[0]
    base = os.path.join(outdir, stem)
    os.makedirs(base, exist_ok=True)

    flatten_hex = None if keep_alpha else background

    # Build only the palette strips the chosen presets actually need, and
    # rebuild them every run: a strip left over from a previous spec would
    # silently remap onto the wrong inks.
    palettes = {}
    needed = {REMAPPED[p][0] for p in presets if p in REMAPPED}
    if "spec" in needed and spec_palette:
        path = os.path.join(base, PALETTE_FILES["spec"])
        if write_palette_strip(path, spec_palette):
            palettes["spec"] = path
    if "auto" in needed:
        path = os.path.join(base, PALETTE_FILES["auto"])
        if write_palette_strip(path, source_palette(src)):
            palettes["auto"] = path

    tasks = [(driver, src, base, preset, flatten_hex, palettes)
             for preset in presets]

    result = {
        "tool": "im_filters.py",
        "source": {"file": src, "sha256": front_common.sha256_file(src)},
        "engine": {"name": driver,
                   "version": front_common.tool_version(driver) or "unknown"},
        "outdir": base,
        "keep_alpha": bool(keep_alpha),
        "background": flatten_hex,
        "palettes": {k: {"file": v, "colours": (spec_palette or [])
                         if k == "spec" else source_palette(src)}
                     for k, v in palettes.items()},
        "presets": list(presets),
        "variants": [],
    }
    if spec_palette is None and "spec" in needed:
        print("note: no usable spec.palette -- remap-spec variants are SKIPPED",
              file=sys.stderr)

    print("=" * 72)
    print("chopshop-im filter bench    source: %s" % src)
    print("                            engine: %s" % result["engine"]["version"])
    print("                            output: %s/" % base)
    print("=" * 72)
    print("presets: %d   alpha: %s"
          % (len(presets),
             "kept" if keep_alpha else "flattened onto %s" % flatten_hex))
    print("")

    entries = front_common.run_parallel(run_preset, tasks, workers)
    entries.sort(key=sort_key)
    result["variants"] = entries

    for entry in entries:
        if entry.get("error"):
            print("  %-18s FAILED  %s" % (entry["preset"], entry["error"]))
        else:
            # .get() throughout: a variant can succeed yet carry no
            # measurements (Pillow missing, unreadable output), and indexing
            # here would crash the whole bench on one bad variant -- the exact
            # thing the per-variant error reporting exists to prevent.
            measured = entry.get("colours_at_95pct")
            edge = entry.get("edge_energy")
            print("  %-18s %6s colours to 95%%   edge %8s   %7.1f KB"
                  % (entry["preset"],
                     "-" if measured is None else measured,
                     "-" if edge is None else "%.4f" % edge,
                     (entry.get("bytes") or 0) / 1024.0))

    written = [e["output"] for e in entries if not e.get("error")]
    if contact_sheet and written:
        sheet = os.path.join(base, "contact-sheet.png")
        if make_contact_sheet(driver, written, sheet):
            result["contact_sheet"] = sheet

    front_common.write_json(os.path.join(base, "filters.json"), result)
    with open(os.path.join(base, "filters.md"), "w", encoding="utf-8") as handle:
        handle.write(format_markdown(result) + "\n")

    print("")
    print(format_report(result))

    return 1 if any(e.get("error") for e in entries) else 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="im_filters.py",
        description="ImageMagick filter passes that run BEFORE the tracer: "
                    "raster in, raster out, for prep_raster.py to pick up. "
                    "Measures, never recommends (auxiliary front-half layer).")
    parser.add_argument("raster", nargs="?", help="source raster (PNG/JPEG/TIFF)")
    parser.add_argument("--presets", default=None,
                        help="comma-separated preset names (default: all)")
    parser.add_argument("--outdir", default=DEFAULT_OUTDIR,
                        help="where filtered variants land (default: 09_filters/)")
    parser.add_argument("--spec", dest="spec", default=DEFAULT_SPEC,
                        help="spec.json, for background_hex (default: spec.json)")
    parser.add_argument("--keep-alpha", action="store_true",
                        help="keep the alpha channel (default: flatten it)")
    parser.add_argument("--workers", type=int, default=None,
                        help="parallel filter processes (default: cpu count)")
    parser.add_argument("--contact-sheet", action="store_true",
                        help="also montage the variants side by side")
    parser.add_argument("--list", action="store_true",
                        help="list the preset bank and exit")
    args = parser.parse_args(argv)

    if args.list:
        print("presets in im_filters.py:")
        for name in sorted(PRESETS):
            note = "" if PRESETS[name] else "(control)"
            print("  %-18s %s" % (name, " ".join(PRESETS[name]) or note))
        print("")
        print("presets needing a generated palette strip (dither only bites with "
              "-remap):")
        for name in sorted(REMAPPED):
            key, dither = REMAPPED[name]
            print("  %-18s -remap <%s palette>%s"
                  % (name, key, " -dither " + dither if dither else ""))
        return 0

    if not args.raster:
        parser.error("give a raster path (or --list)")

    # The spec is optional here: this bench only needs background_hex and
    # palette, and a missing or malformed spec must not stop a filter run.
    background = front_common.PRINT_DEFAULTS["background_hex"]
    spec_palette = None
    try:
        spec = front_common.load_json(args.spec)
        background = front_common.print_options(spec).get("background_hex") \
            or background
        raw = [str(c) for c in (spec.get("palette") or [])]
        cleaned = [c for c in raw if front_common.hex_to_rgb(c) is not None]
        spec_palette = cleaned or None
    except (OSError, ValueError, AttributeError):
        pass

    presets = list(PRESETS) + list(REMAPPED)
    if args.presets:
        presets = [p.strip() for p in args.presets.split(",") if p.strip()]

    return build(args.raster, args.outdir, presets, args.keep_alpha,
                 background, args.workers, args.contact_sheet, spec_palette)


if __name__ == "__main__":
    sys.exit(main())
