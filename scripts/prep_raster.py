#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prep_raster.py -- check a raster for tracing suitability (pipeline stage 1).

Two modes:

  CHECK (default) -- report whether the file checks the boxes for tracing and
  copy it through UNCHANGED.  Nothing is modified: no resizing, no colour
  changes, no alpha flattening.  The sidecar records the acceptability checks
  and their pass/fail, so you see *why* an image is or is not ready to trace.

  FIX (--fix)     -- apply the optional normalising transforms, but only when
  asked: background removal (rembg), upscale to target DPI (realesrgan or
  LANCZOS), colour quantisation (pngquant), alpha flattening.  Each transform
  is skippable via spec.print.*; missing tools skip with a recorded warning.

The motivation for check-first is a real bug this file once had: upscaling a
landscape source against a portrait spec used to *stretch* the image into the
spec's pixel box (img.resize(target) forces the target aspect ratio).  That
distorted the artwork.  Two consequences:

  * upscaling now preserves the source aspect ratio (fit within the target,
    never stretch), and
  * by default the file is not touched at all -- an aspect mismatch is REPORTED
    (check ``aspect_ratio``) rather than silently "fixed" by squashing.

Usage::

    python scripts/prep_raster.py artwork.png spec.json            # check only
    python scripts/prep_raster.py artwork.png spec.json --fix      # also modify
    python scripts/prep_raster.py artwork.png spec.json --out-dir DIR

Exit codes: 0 = checked (or already vector), 2 = bad usage / bad input.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

import numpy as np
from PIL import Image

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import front_common as fc  # noqa: E402

VECTOR_EXTS = {".svg", ".pdf"}
RASTER_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp"}

# Aspect deviation above this fraction of the target is "mismatched".  Below it
# the difference is rounding and a resize can fix it.
ASPECT_TOLERANCE = 0.02

# A border whose RGB stddev stays under this is called "likely uniform".
BG_STDDEV_THRESHOLD = 16.0


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ppmm_to_px(mm, dpi):
    return int(round(mm / 25.4 * dpi))


def _target(spec):
    """((width_px, height_px), dpi) from spec.dimensions at spec.print.dpi,
    or (None, dpi) when no dimensions are declared."""
    popt = fc.print_options(spec)
    dims = spec.get("dimensions") or {}
    dpi = popt.get("dpi")
    w_mm, h_mm = dims.get("width_mm"), dims.get("height_mm")
    if not (w_mm and h_mm and dpi):
        return None, dpi
    return (_ppmm_to_px(w_mm, dpi), _ppmm_to_px(h_mm, dpi)), dpi


# --------------------------------------------------------------------------
# CHECK mode: acceptability checks on the INPUT (never modified)
# --------------------------------------------------------------------------

def _check_format(ext, checks):
    if ext in RASTER_EXTS:
        checks.append({"check": "format", "status": "pass",
                       "detail": "raster %s -- tracing applies" % ext})
    else:
        checks.append({"check": "format", "status": "warn",
                       "detail": "unrecognised extension %s; trying anyway" % ext})


def _check_color_mode(img, checks):
    mode = img.mode
    if mode == "CMYK":
        checks.append({"check": "color_mode", "status": "warn",
                       "detail": "CMYK input; --fix converts to sRGB, check mode "
                                 "leaves it as-is"})
    elif mode in ("RGB", "RGBA", "L", "LA"):
        checks.append({"check": "color_mode", "status": "pass",
                       "detail": "%s" % mode})
    else:
        checks.append({"check": "color_mode", "status": "warn",
                       "detail": "mode %s" % mode})


def _check_alpha(img, checks):
    if img.mode in ("RGBA", "LA", "PA"):
        checks.append({"check": "alpha", "status": "warn",
                       "detail": "has transparency; some tracers bake it onto a "
                                 "background (--fix flattens onto "
                                 "spec.print.background_hex)"})
    else:
        checks.append({"check": "alpha", "status": "pass",
                       "detail": "opaque (no alpha channel)"})


def _check_geometry(img, spec, checks):
    """Report the source's own size/aspect; only pass/fail when the spec orders a size."""
    dims = spec.get("dimensions") or {}
    dpi = fc.print_options(spec).get("dpi")
    w_px, h_px = img.size
    w_mm, h_mm = dims.get("width_mm"), dims.get("height_mm")

    src_aspect = w_px / h_px if h_px else 0.0

    if not (w_mm and h_mm and dpi):
        # No size prescribed: report the facts, never judge them.
        detail = "source is %dx%dpx (aspect %.3f)" % (w_px, h_px, src_aspect)
        if dpi:
            detail += ", %.1fx%.1fmm at %d dpi" % (w_px / dpi * 25.4,
                                                   h_px / dpi * 25.4, dpi)
        checks.append({"check": "geometry", "status": "info",
                       "detail": detail + "; no size prescribed in the spec, "
                                 "so nothing is enforced"})
        return

    tgt_aspect = w_mm / h_mm if h_mm else 0.0
    if src_aspect > 0 and tgt_aspect > 0:
        deviation = abs(src_aspect - tgt_aspect) / tgt_aspect
        if deviation > ASPECT_TOLERANCE:
            checks.append({"check": "aspect_ratio", "status": "fail",
                           "detail": ("source is %.3f (%.0fx%.0fpx) but the spec "
                                      "orders %.3f (%gx%gmm). A resize cannot fix "
                                      "this without distorting the art -- rotate, "
                                      "crop, or change spec.dimensions"
                                      % (src_aspect, w_px, h_px, tgt_aspect,
                                         w_mm, h_mm))})
        else:
            checks.append({"check": "aspect_ratio", "status": "pass",
                           "detail": "matches spec within tolerance (%.3f vs %.3f)"
                                     % (src_aspect, tgt_aspect)})

    src_w_mm = w_px / dpi * 25.4
    src_h_mm = h_px / dpi * 25.4
    smaller = src_w_mm < w_mm * 0.9 or src_h_mm < h_mm * 0.9
    if smaller:
        checks.append({"check": "physical_size", "status": "warn",
                       "detail": ("at %d dpi this is %.1fx%.1fmm -- smaller than "
                                  "the ordered %gx%gmm, so --fix would upscale it"
                                  % (dpi, src_w_mm, src_h_mm, w_mm, h_mm))})
    else:
        checks.append({"check": "physical_size", "status": "pass",
                       "detail": "at %d dpi this is %.1fx%.1fmm (>= ordered "
                                 "%gx%gmm)" % (dpi, src_w_mm, src_h_mm,
                                               w_mm, h_mm)})


def _check_color_count(img, checks):
    """Estimated unique-colour count from a downsampled decode (memory-bounded)."""
    try:
        probe = img.copy()
        probe.thumbnail((256, 256), Image.LANCZOS)
        rgb = probe.convert("RGB")
        arr = np.asarray(rgb, dtype=np.uint8).reshape(-1, 3)
        unique = len(np.unique(arr.view([("", arr.dtype)] * 3)))
        tone = " (photo-like)" if unique > 1000 else (
            " (poster/flat)" if unique <= 64 else "")
        checks.append({"check": "color_count", "status": "info",
                       "detail": ("~%d unique colours%s; many colours trace to "
                                  "many paths/inks" % (unique, tone))})
    except Exception as exc:  # noqa: BLE001
        checks.append({"check": "color_count", "status": "info",
                       "detail": "could not estimate (%s)" % exc})


def _check_background(img, checks):
    """Border-ring uniformity: a cheap proxy for 'solid background vs photo'."""
    try:
        w, h = img.size
        ring = []
        for x in range(0, w, max(1, w // 64)):
            ring.append(img.getpixel((x, 0)))
            ring.append(img.getpixel((x, h - 1)))
        for y in range(0, h, max(1, h // 64)):
            ring.append(img.getpixel((0, y)))
            ring.append(img.getpixel((w - 1, y)))
        arr = np.asarray([px[:3] for px in ring], dtype=np.float64)
        std = float(arr.std())
        mean = tuple(int(round(c)) for c in arr.mean(axis=0))
        if std < BG_STDDEV_THRESHOLD:
            checks.append({"check": "background", "status": "pass",
                           "detail": ("border looks uniform (std %.1f, mean "
                                      "#%02x%02x%02x) -- likely a solid "
                                      "background" % (std, *mean))})
        else:
            checks.append({"check": "background", "status": "warn",
                           "detail": ("border varies (std %.1f) -- full-bleed or "
                                      "photographic; background removal may be "
                                      "needed before tracing" % std)})
    except Exception as exc:  # noqa: BLE001
        checks.append({"check": "background", "status": "info",
                       "detail": "could not sample (%s)" % exc})


def _checks_for(input_path, spec, img):
    checks = []
    ext = os.path.splitext(input_path)[1].lower()
    _check_format(ext, checks)
    _check_color_mode(img, checks)
    _check_alpha(img, checks)
    _check_geometry(img, spec, checks)
    _check_color_count(img, checks)
    _check_background(img, checks)
    return checks


# --------------------------------------------------------------------------
# FIX mode: optional transforms (opt-in, aspect-preserving, never crash)
# --------------------------------------------------------------------------

def _run_rembg(img, notes):
    exe = fc.which("rembg")
    if exe is None:
        # No CLI binary.  rembg is also usable as a plain Python module
        # (`pip install rembg` without the `[cli]` extra ships no binary but
        # does provide remove()).  Fall back to the module so a module-only
        # install is detected rather than reported as missing.
        try:
            import rembg as _rembg  # noqa: F401
            result = _rembg.remove(img.convert("RGB"))
            if result is None:
                # Defensive: a module-only install returning None would crash
                # the caller when it tries to save the image.  Treat it as a
                # skipped step instead.
                return img, {"ran": False,
                             "skipped": "rembg.remove returned None",
                             "install": "pip install 'rembg[cpu]' (module-only; "
                                        "rembg 2.x has no CLI binary)"}
            result = result.convert("RGBA")
            return result, {"ran": True, "tool": "rembg",
                            "via": "python module (no CLI)",
                            "version": fc.package_version("rembg")}
        except Exception:
            return img, {"ran": False, "skipped": "rembg not installed",
                         "install": "pip install 'rembg[cpu]' (module-only; "
                                    "rembg 2.x has no CLI binary)"}
    tmp_in = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp_out = tmp_in.name + ".rembg.png"
    try:
        img.convert("RGB").save(tmp_in.name, "PNG")
        proc = subprocess.run([exe, "i", tmp_in.name, tmp_out],
                              capture_output=True, text=True, timeout=1800)
        if proc.returncode != 0 or not os.path.exists(tmp_out):
            return img, {"ran": False, "skipped": "rembg exited non-zero",
                         "detail": (proc.stderr or proc.stdout or "")[:200]}
        return (Image.open(tmp_out).convert("RGBA"),
                {"ran": True, "tool": "rembg",
                 "version": fc.tool_version("rembg")})
    except (OSError, subprocess.SubprocessError) as exc:
        notes.append("rembg failed; continuing without it: %s" % exc)
        return img, {"ran": False, "skipped": "rembg error", "detail": str(exc)}
    finally:
        for path in (tmp_in.name, tmp_out):
            try:
                os.unlink(path)
            except OSError:
                pass


def _run_upscale(img, target, notes):
    """Upscale to the target, PRESERVING aspect ratio (fit within, no stretch)."""
    if target is None:
        return img, {"ran": False,
                     "skipped": "no target size (spec.dimensions/dpi missing)"}
    w, h = img.size
    src_aspect = w / h if h else 0.0
    tgt_w, tgt_h = target
    tgt_aspect = tgt_w / tgt_h if tgt_h else 0.0

    scale = min(tgt_w / w, tgt_h / h) if (w and h) else 1.0
    fit = (int(round(w * scale)), int(round(h * scale)))
    if scale <= 1.0:
        # the source already fills the target box in its binding dimension
        return img, {"ran": False, "skipped": "already at or above target size"}

    realesrgan = fc.which("realesrgan-ncnn-vulkan")
    if realesrgan is not None:
        needed = max(tgt_w / w, tgt_h / h)
        factor = 4 if needed > 3 else (3 if needed > 2 else 2)
        tmp_in = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp_out = tmp_in.name + ".esrgan.png"
        try:
            img.convert("RGB").save(tmp_in.name, "PNG")
            proc = subprocess.run(
                [realesrgan, "-i", tmp_in.name, "-o", tmp_out, "-s", str(factor)],
                capture_output=True, text=True, timeout=1800)
            if proc.returncode == 0 and os.path.exists(tmp_out):
                up = Image.open(tmp_out).convert("RGB")
                up = up.resize(fit, Image.LANCZOS)
                return up, {"ran": True, "tool": "realesrgan-ncnn-vulkan",
                            "scale": factor, "fit_px": list(fit),
                            "version": fc.tool_version("realesrgan-ncnn-vulkan")}
            notes.append("realesrgan failed; falling back to Pillow LANCZOS")
        except (OSError, subprocess.SubprocessError) as exc:
            notes.append("realesrgan failed (%s); falling back to Pillow "
                         "LANCZOS" % exc)
        finally:
            for path in (tmp_in.name, tmp_out):
                try:
                    os.unlink(path)
                except OSError:
                    pass

    img = img.resize(fit, Image.LANCZOS)
    note = ""
    if src_aspect > 0 and abs(src_aspect - tgt_aspect) / tgt_aspect > ASPECT_TOLERANCE:
        note = (" (aspect preserved %.3f -> %.3f; result fits within the target "
                "without stretching)" % (src_aspect, fit[0] / fit[1]))
    return img, {"ran": True, "tool": "pillow", "method": "LANCZOS",
                 "fit_px": list(fit), "aspect_preserved": True,
                 "note": note, "version": fc.package_version("Pillow")}


def _run_quantize(img, prep_colors, notes):
    exe = fc.which("pngquant")
    if exe is None:
        return img, {"ran": False, "skipped": "pngquant not installed",
                     "install": "apt-get install pngquant (or: brew install "
                                "pngquant)"}
    tmp_in = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp_out = tmp_in.name + ".quant.png"
    try:
        img.save(tmp_in.name, "PNG")
        proc = subprocess.run(
            [exe, "--force", "-o", tmp_out, str(prep_colors), "--", tmp_in.name],
            capture_output=True, text=True, timeout=600)
        if proc.returncode != 0 or not os.path.exists(tmp_out):
            return img, {"ran": False, "skipped": "pngquant exited non-zero",
                         "detail": (proc.stderr or proc.stdout or "")[:200]}
        result = Image.open(tmp_out)
        return (result.convert(img.mode),
                {"ran": True, "tool": "pngquant", "colors": int(prep_colors),
                 "version": fc.tool_version("pngquant")})
    except (OSError, subprocess.SubprocessError) as exc:
        notes.append("pngquant failed; continuing unquantised: %s" % exc)
        return img, {"ran": False, "skipped": "pngquant error", "detail": str(exc)}
    finally:
        for path in (tmp_in.name, tmp_out):
            try:
                os.unlink(path)
            except OSError:
                pass


def _run_flatten(img, background_hex):
    if img.mode not in ("RGBA", "LA"):
        return img, {"ran": False, "skipped": "no alpha channel"}
    if img.mode == "LA":
        img = img.convert("RGBA")
    background = Image.new("RGB", img.size, background_hex)
    background.paste(img, mask=img.split()[-1])
    return background.convert("RGB"), {"ran": True,
                                       "background_hex": background_hex}


def _invert(img, mode="photometric"):
    """Return the photometric inverse (or a negative) of an RGB(A) image.

    ``photometric`` maps each channel c -> 255 - c. That is the true colour
    inverse and is what a "wear a white tee under a black shirt" mock-up needs.
    ``negative`` inverts only the chroma channels and leaves alpha alone, which
    matters when the source carries transparency: inverting alpha would turn a
    transparent background opaque and destroy the matte.
    """
    from PIL import ImageOps
    if mode not in ("photometric", "negative"):
        raise ValueError("invert mode must be 'photometric' or 'negative'")
    # Palette images carry transparency in a tRNS index, not an alpha band.
    # Converting one to RGB drops the matte -- so `negative` must promote P/PA
    # to RGBA first, or the mode silently does the one thing it promises not to.
    if mode == "negative" and img.mode in ("P", "PA"):
        img = img.convert("RGBA")
    if mode == "negative" and img.mode in ("RGBA", "LA"):
        # Invert the colour bands, keep alpha untouched. Handle each mode by its
        # own band count: converting to RGB first would always give 4 bands and
        # then fail to merge back into a 2-band LA.
        bands = list(img.split())
        if img.mode == "RGBA":
            r, g, b, a = bands
            r, g, b = ImageOps.invert(Image.merge("RGB", (r, g, b))).split()
            return Image.merge("RGBA", (r, g, b, a))
        lum, a = bands                                   # LA
        lum = ImageOps.invert(lum)
        return Image.merge("LA", (lum, a))
    return ImageOps.invert(img.convert("RGB"))


def _write_inverse(img, out_dir, stem, spec, invert_mode="photometric"):
    """Write the inverted-colour twin beside the prepped original.

    Returns a dict describing the twin (or why there was not one). The twin is
    DERIVED, never a substitute: the original prepped file is still the one the
    pipeline traces unless a caller explicitly asks for the inverse. It exists
    so the same artwork can be traced against a light-on-dark reading, which is
    where VTracer's behaviour differs most -- see the note below.

    Why this is not just a colour swap: a trace's quality on dark-on-light art
    and light-on-dark art are not the same problem. Speckle filtering keys on
    local contrast, so a design that reads as clean on white can pick up a halo
    of spurious regions on black, and vice versa. Producing both twins at prep
    time means the sweep can compare the two readings instead of guessing.
    """
    popt = fc.print_options(spec)
    setting = popt.get("invert", False)
    inv_path = os.path.join(out_dir, "%s.prepped.inverse.png" % stem)
    if not setting:
        # Idempotency: a twin from an EARLIER run must not survive a run that
        # did not ask for one. `trace_sweep.py` and `run_record.py` glob
        # 01_prepped/, so a stale file would be picked up as if it were current.
        stale = None
        if os.path.exists(inv_path):
            try:
                os.unlink(inv_path)
                stale = inv_path
            except OSError as exc:
                return {"ran": False, "skipped": "spec.print.invert is not set",
                        "stale_file": inv_path,
                        "stale_remove_error": str(exc)}
        out = {"ran": False, "skipped": "spec.print.invert is not set"}
        if stale:
            out["stale_file_removed"] = stale
        return out
    if isinstance(setting, dict):
        invert_mode = setting.get("mode", invert_mode)
    try:
        twin = _invert(img, invert_mode)
    except Exception as exc:  # noqa: BLE001 - a bad mode is data, not a crash
        return {"ran": False, "error": "%s: %s" % (type(exc).__name__, exc)}

    twin.save(inv_path, "PNG")
    return {
        "ran": True,
        "file": inv_path,
        "sha256": fc.sha256_file(inv_path),
        "size_bytes": os.path.getsize(inv_path),
        "size_px": list(twin.size),
        "mode": invert_mode,
        "derived_from": "%s.prepped.png" % stem,
        "note": "colour-inverted twin; the original prepped file is unchanged "
                "and remains the traced input unless a caller asks otherwise",
    }


def _apply_fixes(img, spec, notes):
    popt = fc.print_options(spec)
    target, _dpi = _target(spec)
    steps = {"background_removal": None, "upscale": None,
             "quantize": None, "flatten_alpha": None}

    if popt.get("assume_opaque_bg"):
        steps["background_removal"] = {"ran": False,
                                       "skipped": "spec.print.assume_opaque_bg "
                                                  "is true"}
    else:
        img, steps["background_removal"] = _run_rembg(img, notes)

    img, steps["upscale"] = _run_upscale(img, target, notes)

    prep_colors = popt.get("prep_colors")
    if prep_colors is None:
        steps["quantize"] = {"ran": False,
                             "skipped": "spec.print.prep_colors is null"}
    else:
        img, steps["quantize"] = _run_quantize(img, int(prep_colors), notes)

    bg_hex = popt.get("background_hex") or "#ffffff"
    img, steps["flatten_alpha"] = _run_flatten(img, bg_hex)

    if img.mode != "RGB":
        img = img.convert("RGB")
    return img, steps


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def _prep(input_path, spec, out_dir, fix):
    stem = os.path.splitext(os.path.basename(input_path))[0]
    ext = os.path.splitext(input_path)[1].lower()
    notes = []

    out_path = os.path.join(out_dir, "%s.prepped.png" % stem)
    sidecar_path = os.path.join(out_dir, "%s.prep.json" % stem)

    # --- already-vector input: copy through unchanged ----------------------
    if ext in VECTOR_EXTS:
        dst = os.path.join(out_dir, os.path.basename(input_path))
        shutil.copy2(input_path, dst)
        # No raster to invert: there are no pixels to invert. Recorded
        # explicitly rather than silently omitted, so a consumer asking for the
        # twin can tell "not produced" from "not requested".
        inverse = {"ran": False,
                   "skipped": "input is already vector (%s); nothing to invert"
                              % ext}
        sidecar = {
            "tool": "prep_raster.py",
            "generated": _now(),
            "mode": "check",
            "input": {"file": input_path, "ext": ext,
                      "sha256": fc.sha256_file(input_path),
                      "size_bytes": os.path.getsize(input_path)},
            "already_vector": True,
            "output": {"file": dst, "sha256": fc.sha256_file(dst),
                       "size_bytes": os.path.getsize(dst)},
            "inverse": inverse,
            "notes": ["already vector, no prep needed"],
            "versions": {"pillow": fc.package_version("Pillow")},
        }
        fc.write_json(sidecar_path, sidecar)
        print("prep_raster: %s is already vector, no prep needed; copied to %s"
              % (input_path, dst))
        return 0

    if not os.path.exists(input_path):
        print("prep_raster: input not found: %s" % input_path, file=sys.stderr)
        return 2

    try:
        img = Image.open(input_path)
        orig_mode = img.mode
        orig_size = img.size
    except Exception as exc:  # noqa: BLE001 - a bad raster is a bad input
        print("prep_raster: cannot open %s: %s" % (input_path, exc),
              file=sys.stderr)
        return 2

    checks = _checks_for(input_path, spec, img)
    acceptable = not any(c["status"] == "fail" for c in checks)

    versions = {
        "rembg": fc.tool_version("rembg"),
        "realesrgan": fc.tool_version("realesrgan-ncnn-vulkan"),
        "pngquant": fc.tool_version("pngquant"),
        "pillow": fc.package_version("Pillow"),
    }

    if not fix:
        # CHECK mode: pass the raster through with pixels unchanged.  A PNG
        # source is copied byte-identical; any other raster container (JPEG,
        # TIFF, WEBP, ...) is re-encoded to PNG so the downstream tracer gets a
        # genuine PNG.  Re-encoding decodes and re-saves without touching a
        # pixel -- it changes the container, never the artwork.
        if img.format == "PNG":
            shutil.copy2(input_path, out_path)
            unchanged = True
        else:
            img.convert("RGB").save(out_path, "PNG")
            unchanged = False
        # The twin is derived from the file just written, so it always matches
        # the prepped original in SIZE and GEOMETRY. Note that `photometric`
        # (the default) inverts every channel and therefore flattens a
        # transparent background to opaque -- only `negative` preserves alpha.
        # See `_invert`.
        with Image.open(out_path) as _src:
            inverse = _write_inverse(_src, out_dir, stem, spec)
        sidecar = {
            "tool": "prep_raster.py",
            "generated": _now(),
            "mode": "check",
            "input": {"file": input_path, "ext": ext,
                      "sha256": fc.sha256_file(input_path),
                      "size_bytes": os.path.getsize(input_path),
                      "mode": orig_mode, "size_px": list(orig_size)},
            "already_vector": False,
            "output": {"file": out_path,
                       "sha256": fc.sha256_file(out_path),
                       "size_bytes": os.path.getsize(out_path),
                       "size_px": list(orig_size),
                       "unchanged": unchanged,
                       "container": "png"},
            "inverse": inverse,
            "acceptable": acceptable,
            "checks": checks,
            "notes": notes,
            "versions": versions,
        }
        fc.write_json(sidecar_path, sidecar)
        print("prep_raster: CHECK mode -- %s -> %s (%sacceptable)"
              % (input_path, out_path, "" if acceptable else "NOT "))
        if not unchanged:
            print("  (re-encoded %s -> PNG; pixels unchanged)" % img.format)
        for c in checks:
            print("  [%s] %s: %s" % (c["status"], c["check"], c["detail"]))
        missing = [tool for tool in
                   ("rembg", "realesrgan-ncnn-vulkan", "pngquant")
                   if versions.get(tool) is None]
        if missing:
            print("  --fix tools not installed: %s (install them, then re-run "
                  "with --fix)" % ", ".join(missing))
        print("  sidecar: %s" % sidecar_path)
        if inverse.get("ran"):
            print("  inverse twin: %s (%s)" % (inverse["file"], inverse["mode"]))
        print("  re-run with --fix to apply the optional transforms")
        return 0

    # FIX mode: apply the transforms, aspect-preserving.
    img, steps = _apply_fixes(img, spec, notes)
    img.save(out_path, "PNG")
    with Image.open(out_path) as _src:
        inverse = _write_inverse(_src, out_dir, stem, spec)

    sidecar = {
        "tool": "prep_raster.py",
        "generated": _now(),
        "mode": "fix",
        "input": {"file": input_path, "ext": ext,
                  "sha256": fc.sha256_file(input_path),
                  "size_bytes": os.path.getsize(input_path),
                  "mode": orig_mode, "size_px": list(orig_size)},
        "already_vector": False,
        "output": {"file": out_path, "sha256": fc.sha256_file(out_path),
                   "size_bytes": os.path.getsize(out_path),
                   "size_px": list(img.size)},
        "inverse": inverse,
        "acceptable": acceptable,
        "checks": checks,
        "steps": steps,
        "versions": versions,
        "notes": notes,
    }
    fc.write_json(sidecar_path, sidecar)

    print("prep_raster: FIX mode -- %s -> %s" % (input_path, out_path))
    for c in checks:
        if c["status"] in ("fail", "warn"):
            print("  [%s] %s: %s" % (c["status"], c["check"], c["detail"]))
    for note in notes:
        print("  NOTE: %s" % note)
    for label, key in (("background removal", "background_removal"),
                       ("upscale", "upscale"), ("quantise", "quantize"),
                       ("flatten alpha", "flatten_alpha")):
        step = steps[key]
        if step.get("ran"):
            extra = step.get("note", "")
            print("  %s: %s%s" % (label, step.get("tool") or step.get("method"),
                                  extra))
        else:
            print("  %s: skipped (%s)" % (label, step.get("skipped")))
    print("  sidecar: %s" % sidecar_path)
    if inverse.get("ran"):
        print("  inverse twin: %s (%s)" % (inverse["file"], inverse["mode"]))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="prep_raster.py",
        description="Check (or, with --fix, normalise) a raster for tracing "
                    "(pipeline stage 1). Default is check-only: report and "
                    "copy through unchanged.")
    parser.add_argument("input", help="raster file (PNG/JPG/TIFF) or an "
                                      "already-vector SVG/PDF")
    parser.add_argument("spec", help="path to spec.json")
    parser.add_argument("--out-dir", default=None,
                        help="output directory (default: <project>/01_prepped)")
    parser.add_argument("--fix", action="store_true",
                        help="apply the optional transforms (background "
                             "removal, upscale, quantise, flatten); default "
                             "is check-only")
    parser.add_argument("--invert", dest="invert", action="store_true",
                        default=None,
                        help="force the colour-inverted twin on (overrides "
                             "spec.print.invert)")
    parser.add_argument("--no-invert", dest="invert", action="store_false",
                        help="suppress the colour-inverted twin even when "
                             "spec.print.invert is set")
    parser.add_argument("--invert-mode", choices=("photometric", "negative"),
                        default=None,
                        help="photometric inverts every channel (a true colour "
                             "inverse); negative leaves alpha alone, which is "
                             "what a transparent matte needs (default "
                             "photometric)")
    args = parser.parse_args(argv)

    if not os.path.exists(args.spec):
        print("prep_raster: spec not found: %s" % args.spec, file=sys.stderr)
        return 2
    try:
        spec = fc.load_json(args.spec)
    except Exception as exc:  # noqa: BLE001
        print("prep_raster: cannot read spec %s: %s" % (args.spec, exc),
              file=sys.stderr)
        return 2

    if args.invert is not None:
        # CLI wins over the spec, without mutating the spec file on disk.
        # `spec["print"]` may be absent, null, or a non-dict in a hand-written
        # spec, and `setdefault` returns the existing None rather than
        # replacing it -- which used to die with a TypeError traceback here.
        # A bad spec is data: normalise it and carry on.
        if not isinstance(spec.get("print"), dict):
            spec["print"] = {}
        if not args.invert:
            spec["print"]["invert"] = False
        elif args.invert_mode is not None:
            spec["print"]["invert"] = {"mode": args.invert_mode}
        else:
            # `--invert` alone must NOT clobber a mode the spec already chose.
            # It used to write the argparse default ("photometric") over a spec
            # saying {"mode": "negative"}, so a user who chose negative
            # precisely because their art has a transparent matte silently got
            # photometric instead -- which flattens that matte.
            existing = spec["print"].get("invert")
            if existing is True or existing is None or existing is False:
                spec["print"]["invert"] = {"mode": "photometric"}

    project = os.path.dirname(SCRIPT_DIR)
    out_dir = args.out_dir or os.path.join(project, "01_prepped")
    os.makedirs(out_dir, exist_ok=True)

    return _prep(args.input, spec, out_dir, args.fix)


if __name__ == "__main__":
    sys.exit(main())
