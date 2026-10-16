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
import os
import re

import FreeCAD

from . import parts
from .mates import frame
from .params import FORMAT, VERSION
from .provenance import LABELS, STATED, ParamSet, Value

#: Properties the workbench owns on every part. Prefixed so they never collide with a parameter.
GENERATOR_PROP = "CkGenerator"
PROVENANCE_PROP = "CkProvenance"
SOURCE_PROP = "CkSource"
GROUP_INTERNAL = "ConstraintKit"

#: Shown where a generator name would go, so a bought component is never mistaken for a made one.
IMPORTED_GENERATOR = "(imported)"

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

    # MAKE IT ACTUALLY DRAW. A `Part::FeaturePython` that has a data proxy and no view proxy
    # gets an EMPTY DisplayMode enumeration, so it sits in the tree with a perfectly good shape,
    # `Visibility = True`, and nothing at all in the 3D view. Measured in a real GUI session:
    #
    #   native Part::Feature          mode='Flat Lines'  enum=4 modes  isVisible=True
    #   Part::FeaturePython, as here  mode=None          enum=[]       isVisible=False
    #   ...with ViewObject.Proxy = 0  mode='Flat Lines'  enum=4 modes  isVisible=True
    #
    # `Proxy = 0` is FreeCAD's sentinel for "attach the default C++ view provider"; a Python
    # view-provider class works too but adds a class to maintain, and returning the four modes
    # from `getDisplayModes` duplicates the enumeration to eight entries in the property editor.
    #
    # This hid for a long time because SAVING AND REOPENING the document fixes it -- on restore
    # FreeCAD attaches the view provider properly -- so the geometry was only ever invisible in
    # the session that created it, which is exactly the session where somebody is looking.
    #
    # `ViewObject` is None under freecadcmd, so this is a no-op headless.
    view = getattr(obj, "ViewObject", None)
    if view is not None:
        view.Proxy = 0
    return obj


def is_ck_part(obj) -> bool:
    """A parametric part: has a generator and provenance, so it rebuilds from its values."""
    return hasattr(obj, GENERATOR_PROP) and hasattr(obj, PROVENANCE_PROP)


def is_imported(obj) -> bool:
    """Geometry read from a file. Has a shape and a source, but no generator and no parameters."""
    return hasattr(obj, SOURCE_PROP) and not is_ck_part(obj)


def ck_parts(doc) -> list:
    """Parametric parts only, in document order. This is what the params file describes."""
    return [o for o in doc.Objects if is_ck_part(o)]


def all_parts(doc) -> list:
    """Everything with geometry the workbench manages: parametric AND imported.

    The distinction matters in exactly one direction. The params file can only describe
    parametric parts, so `ck_parts` is right there. Checks, mass, export and physics must see
    bought components too — a vendor bearing escaping clash detection is the failure that makes
    the whole verdict worthless, so those all use this.
    """
    return [o for o in doc.Objects if is_ck_part(o) or is_imported(o)]


def frames_of(obj) -> dict:
    """Named mate frames for any part.

    A parametric part declares real, semantic frames and they are rebuilt from its current
    values. An import has none: a vendor STEP carries no statement about which face is a
    mounting face. Rather than invent one, the bounding box is offered under names that say so —
    `bbox_top`, never `top`. The prefix is the honesty.
    """
    if is_ck_part(obj):
        _, frames = parts.build(getattr(obj, GENERATOR_PROP), **paramset_of(obj).values())
        return frames
    if not is_imported(obj):
        raise TypeError(f"{getattr(obj, 'Name', obj)} is not a workbench part")

    shape = getattr(obj, "Shape", None)
    if shape is None or shape.isNull():
        return {}
    # Local coordinates: the caller composes with obj.Placement, exactly as for a generated part.
    local = shape.copy()
    local.Placement = FreeCAD.Placement()
    bb = local.BoundBox
    cx, cy, cz = (bb.XMin + bb.XMax) / 2.0, (bb.YMin + bb.YMax) / 2.0, (bb.ZMin + bb.ZMax) / 2.0
    return {
        "bbox_top": frame((cx, cy, bb.ZMax), (0, 0, 1)),
        "bbox_bottom": frame((cx, cy, bb.ZMin), (0, 0, -1)),
        "bbox_x+": frame((bb.XMax, cy, cz), (1, 0, 0)),
        "bbox_x-": frame((bb.XMin, cy, cz), (-1, 0, 0)),
        "bbox_y+": frame((cx, bb.YMax, cz), (0, 1, 0)),
        "bbox_y-": frame((cx, bb.YMin, cz), (0, -1, 0)),
        "bbox_centre": frame((cx, cy, cz), (0, 0, 1)),
    }


def import_step(doc, path: str, name: str | None = None, split: bool = False):
    """Read a STEP/IGES/BREP file into the document as imported geometry.

    Returns one object, or a list of them when `split` is set. `split` exists because a vendor
    assembly legitimately holds many solids while a generated part never should: keeping it whole
    preserves the file as supplied, splitting it lets each solid be mated and checked separately.
    """
    import Part

    if not os.path.isfile(path):
        raise FileNotFoundError(f"no such file: {path}")
    try:
        shape = Part.read(path)
    except Exception as exc:  # noqa: BLE001 - OCC raises a bare Exception on a bad file
        raise ValueError(f"cannot read {os.path.basename(path)} as CAD geometry: {exc}") from exc
    if shape is None or shape.isNull() or not shape.Solids:
        raise ValueError(
            f"{os.path.basename(path)} contains no solids — a surface-only or empty file cannot "
            "be checked, weighed or analysed, so it is refused rather than imported as nothing")

    base = name or os.path.splitext(os.path.basename(path))[0]
    shapes = list(shape.Solids) if split else [shape]
    made = []
    for i, piece in enumerate(shapes):
        obj = doc.addObject("Part::Feature", f"{base}_{i + 1}" if split and len(shapes) > 1
                            else base)
        obj.Shape = piece
        obj.addProperty("App::PropertyString", SOURCE_PROP, GROUP_INTERNAL,
                        "The file this geometry was read from")
        setattr(obj, SOURCE_PROP, os.path.abspath(path))
        obj.setEditorMode(SOURCE_PROP, 1)   # read-only: editing it would be a lie
        made.append(obj)
    return made if split else made[0]


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
