# -*- coding: utf-8 -*-
"""The command vocabulary. The toolbar calls these; so do agents. There is no other path.

Every entry here is registered on the process-wide registry, which means it is simultaneously a
toolbar action, a command-palette entry and an MCP tool — the schema the agent server advertises
is generated from this module. A capability that is not registered here cannot be reached by an
agent, which is exactly the property that keeps the GUI and the API from drifting apart.

Conventions, because an agent reads them off the schema and a human reads them off the tooltip:

* Parts are addressed by their document `Name`, never by an index or a selection. Selection is a
  GUI notion and would make a command unreproducible.
* Anything that mutates the document declares `writes=True`, so the registry wraps it in a single
  undo transaction and rolls it back if it raises.
* A failure says what was expected. "no frame 'ceiling'; it has bore, top, bottom" is actionable;
  "KeyError" is not.
"""
from __future__ import annotations

import os

import FreeCAD

from . import checks as _checks
from . import document as _doc
from . import mates as _mates
from . import parts as _parts
from .params import params_path_for
from .registry import registry

#: Last check results per document name, for `get_checks` to report staleness against.
_LAST_CHECKS: dict[str, dict] = {}


def active_document():
    """The document commands act on. Raises with an instruction rather than returning None."""
    doc = FreeCAD.ActiveDocument
    if doc is None:
        docs = FreeCAD.listDocuments()
        if len(docs) == 1:
            doc = FreeCAD.getDocument(next(iter(docs)))
        else:
            raise RuntimeError(
                "no active document; create one first (File > New, or the insert_part command "
                "after opening a document)")
    return doc


def _part(name: str):
    doc = active_document()
    obj = doc.getObject(name)
    if obj is None:
        have = ", ".join(o.Name for o in _doc.ck_parts(doc)) or "none"
        raise KeyError(f"no part named {name!r} in {doc.Name}; have {have}")
    if not _doc.is_ck_part(obj):
        raise TypeError(f"{name!r} is not a constraint-kit part")
    return obj


def _frames(obj) -> dict:
    """Frames for a part, rebuilt from its current parameters (never cached, never stale)."""
    _, frames = _parts.build(getattr(obj, _doc.GENERATOR_PROP),
                             **_doc.paramset_of(obj).values())
    return frames


# ------------------------------------------------------------------------------------- read

@registry.command("list_generators", "List every parametric part generator and its defaults")
def list_generators() -> dict:
    return {name: {"defaults": _parts.defaults_for(name),
                   "doc": (fn.__doc__ or "").strip().split("\n")[0]}
            for name, fn in sorted(_parts.PARTS.items())}


@registry.command("get_tree", "The document's parts, with volumes and placements")
def get_tree() -> dict:
    doc = active_document()
    out = []
    for obj in _doc.ck_parts(doc):
        shape = getattr(obj, "Shape", None)
        pl = obj.Placement
        out.append({
            "name": obj.Name,
            "generator": getattr(obj, _doc.GENERATOR_PROP, ""),
            "volume_mm3": round(float(shape.Volume), 4) if shape and not shape.isNull() else 0.0,
            "position": [round(pl.Base.x, 6), round(pl.Base.y, 6), round(pl.Base.z, 6)],
            "guessed": _doc.paramset_of(obj).guessed(),
            "frames": sorted(_frames(obj)),
        })
    return {"document": doc.Name, "parts": out,
            "fingerprint": _checks.fingerprint(doc)}


@registry.command("get_params", "One part's parameters, each with where its value came from",
                  params={"part": {"type": str, "required": True,
                                   "doc": "the part's document Name"}})
def get_params(part: str) -> dict:
    obj = _part(part)
    p = _doc.paramset_of(obj)
    out = {}
    for key, v in p.items():
        entry = {"value": v.value, "provenance": v.provenance, "explain": v.explain()}
        for extra in ("source", "at", "confidence", "note"):
            got = getattr(v, extra)
            if got is not None:
                entry[extra] = got
        out[key] = entry
    return {"part": obj.Name, "generator": p.generator, "params": out,
            "guessed": p.guessed(), "summary": p.summary()}


@registry.command("run_checks", "Run the geometric checks and return structured verdicts",
                  params={"density": {"type": float, "default": 1.27,
                                      "doc": "g/cm3 for the mass roll-up; PETG by default"}})
def run_checks(density: float = 1.27) -> dict:
    doc = active_document()
    objs = _doc.ck_parts(doc)
    if not objs:
        raise RuntimeError(f"{doc.Name} has no constraint-kit parts to check")
    out = {
        "ok": True,
        "checks": {
            "solids": _checks.solids(objs),
            "interference": _checks.interference(objs),
            "seating": _checks.seating(objs),
            "mass": _checks.mass(objs, density_g_cm3=density),
        },
    }
    out["ok"] = all(c.get("ok", False) for c in out["checks"].values())
    _LAST_CHECKS[doc.Name] = out
    return out


@registry.command("get_checks", "The last check results, and whether the model has moved since")
def get_checks() -> dict:
    doc = active_document()
    last = _LAST_CHECKS.get(doc.Name)
    if last is None:
        return {"ran": False, "stale": True,
                "note": "no checks have been run on this document yet"}
    any_check = next(iter(last["checks"].values()))
    return {"ran": True, "ok": last["ok"],
            "stale": _checks.is_stale(any_check, doc),
            "checks": last["checks"]}


@registry.command("export", "Export every part as one STEP file",
                  params={"path": {"type": str, "required": True,
                                   "doc": "output path; .step or .stp"}})
def export(path: str) -> dict:
    import Part
    doc = active_document()
    objs = _doc.ck_parts(doc)
    if not objs:
        raise RuntimeError(f"{doc.Name} has nothing to export")
    shapes = [o.Shape for o in objs if getattr(o, "Shape", None) is not None]
    Part.export(objs, path)
    total = sum(s.Volume for s in shapes)
    return {"path": os.path.abspath(path), "parts": len(objs),
            "volume_mm3": round(float(total), 4)}


# ------------------------------------------------------------------------------------ write

@registry.command("insert_part", "Add a parametric part; unstated values arrive tagged as guesses",
                  writes=True,
                  params={"generator": {"type": str, "required": True,
                                        "doc": "a name from list_generators"},
                          "name": {"type": str, "default": "",
                                   "doc": "document name; defaults to the generator's"}})
def insert_part(generator: str, name: str = "") -> dict:
    doc = active_document()
    obj = _doc.make_part(doc, generator, None, name=name or None)
    doc.recompute()
    p = _doc.paramset_of(obj)
    return {"name": obj.Name, "generator": generator, "summary": p.summary(),
            "guessed": p.guessed(), "frames": sorted(_frames(obj))}


@registry.command("set_param", "Set one parameter and rebuild; the value becomes 'stated'",
                  writes=True,
                  params={"part": {"type": str, "required": True},
                          "name": {"type": str, "required": True,
                                   "doc": "the snake_case parameter name"},
                          "value": {"type": float, "required": True}})
def set_param(part: str, name: str, value: float) -> dict:
    obj = _part(part)
    _doc.set_param(obj, name, value)
    obj.Document.recompute()
    return {"part": obj.Name, "name": name, "value": value,
            "volume_mm3": round(float(obj.Shape.Volume), 4)}


@registry.command("promote_param", "Accept a guessed value as a decision, without changing it",
                  writes=True,
                  params={"part": {"type": str, "required": True},
                          "name": {"type": str, "required": True}})
def promote_param(part: str, name: str) -> dict:
    obj = _part(part)
    _doc.promote(obj, name)
    return {"part": obj.Name, "name": name,
            "summary": _doc.paramset_of(obj).summary()}


@registry.command("join", "Mate two parts by their named frames (seat = face to face)",
                  writes=True,
                  params={"kind": {"type": str, "required": True, "enum": sorted(_mates.KINDS),
                                   "doc": "seat puts faces in contact; align shares an axis"},
                          "target": {"type": str, "required": True},
                          "target_frame": {"type": str, "required": True},
                          "moving": {"type": str, "required": True},
                          "moving_frame": {"type": str, "required": True}})
def join(kind: str, target: str, target_frame: str, moving: str, moving_frame: str) -> dict:
    if kind not in _mates.KINDS:
        raise ValueError(f"unknown mate kind {kind!r}; have {sorted(_mates.KINDS)}")
    tgt, mov = _part(target), _part(moving)
    tf, mf = _frames(tgt), _frames(mov)
    if target_frame not in tf:
        raise ValueError(
            f"{target} has no frame {target_frame!r}; it has {', '.join(sorted(tf))}")
    if moving_frame not in mf:
        raise ValueError(
            f"{moving} has no frame {moving_frame!r}; it has {', '.join(sorted(mf))}")

    mov.Placement = _mates.KINDS[kind](tgt.Placement, tf[target_frame], mf[moving_frame])
    mov.Document.recompute()
    return {"kind": kind, "target": tgt.Name, "moving": mov.Name,
            "gap_mm": round(_mates.gap(tgt.Shape, mov.Shape), 9),
            "position": [round(v, 6) for v in tuple(mov.Placement.Base)]}


@registry.command("save_params", "Write the parameters to their text file — the source of truth",
                  writes=True,
                  params={"path": {"type": str, "default": "",
                                   "doc": "defaults to <Document>.params.json beside the file"}})
def save_params(path: str = "") -> dict:
    doc = active_document()
    target = path or params_path_for(doc.FileName)
    if not target:
        raise RuntimeError(
            "this document has never been saved, so there is nowhere to put its parameters; "
            "save the document first or pass an explicit path")
    _doc.save_document_params(doc, target)
    return {"path": os.path.abspath(target), "parts": len(_doc.ck_parts(doc))}


@registry.command("load_params", "Rebuild parts from a parameters file", writes=True,
                  params={"path": {"type": str, "required": True}})
def load_params(path: str) -> dict:
    doc = active_document()
    made = _doc.load_document_params(doc, path)
    doc.recompute()
    return {"path": os.path.abspath(path), "parts": [o.Name for o in made]}


@registry.command("undo", "Undo the last transaction", writes=False)
def undo() -> dict:
    doc = active_document()
    doc.undo()
    return {"document": doc.Name, "undo_count": doc.UndoCount}
