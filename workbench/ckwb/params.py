# -*- coding: utf-8 -*-
"""The params file — the source of truth, in text, next to the document.

This module is the discipline that keeps the workbench from re-creating the problem it exists to
solve. FreeCAD's document is a superb *live editing surface*: a dependency graph, recompute,
undo, a property editor. It is a poor *archive*: it is binary, it cannot be diffed or reviewed,
and saving it changes the bytes whether or not anything moved. A project whose model lives only
inside an .FCStd accumulates one-way edits until nobody can reconstruct how it got there.

So: parameters live here, in `<Document>.params.json`. The .FCStd is a cache of this file.
Open reads it, save writes it, and the file is what belongs in git.

Two properties are load-bearing and both are tested as invariants:

* **Exact round-trip.** If a save/load loses provenance or precision, the claim above is a
  slogan. `float` is written as JSON number, so it round-trips to full double precision.
* **Byte stability.** Keys sorted, fixed separators, trailing newline, absent rather than null
  for unset fields. Two saves of the same parameters are the same bytes, so a rebuild does not
  show up as a diff and the history stays readable.
"""
from __future__ import annotations

import json
import os

from .provenance import ParamSet, Value

FORMAT = "constraint-kit/params"
VERSION = 1

#: Keys written only when they carry something. Absent beats null in a file people read.
_OPTIONAL = ("source", "at", "confidence", "note")


def params_path_for(doc_filename: str | None) -> str | None:
    """`.../Bear.FCStd` -> `.../Bear.params.json`; None for a document never saved.

    Returning None rather than inventing a temp path is deliberate: an unsaved document has no
    home yet, and writing the source of truth somewhere the user did not choose is how files get
    lost. The caller prompts for a save instead.
    """
    if not doc_filename:
        return None
    base, _ = os.path.splitext(doc_filename)
    return base + ".params.json"


def to_dict(p: ParamSet) -> dict:
    params = {}
    for name, v in p.items():
        entry = {"value": v.value, "provenance": v.provenance}
        for key in _OPTIONAL:
            got = getattr(v, key)
            if got is not None:
                entry[key] = got
        params[name] = entry
    return {"format": FORMAT, "version": VERSION, "generator": p.generator, "params": params}


def from_dict(raw: dict) -> ParamSet:
    fmt = raw.get("format")
    if fmt != FORMAT:
        raise ValueError(
            f"not a constraint-kit params file: format is {fmt!r}, expected {FORMAT!r}")
    version = raw.get("version")
    if version != VERSION:
        raise ValueError(
            f"params file version {version!r} is not supported (this build reads {VERSION}); "
            "refusing rather than guessing at a schema it does not know")
    p = ParamSet(raw.get("generator", ""))
    for name, entry in raw.get("params", {}).items():
        if "value" not in entry:
            raise ValueError(f"param {name!r} has no value")
        p.put(name, Value(
            entry["value"],
            entry.get("provenance", "stated"),
            source=entry.get("source"),
            at=entry.get("at"),
            confidence=entry.get("confidence"),
            note=entry.get("note"),
        ))
    return p


def save_params(p: ParamSet, path: str) -> str:
    """Write the params file. Stable bytes; written whole, never appended to."""
    text = json.dumps(to_dict(p), indent=2, sort_keys=True,
                      separators=(",", ": "), ensure_ascii=False) + "\n"
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return path


def load_params(path: str) -> ParamSet:
    with open(path, encoding="utf-8") as fh:
        return from_dict(json.load(fh))
