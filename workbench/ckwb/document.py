# -*- coding: utf-8 -*-
"""The document object: parameters as FreeCAD properties, geometry rebuilt from them.

One `Part::FeaturePython` per part, whose properties *are* its parameters. FreeCAD then supplies
for free the things that would otherwise be months of UI work: a dependency graph, recompute on
edit, undo/redo, the expression engine (`=Width * 2`), and a property editor that is already the
right shape for a provenance inspector.

**Provenance rides on the property group.** A guessed value lands in a group called
"Guessed for you" and a stated one in "You specified", so the standard FreeCAD property editor
shows the distinction with no custom widget at all. The authoritative copy lives in a hidden
`CkProvenance` JSON property so nothing is lost on save, and `promote()` moves a value between
groups when a human accepts a guess.

**The .FCStd is a cache; `save_document_params` writes the source of truth.** That is the whole
discipline of this workbench, and it is why `execute()` always rebuilds from parameters and
never reads its own Shape back. That also makes the placement-baking trap structurally
impossible: `obj.Shape` returns geometry with `Placement` already applied, so a read-modify-write
on a posed part bakes the pose in permanently — but nothing here ever reads Shape to modify it.
"""
from __future__ import annotations

import json
import math
import re

import FreeCAD

from . import parts
from .params import FORMAT, VERSION
from .provenance import LABELS, STATED, ParamSet, Value

#: Properties the workbench owns on every part. Prefixed so they never collide with a parameter.
GENERATOR_PROP = "CkGenerator"
PROVENANCE_PROP = "CkProvenance"
GROUP_INTERNAL = "ConstraintKit"

_TYPE_PROPS = {float: "App::PropertyFloat", int: "App::PropertyInteger",
               str: "App::PropertyString", bool: "App::PropertyBool"}


def prop_name(param: str) -> str:
    """`bore_d` -> `BoreD`, `id_` -> `Id`. FreeCAD properties are CamelCase by convention.

    The reverse mapping is never computed by un-camelling -- it is stored explicitly in
    `CkProvenance`, because `id_` and `bore_d` do not round-trip through a naming rule.
    """
    cleaned = re.sub(r"[^0-9a-zA-Z_]", "_", param).rstrip("_")
    return "".join(part[:1].upper() + part[1:] for part in cleaned.split("_") if part) or "Param"


def _meta(obj) -> dict:
    try:
        return json.loads(getattr(obj, PROVENANCE_PROP) or "{}")
    except (ValueError, AttributeError):
        return {}


def _write_meta(obj, meta: dict) -> None:
    setattr(obj, PROVENANCE_PROP, json.dumps(meta, sort_keys=True, separators=(",", ":")))


class CkPart:
    """Proxy for a parametric constraint-kit part."""

    def __init__(self, obj=None):
        if obj is not None:
            obj.Proxy = self

    def execute(self, obj):
        """Rebuild the shape from the current properties. The only path to geometry."""
        generator = getattr(obj, GENERATOR_PROP, "")
        if not generator:
            return
        values = paramset_of(obj).values()
        shape, frames = parts.build(generator, **values)
        obj.Shape = shape
        # Frames are derived, so they are recomputed rather than stored: a stale frame is a
        # mate that silently points at the wrong place.
        self.frames = frames

    def onDocumentRestored(self, obj):
        obj.Proxy = self

    def dumps(self):
        return None

    def loads(self, state):
        return None

    # FreeCAD 0.x names; kept so a document saved by either version restores.
    __getstate__ = dumps
    __setstate__ = loads


def make_part(doc, generator: str, params: ParamSet | None = None, name: str | None = None):
    """Create a parametric part in `doc`. `params` defaults to the generator's own signature.

    Defaulting from the signature is what makes an unspecified value *visibly* a guess: every
    parameter the caller did not state arrives tagged `default` and lands in the guessed group.
    """
    if generator not in parts.PARTS:
        raise KeyError(f"no generator named {generator!r}; have {sorted(parts.PARTS)}")

    if params is None:
        params = ParamSet(generator)
        for key, default in parts.defaults_for(generator).items():
            params.set(key, default, "default")
    elif params.generator != generator:
        raise ValueError(
            f"params are for generator {params.generator!r} but part is {generator!r}")

    obj = doc.addObject("Part::FeaturePython", name or generator)
    CkPart(obj)
    obj.addProperty("App::PropertyString", GENERATOR_PROP, GROUP_INTERNAL,
                    "Which parametric generator builds this part")
    setattr(obj, GENERATOR_PROP, generator)
    obj.addProperty("App::PropertyString", PROVENANCE_PROP, GROUP_INTERNAL,
                    "Where each value came from (JSON; edited through the workbench)")
    obj.setEditorMode(PROVENANCE_PROP, 2)   # hidden: authoritative, but not hand-editable

    meta = {}
    for key, value in params.items():
        ptype = type(value.value)
        prop_type = _TYPE_PROPS.get(ptype)
        if prop_type is None:
            raise TypeError(
                f"parameter {key!r} has unsupported type {ptype.__name__}; "
                f"supported: {', '.join(t.__name__ for t in _TYPE_PROPS)}")
        pname = prop_name(key)
        obj.addProperty(prop_type, pname, LABELS[value.provenance], value.explain())
        setattr(obj, pname, value.value)
        entry = {"prop": pname, "provenance": value.provenance}
        for extra in ("source", "at", "confidence", "note"):
            got = getattr(value, extra)
            if got is not None:
                entry[extra] = got
        meta[key] = entry
    _write_meta(obj, meta)
    return obj


def is_ck_part(obj) -> bool:
    return hasattr(obj, GENERATOR_PROP) and hasattr(obj, PROVENANCE_PROP)


def ck_parts(doc) -> list:
    """Every constraint-kit part in the document, in document order."""
    return [o for o in doc.Objects if is_ck_part(o)]


def paramset_of(obj) -> ParamSet:
    """Read the live properties back into a ParamSet, provenance intact.

    Values come from the properties (so a hand edit in the property editor is picked up) while
    provenance comes from the stored metadata. A value a human typed over is still tagged
    however it was tagged -- `promote()` is the explicit way to claim it.
    """
    out = ParamSet(getattr(obj, GENERATOR_PROP, ""))
    for key, entry in _meta(obj).items():
        pname = entry.get("prop") or prop_name(key)
        if not hasattr(obj, pname):
            raise AttributeError(
                f"{obj.Name}: parameter {key!r} expects property {pname!r}, which is missing; "
                "the document and its provenance metadata have diverged")
        out.put(key, Value(
            getattr(obj, pname),
            entry.get("provenance", STATED),
            source=entry.get("source"), at=entry.get("at"),
            confidence=entry.get("confidence"), note=entry.get("note")))
    return out


def promote(obj, param: str) -> None:
    """Accept a guessed value: re-tag it as stated and move it into the specified group."""
    meta = _meta(obj)
    if param not in meta:
        raise KeyError(f"{obj.Name} has no parameter {param!r}; have {sorted(meta)}")
    meta[param]["provenance"] = STATED
    _write_meta(obj, meta)
    obj.setGroupOfProperty(meta[param].get("prop") or prop_name(param), LABELS[STATED])


def set_param(obj, param: str, value) -> None:
    """Set one parameter by its snake_case name, and mark it stated -- a human chose it."""
    meta = _meta(obj)
    if param not in meta:
        raise KeyError(f"{obj.Name} has no parameter {param!r}; have {sorted(meta)}")
    pname = meta[param].get("prop") or prop_name(param)
    current = getattr(obj, pname)
    setattr(obj, pname, type(current)(value) if not isinstance(value, type(current)) else value)
    if meta[param].get("provenance") != STATED:
        promote(obj, param)


# --------------------------------------------------------------- the text source of truth

def document_dict(doc) -> dict:
    """The whole document's parts as a plain dict, ready to be written as the source of truth."""
    out = []
    for obj in ck_parts(doc):
        p = paramset_of(obj)
        entry = {"name": obj.Name, "generator": p.generator, "params": {}}
        for key, v in p.items():
            pe = {"value": v.value, "provenance": v.provenance}
            for extra in ("source", "at", "confidence", "note"):
                got = getattr(v, extra)
                if got is not None:
                    pe[extra] = got
            entry["params"][key] = pe
        if not obj.Placement.isIdentity():
            pl = obj.Placement
            entry["placement"] = {
                "position": [pl.Base.x, pl.Base.y, pl.Base.z],
                "axis": [pl.Rotation.Axis.x, pl.Rotation.Axis.y, pl.Rotation.Axis.z],
                "angle": pl.Rotation.Angle,
            }
        out.append(entry)
    return {"format": FORMAT, "version": VERSION, "parts": out}


def save_document_params(doc, path: str) -> str:
    """Write every part's parameters as text. Stable bytes so a rebuild is not a diff."""
    text = json.dumps(document_dict(doc), indent=2, sort_keys=True,
                      separators=(",", ": "), ensure_ascii=False) + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return path


def load_document_params(doc, path: str) -> list:
    """Rebuild parts in `doc` from a params file. Refuses a file it does not understand."""
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    if raw.get("format") != FORMAT:
        raise ValueError(f"not a constraint-kit params file: format {raw.get('format')!r}")
    if raw.get("version") != VERSION:
        raise ValueError(
            f"params version {raw.get('version')!r} unsupported (this build reads {VERSION})")

    made = []
    for entry in raw.get("parts", []):
        p = ParamSet(entry["generator"])
        for key, pe in entry.get("params", {}).items():
            p.put(key, Value(pe["value"], pe.get("provenance", STATED),
                             source=pe.get("source"), at=pe.get("at"),
                             confidence=pe.get("confidence"), note=pe.get("note")))
        obj = make_part(doc, entry["generator"], p, name=entry.get("name"))
        pl = entry.get("placement")
        if pl:
            # The file stores the rotation angle in radians (as FreeCAD reports it) while
            # FreeCAD.Rotation's axis/angle constructor takes degrees.
            obj.Placement = FreeCAD.Placement(
                FreeCAD.Vector(*pl["position"]),
                FreeCAD.Rotation(FreeCAD.Vector(*pl["axis"]), math.degrees(pl["angle"])))
        made.append(obj)
    return made
