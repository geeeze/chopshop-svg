#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
front_common.py -- shared helpers for the front half of the print pipeline.

The front half (prep -> trace sweep -> compare) shares a handful of small,
boring utilities: spec loading, sha256, tool-version stamping, and the default
values for the ``print.*`` keys the front half owns.  Keeping them in one place
means the three scripts agree on exactly what "missing tool" and "default
prep_colors" mean.

Nothing here is specific to any one stage; the scripts import it by putting
their own directory (scripts/) on sys.path.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess

# Defaults for the print.* keys introduced by the front half (see
# spec.example.json).  These are the same defaults prep_raster.py and
# trace_sweep.py fall back to when a spec omits them, so a minimal spec still
# behaves sensibly.
PRINT_DEFAULTS = {
    "assume_opaque_bg": False,
    "prep_colors": 16,
    "background_hex": "#ffffff",
    "sweep_max_candidates": 12,
    "pitch_shift": False,
}

# The dpi used for prep/trace when spec.print.dpi is absent.
DEFAULT_DPI = 300


def load_json(path):
    """Read a JSON file.  Raises the underlying error on failure (the caller
    owns the friendly message)."""
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path, payload):
    """Write a JSON file atomically-ish (write then flush), pretty-printed."""
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def sha256_file(path):
    """Hex sha256 of a file's bytes, streamed so large files stay cheap."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def which(name):
    """shutil.which, short-named for call sites."""
    return shutil.which(name)


def package_version(name):
    """Version of an installed Python distribution, or None."""
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def tool_version(name):
    """Best-effort version string for a binary on PATH.

    Tries ``--version``, ``-v``, then ``-h`` and returns the first line of
    output.  Returns None when the binary is absent or unresponsive.  This is
    the front-half analogue of preflight.tool_versions(): the goal is a string
    that can be recorded in a JSON manifest so a result is still interpretable
    later, not a precise semver.

    Uses ``which()`` (not ``shutil.which`` directly) so presence detection and
    version-stamping share one source of truth -- otherwise a monkeypatched or
    overridden ``which`` and ``tool_version`` can disagree about whether a
    binary is present.
    """
    if which(name) is None:
        return None
    for probe in ([name, "--version"], [name, "-v"], [name, "-h"], [name, "--help"]):
        try:
            proc = subprocess.run(probe, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        text = (proc.stdout or proc.stderr or "").strip()
        if text:
            return text.splitlines()[0][:80]
    return None


def hex_to_rgb(hex_colour):
    """'#rrggbb' (or '#rgb') -> (r, g, b).  Returns None on malformed input."""
    value = (hex_colour or "").strip().lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) != 6:
        return None
    try:
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def print_options(spec):
    """Return the ``print`` block merged over the front-half defaults.

    Also carries ``dpi`` through to its default so callers can treat it as
    always present.  The spec's own values win.
    """
    block = dict(spec.get("print") or {})
    merged = dict(PRINT_DEFAULTS)
    merged.update(block)
    if merged.get("dpi") is None:
        merged["dpi"] = DEFAULT_DPI
    return merged


def substrate_hex(spec):
    """The fabric colour: the palette entry that is a substrate, not an ink.

    A screen-printed garment is not a white sheet. One palette colour may be
    the garment itself -- a black tee, a natural cotton -- and that colour is
    never laid down as ink. It still has to be declared in the palette,
    because the tracer and the validator both reason about declared colours,
    so tagging it rather than deleting it keeps one list of truth.

    Resolution order:
      1. ``print.substrate`` -- an explicit hex, wins outright.
      2. ``print.substrate_index`` -- an index into ``palette``.
      3. ``None`` -- nothing is declared as fabric. Callers must then infer,
         which is the old behaviour and is correct only for paper.

    A value that is in neither the palette nor a valid hex is a spec error, so
    it raises rather than silently degrading to "no substrate" and flooding
    the sheet again.
    """
    popt = print_options(spec)
    palette = [str(c) for c in (spec.get("palette") or [])]

    explicit = popt.get("substrate")
    if explicit:
        value = str(explicit).strip().upper()
        if value.startswith("#") and hex_to_rgb(value) is not None:
            return value
        if value in [p.upper() for p in palette]:
            return value
        raise ValueError(
            "print.substrate %r is neither a #rrggbb colour nor a member of "
            "spec.palette" % explicit)

    index = popt.get("substrate_index")
    if index is not None:
        try:
            idx = int(index)
        except (TypeError, ValueError):
            raise ValueError("print.substrate_index %r is not an integer" % index)
        if not 0 <= idx < len(palette):
            raise ValueError(
                "print.substrate_index %d is out of range for a %d-colour "
                "palette" % (idx, len(palette)))
        return palette[idx].upper()

    return None


def resolve_workers(n_tasks, explicit=None):
    """How many worker processes to use for ``n_tasks``.

    Priority: the explicit ``--workers`` value, then the ``FRONT_PIPELINE_WORKERS``
    environment variable, then ``min(cpu_count, n_tasks)``.  Never less than 1.
    """
    if explicit is not None:
        return max(1, int(explicit))
    env = os.environ.get("FRONT_PIPELINE_WORKERS")
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            pass
    return max(1, min(os.cpu_count() or 1, int(n_tasks)))


def _worker_init():
    """Neutralise the session D-Bus in each worker process.

    Inkscape (and other GTK/Gio tools) registers a GApplication on the session
    bus at startup; concurrent instances race on that connection and abort with
    ``Gio::DBus::Error`` (exit -6).  Pointing the bus at ``disabled:`` makes
    such tools skip D-Bus entirely, which is exactly what headless CLI export
    wants.  Mutates ``os.environ`` so every child subprocess inherits it.
    """
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = "disabled:"


def run_parallel(fn, tasks, workers=None):
    """Map ``fn`` over ``tasks`` in a process pool, results in task order.

    ``fn`` must be a module-level (picklable) callable taking one task as its
    single argument.  Falls back to a plain sequential map when there is nothing
    to gain (one task, one worker) or the pool cannot start: parallelism is an
    optimisation, never a requirement, so a pool failure must never take down a
    stage that would have run fine sequentially.
    """
    tasks = list(tasks)
    if not tasks:
        return []
    if workers is None:
        workers = resolve_workers(len(tasks))
    workers = max(1, int(workers))
    if workers <= 1 or len(tasks) <= 1:
        return [fn(t) for t in tasks]
    from concurrent.futures import ProcessPoolExecutor
    from concurrent.futures.process import BrokenProcessPool
    try:
        with ProcessPoolExecutor(max_workers=workers,
                                 initializer=_worker_init) as executor:
            return list(executor.map(fn, tasks))
    except (BrokenProcessPool, OSError):
        return [fn(t) for t in tasks]
