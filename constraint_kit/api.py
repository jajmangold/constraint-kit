"""cadkit HTTP API (FastAPI) -- CPU-only geometry + planning service.

Endpoints:
  GET  /health
  POST /assemble  {spec}            -> build + export from an explicit spec
  POST /plan      {request}         -> LLM spec only (no geometry)
  POST /build     {request,name?}   -> plan THEN assemble + export (the Phase-0 one-shot)

Outputs land in OUTPUT_DIR, which is the SHARED host path (mounted identically in the container),
so returned paths are valid on the host for the Blender render step. No GPU.
"""
from __future__ import annotations

import os
import time

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from . import (assembly, bom, builder, drawing, layout, library, linkage, planner, planetary, rules,
               spec_compiler, spec_db, spec_sources, store, synthesis, tolerance)

OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/srv/nvme-data/containers/constraint-kit/cadkit/output")

app = FastAPI(title="cadkit", version="0.2")


class BuildReq(BaseModel):
    request: str
    name: str | None = None
    resolve_specs: bool = False       # attach provenance-linked thread facts to screw/thread parts
    allow_live_spec: bool = False     # let spec resolution use live SearXNG confirmation (default offline)


class PlanReq(BaseModel):
    request: str


class AssembleReq(BaseModel):
    spec: dict
    name: str | None = None
    request: str | None = None
    resolve_specs: bool = False
    allow_live_spec: bool = False


def _inject_spec_flags(spec: dict, resolve_specs: bool, allow_live_spec: bool) -> dict:
    """Enable provenance attachment if requested at the request level OR already set in the spec dict."""
    spec = dict(spec)
    spec["resolve_specs"] = bool(resolve_specs) or bool(spec.get("resolve_specs"))
    spec["allow_live_spec"] = bool(allow_live_spec) or bool(spec.get("allow_live_spec"))
    return spec


class QAReq(BaseModel):
    name: str
    png: str | None = None
    verdict: str | None = None


class LayoutReq(BaseModel):
    request: str | None = None
    spec: dict | None = None
    name: str | None = None


class LayoutQAReq(BaseModel):
    name: str
    verdict: str | None = None


def _do_layout(spec: dict, name: str | None, request: str | None) -> dict:
    name = name or f"layout_{int(time.time())}"
    solved = layout.solve(spec)
    rep = layout.report(solved)
    dxf = layout.export_dxf(solved, os.path.join(OUTPUT_DIR, name + ".dxf"))
    png = layout.export_png(solved, os.path.join(OUTPUT_DIR, name + ".png"))
    stored = store.record_layout(name, request, spec, rep, dxf, png,
                                 os.environ.get("MODEL_PLANNER", "qwen27b"))
    return {"name": name, "spec": spec, "dxf": dxf, "png": png, "stored": stored, **rep}


def _do_assemble(spec: dict, name: str | None, request: str | None = None) -> dict:
    name = name or f"asm_{int(time.time())}"
    assy, parts = builder.build_assembly(spec)
    exported = builder.export(assy, os.path.join(OUTPUT_DIR, name))
    loops = builder.find_loops(spec)
    result = {"name": name, "spec": spec, "parts": parts,
              "loop_constraints": loops, "needs_dof_solver": bool(loops), **exported}
    # persist everything to atlas (fail-soft: never break a build over storage)
    result["stored"] = store.record_build(
        name, request, spec, parts, exported,
        os.environ.get("MODEL_PLANNER", "qwen27b"))
    return result


@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "cadkit", "output_dir": OUTPUT_DIR,
            "atlas": store.status()}


@app.post("/plan")
def plan(req: PlanReq) -> dict:
    try:
        return {"spec": planner.plan(req.request)}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"planner failed: {exc}") from exc


@app.post("/assemble")
def assemble(req: AssembleReq) -> dict:
    try:
        spec = _inject_spec_flags(req.spec, req.resolve_specs, req.allow_live_spec)
        return _do_assemble(spec, req.name, req.request)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"assemble failed: {exc}") from exc


@app.post("/build")
def build(req: BuildReq) -> dict:
    try:
        spec = planner.plan(req.request)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"planner failed: {exc}") from exc
    try:
        spec = _inject_spec_flags(spec, req.resolve_specs, req.allow_live_spec)
        return _do_assemble(spec, req.name, req.request)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"assemble failed: {exc} (spec={spec})") from exc


@app.post("/qa")
def qa(req: QAReq) -> dict:
    """Attach a render + VLM verdict to a stored assembly (called by the driver after QA)."""
    return {"stored": store.attach_render(req.name, req.png, req.verdict)}


@app.get("/runs")
def runs(limit: int = 20) -> dict:
    return {"atlas": store.status(), "runs": store.recent_runs(limit)}


@app.get("/assembly/{name}")
def get_assembly(name: str) -> dict:
    a = store.assembly(name)
    if a is None:
        raise HTTPException(404, f"assembly {name!r} not in atlas (or atlas disabled)")
    return a


class PlanetaryReq(BaseModel):
    module: float = 1.0
    sun_teeth: int = 12
    planet_teeth: int = 12
    n_planets: int = 3
    grounded: str = "ring"


class FourBarReq(BaseModel):
    ground: float = 100.0
    crank: float = 30.0
    coupler: float = 90.0
    rocker: float = 60.0
    input_angle_deg: float | None = None


class SliderCrankReq(BaseModel):
    crank: float = 25.0
    rod: float = 80.0
    offset: float = 0.0
    input_angle_deg: float | None = None


@app.post("/dof/planetary")
def dof_planetary(req: PlanetaryReq) -> dict:
    """GEAR-loop DOF (analytical epicyclic mobility) — each planet meshes both sun and ring."""
    return planetary.mobility(req.module, req.sun_teeth, req.planet_teeth, req.n_planets, req.grounded)


@app.post("/dof/four_bar")
def dof_four_bar(req: FourBarReq) -> dict:
    """GEOMETRIC-loop DOF via SolveSpace — solves the 4-bar closed loop + reports mobility
    (cross-checked against Gruebler). This is the class neither the tree nor the analytical solver covers."""
    return linkage.solve_four_bar(req.ground, req.crank, req.coupler, req.rocker, req.input_angle_deg)


class FourBarPoseReq(BaseModel):
    ground: float = 100.0
    crank: float = 30.0
    coupler: float = 90.0
    rocker: float = 60.0
    input_angle_deg: float = 60.0
    width: float = 8.0
    thickness: float = 4.0
    name: str | None = None


@app.post("/mechanism/four_bar")
def mechanism_four_bar(req: FourBarPoseReq) -> dict:
    """E10/T10.2: route a 4-bar loop through the GEOMETRIC solver and POSE it as an assembly of 4 distinct
    link parts (the bridge from SolveSpace to the builder) → one STEP+GLB. The loop closes by construction
    (each link placed on its solved joints). Raises 400 if the linkage can't close."""
    name = req.name or "four_bar"
    try:
        res = builder.pose_four_bar(req.ground, req.crank, req.coupler, req.rocker, req.input_angle_deg,
                                    req.width, req.thickness)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"four-bar pose failed: {exc}") from exc
    exported = builder.export(res["assy"], os.path.join(OUTPUT_DIR, name))
    return {"ok": True, "positions": res["positions"], "links": res["links"], "mass_g": res["mass_g"],
            "mechanism_dof": res["mechanism_dof"], "gruebler_dof": res["gruebler_dof"],
            "grashof": res["grashof"], "solver": res["solver"], **exported}


@app.post("/dof/slider_crank")
def dof_slider_crank(req: SliderCrankReq) -> dict:
    """GEOMETRIC-loop DOF via SolveSpace — slider-crank (revolute×3 + prismatic)."""
    return linkage.solve_slider_crank(req.crank, req.rod, req.offset, req.input_angle_deg)


class SpecResolveReq(BaseModel):
    query: str
    kind: str = "unknown"
    prefer_cache: bool = True
    allow_live: bool = True


class SpecValidateReq(BaseModel):
    fact: dict


class SpecSearchReq(BaseModel):
    query: str


@app.post("/spec/resolve")
def spec_resolve(req: SpecResolveReq) -> dict:
    """Resolve an engineering fact (v1: ISO metric threads) via the LangGraph spec compiler:
    SQLite cache -> SearXNG discovery -> fetch -> extract (text/table/VLM) -> normalize -> trust score
    -> validate -> persist SQLite (+ optional JSON artifact). Always returns a structured result."""
    return spec_compiler.resolve(req.query, req.kind, req.prefer_cache, req.allow_live)


@app.get("/spec/cache")
def spec_cache_recent(limit: int = 20) -> dict:
    try:
        return {"ok": True, "recent": spec_db.recent(limit), "integrity": spec_db.validate_integrity()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "recent": []}


@app.post("/spec/validate")
def spec_validate(req: SpecValidateReq) -> dict:
    issues = spec_compiler.validate_fact(req.fact)
    return {"ok": not issues, "issues": issues}


@app.post("/spec/search")
def spec_search(req: SpecSearchReq) -> dict:
    """Debug: SearXNG discovery only (candidates are NOT facts)."""
    cands = spec_sources.rank_sources(spec_sources.search_searxng(req.query))
    return {"ok": True, "query": req.query, "candidates": cands}


class AssemblyTreeReq(BaseModel):
    defs: dict
    root: str
    name: str | None = None


class BomReq(BaseModel):
    defs: dict
    root: str
    name: str | None = None
    fmt: str = "md"


class ExplodeReq(BaseModel):
    defs: dict
    root: str
    name: str | None = None
    factor: float = 20.0
    axis: tuple[float, float, float] = (0.0, 0.0, 1.0)


class DrawingReq(BaseModel):
    defs: dict
    root: str
    name: str | None = None
    plane: str = "XZ"
    height: float = 0.0
    views: list[str] = ["front", "top", "right"]


class EngageReq(BaseModel):
    nominal_d: float
    engagement_len: float
    mating_material: str = "steel"


class FitRuleReq(BaseModel):
    application: str
    fit_class: str


class ClearanceReq(BaseModel):
    defs: dict
    root: str
    required: float = 0.5


class BeamReq(BaseModel):
    material: str
    length_mm: float
    width_mm: float
    height_mm: float
    load_n: float
    safety_factor: float = 2.0


class BoltLoadReq(BaseModel):
    size: str
    prop_class: str = "8.8"
    applied_load_n: float = 0.0
    preload_fraction: float = 0.75


class ToleranceReq(BaseModel):
    dims: list
    as_clearance: bool = False


class FromLibraryReq(BaseModel):
    library: str
    root: str
    name: str | None = None


class DesignReq(BaseModel):
    requirements: dict
    relations: list[dict] | None = None
    asserts: list[dict] | None = None
    defs: dict
    root: str
    name: str | None = None


@app.post("/design")
def design(req: DesignReq) -> dict:
    """TOP-DOWN parametric design (Phase B): requirements + relations -> derived parameters (provenance)
    -> substituted into the assembly defs -> hierarchical build. Returns the parameter table + rolled-up
    BOM/mass + artifacts, or a structured ok:false if a requirement assertion is violated."""
    name = req.name or req.root
    try:
        res = assembly.design_and_export(req.requirements, req.relations, req.defs, req.root,
                                         os.path.join(OUTPUT_DIR, name), req.asserts)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"design failed: {exc}") from exc
    if res.get("ok"):
        res["stored"] = store.record_assembly_tree(res)
        res["version_id"] = store.record_design_version(res)   # T8.1: version the design (params + git SHA)
    return res


class ReeditReq(DesignReq):
    edits: dict


@app.post("/design/reedit")
def design_reedit(req: ReeditReq) -> dict:
    """PARAMETRIC EDIT / incremental re-solve (T8.2): apply `edits` (changed requirement values) to a
    baseline design, report exactly which defs the change affects (dirty vs clean subtrees), and rebuild —
    regenerating ONLY the affected subtree's parts (the rest reuse the T3.1 part cache). Returns the
    dependency report + rebuild summary (parts_generated vs parts_reused) + BOM/mass + artifacts, or a
    structured ok:false if the edited design is invalid. Warm the cache by building the baseline first."""
    name = req.name or req.root
    try:
        res = assembly.reedit_and_export(req.requirements, req.relations, req.defs, req.root,
                                         req.edits, os.path.join(OUTPUT_DIR, name), req.asserts)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"reedit failed: {exc}") from exc
    if res.get("ok"):
        res["stored"] = store.record_assembly_tree(res)
        res["version_id"] = store.record_design_version(res)   # T8.1: edits create a new linked version
    return res


@app.get("/design/versions")
def design_versions(name: str, limit: int = 20) -> dict:
    """T8.1: list the recorded versions of a design (CkDesignVersion: params + git SHA, most recent first)."""
    return {"name": name, "versions": store.design_versions(name, limit)}


class SynthPlanetaryReq(BaseModel):
    target_ratio: float
    n_planets: int = 3
    teeth_min: int = 12
    teeth_max: int = 40
    module: float = 1.0
    width: float = 6.0
    ratio_tol: float = 0.0
    objective: str = "compact"
    build: bool = False
    name: str | None = None


@app.post("/synthesize/planetary")
def synthesize_planetary(req: SynthPlanetaryReq) -> dict:
    """SMT design synthesis: solve for a planetary gearset meeting a ratio spec (Z3), and optionally build
    the exact geometry from the synthesized config — spec → design → CAD. Honest UNSAT if infeasible."""
    res = synthesis.synthesize_planetary(req.target_ratio, req.n_planets, req.teeth_min, req.teeth_max,
                                         req.module, req.width, req.ratio_tol, req.objective)
    if res.get("ok") and req.build:
        try:
            built = _do_assemble({"parts": [res["part_spec"]], "mates": []}, req.name or "planetary_synth")
            res["built"] = {k: built[k] for k in ("name", "glb", "step", "bbox", "parts") if k in built}
        except Exception as exc:  # noqa: BLE001
            res["built"] = {"ok": False, "error": str(exc)}
    return res


class ToleranceAllocReq(BaseModel):
    dims: list[dict]
    budget_um: float
    grade_min: int = 5
    grade_max: int = 12
    method: str = "worst_case"


@app.post("/synthesize/tolerance")
def synthesize_tolerance(req: ToleranceAllocReq) -> dict:
    """E10/T10.1: SMT tolerance ALLOCATION (Z3) — the inverse of /tolerance/stackup. Allocate the loosest
    (cheapest) ISO 286 IT grade per dimension whose stack-up still fits `budget_um`; honest infeasible
    (with the tightest achievable total) when even all-tight exceeds the budget."""
    return synthesis.synthesize_tolerance_allocation(req.dims, req.budget_um, req.grade_min,
                                                     req.grade_max, req.method)


class GearTrainReq(BaseModel):
    target_ratio: float
    n_stages: int = 2
    teeth_min: int = 12
    teeth_max: int = 40
    module: float = 1.0
    width: float = 6.0
    stage_ratio_min: float = 2.0
    stage_ratio_max: float = 8.0
    ratio_tol: float = 0.25


@app.post("/synthesize/gear_train")
def synthesize_gear_train(req: GearTrainReq) -> dict:
    """E10/T10.3: SMT multi-STAGE gear-train synthesis (Z3) — split a target ratio across N planetary
    stages whose ratios multiply to the target (within tol), each a valid planetary set; honest UNSAT."""
    return synthesis.synthesize_gear_train(req.target_ratio, req.n_stages, req.teeth_min, req.teeth_max,
                                           req.module, req.width, req.stage_ratio_min, req.stage_ratio_max,
                                           req.ratio_tol)


class InterferenceReq(BaseModel):
    defs: dict
    root: str
    tol_volume: float = 0.01


@app.post("/assembly/interference")
def assembly_interference(req: InterferenceReq) -> dict:
    """Validation at scale (Phase C): build the tree and report solid-solid clashes (bbox-prefiltered,
    exact OCC boolean on overlaps)."""
    try:
        return assembly.check_interference(req.root, req.defs, req.tol_volume)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"interference check failed: {exc}") from exc


@app.post("/assembly/tree")
def assembly_tree(req: AssemblyTreeReq) -> dict:
    """Build a HIERARCHICAL assembly from a registry of defs (recursive subassemblies + port mates):
    one combined STEP+GLB, rolled-up BOM + mass, persisted as an atlas work-breadcrumb."""
    name = req.name or req.root
    try:
        res = assembly.build_and_export(req.root, req.defs, os.path.join(OUTPUT_DIR, name))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"assembly tree failed: {exc}") from exc
    res["stored"] = store.record_assembly_tree(res)   # fail-soft
    return {"ok": True, **res}


@app.post("/assembly/bom")
def assembly_bom(req: BomReq) -> dict:
    """Export a Bill of Materials (Markdown or CSV) for a hierarchical assembly — qty + mass per part
    type + totals + CG. Writes the document under OUTPUT_DIR and returns its text."""
    try:
        result = assembly.build_tree(req.root, req.defs)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"bom build failed: {exc}") from exc
    fmt = req.fmt if req.fmt in ("md", "csv") else "md"
    doc = bom.bom_document(result, fmt)
    path = os.path.join(OUTPUT_DIR, f"{req.name or req.root}_bom.{fmt}")
    with open(path, "w") as fh:
        fh.write(doc)
    return {"ok": True, "fmt": fmt, "path": path, "document": doc,
            "bom": dict(result["bom"]), "mass_g": result["mass_g"]}


@app.post("/assembly/explode")
def assembly_explode(req: ExplodeReq) -> dict:
    """E7/T7.1: exploded view for assembly docs — separate stacked parts along `axis` (default +Z) by
    `factor` per rank, export one STEP+GLB of the exploded view."""
    name = req.name or f"{req.root}_exploded"
    try:
        return {"ok": True, **assembly.explode_and_export(
            req.root, req.defs, os.path.join(OUTPUT_DIR, name), req.factor, tuple(req.axis))}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"explode failed: {exc}") from exc


@app.post("/assembly/drawing")
def assembly_drawing(req: DrawingReq) -> dict:
    """E7/T7.2: a 2D drawing set for an assembly — a cross-section DXF (cut on `plane`) + orthographic
    projection SVGs (`views`) + overall dimensions. Section/projection geometry is exact (OCC); GD&T
    tolerance frames are not generated."""
    name = req.name or req.root
    try:
        asm = assembly.build_tree(req.root, req.defs)["cq_assembly"]
        return {"ok": True, **drawing.drawing(asm, os.path.join(OUTPUT_DIR, name),
                                              req.plane, req.height, tuple(req.views))}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"drawing failed: {exc}") from exc


@app.post("/rules/fastener_engagement")
def rule_engagement(req: EngageReq) -> dict:
    """E9/T9.1: is the thread engagement length sufficient for the mating material?"""
    return rules.fastener_engagement(req.nominal_d, req.engagement_len, req.mating_material)


@app.post("/rules/fit")
def rule_fit(req: FitRuleReq) -> dict:
    """E9/T9.2: is the ISO 286 fit class appropriate for the application?"""
    return rules.fit_appropriateness(req.application, req.fit_class)


@app.post("/rules/clearance")
def rule_clearance(req: ClearanceReq) -> dict:
    """E9/T9.3: minimum gap between parts (exact OCC distance); flag pairs closer than `required` mm."""
    try:
        asm = assembly.build_tree(req.root, req.defs)["cq_assembly"]
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"clearance build failed: {exc}") from exc
    return rules.min_clearance(asm, req.required)


@app.post("/rules/beam_bending")
def rule_beam_bending(req: BeamReq) -> dict:
    """E4/T4.3: cantilever beam max bending stress + tip deflection vs material yield over a safety factor."""
    return rules.beam_bending(req.material, req.length_mm, req.width_mm, req.height_mm,
                              req.load_n, req.safety_factor)


@app.post("/rules/bolt_preload")
def rule_bolt_preload(req: BoltLoadReq) -> dict:
    """E4/T4.3: bolt proof load + recommended preload (ISO 898-1 Sp × tensile stress area); checks an
    applied tensile load stays below proof."""
    return rules.bolt_preload(req.size, req.prop_class, req.applied_load_n, req.preload_fraction)


@app.post("/tolerance/stackup")
def tolerance_stackup(req: ToleranceReq) -> dict:
    """E4/T4.2: chain toleranced dims -> worst-case + RSS bounds (fits sourced from ISO 286)."""
    return tolerance.stackup(req.dims, req.as_clearance)


@app.get("/libraries")
def get_libraries() -> dict:
    """E6/T6.1: list domain libraries + their roots."""
    return {"libraries": [library.library_meta(n) for n in library.list_libraries()]}


@app.post("/assembly/from_library")
def assembly_from_library(req: FromLibraryReq) -> dict:
    """E6: build a root from a domain library's defs (compose, don't model from scratch)."""
    try:
        defs = library.load_library(req.library)
        res = assembly.build_and_export(req.root, defs, os.path.join(OUTPUT_DIR, req.name or req.root))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"library build failed: {exc}") from exc
    res["stored"] = store.record_assembly_tree(res)
    return {"ok": True, **res}


@app.get("/catalog")
def get_catalog() -> dict:
    return {"part_types_3d": sorted(builder.ALL_PART_GENS),
            "part_types_2d": sorted(layout.PART2D_GENS),
            "atlas_catalog": store.catalog()}


@app.post("/layout2d")
def layout2d(req: LayoutReq) -> dict:
    spec = req.spec
    if spec is None:
        if not req.request:
            raise HTTPException(400, "provide either spec or request")
        try:
            spec = planner.plan_layout(req.request)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"layout planner failed: {exc}") from exc
    try:
        return _do_layout(spec, req.name, req.request)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"layout failed: {exc} (spec={spec})") from exc


@app.post("/layout_qa")
def layout_qa(req: LayoutQAReq) -> dict:
    return {"stored": store.attach_layout_verdict(req.name, req.verdict)}


@app.get("/layouts")
def layouts(limit: int = 20) -> dict:
    return {"atlas": store.status(), "layouts": store.recent_layouts(limit)}
