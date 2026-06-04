"""Assemble a spec into a placed cadquery.Assembly + compute mass/bbox, and export STEP+GLB.

Spec shape (validated loosely; the planner emits exactly this via guided JSON):
{
  "parts": [ {"id","type","params":{...},"material"} , ... ],
  "mates": [ {"a","a_joint","b","b_joint","type"} , ... ]    # ordered
}

The first part is fixed at the origin; each mate places its B part relative to the running world
location of its A part, so chains (A->B->C) compose. This is the deterministic ordered solve.
"""
from __future__ import annotations

import hashlib
import json
import math
import os

import cadquery as cq

from . import mates
from .parts import DENSITY_G_MM3, PART_GENS
from .parts_bd import PART_GENS_BD

# native cadquery parts + build123d/bd_warehouse catalog parts share one dispatch table.
ALL_PART_GENS = {**PART_GENS, **PART_GENS_BD}

# Content-hash part-generation cache (T3.1). The cost is part generation (cq_gears, IsoThread); identical
# (type, params) yields identical geometry, so we memoize it. Caveat: cadquery may mutate an object's .loc
# on assembly.add, so the cached wp is NEVER handed out directly — every fetch returns an independent COPY
# (translate by 0 = a fresh transformed shape), keeping the cache pristine and callers isolated.
_GEN_CACHE: dict[str, tuple] = {}
_GEN_STATS = {"hits": 0, "misses": 0}


def _gen_key(ptype: str, params: dict) -> str:
    return ptype + ":" + hashlib.sha256(
        json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()


def _generate(ptype: str, params: dict):
    """Generate a part (type, params) through the cache, returning an independent copy each time."""
    key = _gen_key(ptype, params)
    cached = _GEN_CACHE.get(key)
    if cached is None:
        _GEN_STATS["misses"] += 1
        cached = ALL_PART_GENS[ptype](**params)
        _GEN_CACHE[key] = cached
    else:
        _GEN_STATS["hits"] += 1
    wp, anchors = cached
    return wp.translate((0, 0, 0)), dict(anchors)     # copy -> cache stays pristine, callers isolated


def cache_stats() -> dict:
    """Part-generation cache hit/miss counters (T3.1)."""
    return {**_GEN_STATS, "entries": len(_GEN_CACHE)}


def clear_cache() -> None:
    _GEN_CACHE.clear()
    _GEN_STATS.update(hits=0, misses=0)


def _shape(wp: cq.Workplane) -> cq.Shape:
    return wp.val() if isinstance(wp, cq.Workplane) else wp


def _pid(ps: dict) -> str:
    # tolerate the model emitting "name" instead of "id" (schema not always enforced).
    pid = ps.get("id") or ps.get("name")
    if not pid:
        raise ValueError(f"part missing id/name: {ps}")
    return pid


def _apply_finish(wp, finish: dict | None):
    """OPT-IN composable post-ops (T1.1 fillet/chamfer + T1.2 shell), applied in a fixed order
    shell → chamfer → fillet so a housing can do `{"shell": 2, "fillet": 1}`. Schema:
    `{"shell"?: t, "open_face"?: selector, "chamfer"?: r, "fillet"?: r, "edges"?: selector}`.
    `shell` hollows to wall thickness t (sealed); `open_face` removes a face for an open shell.
    fillet/chamfer default to ALL edges (deterministic); an optional `edges` selector is fragile across
    param changes (research R1.a). FAIL-SOFT per op: an impossible op is skipped (keeps the prior solid)
    with an ok:false entry — never breaks the build. Returns (wp, {ok, applied:[...]} | None)."""
    if not finish:
        return wp, None
    applied: list[dict] = []
    if "shell" in finish:
        try:
            t = float(finish["shell"])
            of = finish.get("open_face")
            wp = wp.faces(of).shell(-t) if of else wp.shell(-t)
            applied.append({"op": "shell", "ok": True, "thickness": t, "open_face": of or "none"})
        except Exception as exc:  # noqa: BLE001
            applied.append({"op": "shell", "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    sel = finish.get("edges")
    for op in ("chamfer", "fillet"):
        if op not in finish:
            continue
        try:
            r = float(finish[op])
            edges = wp.edges(sel) if sel else wp.edges()
            wp = edges.chamfer(r) if op == "chamfer" else edges.fillet(r)
            applied.append({"op": op, "ok": True, "radius": r, "edges": sel or "all"})
        except Exception as exc:  # noqa: BLE001 -- best-effort; keep the prior solid
            applied.append({"op": op, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    if not applied:
        return wp, {"ok": False, "applied": [], "error": "finish needs shell/chamfer/fillet"}
    return wp, {"ok": all(a["ok"] for a in applied), "applied": applied}


def build_assembly(spec: dict):
    if not spec.get("parts"):
        raise ValueError("spec has no parts")

    # resolve specs FIRST (may inject resolved major_diameter+pitch into threaded_rod params) so the
    # resolved values drive geometry; the summary is attached to the report after the build.
    spec_meta = _resolve_specs_prepass(spec)

    parts: dict[str, dict] = {}
    for ps in spec["parts"]:
        ptype = ps["type"]
        if ptype not in ALL_PART_GENS:
            raise ValueError(f"unknown part type {ptype!r}; known: {sorted(ALL_PART_GENS)}")
        params = ps.get("params", {})
        wp, anchors = _generate(ptype, params)             # content-hash cache (T3.1)
        wp, finish = _apply_finish(wp, ps.get("finish"))   # opt-in fillet/chamfer post-op
        parts[_pid(ps)] = {
            "wp": wp, "anchors": anchors, "params": params,
            "material": ps.get("material", "steel"), "type": ptype,
            "fallback": "_fallback" in anchors, "finish": finish,
        }

    # deterministic ordered placement: part 0 at origin, then apply mates in order. A mate whose target
    # B is ALREADY positioned closes a loop — the tree solver cannot honor it (B can't be in two places).
    # We skip such mates here and surface them; a DOF solver is needed to close them (see find_loops).
    locs: dict[str, cq.Location] = {pid: cq.Location() for pid in parts}
    positioned = {_pid(spec["parts"][0])}
    placed = set(positioned)
    loop_constraints: list[dict] = []
    for m in spec.get("mates", []):
        if m["b"] in positioned:
            loop_constraints.append(m)        # loop-closing constraint -> needs the DOF solver
            continue
        b, loc = _apply_mate(parts, locs, m)
        locs[b] = loc
        positioned.add(b)
        placed.add(b)

    assy = cq.Assembly()
    report_parts = []
    for pid, p in parts.items():
        assy.add(p["wp"], name=pid, loc=locs[pid])
        vol = _shape(p["wp"]).Volume()
        density = DENSITY_G_MM3.get(p["material"].lower(), DENSITY_G_MM3["steel"])
        entry = {
            "id": pid, "type": p["type"], "material": p["material"],
            "volume_mm3": round(vol, 2), "mass_g": round(vol * density, 2),
            "placed": pid in placed, "gear_fallback": p["fallback"],
        }
        if p.get("finish") is not None:
            entry["finish"] = p["finish"]
        report_parts.append(entry)
    # OPT-IN provenance: attach the pre-resolved spec summary (computed BEFORE part-gen, see prepass) to
    # each part report. Default OFF -> existing builds unchanged.
    for rp in report_parts:
        if rp["id"] in spec_meta:
            rp["spec"] = spec_meta[rp["id"]]
    return assy, report_parts


def _resolve_specs_prepass(spec: dict) -> dict:
    """If `resolve_specs`: resolve thread facts for screw/threaded parts BEFORE geometry generation. For
    `threaded_rod`, INJECT the resolved major_diameter+pitch into the part params (only where not already
    given) so the RESOLVED pitch literally drives the thread geometry. Returns {pid: spec_summary} for the
    report. Fail-soft: a failure -> a `spec.ok:false` note, never raises."""
    if not spec.get("resolve_specs"):
        return {}
    from . import spec_compiler
    allow_live = bool(spec.get("allow_live_spec", False))
    out: dict = {}
    for ps in spec.get("parts", []):
        params = ps.setdefault("params", {})
        desig = params.get("designation") or params.get("size")
        if ps.get("type") not in ("screw", "threaded_rod", "nut") and not params.get("designation"):
            continue
        if not desig:
            continue
        try:
            res = spec_compiler.resolve_thread(str(desig), prefer_cache=True, allow_live=allow_live)
            if res.get("ok") and res.get("facts"):
                f = res["facts"][0]
                nd = f["values"]["nominal_diameter"]["value"]
                p = f["values"]["pitch"]["value"]
                if ps.get("type") == "threaded_rod":   # resolved fact -> geometry parameters
                    params.setdefault("major_diameter", nd)
                    params.setdefault("pitch", p)
                out[_pid(ps)] = {
                    "ok": True, "designation": f["designation"], "nominal_diameter_mm": nd,
                    "pitch_mm": p, "confidence": f["confidence"],
                    "drove_geometry": ps.get("type") == "threaded_rod",
                    "source_refs": sorted({v["source_ref"] for v in f["values"].values()}),
                }
            else:
                out[_pid(ps)] = {"ok": False, "warnings": res.get("warnings", [])}
        except Exception as exc:  # noqa: BLE001
            out[_pid(ps)] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return out


def find_loops(spec: dict) -> list[dict]:
    """Walk the mate graph in order; return the mates that CLOSE A LOOP (target B already positioned by
    an earlier mate). These cannot be satisfied by the deterministic tree solver — they require a DOF
    solver. A planetary set produces these (each planet meshes both sun and ring)."""
    parts_order = [(_pid(p)) for p in spec.get("parts", [])]
    positioned = {parts_order[0]} if parts_order else set()
    loops = []
    for m in spec.get("mates", []):
        if m.get("b") in positioned:
            loops.append(m)
        else:
            positioned.add(m.get("b"))
    return loops


def _apply_mate(parts: dict, locs: dict, m: dict):
    """Resolve one mate -> (moving_part_id, world Location). Dispatches by mate type."""
    a, b = m["a"], m["b"]
    if a not in parts or b not in parts:
        raise ValueError(f"mate references unknown part: {m}")
    a_anchor_world = locs[a] * parts[a]["anchors"][m["a_joint"]]
    mtype = m.get("type", "coincident")
    if mtype == "contact":  # geometry-aware seat: needs B's solid
        return b, mates.solve(mtype, a_anchor_world, b_wp=parts[b]["wp"])
    if mtype == "mesh":     # param-aware: gear center distance from module+teeth
        return b, _gear_mesh(parts[a], parts[b], a_anchor_world, float(m.get("angle_deg", 0.0)))
    if mtype in ("revolute", "cylindrical"):  # kinematic DOF joints (angle + optional axial slide)
        return b, mates.solve(mtype, a_anchor_world, b_anchor_local=parts[b]["anchors"][m["b_joint"]],
                              angle_deg=float(m.get("angle_deg", 0.0)), slide=float(m.get("slide", 0.0)))
    return b, mates.solve(mtype, a_anchor_world, b_anchor_local=parts[b]["anchors"][m["b_joint"]])


def _gear_mesh(pa: dict, pb: dict, a_anchor_world: cq.Location, angle_deg: float) -> cq.Location:
    """Place spur gear B meshing with spur gear A at the exact involute center distance
    c = module*(za+zb)/2, coplanar with A, B's axis at `angle_deg` around A's axis. B is phased by
    half a tooth (180/zb deg) so the teeth interleave. Meshing gears must share module."""
    if pa["type"] != "spur_gear" or pb["type"] != "spur_gear":
        raise ValueError("mesh mate requires two spur_gear parts")
    ma = float(pa["params"].get("module", 1.0))
    mb = float(pb["params"].get("module", 1.0))
    if abs(ma - mb) > 1e-9:
        raise ValueError(f"meshing gears must share module (got {ma} vs {mb})")
    za = int(pa["params"].get("teeth", 20))
    zb = int(pb["params"].get("teeth", 20))
    c = mates.gear_mesh_center_distance(ma, za, zb)
    (ax, ay, az), _ = a_anchor_world.toTuple()
    th = math.radians(angle_deg)
    dx, dy = c * math.cos(th), c * math.sin(th)
    phase = 180.0 / zb  # half-tooth offset so teeth interleave
    return cq.Location(cq.Vector(ax + dx, ay + dy, az), cq.Vector(0, 0, 1), phase)


def _bbox(assy: cq.Assembly):
    comp = assy.toCompound()
    bb = comp.BoundingBox()
    return {
        "min": [round(bb.xmin, 2), round(bb.ymin, 2), round(bb.zmin, 2)],
        "max": [round(bb.xmax, 2), round(bb.ymax, 2), round(bb.zmax, 2)],
        "size": [round(bb.xlen, 2), round(bb.ylen, 2), round(bb.zlen, 2)],
    }


def export(assy: cq.Assembly, base_path: str) -> dict:
    """Write STEP + GLB next to base_path. GLB tries native cadquery GLTF, falls back to
    STL -> trimesh -> GLB (Blender's render_glb.py consumes the GLB)."""
    os.makedirs(os.path.dirname(base_path), exist_ok=True)
    step = base_path + ".step"
    glb = base_path + ".glb"
    assy.save(step)

    glb_method = "cadquery-gltf"
    try:
        assy.save(glb, "GLTF")
        if not (os.path.exists(glb) and os.path.getsize(glb) > 0):
            raise RuntimeError("empty glb from cadquery")
    except Exception:  # noqa: BLE001
        import trimesh
        stl = base_path + ".stl"
        assy.toCompound().exportStl(stl)
        trimesh.load(stl).export(glb)
        glb_method = "stl->trimesh"

    return {"step": step, "glb": glb, "glb_method": glb_method, "bbox": _bbox(assy)}
