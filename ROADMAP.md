# constraint-kit roadmap — from a screw to a car to a home

The thesis: **natural language → exact, provenance-linked, deterministically-assembled CAD**, scaling by
*composition*, not by a bigger model. This doc is the plan; `CLAUDE.md` is how the built parts work.

## The scaling question

A screw is one part. A car is ~30,000. A house is thousands of components across structure + envelope +
MEP. You cannot model those as one flat parts list — and you cannot trust an LLM to place 30,000 things.
What makes it tractable is the same thing that makes real engineering tractable: **hierarchy + interfaces
+ roll-ups + requirements**, with deterministic geometry and provenance underneath.

What already exists (the kernel):
- exact parametric parts (cadquery / build123d / bd_warehouse) + a deterministic mate kernel
- **four solver classes**: tree (mates) · gear loops (Willis) · geometric loops (SolveSpace) · **discrete
  synthesis/configuration (SMT/Z3)** — each used only where it's actually best; SMT never touches geometry
- first-class oriented **joints** (the per-part interface primitive)
- the **spec compiler** (engineering facts with provenance: threads, fits→real ISO 286 tolerances, bearings, materials)
- persistence (atlas for work, SQLite for spec data), render+VLM QA, 57 green tests

What is structurally missing for scale (the foundations this roadmap lays):

| foundation | why a car/home needs it | status |
|---|---|---|
| **Hierarchical assemblies** (subassemblies as units) | a car = chassis+drivetrain+body+interior, recursively; never one flat list | ✅ **Phase A** |
| **Named interface ports** (assembly-level, like joints are part-level) | mate a "wheel" to an "axle" by `axle_bore`↔`hub` without knowing internals | ✅ **Phase A** |
| **Roll-ups** (BOM, mass, later cost/CG) | a parts list + total mass is the first real deliverable at scale | ✅ **Phase A** |
| **Requirements / parameter propagation** (top-down) | car wheelbase / home footprint drives subassembly params | ✅ **Phase B** |
| **Validation at scale** (interference, clearance, rule checks) | 30k parts must not collide; codes/standards must hold | ✅ **Phase C** (interference) |
| **Spec breadth** via the compiler (not a static DB) | fasteners, profiles, building materials, electrical, plumbing codes | Phase D (incremental) |
| **Domain libraries** (assembly-def packages) | automotive / architectural component catalogs as reusable defs | Phase E |
| **Performance** (build caching, parallel subtrees) | rebuilding 30k parts must be incremental | Phase F |

## Phases

### Phase A — Hierarchical composition  ◀ ENACTING NOW
- `assembly.py`: an assembly node = leaf parts (via the existing builder) **+ child sub-assemblies** placed
  by **port-to-port** mates, exposing its own named **ports** upward. Recursive. Multi-instance (the same
  `wheel` def placed 4×). One combined STEP/GLB.
- **Roll-ups**: BOM (recursive part counts) + mass, computed bottom-up.
- Persist the assembly **tree** to atlas (`CkAssemblyNode -[:HAS_CHILD]-> …`), spec data still SQLite.
- Proof: `wheel` (rim+tire+hub) → `axle` (2× wheel + shaft) → BOM shows 2 rims/2 tires/2 hubs, mass rolls
  up, single export. This *is* the car-in-miniature.

### Phase B — Requirements & parameter propagation  ✅ DONE
- `parameters.py`: requirements → derived params via explicit relations, evaluated by a **safe AST
  evaluator** (whitelist, never `eval`), fixpoint with cycle detection, **provenance** on every derived
  value (relation + input values). `assembly.build_design`/`design_and_export` substitute params into the
  defs (`"=expr"` strings) so geometry reads requirements; requirement `asserts` refuse an invalid design.
  API `POST /design`. Proven: `track` → shaft length + wheel placement; changing it scales the geometry.

### Phase C — Validation at scale  ✅ DONE (interference; rule-checks remain)
- `validate.py`: flatten the (nested) assembly to world solids; **bbox prefilter → exact OCC boolean** on
  overlapping pairs only; report clashes with overlap volume (pair counts reported, no silent caps).
  `assembly.check_interference`, API `POST /assembly/interference`. Remaining: spatial bucketing for
  car/home scale (only test neighbors), and rule/code checks fed by the spec compiler.

### Phase D — Spec breadth (compiler-driven, not static)
- Grow `spec_compiler` kinds as needed: full ISO 286 interference fits (needs the authoritative table or
  live fetch), structural sections, electrical, plumbing/building-code facts — each provenance-linked.

### Phase E — Domain libraries
- `libraries/automotive`, `libraries/architectural`: reusable assembly defs + interface conventions. A car
  or a wall becomes "compose these defs," not "model from scratch."

### Phase F — Performance & packaging
- Content-hash build cache (don't rebuild unchanged subtrees), parallel subtree builds. Reorganize the
  package into subpackages (`solvers/`, `spec/`, `assembly/`, `parts/`, `io/`) once the module count and
  the interfaces have settled — cheaper to do deliberately than continuously.

## What "build a car this way" needs (mapping)
wheel/axle/diff/gearbox geometry (have: gears, bearings, shafts) · gear-train DOF (have: Willis) · linkage
DOF for suspension (have: SolveSpace) · **gearbox SYNTHESIS to a ratio spec** (have: SMT/Z3 →
`synthesize_planetary` → exact CAD) · fasteners + fits with real tolerances (have: spec compiler) ·
**hierarchy + BOM + mass** (Phase A) · requirements (wheelbase→geometry, Phase B) · interference of moving
parts (Phase C).

## What "build a home this way" needs (mapping)
walls/floors/openings (extrude 2D layouts — have the 2D layout solver) · materials with real properties
(have: spec compiler materials) · **hierarchy** (building→floor→room→wall→stud, Phase A) · quantities/BOM
(Phase A) · code compliance + clash detection (Phase C) · MEP routing (pipe/flange exist; routing = Phase E).

## Non-negotiable foundations (apply to every phase)
- Deterministic geometry + the LLM only proposes; **provenance on every value**; **fail-soft** everywhere.
- **No dead code, no silent truncation, tests with each capability** (geometry verified by math, not the VLM).
- Reuse the resident models; one CPU service; SQLite for spec data, atlas for work-tracking.
