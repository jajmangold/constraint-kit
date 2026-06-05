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
import re

import cadquery as cq

from . import mate_intent, mates
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


# standard metric coarse-thread pitch (mm) -> turns a bare size like "M5" into the catalog's "M5-0.8"
_COARSE_PITCH = {"M2": 0.4, "M2.5": 0.45, "M3": 0.5, "M4": 0.7, "M5": 0.8, "M6": 1.0,
                 "M8": 1.25, "M10": 1.5, "M12": 1.75, "M16": 2.0, "M20": 2.5}
# params that are legitimately STRING-typed (designations/enums) -> never numeric-coerce these
_STRING_PARAMS = {"rail_size", "size", "nps", "kind", "material", "designation", "identifier",
                  "bearing_type", "fastener_type", "hand", "name"}


def _coerce_params(ptype: str, params: dict) -> dict:
    """Be liberal in what we accept: map common human/LLM param-VALUE formats to the canonical values the
    generators require (measured from the gauntlet: rail_size '2020'->'20x20', screw 'M5'->'M5-0.8', flange
    nps '2-inch'->'2'). Non-matching values pass through untouched. Applied at the single generate chokepoint,
    so it helps EVERY build path (intent + direct DSL)."""
    if not params:
        return params
    p = dict(params)
    if ptype == "extrusion":
        rs = str(p.get("rail_size", "")).lower().replace(" ", "")
        if rs.isdigit() and len(rs) == 4:                      # "2020" -> "20x20"
            p["rail_size"] = f"{rs[:2]}x{rs[2:]}"
    elif ptype == "screw":
        sz = str(p.get("size", ""))
        if re.fullmatch(r"[Mm]\d+(\.\d+)?", sz):               # bare "M5" -> "M5-0.8" (standard coarse pitch)
            pitch = _COARSE_PITCH.get(sz.upper())
            if pitch:
                p["size"] = f"{sz.upper()}-{pitch}"
    elif ptype == "flange":
        nps = re.sub(r'[-\s]*(?:inches|inch|in)\.?$', '', str(p.get("nps", "")).strip().rstrip('"'),
                     flags=re.I).strip()                       # '2"', "2-inch", "2 inch" -> "2"
        if nps:
            p["nps"] = nps
    # generic: a numeric param the LLM sent as a string ("12.7", "10mm") -> a number (int if whole). Measured
    # from the census: the top build-failure was sprocket chain_pitch/bore_d arriving as strings. String-typed
    # params (designations/enums) are excluded so e.g. flange nps "2" stays "2".
    for k, v in list(p.items()):
        if k not in _STRING_PARAMS and isinstance(v, str):
            m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*(?:mm|millimet(?:er|re)s?)?\s*", v, flags=re.I)
            if m:
                num = float(m.group(1))
                p[k] = int(num) if num.is_integer() else num
    return p


def _generate(ptype: str, params: dict):
    """Generate a part (type, params) through the cache, returning an independent copy each time."""
    params = _coerce_params(ptype, params)                     # canonicalize human-style param values first
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


def _gen_worker(spec: tuple):
    """Process-pool worker: generate one (type, params) part and return (cache_key, wp, anchors). cadquery
    Workplane + anchor Locations pickle cleanly, so the cache entry crosses the process boundary intact."""
    ptype, params = spec
    from . import builder as _b
    wp, anchors = _b.ALL_PART_GENS[ptype](**params)
    return _b._gen_key(ptype, params), wp, anchors


def prewarm_cache(specs: list[tuple], max_workers: int | None = None) -> int:
    """T3.3: generate the DISTINCT (type, params) parts in `specs` across a PROCESS pool and populate the
    T3.1 cache, so a subsequent (serial, unchanged) build hits the cache for every part — parallelizing the
    expensive cq_gears/IsoThread generation WITHOUT touching the deterministic build or the validated
    density-weighted mass-properties roll-up (those need per-leaf shapes; this sidesteps that entirely).
    Already-cached specs are skipped. Serial fallback on <=1 worker / a single part / any pool failure.
    Returns the number of parts generated."""
    distinct = {}
    for ptype, params in specs:
        key = _gen_key(ptype, params)
        if key not in _GEN_CACHE:
            distinct.setdefault(key, (ptype, params))
    todo = list(distinct.values())
    if not todo:
        return 0
    if max_workers is None:
        max_workers = max(1, (os.cpu_count() or 2) - 1)

    def _serial():
        for ptype, params in todo:
            _GEN_CACHE[_gen_key(ptype, params)] = ALL_PART_GENS[ptype](**params)

    if max_workers <= 1 or len(todo) == 1:
        _serial()
        return len(todo)
    try:
        import multiprocessing as _mp
        from concurrent.futures import ProcessPoolExecutor
        # 'spawn' (fresh interpreter), NOT the Linux default 'fork': the caller has already loaded
        # cadquery/OCC (which run threads), and fork-after-threads deadlocks the workers. Spawn is slower
        # (re-imports per worker) but safe; the heavy cq_gears/IsoThread generation still parallelizes.
        with ProcessPoolExecutor(max_workers=max_workers, mp_context=_mp.get_context("spawn")) as ex:
            for key, wp, anchors in ex.map(_gen_worker, todo):
                _GEN_CACHE[key] = (wp, anchors)
    except Exception:  # noqa: BLE001 -- pool/pickle/spawn failure -> identical serial result
        _serial()
    return len(todo)


def cache_stats() -> dict:
    """Part-generation cache hit/miss counters (T3.1)."""
    return {**_GEN_STATS, "entries": len(_GEN_CACHE)}


def clear_cache() -> None:
    _GEN_CACHE.clear()
    _GEN_STATS.update(hits=0, misses=0)


def _parallel_build_worker(spec: dict):
    """Process-pool worker (module-level so it's picklable): build a flat spec, return its compound as BREP
    bytes + the part report. BREP crosses the process boundary; live cq objects do not."""
    import io

    from . import builder as _b
    assy, report = _b.build_assembly(spec)
    bio = io.BytesIO()
    assy.toCompound().exportBrep(bio)
    return bio.getvalue(), report


def build_assemblies_parallel(specs: list[dict], max_workers: int | None = None) -> list[tuple]:
    """T3.3: build a batch of INDEPENDENT flat assembly specs across a PROCESS pool — real parallelism for
    the CPU-bound OCC/cq_gears generation (threads don't help: the GIL + Python-heavy involute math give no
    speedup, measured). Each worker returns its compound as BREP bytes + report; the caller reimports the
    geometry. SERIAL fallback on max_workers<=1, a single spec, or any pool failure (fail-soft). Returns
    [(cq.Shape, report)] aligned to `specs`.

    SCOPE: this parallelizes flat leaf builds. Wiring it into the recursive density-weighted mass-properties
    roll-up of assembly.build_tree is open — massprops needs per-leaf shapes, which are lost when a subtree
    crosses the process boundary as one compound (research R-T3.3)."""
    import io
    if max_workers is None:
        max_workers = max(1, (os.cpu_count() or 2) - 1)

    def _serial():
        out = []
        for s in specs:
            assy, rep = build_assembly(s)
            out.append((assy.toCompound(), rep))
        return out

    if max_workers <= 1 or len(specs) <= 1:
        return _serial()
    try:
        import multiprocessing as _mp
        from concurrent.futures import ProcessPoolExecutor
        # 'spawn', not 'fork': forking after cadquery/OCC has loaded threads deadlocks the workers.
        with ProcessPoolExecutor(max_workers=max_workers, mp_context=_mp.get_context("spawn")) as ex:
            pairs = list(ex.map(_parallel_build_worker, specs))
        return [(cq.Shape.importBrep(io.BytesIO(b)), rep) for b, rep in pairs]
    except Exception:  # noqa: BLE001 -- any pool/pickle/spawn failure -> deterministic serial result
        return _serial()


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

    # mate-by-intent (T2.2): resolve any intent-carrying mate to a concrete {a_joint,b_joint,type} against
    # the just-generated parts' authored + derived (T2.3) frames, BEFORE placement. Explicit mates pass through.
    resolved_mates = mate_intent.normalize_mates(parts, spec.get("mates", []))

    # deterministic ordered placement: part 0 at origin, then apply mates in order. A mate whose target
    # B is ALREADY positioned closes a loop — the tree solver cannot honor it (B can't be in two places).
    # We skip such mates here and surface them; a DOF solver is needed to close them (see find_loops).
    locs: dict[str, cq.Location] = {pid: cq.Location() for pid in parts}
    positioned = {_pid(spec["parts"][0])}
    placed = set(positioned)
    loop_constraints: list[dict] = []
    for m in resolved_mates:
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


def _two_point_loc(p0, p1) -> cq.Location:
    """Rigid z=0-plane placement that lands a part's local (0,0)->p0 and (length,0)->p1: rotate about Z by
    the p0->p1 direction, then translate to p0 (cq.Location applies rotation about the origin first)."""
    ang = math.degrees(math.atan2(p1[1] - p0[1], p1[0] - p0[0]))
    return cq.Location(cq.Vector(p0[0], p0[1], 0), cq.Vector(0, 0, 1), ang)


def pose_four_bar(ground: float, crank: float, coupler: float, rocker: float,
                  input_angle_deg: float = 60.0, width: float = 8.0, thickness: float = 4.0,
                  material: str = "steel") -> dict:
    """T10.2: route a 4-bar loop through the GEOMETRIC solver and POSE it as an assembly of distinct link
    parts — the bridge from the geometric-loop solver (SolveSpace) to the assembly builder. linkage.solve_
    four_bar gives the four joint positions; each of the 4 links (ground A-D, crank A-B, coupler B-C,
    rocker D-C) is placed by a 2-point rigid transform onto its solved joints, so the loop CLOSES by
    construction (shared joints coincide). Returns {assy, positions, dof, links, mass_g, ...}; raises if the
    linkage can't close. DOF reported two ways that must agree (SolveSpace solved-dof == Gruebler)."""
    from . import linkage
    sol = linkage.solve_four_bar(ground, crank, coupler, rocker, input_angle_deg)
    if not sol["ok"]:
        raise ValueError(f"4-bar cannot close (grashof={sol['grashof']}, input={input_angle_deg}deg)")
    P = sol["positions"]
    spec_links = [("ground", "A", "D", ground), ("crank", "A", "B", crank),
                  ("coupler", "B", "C", coupler), ("rocker", "D", "C", rocker)]
    assy = cq.Assembly()
    links, mass = [], 0.0
    density = DENSITY_G_MM3.get(material.lower(), DENSITY_G_MM3["steel"])
    for name, j0, j1, length in spec_links:
        wp, _anch = _generate("link", {"length": length, "width": width, "thickness": thickness})
        loc = _two_point_loc(P[j0], P[j1])
        assy.add(wp, name=name, loc=loc)
        m = round(_shape(wp).Volume() * density, 2)
        mass += m
        links.append({"name": name, "joints": [j0, j1], "length_mm": length, "mass_g": m})
    return {"assy": assy, "positions": P, "links": links, "mass_g": round(mass, 2),
            "mechanism_dof": sol["mechanism_dof"], "gruebler_dof": sol["gruebler_dof"],
            "grashof": sol["grashof"], "solver": sol["solver"]}


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
