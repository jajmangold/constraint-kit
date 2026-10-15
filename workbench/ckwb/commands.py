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
        have = ", ".join(o.Name for o in _doc.all_parts(doc)) or "none"
        raise KeyError(f"no part named {name!r} in {doc.Name}; have {have}")
    if not (_doc.is_ck_part(obj) or _doc.is_imported(obj)):
        raise TypeError(f"{name!r} is not a constraint-kit part")
    return obj


def _frames(obj) -> dict:
    """Frames for a part, derived fresh every time (never cached, so never stale).

    Semantic for a generated part, bounding-box-derived and prefixed `bbox_` for an import.
    """
    return _doc.frames_of(obj)


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
    for obj in _doc.all_parts(doc):
        shape = getattr(obj, "Shape", None)
        pl = obj.Placement
        imported = _doc.is_imported(obj)
        entry = {
            "name": obj.Name,
            "generator": (_doc.IMPORTED_GENERATOR if imported
                          else getattr(obj, _doc.GENERATOR_PROP, "")),
            "volume_mm3": round(float(shape.Volume), 4) if shape and not shape.isNull() else 0.0,
            "position": [round(pl.Base.x, 6), round(pl.Base.y, 6), round(pl.Base.z, 6)],
            "guessed": [] if imported else _doc.paramset_of(obj).guessed(),
            "frames": sorted(_frames(obj)),
        }
        if imported:
            entry["source"] = getattr(obj, _doc.SOURCE_PROP, "")
        out.append(entry)
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
    objs = _doc.all_parts(doc)
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
    objs = _doc.all_parts(doc)
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


@registry.command("plan", "Turn a plain-English request into a reviewable program (builds nothing)",
                  params={"request": {"type": str, "required": True,
                                      "doc": "what you want, in plain English"}})
def plan(request: str) -> dict:
    from . import planner as _planner
    from .settings import llm_settings

    cfg = llm_settings()
    p = _planner.plan(request, base_url=cfg["base_url"], api_key=cfg["api_key"],
                      model=cfg["planner_model"])
    if p.declined:
        return {"declined": True, "reason": p.reason, "nearest": p.nearest,
                "attempts": p.attempts}
    return {"declined": False, "parts": p.parts, "mates": p.mates, "notes": p.notes,
            "summary": p.summary(), "program": p.render(), "attempts": p.attempts}


@registry.command("describe", "Plan from plain English and build it in one step", writes=True,
                  params={"request": {"type": str, "required": True}})
def describe(request: str) -> dict:
    from . import planner as _planner
    from .settings import llm_settings

    doc = active_document()
    cfg = llm_settings()
    p = _planner.plan(request, base_url=cfg["base_url"], api_key=cfg["api_key"],
                      model=cfg["planner_model"])
    if p.declined:
        return {"declined": True, "reason": p.reason, "nearest": p.nearest, "built": []}
    made = _planner.build(p, doc)
    return {"declined": False, "built": [o.Name for o in made], "summary": p.summary(),
            "notes": p.notes, "program": p.render()}


@registry.command("import_step", "Read a STEP/IGES/BREP file in as imported geometry",
                  writes=True,
                  params={"path": {"type": str, "required": True,
                                   "doc": "path to a .step / .stp / .iges / .brep file"},
                          "name": {"type": str, "default": "",
                                   "doc": "document name; defaults to the file stem"},
                          "split": {"type": bool, "default": False,
                                    "doc": "one object per solid, for a vendor assembly"}})
def import_step(path: str, name: str = "", split: bool = False) -> dict:
    doc = active_document()
    made = _doc.import_step(doc, path, name=name or None, split=split)
    doc.recompute()
    objs = made if isinstance(made, list) else [made]
    total = sum(float(o.Shape.Volume) for o in objs)
    return {"name": objs[0].Name if len(objs) == 1 else [o.Name for o in objs],
            "imported": True,
            "source": getattr(objs[0], _doc.SOURCE_PROP, ""),
            "solids": sum(len(o.Shape.Solids) for o in objs),
            "volume_mm3": round(total, 4),
            "frames": sorted(_frames(objs[0]))}


@registry.command(
    "sweep",
    "Drive one part along a travel and check interference at every station",
    params={"moving": {"type": str, "required": True, "doc": "the part that travels"},
            "target": {"type": str, "required": True, "doc": "the part it travels along"},
            "target_frame": {"type": str, "required": True,
                             "doc": "frame on the target; travel runs along its +Z"},
            "moving_frame": {"type": str, "required": True},
            "start": {"type": float, "required": True, "doc": "mm"},
            "stop": {"type": float, "required": True, "doc": "mm"},
            "stations": {"type": int, "default": 11,
                         "doc": "positions sampled, both ends included"}})
def sweep(moving: str, target: str, target_frame: str, moving_frame: str,
          start: float, stop: float, stations: int = 11) -> dict:
    """Check a mechanism through its travel instead of at one pose.

    This is the check a static clash test cannot do. A gantry that clears at both ends of its
    rail can still hit a post halfway along, and nothing about examining either end reveals it.

    Declared read-only, and it keeps that promise: the moving part is restored to the placement
    it started from, in a `finally`, even if a station raises. A check that quietly leaves the
    model somewhere else would be a side effect wearing a verdict as a disguise.
    """
    from . import motion as _motion

    tgt, mov = _part(target), _part(moving)
    tf, mf = _frames(tgt), _frames(mov)
    if target_frame not in tf:
        raise ValueError(
            f"{target} has no frame {target_frame!r}; it has {', '.join(sorted(tf))}")
    if moving_frame not in mf:
        raise ValueError(
            f"{moving} has no frame {moving_frame!r}; it has {', '.join(sorted(mf))}")

    doc = tgt.Document
    others = [o for o in _doc.all_parts(doc) if o.Name != mov.Name]
    positions = _motion.sweep_stations(start, stop, stations)
    original = mov.Placement

    clash_stations, worst = [], None
    details = []
    try:
        for where in positions:
            mov.Placement = _motion.slide(tgt.Placement, tf[target_frame], mf[moving_frame],
                                          where)
            hit, gap = [], None
            for other in others:
                shape = getattr(other, "Shape", None)
                if shape is None or shape.isNull():
                    continue
                common = mov.Shape.common(shape)
                volume = common.Volume if not common.isNull() else 0.0
                if volume > _checks.CLASH_TOL_MM3:
                    hit.append({"with": other.Name, "volume_mm3": round(volume, 4)})
                else:
                    d = float(mov.Shape.distToShape(shape)[0])
                    gap = d if gap is None else min(gap, d)
            if hit:
                clash_stations.append(round(where, 6))
                details.append({"at_mm": round(where, 6), "clashes": hit})
            if gap is not None:
                worst = gap if worst is None else min(worst, gap)
    finally:
        mov.Placement = original
        doc.recompute()

    return _checks._stamp(doc, {
        "check": "sweep",
        "ok": not clash_stations,
        "moving": mov.Name,
        "along": f"{tgt.Name}.{target_frame}",
        "start_mm": float(start),
        "stop_mm": float(stop),
        "stations": len(positions),
        "clash_stations": clash_stations,
        "clashes": details,
        "min_gap_mm": round(worst, 6) if worst is not None else None,
    })


@registry.command(
    "analyse",
    "Deflection, stress and safety factor for one part. You choose where it is held and "
    "where it is pushed; nothing is inferred",
    params={"part": {"type": str, "required": True},
            "fix": {"type": str, "required": True,
                    "doc": "where it is HELD, as a selector over placed coordinates in mm: "
                           "'x<=-59', or 'x>=24@xz' to restrain two axes as a roller does"},
            "load": {"type": str, "default": "",
                     "doc": "where the force is applied, same selector language. Use this for "
                            "a single load; use `cases` for several"},
            "force": {"type": list, "default": [0.0, 0.0, -100.0],
                      "doc": "total force in newtons as [fx, fy, fz], divided over the "
                             "selected material nodes"},
            "cases": {"type": list, "default": None,
                      "doc": "several named LOAD CASES instead of one load: a list of "
                             "{name, load, force}. Solved on one assembled matrix and "
                             "enveloped ELEMENTWISE, so the reported safety factor is never "
                             "better than the worst case and `governs_by_element` says whether "
                             "any single case is the design case at all"},
            "material": {"type": str, "default": "petg",
                         "doc": "petg, pla, abs, al6061 or steel"},
            "pitch": {"type": float, "default": 2.0, "doc": "voxel pitch in mm"}})
def analyse(part: str, fix: str, load: str = "", force: list = None,
            cases: list = None, material: str = "petg", pitch: float = 2.0) -> dict:
    """Is it strong enough? The question the agent surface could not previously ask.

    `physics.stiffness` existed and was verified against beam theory, and it was not a registered
    command — so an agent could insert a part, mate it, check it for interference and export it,
    and could not find out whether it would hold. A closed loop from parameters to performance
    cannot exist while the loop is not connected.

    Two things are deliberately NOT done here. **The boundary conditions are not guessed.**
    Choosing where a part is held and where it is pushed is the engineering; a tool that infers
    them produces something that looks like analysis. And **one mesh is not a converged answer** —
    the verdict carries its pitch and element count so a number is never quoted without the
    resolution it came from. Call it at two or three pitches and watch whether the answer moves.

    For a printed material the interlayer verdict comes back as well, without being asked for,
    because the isotropic answer is the wrong default for a part made of welds.
    """
    from . import physics as _physics

    obj = _part(part)
    if cases and (load or force not in (None, [0.0, 0.0, -100.0])):
        raise ValueError(
            "give either one load/force or several cases, not both. The two are different "
            "questions with different answers, and choosing a reading for you is how a safety "
            "factor comes to mean something nobody asked for.")
    if not cases and not load:
        raise ValueError("a load selector is needed, or a list of cases")
    out = _physics.stiffness(obj, material=material, pitch=float(pitch), fix=fix, load=load,
                             force=tuple(float(v) for v in (force or [0.0, 0.0, -100.0])),
                             cases=cases)
    return _checks._stamp(obj.Document, out)


@registry.command("undo", "Undo the last transaction", writes=False)
def undo() -> dict:
    doc = active_document()
    doc.undo()
    return {"document": doc.Name, "undo_count": doc.UndoCount}
