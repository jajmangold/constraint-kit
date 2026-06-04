"""Domain library loader (T6.1) — reusable assembly defs as versioned JSON packages under `libraries/`.
The screw→car→home leverage: compose pre-built subassemblies instead of modeling from scratch. A library
file is either a bare defs dict or `{"version": ..., "defs": {...}}`; the returned defs plug straight into
`assembly.build_tree`. Pure I/O over version-controlled JSON — no geometry here."""
from __future__ import annotations

import json
import os

_LIB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "libraries")


def list_libraries() -> list:
    if not os.path.isdir(_LIB_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(_LIB_DIR) if f.endswith(".json"))


def _load_raw(name: str) -> dict:
    path = os.path.join(_LIB_DIR, name + ".json")
    if not os.path.exists(path):
        raise ValueError(f"unknown library {name!r}; have {list_libraries()}")
    with open(path) as fh:
        return json.load(fh)


def load_library(name: str) -> dict:
    """Return the defs dict for a named library (ready for assembly.build_tree)."""
    raw = _load_raw(name)
    return raw.get("defs", raw)


def library_meta(name: str) -> dict:
    """Version + available roots for a library."""
    raw = _load_raw(name)
    defs = raw.get("defs", raw)
    return {"name": name, "version": raw.get("version"), "roots": sorted(defs)}
