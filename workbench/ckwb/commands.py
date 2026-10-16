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
from . import requirements as _req
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
                         "doc": "positions sampled, both ends included"},
            "motion": {"type": str, "default": "slide",
                       "doc": "slide (a rail) or rack (a pinion that must also turn)"},
            "teeth": {"type": int, "default": 0, "doc": "pinion teeth, for motion=rack"},
            "module": {"type": float, "default": 0.0, "doc": "module, for motion=rack"},
            "rack_teeth": {"type": int, "default": 0,
                           "doc": "teeth on the rack, for motion=rack"}})
def sweep(moving: str, target: str, target_frame: str, moving_frame: str,
          start: float, stop: float, stations: int = 11, motion: str = "slide",
          teeth: int = 0, module: float = 0.0, rack_teeth: int = 0) -> dict:
    """Check a mechanism through its travel instead of at one pose.

    This is the check a static clash test cannot do. A gantry that clears at both ends of its
    rail can still hit a post halfway along, and nothing about examining either end reveals it.

    It answers two questions, not one. Does the moving part hit anything, and **is it still on
    the thing it is travelling along**. The second is not a refinement of the first: a carriage
    driven off the end of its rail touches nothing, so an interference check alone calls the
    worst position in the travel the best one. Measured, before this check existed: a carriage
    swept 800 mm past the end of a 200 mm shaft reported `ok` with its worst gap unchanged.

    `start` and `stop` are millimetres of travel whichever motion is used, because that is the
    number a machine's travel is quoted in. **`motion="rack"` is not optional dressing**: a
    pinion sliding along a rack without turning ploughs its teeth straight through the rack's,
    so sweeping a rack drive as a plain slide reports a collision at every station and reports
    it as a design fault rather than as the wrong kinematics. A ball nut needs no such mode --
    it translates and does not turn, so a slide is already exactly right for it.

    Declared read-only, and it keeps that promise: the moving part is restored to the placement
    it started from, in a `finally`, even if a station raises. A check that quietly leaves the
    model somewhere else would be a side effect wearing a verdict as a disguise.
    """
    from . import motion as _motion

    kind = str(motion or "slide").strip().lower()
    if kind not in ("slide", "rack"):
        raise ValueError(f"motion must be 'slide' or 'rack', got {motion!r}")
    if kind == "rack":
        missing = [n for n, v in (("teeth", teeth), ("module", module),
                                  ("rack_teeth", rack_teeth)) if not v]
        if missing:
            raise ValueError(
                "motion='rack' needs %s: a rack drive's travel is set by the pinion's size, and "
                "the rack's length is what says when the pinion runs off the end"
                % ", ".join(missing))

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

    # The travel axis, and how far the TARGET reaches along it. Measured once, in world
    # coordinates, from the same frame the travel itself runs along.
    axis_world = tgt.Placement.multiply(tf[target_frame])
    axis_origin = axis_world.Base
    axis_dir = axis_world.Rotation.multVec(FreeCAD.Vector(0, 0, 1))
    support = _motion.axis_extent(tgt.Shape, axis_origin, axis_dir)

    # What counts as "still on the support" is NOT the same question for the two motions, and
    # treating it as one asks a pinion to behave like a carriage.
    #
    #   slide: the moving part must be wholly WITHIN the support's extent. A carriage hanging
    #          over the end of its rail is a carriage held up by nothing.
    #   rack:  only the pinion's CENTRE need be over the rack, because that is what keeps teeth
    #          engaged. Its body overhanging the rack's end is what a pinion at the end of its
    #          travel always does, and demanding containment would refuse the last tip radius
    #          of travel at each end for no physical reason. `rack_drive` enforces the centre
    #          limit itself, by arithmetic, and raises -- so there is nothing to collect here.
    rack_span = _motion.rack_length(rack_teeth, module) if kind == "rack" else None

    clash_stations, worst = [], None
    details = []
    off_support, limits = [], None

    def place(where: float):
        if kind == "rack":
            return _motion.rack_drive(tgt.Placement, tf[target_frame], mf[moving_frame],
                                      turns=_motion.rack_turns(teeth, module, where),
                                      teeth=teeth, module=module, rack_teeth=rack_teeth)
        return _motion.slide(tgt.Placement, tf[target_frame], mf[moving_frame], where)

    try:
        for where in positions:
            mov.Placement = place(where)
            # Is the moving part still ON the thing it is travelling along? This has to be asked
            # separately, because a carriage driven off the end of its rail collides with
            # nothing and so reads as the CLEANEST station in the sweep -- a measured false pass
            # (a carriage run 800 mm past the end of a 200 mm shaft reported ok with its worst
            # gap unchanged).
            if kind == "slide":
                reach = _motion.axis_extent(mov.Shape, axis_origin, axis_dir,
                                            conservative=True)
                # Recomputed at every station and kept at its TIGHTEST, because a moving part
                # need not present the same length to the axis all the way along.
                here = _motion.travel_limits(support[0], support[1], reach[0], reach[1],
                                             at=where)
                if limits is None:
                    limits = here
                else:
                    limits = {"min_mm": max(limits["min_mm"], here["min_mm"]),
                              "max_mm": min(limits["max_mm"], here["max_mm"]),
                              "usable_mm": min(limits["usable_mm"], here["usable_mm"]),
                              "fits": limits["fits"] and here["fits"]}
                over = max(support[0] - reach[0], reach[1] - support[1])
                if over > _motion.SUPPORT_TOL_MM:
                    off_support.append({"at_mm": round(where, 6),
                                        "overhang_mm": round(over, 4)})
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
        "ok": not clash_stations and not off_support,
        "moving": mov.Name,
        "along": f"{tgt.Name}.{target_frame}",
        "motion": kind,
        "start_mm": float(start),
        "stop_mm": float(stop),
        "stations": len(positions),
        "clash_stations": clash_stations,
        "clashes": details,
        "min_gap_mm": round(worst, 6) if worst is not None else None,
        # Travel limits are part of the verdict, not a footnote: a sweep that leaves the support
        # is a failure even though every station is clear of everything.
        "travel_limits": (limits if kind == "slide"
                          else {"min_mm": 0.0, "max_mm": round(rack_span, 6),
                                "usable_mm": round(rack_span, 6), "fits": True}),
        "off_support": off_support,
        "off_support_stations": [o["at_mm"] for o in off_support],
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
            "pitch": {"type": float, "default": 2.0, "doc": "voxel pitch in mm"},
            "buckling": {"type": int, "default": 0,
                         "doc": "number of buckling modes to find, 0 for none. Worth asking "
                                "for on anything slender: a stress check is BLIND to "
                                "stability, and a strut sits nowhere near its allowable right "
                                "up until it folds"},
            "rules": {"type": dict, "default": None, "doc": "printability rules as {min_wall_mm, min_channel_mm, max_overhang_deg, max_thin_fraction, build_volume_mm, build_dir} -- any subset; the rest take sensible 0.4 mm nozzle defaults. Reports unsupported_area_mm2, thin_fraction, narrow_fraction, fits_build_volume and a single `printable` flag, all as plain numbers so they can be CONSTRAINED rather than audited"}})
def analyse(part: str, fix: str, load: str = "", force: list = None,
            cases: list = None, material: str = "petg", pitch: float = 2.0,
            buckling: int = 0, rules: dict = None) -> dict:
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
                             cases=cases, buckling=int(buckling), rules=rules)
    return _checks._stamp(obj.Document, out)


@registry.command(
    "optimise",
    "Find the best parameter values that still satisfy every requirement. You choose the "
    "requirements; the search reports which one binds",
    params={"part": {"type": str, "required": True},
            "parameters": {"type": list, "required": True,
                           "doc": "what may change: a list of {name, low, high, step?} in the "
                                  "part own units. `step` puts it on a lattice -- a 0.8 mm wall "
                                  "rather than 0.73"},
            "objective": {"type": str, "default": "mass_g",
                          "doc": "the metric to MINIMISE: mass_g, max_disp_mm, stress_p99_mpa "
                                 "or compliance"},
            "constraints": {"type": list, "default": None,
                            "doc": "requirements as {name, metric, op, limit}, op being >= or "
                                   "<=. e.g. {name: strength, metric: safety_factor, op: '>=', "
                                   "limit: 1.5}"},
            "fix": {"type": str, "required": True, "doc": "where the part is HELD"},
            "load": {"type": str, "default": "", "doc": "where the force is applied"},
            "force": {"type": list, "default": [0.0, 0.0, -100.0], "doc": "newtons [fx, fy, fz]"},
            "cases": {"type": list, "default": None,
                      "doc": "several named load cases instead of one load; the optimum is then "
                             "taken against the ENVELOPE over them"},
            "material": {"type": str, "default": "petg"},
            "pitch": {"type": float, "default": 4.0,
                      "doc": "voxel pitch in mm. Coarser than you would quote a result at: an "
                             "optimizer wants many cheap evaluations, and the shape of the "
                             "answer is what matters here. Re-analyse the winner properly"},
            "size": {"type": str, "default": "",
                     "doc": "optional: one parameter to BISECT to the constraint boundary while "
                            "the rest are searched. The right choice when one dimension is pure "
                            "sizing (a thickness) and the others are shape"},
            "tol": {"type": float, "default": 0.05,
                    "doc": "parameter tolerance in mm. 0.05 is finer than a printer holds"},
            "buckling": {"type": int, "default": 0,
                         "doc": "buckling modes per evaluation, 0 for none. Set 1 and add "
                                "{metric: buckling_factor, op: '>=', limit: 2.0} when the part "
                                "is slender: making something light is exactly what finds a "
                                "stability failure a stress check cannot see"},
            "rules": {"type": dict, "default": None, "doc": "printability rules as {min_wall_mm, min_channel_mm, max_overhang_deg, max_thin_fraction, build_volume_mm, build_dir} -- any subset; the rest take sensible 0.4 mm nozzle defaults. Reports unsupported_area_mm2, thin_fraction, narrow_fraction, fits_build_volume and a single `printable` flag, all as plain numbers so they can be CONSTRAINED rather than audited"},
            "max_evaluations": {"type": int, "default": 60,
                                "doc": "budget. Each evaluation is a mesh and a solve, so this "
                                       "is a wall-clock decision"}})
def optimise(part: str, parameters: list, fix: str, objective: str = "mass_g",
             constraints: list = None, load: str = "", force: list = None,
             cases: list = None, material: str = "petg", pitch: float = 4.0,
             size: str = "", tol: float = 0.05, buckling: int = 0,
             rules: dict = None, max_evaluations: int = 60) -> dict:
    """Parameters -> geometry -> physics -> better parameters, closed.

    Everything this needs already existed and was verified separately: `set_param` rebuilds the
    geometry from a parameter, `analyse` says whether the result holds, and
    `constraint_kit.optimise` does the search against closed-form optima in its own suite. This
    is the wiring, and the wiring is the point -- a loop that is not connected is not a loop.

    Three things it is careful about.

    **It is declared read-only and keeps the promise.** A search sets parameters dozens of
    times; every one is restored in a `finally`, because a search that leaves the model at its
    last trial value has edited the document while reporting a verdict.

    **The pitch is deliberately coarse.** An optimizer wants many cheap evaluations, and what it
    needs from each is the right *shape* of answer, not a quotable number. Re-run `analyse` on
    the winner at a proper pitch -- and the report says so.

    **The binding constraint is the real output.** Knowing the answer is 7.3 mm is useful;
    knowing that stiffness rather than strength stopped you is what tells you to add a rib
    instead of more material.

    Two constraints worth adding by default. `rules={...}` turns printability on and
    `{"metric": "printable", "op": ">=", "limit": 1.0}` then refuses any design the process
    cannot make -- which is the whole of issue #18: an optimizer asked for the lightest part
    will return a 0.9 mm wall at an 80 degree overhang unless something in its problem statement
    says otherwise, and auditing that afterwards is a loop with a human in it.

    And one on a flexible part:
    `{"name": "small deflection", "metric": "linear_valid", "op": ">=", "limit": 1.0}`. The
    physics is linear, so a design that deflects through a tenth of its own span is outside what
    the solver can speak about -- and without that constraint the lightest design in the box is
    very often exactly such a design. Booleans are reported as 1.0 / 0.0 precisely so this can
    be written.
    """
    import sys as _sys

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    if root not in _sys.path:
        _sys.path.insert(0, root)
    from constraint_kit import optimise as _opt

    obj = _part(part)
    doc = obj.Document
    meta = _doc.paramset_of(obj)
    known = sorted(name for name, _v in meta.items())

    specs = []
    for i, entry in enumerate(parameters or []):
        if not isinstance(entry, dict):
            raise ValueError("parameter %d must be an object with name, low and high" % i)
        name = str(entry.get("name") or "").strip()
        if name not in meta:
            raise ValueError(
                "%s has no parameter %r; it has %s" % (part, name, ", ".join(known)))
        specs.append(_opt.Parameter(name=name, low=float(entry["low"]),
                                    high=float(entry["high"]),
                                    step=(float(entry["step"]) if entry.get("step") else None)))
    if not specs:
        raise ValueError("at least one parameter must be free to change")

    wants = []
    for i, entry in enumerate(constraints or []):
        if not isinstance(entry, dict):
            raise ValueError("constraint %d must be an object with name, metric, op and limit" % i)
        wants.append(_opt.Constraint(name=str(entry.get("name") or "c%d" % i),
                                     key=str(entry["metric"]),
                                     op=str(entry["op"]),
                                     limit=float(entry["limit"])))

    original = {p.name: _doc.paramset_of(obj)[p.name].value for p in specs}

    # A voxel grid quantises the geometry, so the search cannot resolve a parameter finer than
    # the mesh can see it. Measured at 4 mm pitch: a plate 7 mm thick and one 10 mm thick sample
    # to the same 300 elements and report the same safety factor to three decimals. Asking for
    # 0.05 mm from that is chasing a plateau, and the answer that comes back is only good to
    # about one pitch. Said rather than enforced -- a coarse search is a legitimate choice -- but
    # never left for the reader to discover.
    warnings = []
    if float(tol) < float(pitch) / 2.0:
        warnings.append(
            "tol is %.3g mm but the voxel pitch is %.3g mm, so the geometry is quantised far "
            "more coarsely than the search tolerance. The result is resolved to about one pitch, "
            "not to tol; either coarsen tol or refine the pitch."
            % (float(tol), float(pitch)))

    def evaluate(x: dict) -> dict:
        for name, value in x.items():
            _doc.set_param(obj, name, value)
        doc.recompute()
        return _physics_stiffness(obj, material, pitch, fix, load, force, cases,
                                  int(buckling), rules)

    try:
        if size:
            if size not in {p.name for p in specs}:
                raise ValueError(
                    "size=%r is not one of the free parameters (%s)"
                    % (size, ", ".join(sorted(p.name for p in specs))))
            found = _opt.minimise_sized(
                evaluate, specs, size=size, objective=objective, constraints=wants,
                tol=float(tol), max_evaluations=int(max_evaluations))
        else:
            found = _opt.minimise(
                evaluate, specs, objective=objective, constraints=wants,
                tol=float(tol), max_evaluations=int(max_evaluations))
    except _opt.InfeasibleBox as exc:
        best = exc.best
        return _checks._stamp(doc, {
            "check": "optimise",
            "feasible": False,
            "reason": str(exc),
            "best": dict(best.x) if best else {},
            "metrics": {k: v for k, v in (best.metrics if best else {}).items()
                        if not isinstance(v, (dict, list))},
            "violations": list(best.violations) if best else [],
            "warnings": warnings,
            "note": "no setting of these parameters within these bounds satisfies the "
                    "requirements. Widen a bound, loosen a requirement, or change the material "
                    "-- this is not reachable by sizing.",
        })
    finally:
        # Read-only means read-only, even on the exception paths.
        for name, value in original.items():
            _doc.set_param(obj, name, value)
        doc.recompute()

    return _checks._stamp(doc, {
        "check": "optimise",
        "feasible": True,
        "best": {k: round(float(v), 6) for k, v in found.best.x.items()},
        "objective": objective,
        "objective_value": round(float(found.best.objective), 6),
        "metrics": {k: v for k, v in found.best.metrics.items()
                    if not isinstance(v, (dict, list))},
        # Both notions, because they answer different questions. `blocking` is what refused
        # every better design the search tried -- evidence, and the useful answer. `active` is
        # whether the optimum sits exactly on a constraint surface, which on a voxel-quantised
        # evaluation is often false even when the constraint is unambiguously the limit.
        "blocking_constraints": found.blocking_constraints,
        "active_constraints": found.active_constraints,
        "at_bounds": found.at_bounds,
        "evaluations": found.evaluations,
        "cache_hits": found.cache_hits,
        "failures": found.failures,
        "converged": found.converged,
        "method": found.method,
        "report": found.report(),
        "warnings": warnings,
        "note": "the document was restored to the values it started with; nothing was changed. "
                "Apply the winner with set_param, then re-run analyse at a finer pitch -- %.2f "
                "mm was chosen for search speed, not for a quotable number." % float(pitch),
    })


def _physics_stiffness(obj, material, pitch, fix, load, force, cases,
                       buckling: int = 0, rules=None) -> dict:
    """One evaluation, flattened to the metrics an objective and a constraint can name."""
    from . import physics as _physics

    out = _physics.stiffness(
        obj, material=material, pitch=float(pitch), fix=fix, load=load,
        force=tuple(float(v) for v in (force or [0.0, 0.0, -100.0])), cases=cases,
        buckling=buckling, rules=rules)
    # A member that does not buckle at all reports None; for an optimizer that is an unbounded
    # factor, not a missing metric, or a constraint on it would stop resolving exactly where the
    # design got safe.
    if buckling and out.get("buckling_factor") is None:
        out = dict(out)
        out["buckling_factor"] = float("inf")
    flat = {}
    for key, value in out.items():
        if isinstance(value, bool):
            # Booleans become 1.0 / 0.0 rather than being dropped, which makes them
            # CONSTRAINABLE -- and `linear_valid` is the one that matters. Without this an
            # optimizer could happily return the lightest design in the box while that design
            # deflects through a tenth of its own span, where the linear solve it was judged by
            # does not hold. `{metric: linear_valid, op: ">=", limit: 1.0}` refuses those
            # outright, which is the right way to say "only designs the physics can speak about".
            flat[key] = 1.0 if value else 0.0
        elif isinstance(value, (int, float)):
            flat[key] = float(value)
    # `safety_factor` is None when there is no stress at all, which an optimizer cannot order.
    # Treat it as unbounded rather than dropping the key, so a constraint on it still resolves.
    if out.get("safety_factor") is None:
        flat["safety_factor"] = float("inf")
    return flat


# ------------------------------------------------------------------------------ requirements

@registry.command(
    "set_requirement",
    "State a requirement: a named value with a unit and a reason. Driven parameters follow it",
    writes=True,
    params={"name": {"type": str, "required": True,
                     "doc": "a plain identifier, because expressions read it"},
            "value": {"type": float, "required": True},
            "unit": {"type": str, "default": "", "doc": "mm, N, Nm, deg -- whatever it is"},
            "note": {"type": str, "default": "",
                     "doc": "WHY. A number whose reason lives in somebody head is the thing "
                            "requirements exist to replace"}})
def set_requirement(name: str, value: float, unit: str = "", note: str = "") -> dict:
    """A requirement is the top of the chain: everything derived or driven follows from here."""
    doc = active_document()
    out = _req.set_requirement(doc, name, value, unit=unit, note=note)
    return _checks._stamp(doc, {"requirement": name, **out})


@registry.command(
    "derive_requirement",
    "Define a value computed from other requirements by an explicit relation",
    writes=True,
    params={"name": {"type": str, "required": True},
            "expression": {"type": str, "required": True,
                           "doc": "arithmetic over other requirement names, e.g. "
                                  "'load * span / 1000'. Evaluated by a strict AST whitelist, "
                                  "never Python eval"},
            "unit": {"type": str, "default": ""},
            "note": {"type": str, "default": ""}})
def derive_requirement(name: str, expression: str, unit: str = "", note: str = "") -> dict:
    doc = active_document()
    out = _req.derive(doc, name, expression, unit=unit, note=note)
    return _checks._stamp(doc, {"requirement": name, **out})


@registry.command(
    "drive_param",
    "Bind a part's parameter to an expression over requirements, so geometry follows intent",
    writes=True,
    params={"part": {"type": str, "required": True},
            "param": {"type": str, "required": True},
            "expression": {"type": str, "required": True,
                           "doc": "e.g. 'deck_span' or 'span + 2 * margin'"}})
def drive_param(part: str, param: str, expression: str) -> dict:
    """This is the one that makes the dependency real rather than remembered.

    The bound parameter is tagged `derived`, never `stated`, so the property editor shows a
    computed value as computed and names the expression it came from.
    """
    doc = active_document()
    out = _req.drive(doc, part, param, expression)
    return _checks._stamp(doc, out)


@registry.command("release_param", "Stop a parameter following a requirement", writes=True,
                  params={"part": {"type": str, "required": True},
                          "param": {"type": str, "required": True}})
def release_param(part: str, param: str) -> dict:
    doc = active_document()
    out = _req.release(doc, part, param)
    return _checks._stamp(doc, out)


@registry.command("get_requirements",
                  "Every requirement, what it derives, what it drives, and whether the "
                  "geometry has caught up")
def get_requirements() -> dict:
    doc = active_document()
    is_stale, reasons = _req.stale(doc)
    try:
        values = _req.solve(doc)
        error = None
    except _req.RequirementError as exc:
        values, error = {}, str(exc)
    return _checks._stamp(doc, {
        "requirements": _req.requirements(doc),
        "values": values,
        "driven": _req.driven(doc),
        "targets": _req.targets(doc),
        "stale": is_stale,
        "stale_params": reasons,
        "error": error,
    })


@registry.command(
    "verify_requirements",
    "Check every recorded target against a real analysis. Met or unmet, with the number",
    params={"part": {"type": str, "required": True},
            "fix": {"type": str, "required": True, "doc": "where the part is HELD"},
            "load": {"type": str, "default": "", "doc": "where the force is applied"},
            "force": {"type": list, "default": [0.0, 0.0, -100.0], "doc": "newtons"},
            "cases": {"type": list, "default": None, "doc": "named load cases instead"},
            "material": {"type": str, "default": "petg"},
            "pitch": {"type": float, "default": 2.0},
            "buckling": {"type": int, "default": 0},
            "rules": {"type": dict, "default": None,
                      "doc": "printability rules, if a target names one of their metrics"}})
def verify_requirements(part: str, fix: str, load: str = "", force: list = None,
                        cases: list = None, material: str = "petg", pitch: float = 2.0,
                        buckling: int = 0, rules: dict = None) -> dict:
    """A requirement nobody checks is a comment. This is what turns one into a verdict.

    The load case is an argument for the same reason it is everywhere else here: choosing where
    a part is held and pushed IS the engineering, and a tool that guesses produces something
    that looks like verification.
    """
    doc = active_document()
    out = _req.verify(doc, part=part, material=material, pitch=float(pitch), fix=fix,
                      load=load,
                      force=tuple(float(v) for v in (force or [0.0, 0.0, -100.0])),
                      cases=cases, buckling=int(buckling), rules=rules)
    return _checks._stamp(doc, out)


@registry.command(
    "mobility",
    "How many degrees of freedom the mechanism has, from the joints you declare, with the "
    "loop count beside it",
    params={"joints": {"type": list, "required": True,
                       "doc": "a list of {kind, a, b}: revolute, prismatic, screw, belt, gear, "
                              "gear_rack, cylindrical, spherical, planar, fixed (plus aliases "
                              "like hinge, slider, rack). A screw leaves ONE freedom, not two"},
            "grounded": {"type": list, "default": None,
                         "doc": "parts fixed to the world. However many you name they count as "
                                "one body, which is what grounding means"},
            "bodies": {"type": list, "default": None,
                       "doc": "which parts to count; defaults to every part in the document"},
            "spatial": {"type": bool, "default": False,
                        "doc": "false for a planar mechanism, which is most of what is on this "
                               "bench -- a four-bar, a slider-crank and a belt drive are all "
                               "planar, and the spatial formula gives three times too much"}})
def mobility(joints: list, grounded: list = None, bodies: list = None,
             spatial: bool = False) -> dict:
    """Can the thing you just assembled actually move, and by how many inputs?

    Mobility is a property of the joint GRAPH rather than of the geometry, which is why this
    needs no solver: Grubler and Kutzbach wrote the arithmetic down in the 1880s. It answers the
    question #11-#14 were really asking -- DOF analysis and loop closure -- without the
    undocumented FreeCAD joint format that was thought to be blocking it.

    It is a COUNT and not a proof, and the report says so whenever a loop exists: the formula
    assumes the joint constraints are independent, and in special geometries they are not.
    """
    from . import mobility as _mob

    doc = active_document()
    present = [o.Name for o in _doc.all_parts(doc)]
    use = [str(b) for b in bodies] if bodies else present
    for name in use:
        if name not in present:
            raise KeyError(
                f"no part named {name!r} in {doc.Name}; have {', '.join(present) or 'none'}")
    for i, joint in enumerate(joints or []):
        for role in ("a", "b"):
            who = (joint or {}).get(role)
            if who not in use:
                raise KeyError(
                    f"joint {i} connects {who!r}, which is not a part being counted; "
                    f"have {', '.join(use) or 'none'}")

    out = _mob.mobility(bodies=use, joints=joints or [], grounded=grounded or [],
                        spatial=bool(spatial))
    return _checks._stamp(doc, {
        "check": "mobility",
        "mobility": out.mobility,
        "bodies": out.bodies,
        "joints": out.joints,
        "loops": out.loops,
        "spatial": out.spatial,
        "grounded": out.grounded,
        "floating": out.floating,
        "over_constrained": out.over_constrained,
        "is_structure": out.is_structure,
        "report": out.report(),
    })


@registry.command("undo", "Undo the last transaction", writes=False)
def undo() -> dict:
    doc = active_document()
    doc.undo()
    return {"document": doc.Name, "undo_count": doc.UndoCount}
