# constraint-kit (SEED / captured idea — not started)

A domain-agnostic **constraint-based placement / assembly-mate solver**, extracted in spirit from
`../placement/scripts/constraint_place.py`. Captured 2026-06-02 while building the avatar/scene pipeline;
**parked** until the scene pipeline's body-pose half lands. This README is the thesis, not an implementation.

## Thesis: the placement pipeline, with parametric CAD swapped in for the mesh-lift stage

The `../placement` project is not "a placement script." It's a proven **5-stage multi-model pipeline**
(ComfyUI/Docker, all local on the V100/CMP box) whose doctrine is stated throughout its `CLAUDE.md`:

> **AI proposes the arrangement in 2D → monocular models lift each element to 3D → constraints solve the
> final contact/scale deterministically.** Model for *semantics*; geometry for *placement*.

`constraint_place.py` is only **stage 5** of that pipeline — and it is deliberately the *one AI-free,
deterministic* stage, because the project learned the hard way that the AI/silhouette loop is weak for
precision ("the deterministic eye-line anchor did ~90% of the glasses placement; the silhouette fit only
nudged it").

constraint-kit is that **same architecture**, with the single fidelity-limited stage replaced. The placement
pipeline lifts geometry with **SAM3D**, which produces approximate, scale-ambiguous, organic triangle meshes
— shape-only, explicitly *not trustable for precise placement*. Swap that one stage for **parametric CAD
generation** (exact B-rep solids) and the whole pipeline becomes a CAD assembly generator. Stage for stage:

| `../placement` pipeline | constraint-kit (CAD) |
|---|---|
| Z-Image / Qwen-edit — design + place in 2D | **LLM** picks parts + parameters + mate intent |
| SAM 3.1 — mask | (n/a — parts are generated, not segmented) |
| **SAM3D Objects — 2D→3D mesh**  ← the fidelity-limited stage | **build123d / CadQuery** — parametric generation → exact OCC B-rep |
| `constraint_place.py` — rig-anchored deterministic fit | **the constraint-kit mate solver** |

This is the credible CAD bridge: AI decides *what* + *where*; parametric generators supply *exact* geometry;
the kernel *assembles* it. It is arguably **cleaner than the original**, because it removes the single most
error-prone stage (monocular mesh lift) that the placement project spends most of its gotchas fighting.

## The mate vocabulary (what the kernel solves)

Given an anchor frame on target A + an ordered list of mates, B's world transform is solved
**deterministically** — no AI, no optimization. The mates, and their avatar origins:

| avatar anchor (placement stage 5) | general mate (the kernel) |
|---|---|
| center on head/eye axis | **coincident** — put B's ref point/frame on A's anchor |
| orient asset to face −Y | **align-axis** — orient B's axis to A's / world up |
| scale to head/face/body width | **scale-to-feature** — match B's dimension to a measured feature of A |
| crown-seat / floor-seat / nose-bridge depth | **contact** — slide B along an axis until surfaces touch |
| clip hair/ears under a hat | **interference removal** — trim A-geometry penetrating B |

Order matters (scale → align → coincident → contact → interference), exactly as in `constraint_place`.

## What actually transfers from stage 5 (and what does NOT)

Stage 5's code is avatar-tuned and loose-tolerance — bbox-width "scale", lowest-vertex-of-a-central-column
"contact", ray-toward-the-head-axis "interference". That math works because avatar placement only has to
*look* right; it will **not** survive CAD tolerances (a bearing seat needs face-to-face contact, not "lowest
vertex"). So this is **write the kernel**, not port it. The genuinely reusable IP is three ideas:

1. **The ordering discipline** — each mate closed-form, applied in a fixed order. Sound, transfers as-is.
2. **Central-column seating, not bbox seating** (`constraint_place.py:130–135`) — seat the central column so
   an overhang (hat brim / gear hub flange) doesn't false-trigger the seat. Generalizes directly.
3. **Interference removal by ray-toward-reference-axis** — elegant and domain-independent in spirit.

`scene_place.py` is the cleanest, most portable piece: scale-to-height + seat-lowest-on-plane + xy-offset,
with no rig dependency. It is already, essentially, a **2D layout solve with one axis dropped**.

## The hard part the libraries don't solve: semantic grounding

In stage 5 anchors are *free* — they come from a **named rig** (`J_Bip_C_Head`, `L_FaceEye`, `C_Neck`). A
procedurally generated gear or extrusion has no rig: just a soup of faces/edges with unstable IDs (the
topological-naming problem). So "bolt the motor to the top plate, concentric with the shaft" must be resolved
to *actual face/datum references* by geometric query — and that resolver is the real product.

**build123d gives you most of this for free.** Its **Joint** system (`RigidJoint`, `RevoluteJoint`,
`CylindricalJoint`, …) *is* a named mate frame baked onto a part — the CAD equivalent of the rig bones stage 5
relied on. The LLM mates frame-to-frame by name (`motor.RigidJoint("shaft")` → `coupler.RigidJoint("boreA")`)
and never touches a raw face ID. The residual work is **authoring/deriving stable joints** for generated
parts; query-based selectors (`.faces(">Z")`) are positional and can drift when parameters change (selector
fragility = the topo-naming problem reincarnated, but smaller).

## Backend choice: build123d / CadQuery, not OpenSCAD

OpenSCAD libs (BOSL2, NopSCADlib — gears, fasteners, 2020/2040 extrusions, plumbing) are excellent as a
*catalog of what parts exist*, and an OpenSCAD part **can** become a FreeCAD solid with mass + material — but
it's **faceted** (mass approximate to tessellation, not feature-parametric), because OpenSCAD has no true
curves (every involute is a polygon approximation). Fine for mass/BOM/visualization/rough layout; wrong for
exact mating, threads-that-screw, or post-hoc parametric edits.

**build123d / CadQuery** (+ `cq_warehouse`, `cq_gears`) build on OpenCASCADE → true B-rep, exact curved
faces, exact mass, STEP that imports into FreeCAD as native parametric solids. Same library coverage, right
geometry type. Backends can be **mixed per part** by what each part must do. For closed kinematic loops
(planetary gearset, linkage) the deterministic ordered solve isn't enough — *delegate to FreeCAD's Assembly
DOF solver*; the constraint-kit kernel handles assembly *trees*.

## "Zoom in" = tier by interaction (the project's existing doctrine)

The scene pipeline already tiers fidelity by interaction — *only make real what gets touched* (far field =
HDRI; contact set = real mesh; pose = SAM3D-Body). The same principle is how CAD assemblies nest: a gearbox
is housing + gears + shafts + bearings + fasteners; each subassembly solved locally then placed as a unit.
"Zooming in" = re-running the same pipeline at finer parameter granularity on the part you're detailing, or
swapping a placeholder bbox for a fully generated part. The architecture nests cleanly.

## Suggested first cut: 2D layout

The cheapest faithful test of the whole thesis is the **2D layout** reduction of the kernel — it's literally
`scene_place.py` with z dropped: scale-to-feature + seat-on-line + offset, deterministic, tolerance-tolerant,
DXF/SVG out (laser/CNC). It exercises the LLM→constraints→deterministic-solve loop in the entity space where
grounding is *easiest* (points/lines/arcs, a tiny nameable vocabulary). Then layer in real B-rep parts +
build123d Joints for 3D. (Distinct from a 2D **sketch** constraint solver / GCS — planegcs, SolveSpace —
which is the foundation of parametric *parts* but is iterative, not the deterministic ordered solve here.)

## Status
**Phase 0 BUILT & verified end-to-end (2026-06-03)** — see `cadkit/` and `drivers/phase0.py`. A CPU-only
`cadkit` service (FastAPI, port 8195) plans an assembly with the resident **qwen27b** model, generates
exact B-rep parts (cadquery + cq_gears), assembles them with the deterministic coincident mate, exports
STEP+GLB, renders via the reused **vrm-automation-blender:4.2.0** image, and QAs with **qwen27b** vision.
No new models spun up; no GPU used. Proven: a plate + 20T module-1 involute gear seated on the boss with
zero gap (geometric ground truth + VLM confirmed), and a 3-part chained stack (plate→spacer→gear).

**Phase 1 BUILT — 2D layout → DXF.** `parts2d.py` (rect_panel/disc/bracket footprints with named 2D
anchors) + `layout.py` (deterministic in-plane coincident+offset solve → DXF with one layer per part +
matplotlib PNG preview). Proven: LLM-planned row of 3 panels + a disc, exact gaps, valid DXF (0 audit
errors), qwen27b QA = no overlap. All builds/layouts persist to **atlas** (Neo4j, `Ck*` namespace).
Ops doc: `CLAUDE.md`.

**Phase 2 (cadquery-native).** `mesh` gear-mate at the exact involute center distance `m·(z₁+z₂)/2`
(verified geometrically + qwen27b); native fasteners (bolt/nut/washer) with **per-hole plate anchors**
(`plate.bolt{i}`); a simplified bearing envelope; multi-gear trains via chained mesh. An LLM-planned
6-part fastened assembly (plate + gear-on-boss + 4 corner bolts) builds and persists. A **19-test
dependency-free regression suite** (`tests/run.sh`, exit 0 gates) asserts every geometric invariant.

**Phase 2b — build123d/bd_warehouse catalog (no second image needed).** The cadquery-ocp conflict was
*tested* and does NOT occur (build123d 0.10 + cadquery 2.7 share cadquery-ocp 7.8.1.1), so build123d +
bd_warehouse live in the same `cadkit` image. Added real catalog parts — aluminum **`extrusion`** (V-slot
2020/2040…), **`ball_bearing`** (608 etc.), ISO **`screw`** — converted to cadquery via the shared TopoDS
and flowing through the same builder/export/mass/atlas path. LLM-driven catalog build (120 mm extrusion +
608 bearing) proven. **21-test suite** green. This reaches back to the project's original vision
(extrusions, standard fasteners, bearings; plumbing pipe/flange available next in bd_warehouse).

**Phase 2c — build123d Joints as first-class oriented mate frames** (the durable grounding for mate
intent). `joints.py`: a joint is a named frame whose +Z is the mate axis; build123d parts author real
`RigidJoint`s (extracted via `joints_from_b123d`), cadquery parts use `frame()`. Because frames carry
orientation, the deterministic `rigid`/`coincident` solve now aligns **axes**, not just points — and joint
**names are recomputed from params, so they survive any parameter change** (no `.faces(">Z")` selector
drift). Added `bearing_block` (RigidJoints), extrusion slot-mount frames, bearing `bore_mid`, the `rigid`
mate. Proven on the target *"screw fastens a bearing block to a 2020 extrusion"*: block→slot, bearing→bore,
screw→bolt-hole — all axes aligned, all contact gaps exact, joint names stable across param changes,
valid STEP/GLB. **25-test suite** green; LLM builds it from natural language.

**Phase 2d — kinematic joints + catalog completion.** Added `revolute` (1-DOF: rigid + rotate about the
shared axis by `angle_deg` — a shaft spinning in a bore) and `cylindrical` (revolute + axial `slide`)
mates, a `shaft` part (with an off-axis `key` marker so rotation is testable), and the `sprocket`/`flange`/
`pipe` catalog wrappers (chain drive + plumbing). The LLM builds a shaft journalled in a bearing-block bore
with a sprocket on top (revolute 30°). **29-test suite** green. The kit now spans gears, extrusions,
fasteners, bearings, plumbing, sprockets, and kinematic motion — the original vision, realized.

**Phase 3 — the planetary benchmark + closed-loop DOF.** Added `ring_gear` + `planetary_gearset` (cq_gears;
correct meshed sun+N-planets+ring, ground-truth-verified as 5 solids at the right radii), `planetary.py`
(epicyclic math: `compute`/`validate`/`mobility`), builder **loop detection** (`find_loops` — flags the
planet's 2nd mesh as the loop the tree solver provably skips; surfaced as `needs_dof_solver`), and the
`/dof/planetary` closed-loop DOF/mobility analysis (Willis: 2-DOF free, 1-DOF ring-grounded; validates the
loop closes). 34-test suite green; LLM builds a planetary set.

**Phase 3b — geometric closed-loop DOF (SolveSpace).** `linkage.py` solves a planar **4-bar** and
**slider-crank** via `python-solvespace` (the same SolveSpace engine FreeCAD's Assembly3 wraps — lighter,
no GUI, in the one CPU image). Reports mechanism mobility cross-checked two ways (SolveSpace solved-DOF ==
Gruebler/Kutzbach): 4-bar = 1, slider-crank = 1; fixing the input determines the pose and the loop closes
(residuals ~1e-5). A `four_bar` part builds the posed bars+pins (one connected closed solid). Endpoints
`/dof/four_bar`, `/dof/slider_crank`. We deliberately did **not** stand up FreeCAD to use FreeCAD — that
would be fake progress; SolveSpace is the honest geometric DOF solver.

**This completes the solver taxonomy:** tree assemblies → deterministic mate kernel; gear loops →
analytical Willis mobility; geometric loops → SolveSpace constraint solver. 38-test suite green.

## Spec compiler — on-demand engineering-fact resolution (LangGraph + SearXNG + qwen27b + SQLite)

The next layer turns authoritative sources into normalized, **provenance-linked, executable facts** for
the CAD generators/validators. The moat is the bot that reliably does:

```
need fact → check SQLite cache → (miss) SearXNG discovery → fetch source
          → deterministic HTML/PDF extraction → (visual tables) qwen27b VLM
          → normalize units/schema → rule-based trust score → validate (schema/unit/provenance)
          → persist SQLite → optional JSON artifact → executable CAD params/validator facts
```

Orchestrated by **LangGraph** (`spec_graph.py`, 13 nodes), all **fail-soft** (no internet/PDF/VLM → a
structured `ok:false`, never a crash). **SQLite** (`SPEC_DB_PATH`) is the durable store — *not atlas*
(atlas gets only a `CkSpecResolution` work-breadcrumb). **Every value carries `{value, unit, quantity,
source_ref, confidence}`**; search snippets are never executable facts. **Trust is rule-based + clamped**
(official 1.0 … preseed 0.85 … forum 0.1, with evidence modifiers). **VLM/LLM are not ground truth** —
qwen27b assists visual-table extraction only and its output is accepted only after schema/unit/value-match
validation. kinds: **threads** (M3–M10, pitch drives real thread geometry), **bearings**, **materials**, **fits** (real ISO 286 µm tolerances when given a nominal size) (+ auto-detect): `POST /spec/resolve {"query":"M6x1",
"kind":"thread"}` → provenance-linked nominal-diameter + pitch facts, persisted in SQLite, resolvable
offline from the designation+preseed with live sources adding confirmation. 9 spec tests (mocked
SearXNG/VLM/fetch); 70/70 total.

Lower-leverage / open: wire `resolve_thread` into screw generation (attach source_refs to part metadata);
real-thread fasteners; more bd_warehouse wrappers; 2D nesting (an optimizer).

Seed-stage source of truth for the working (Blender-coupled) **placement stage 5**:
`../placement/scripts/constraint_place.py`, `constraint_place_multi.py`, `scene_place.py`. Full pipeline
context: `../placement/CLAUDE.md`. See `constraint_kit.py` for the intended geometry-only API sketch.
Next decision after the scene pipeline's body-pose half lands.
