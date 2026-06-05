# constraint-kit

Generate **exact parametric CAD assemblies** from natural language: an LLM plans parts+params+mates,
parametric generators emit B-rep geometry, a deterministic mate kernel assembles them, and the result
is rendered + QA'd. The CAD analog of the `../placement` pipeline, with parametric generation replacing
placement's fidelity-limited SAM3D mesh-lift stage. Thesis: `README.md`.

Working dir: `/srv/nvme-data/containers/constraint-kit` (also `/home/josh/containers/constraint-kit`).

## ⭐ Architecture — reuse the resident models, add one CPU service

The doctrine (from `../placement`): **AI for semantics, deterministic geometry for placement.** The LLM
proposes; exact geometry + the mate kernel dispose. The GPU-heavy models are REUSED, never duplicated:

| stage | how | reused? |
|---|---|---|
| plan (parts+params+mates) | `qwen27b` VLM @ `localhost:8000`, **`response_format` json_schema** | reused |
| generate (params → B-rep) | cadquery + cq_gears, in `cadkit` | new (CPU) |
| assemble (deterministic mate) | pure-Python kernel, in `cadkit` | new (CPU) |
| persist (all work) | **atlas** = shared `n4j_atlas` Neo4j (`bolt://127.0.0.1:7687`) | reused |
| render (for QA) | `vrm-automation-blender:4.2.0` + `../placement/scripts/render_glb.py` | reused |
| QA / verify | render → `qwen27b` vision **+ geometric ground truth** | reused |

`cadkit` is **CPU-only** (OpenCASCADE is CPU) → it never contends with the comfy/LLM GPU instances.

## Layout

```
constraint_kit/        # the package (bind-mounted into cadkit -> edit + restart, no rebuild)
  parts.py             # native 3D parametric generators; (cadquery.Workplane, anchors) + PART_GENS
  parts_bd.py          # build123d/bd_warehouse catalog parts (extrusion/bearing/screw) + PART_GENS_BD
  parts2d.py           # 2D footprint generators (entities+anchors+bbox) + PART2D_GENS   [Phase 1]
  joints.py            # first-class ORIENTED mate frames: frame() + joints_from_b123d() (build123d)
  mate_intent.py       # MATE-BY-INTENT resolver [T2.2]: semantic intent -> concrete frames+mate type (deterministic)
  planetary.py         # GEAR-loop: epicyclic math compute()/validate()/mobility() (analytical, no deps)
  linkage.py           # GEOMETRIC-loop: 4-bar/slider-crank DOF via python-solvespace (constraint solver)
  mates.py             # deterministic 3D mate kernel: coincident/rigid (frame->frame), contact, mesh
  layout.py            # deterministic 2D layout solver + DXF/PNG export                 [Phase 1]
  builder.py           # 3D LEAF assembly: ordered mate solve -> cadquery.Assembly -> STEP+GLB + mass
  assembly.py          # HIERARCHY: recursive subassemblies + ports + BOM/mass + build_design + interference
  parameters.py        # TOP-DOWN design: requirements -> derived params (safe AST eval, provenance) [Phase B]
  massprops.py         # mass properties: exact CG + inertia tensor (OCC GProp, density-weighted) [T4.1]
  bom.py               # BOM document export (Markdown/CSV) from a build-tree roll-up [T7.3]
  drawing.py           # 2D drawing export [T7.2]: cross-section DXF + orthographic projection SVGs + overall dims
  rules.py             # design-rule + strength checks [E9/T4.3]: engagement/fit/clearance, beam-bending, bolt-preload
  validate.py          # interference/clash check: bbox prefilter + exact OCC boolean (world solids) [Phase C]
  synthesis.py         # SMT design synthesis (Z3): discrete/mixed-int constraint solving -> design to a spec
  dsl.py               # the DSL recipe layer: validate() diagnostics (LSP backend) + verify() exact-volume vs reference
  planner.py           # qwen27b, schema-enforced specs: plan() [3D] + plan_layout() [2D]
  store.py             # atlas persistence (Neo4j); FAIL-SOFT. Also CkSpecResolution work-breadcrumbs
  qa.py                # qwen27b vision QA
  spec_compiler.py     # SPEC COMPILER core: preseed, trust scoring, fact build, validate, resolve()
  iso286.py            # ISO 286 limits&fits: IT-grade + fundamental-deviation formulas (real µm tolerances)
  spec_graph.py        # LangGraph orchestration of the spec-compilation flow (13 nodes)
  spec_db.py           # SQLite durable spec store (resolutions/facts/sources/links/cache) — NOT atlas
  spec_sources.py      # SearXNG discovery + rule-based source ranking (snippets are NOT facts)
  spec_extract.py      # fetch + deterministic HTML/PDF extraction + qwen27b VLM (visual tables only); hardened parse/confirm [T5.3]; pdf_oxide layout-aware PDF reader [durable]
  spec_cache.py        # optional JSON artifact dump (debug/export) under SPEC_CACHE_DIR
  api.py               # FastAPI (port 8195)
cadkit/                # Dockerfile + docker-compose.yaml + README + output/
drivers/phase0.py      # 3D orchestrator: plan -> build -> Blender render -> QA -> persist
drivers/phase1.py      # 2D orchestrator: plan_layout -> solve+DXF+PNG -> QA -> persist (no Blender)
drivers/showcase.py    # FULL-STACK demo: SMT-synthesize a planetary gearbox -> hierarchy + fasteners +
                       #   frame -> BOM/mass -> interference -> thread provenance -> render -> qwen27b QA
drivers/capstone_gearbox.py  # CAPSTONE: the whole stack with solvers CROSS-VALIDATING — SMT designs the
                       #   ratio, Willis independently confirms it; build (parallel prewarm) -> interference
                       #   -> BOM/mass/CG -> exploded + 2D drawing + BOM doc -> render+QA (fail-soft). Hermetic
                       #   proof: tests/test_kernel.py::test_capstone_gearbox_full_stack
```

## Run

```bash
CK_GIT_SHA=$(git rev-parse --short HEAD) HOST_UID=$(id -u) HOST_GID=$(id -g) docker compose -f cadkit/docker-compose.yaml up -d --build   # CK_GIT_SHA optional (design-version provenance, T8.1)
curl -s http://127.0.0.1:8195/health        # check "atlas": "connected:..."
python3 drivers/phase0.py "a plate with a spacer and a 24-tooth gear stacked on its boss"
```

Edit `constraint_kit/*.py` then `docker restart cadkit` (uvicorn is not in `--reload`). Rebuild the
image only when changing `cadkit/Dockerfile` (deps).

## API (`127.0.0.1:8195`, host-only)

- `GET  /health` — includes atlas status
- `POST /plan   {request}` — LLM spec only
- `POST /assemble {spec,name?,request?,resolve_specs?,allow_live_spec?}` — build+export+persist from a spec
- `POST /build  {request,name?,resolve_specs?,allow_live_spec?}` — plan THEN assemble (one shot)
- `POST /qa     {name,png?,verdict?}` — attach render+verdict to a stored assembly
- `POST /layout2d {request|spec,name?}` — **[Phase 1]** plan/solve a 2D layout → DXF + PNG preview + persist
- `POST /layout_qa {name,verdict?}` — attach a layout verdict
- `POST /dof/planetary {…}` — GEAR-loop DOF (analytical Willis mobility)
- `POST /dof/four_bar {ground,crank,coupler,rocker,input_angle_deg?}` — GEOMETRIC-loop DOF (SolveSpace)
- `POST /mechanism/four_bar {ground,crank,coupler,rocker,input_angle_deg,width?,thickness?,name?}` —
  **[E10/T10.2]** route the 4-bar loop through SolveSpace and POSE it as an assembly of 4 distinct `link`
  parts (the bridge geometric-solver → builder); loop closes by construction → one STEP+GLB
- `POST /dof/slider_crank {crank,rod,offset,input_angle_deg?}` — GEOMETRIC-loop DOF (SolveSpace)
- `GET  /runs?limit=` · `GET /assembly/{name}` · `GET /catalog` · `GET /layouts?limit=` — read back from atlas
- `/assemble` & `/build` responses include `loop_constraints` + `needs_dof_solver` (mates the tree solver skipped)
- `POST /spec/resolve {query,kind,prefer_cache,allow_live}` — resolve an engineering fact (provenance-linked)
- `GET  /spec/cache?limit=` — recent SQLite resolutions + integrity · `POST /spec/validate {fact}` — schema check
- `POST /spec/search {query}` — debug SearXNG discovery (candidates only, not facts)
- `POST /assembly/tree {defs,root,name?}` — build a HIERARCHICAL assembly (subassemblies + ports) → one
  STEP+GLB + rolled-up BOM/mass; persisted as a `CkAssemblyTree` atlas breadcrumb
- `POST /design {requirements,relations?,asserts?,defs,root,name?}` — **[Phase B]** top-down parametric
  design: requirements → derived params (provenance) → substituted into defs → build; returns the param table
- `POST /design/reedit {…design…,edits}` — **[T8.2]** incremental re-solve: apply `edits` (changed
  requirement values), report which defs are dirty vs clean, and rebuild regenerating ONLY the affected
  subtree's parts (rest reuse the T3.1 cache); returns dependency report + rebuild summary. Warm via `/design` first
- `POST /assembly/bom {defs,root,fmt?,name?}` — export a Bill of Materials (md/csv): qty + mass per type + totals + CG
- `POST /assembly/interference {defs,root,tol_volume?}` — **[Phase C]** clash check (bbox prefilter + exact OCC)
- `POST /assembly/explode {defs,root,factor?,axis?,name?}` — **[E7/T7.1]** exploded view for docs: rank world
  parts along `axis` (default +Z), offset each by rank·factor → one STEP+GLB of the separated assembly
- `POST /assembly/drawing {defs,root,plane?,height?,views?,name?}` — **[E7/T7.2]** 2D drawing set: cross-section
  DXF (cut on plane) + orthographic projection SVGs + overall dims (exact OCC geometry; no GD&T frames)
- `POST /dsl/check {program}` — DSL static validation → LSP-shaped diagnostics (LLM self-correction signal)
- `POST /dsl/verify {program, reference|expected_volume, tol_frac?}` — compile + exact-volume match vs a known
  reference (the corpus filter / RL reward). `drivers/deepseek_dsl.py` closes the loop: deepseek-v4-flash
  (thinking disabled) → DSL → /dsl/check correct → /dsl/verify → keep verified pairs (cadkit/output/dsl_corpus.jsonl)
- `POST /synthesize/planetary {target_ratio,n_planets?,teeth_min?,teeth_max?,module?,ratio_tol?,objective?,build?}`
  — **SMT** design synthesis (Z3): solve a gearset to a ratio spec → optionally build the exact CAD; honest UNSAT
- `POST /synthesize/tolerance {dims,budget_um,grade_min?,grade_max?,method?}` — **[E10/T10.1] SMT** tolerance
  ALLOCATION (Z3, inverse of /tolerance/stackup): loosest ISO 286 IT grades per dim whose stack-up fits the
  budget; honest infeasible (with tightest achievable) when even all-tight busts it
- `POST /synthesize/gear_train {target_ratio,n_stages?,teeth_min?,teeth_max?,stage_ratio_min?,stage_ratio_max?,ratio_tol?}`
  — **[E10/T10.3] SMT** multi-stage synthesis (Z3, nonlinear-int): split a ratio across N planetary stages
  whose ratios multiply to the target, each a valid set (planetary-validated); honest UNSAT
- `POST /rules/{fastener_engagement,fit,clearance}` — **[E9]** design-rule checks (engagement/fit/min-gap)
- `POST /rules/beam_bending {material,length_mm,width_mm,height_mm,load_n,safety_factor?}` · `POST /rules/bolt_preload
  {size,prop_class?,applied_load_n?,preload_fraction?}` — **[T4.3]** closed-form strength checks (cantilever
  stress+deflection vs yield/SF; bolt proof load + preload from ISO 898-1); honest `ok:null` for unknown material/size

## Hierarchical assemblies (`assembly.py`) — the scale foundation

The builder makes a FLAT leaf assembly (parts + mates). `assembly.py` composes those recursively — the
foundation for going from a part to a car to a home (see `ROADMAP.md`). A node def has `parts` (its own
leaf geometry via the builder), `children` (sub-assembly *instances*, the same `ref` reusable N×), and
`ports` (named frames it exposes upward). A parent places a whole child as a unit by mating the child's
**port** to a frame in the parent's space — the SAME `mates.coincident` used for leaf joints, so ports are
just assembly-level joints. Each node builds in its own local frame and is relocated by its parent, so
composition is clean. **Roll-ups** (BOM = recursive part counts, mass, and **CG + inertia tensor** via
`massprops` — exact OCC GProp, density-weighted, about the CoM, validated analytically) accumulate
bottom-up; one combined
STEP/GLB via `builder.export`. Cyclic/unknown refs raise. Proven: `wheel`(rim+hub) reused 2× in
`axle`(+shaft) → BOM `{shaft:1, spacer:4}`, mass rolls up, depth 2 — the car-in-miniature.

**Top-down design (`parameters.py`, Phase B):** requirements drive geometry. `resolve(requirements,
relations)` evaluates derived params with a **safe AST evaluator** (strict whitelist — never `eval`),
fixpoint + cycle detection, and **provenance** (each derived value records its relation + the input values).
`assembly.build_design`/`design_and_export` substitute params into the defs — any `"=expr"` string (e.g.
`"length":"=track"`, `"at":[0,0,"=track"]`) becomes a number — so changing a requirement changes the
geometry deterministically. Requirement `asserts` (e.g. `"clearance > 0"`) refuse to build an invalid design.

**Incremental re-solve (`assembly.reedit_design`, T8.2):** changing a requirement re-solves WITHOUT a full
rebuild. `parameters.param_dependencies` collects the `=expr` free-names each def's geometry reads;
`changed_values` diffs the old/new parameter tables; `affected_defs` marks a def `param_dirty` if it reads a
changed value and `subtree_dirty` if it or any child is — so the report names exactly the dirty vs clean
subtrees. The rebuild then regenerates ONLY the affected subtree's parts (clean subtrees have identical
params → T3.1 part-cache hits), and reports `parts_generated`/`parts_reused`. Proven equal to a from-scratch
design (`test_reedit_rebuilds_only_affected_subtree`). `reedit_and_export` / `POST /design/reedit`.

**Interference validation (`validate.py`, Phase C):** flattens the nested assembly to world-placed parts at
**PART granularity** (one whole shape per leaf — a part's intended internal contact like a gear mesh is NOT
a clash, and this avoids `.Solids()` dropping a compound's per-solid locations), bbox-prefilters pairs, runs
the exact OCC boolean ONLY on overlaps, reports clashes with overlap volume + pair counts (no silent caps).
`assembly.check_interference`. At car/home scale, swap the O(n²) prefilter for spatial bucketing (ROADMAP C).

**Design synthesis (`synthesis.py`, the 4th solver class — SMT/Z3):** the others CHECK or PLACE a given
design; this one DESIGNS one to a spec. `synthesize_planetary(target_ratio, n_planets, teeth bounds,
objective)` solves a discrete/mixed-integer constraint problem (ring teeth = sun+2·planet, ring-fixed
ratio, ISO assembly condition, planet non-interference, tooth bounds) with Z3 Optimize → a valid config +
a ready-to-build `part_spec`, or a **provable UNSAT** when the spec is infeasible (honest, not a near-miss).
The SMT result is cross-checked by the independent `planetary.validate`. `POST /synthesize/planetary`
(`build:true` flows straight to exact CAD). SMT is for **discrete synthesis/configuration/optimization
ONLY** — continuous geometry stays with SolveSpace, gear mobility with the analytical solver, forward
params with `parameters.py`. Solver taxonomy: tree→closed-form · gear→analytical · geometric→SolveSpace ·
**discrete/synthesis→SMT**.

## Spec compiler (on-demand engineering-fact resolution)

The moat is the *bot that reliably resolves a fact with provenance*, NOT a giant static DB. Flow,
orchestrated by **LangGraph** (`spec_graph.py`, 13 nodes), all fail-soft:

```
need fact -> check SQLite cache -> (miss) SearXNG discovery -> rank -> fetch source
          -> deterministic extraction: HTML tables | PDF via pdf_oxide LAYOUT (extract_pdf/_pdf_tables,
             Rust, recovers dense-table cells pypdf scrambles) -> confirm values -> (only if unconfirmed)
             qwen27b VLM last resort
          -> normalize units/schema -> rule-based trust score -> validate -> persist SQLite
          -> optional JSON artifact -> return  (provenance attached to EVERY value)
```

- **SQLite is the durable store** (`spec_db.py`, `SPEC_DB_PATH`) — tables resolutions/facts/sources/
  fact_sources/cache_index, parameterized SQL, atomic per-resolution transaction. **Not atlas.** (Atlas
  gets only a `CkSpecResolution` work-breadcrumb via `store.record_spec_resolution` — never spec data.)
- **Provenance on every value**: `{value, unit, quantity, source_ref, confidence}`; every source carries
  type/url/hash/retrieved_at/license_note/confidence. Search snippets are NEVER executable facts.
- **Trust scoring** (`spec_compiler.score_confidence`, rule-based, clamped [0,1]): base by source type
  (official 1.0 / manufacturer 0.9 / distributor 0.75 / handbook 0.7 / preseed 0.85 / webpage 0.25 /
  forum 0.1) + modifiers (structured table +0.2, 2nd-source confirm +0.2, edition/date +0.1, unit +0.1,
  preseed +0.05, OCR −0.2, VLM-only −0.15, conflict −0.3, missing edition −0.2).
- **VLM/LLM are NOT ground truth.** qwen27b assists visual-table extraction only; its output is accepted
  only after schema + unit + value-match + provenance checks (a VLM value that doesn't match the
  deterministic designation is discarded).
- **Hardened extraction/confirmation (T5.3):** `spec_extract.value_confirmed` gates "a source confirms this
  fact" on every numeric value appearing as a standalone numeric TOKEN (`numbers_in` tokenizes full numbers,
  so `6` no longer confirms against `16`/`0.6` — substring false-positives eliminated); `parse_quantity`
  pulls `(value, unit)` robustly; `extract_text_tables` expands colspan, collapses whitespace, drops empty
  rows, caps pathological size (`truncated` flag). All deterministic + offline-tested.
- **Preseed** is small bootstrap data, explicitly `source_type:"preseed"` — never pretends to be an
  official standard. **Kinds** (each a tiny preseed + parser in `spec_compiler.PARSERS`, generic
  `build_fact`): `thread` (M3–M10, designation encodes nominal+pitch → resolves **offline**), `bearing`
  (608/6000/…→ bore/OD/width), `material` (→ density g/cm³), `fit` (ISO 286), `section` [T5.2] (metric
  structural sections — IPE/UPN I-beam/channel → depth/width/web/flange/mass-per-m, EN 10056 `L a×a×t`
  angles → leg/thickness/mass; offline preseed). `resolve_thread/_bearing/_material/_fit/_section`; `kind:"unknown"`
  **auto-detects**. Each value carries full provenance; live sources add confirmation.
- **Fits are REAL when a nominal size is given** (`iso286.py`): `resolve_fit("H7/g6 at 20mm")` computes
  IT grades + fundamental deviations from the ISO 286 formulas (`i=0.45·∛D+0.001·D`, validated vs ISO
  286-2 tables) → exact hole/shaft deviations + min/max clearance in µm. Hole-basis H + shaft f/g/h/js/k/m/n are
  exact (closed-form, ISO-table-validated). **Interference p/r/s/t/u/v [T5.1]** now resolve too: their fundamental
  deviations (`iso286.INTERFERENCE_EI`) were extracted DETERMINISTICALLY from the authoritative ISO 286-2:2010
  PDF (via `pdf_oxide` bbox reconstruction, x-clustered columns for multi-letter pages —
  `tools/extract_iso286_interference.py`, offline/repro), cross-validated against the closed-form m/n columns
  + known anchors (p6@20=+22, s6@20=+35, t6@24-30=+41, u6@18-24=+41, v6@24-30=+55); the de-scrambling beat
  pypdf, which flattens these visual tables. t is undefined ≤24mm; u/v subdivide 18-30 finer than the IT
  steps. Letters x/y/z… stay honest **class-only + warning** (not yet extracted, never faked); no nominal →
  class-only + "give a size" warning. 3 < D ≤ 500 mm.
- Env: `SPEC_SEARCH_URL`, `SPEC_DB_PATH`, `SPEC_CACHE_DIR`, `QWEN_BASE_URL`, `QWEN_MODEL` (compose).
- **Wired into the builder (opt-in):** a build spec with `"resolve_specs": true` resolves each screw/
  thread part's nominal-diameter+pitch via the spec compiler and attaches `spec`
  `{designation, nominal_diameter_mm, pitch_mm, confidence, source_refs}` to that part's report —
  geometry untouched, fail-soft, **default off** so existing builds are unchanged. Exposed at the request
  level on both `/assemble` and `/build` (`resolve_specs`, `allow_live_spec`) OR via the spec dict — so any
  natural-language `/build` can be provenance-aware. `allow_live_spec` opts into live confirmation (default
  offline/preseed). **For `threaded_rod` the resolved pitch is injected into the geometry params** (a build
  pre-pass) so the spec fact literally drives the thread; for other parts it's report metadata.

## Part + mate vocabulary

Parts (`parts.py::PART_GENS`), each exposes **named anchor frames** (the rig-equivalent the kernel mates to):
- `plate(width,depth,thick,boss_d,boss_h,bolt_d,bolt_circle,bolt_count)` → `mount`,`base`,`boss_axis`,
  **`bolt0..boltN`** (per-hole anchors on the top face, matching the polarArray hole centers — seat a
  fastener in a specific hole via `bolt.seat → plate.bolt{i}`)
- `spur_gear(module,teeth,width,bore_d,pressure_angle)` → `bore_base`,`bore_top` (involute via cq_gears;
  falls back to a bored cylinder blank if cq_gears errors — `gear_fallback` flag surfaces it)
- `ring_gear(module,teeth,width,rim_width)` → `bore_base`,`bore_top`,`axis` (internal gear, cq_gears)
- `planetary_gearset(module,sun_teeth,planet_teeth,width,rim_width,n_planets)` → `base`,`top`,`axis` —
  complete meshed sun+N-planets+ring (cq_gears), RAISES on an invalid set (`planetary.validate`)
- `adapter(bottom_w,bottom_d,top_d,height)` → `base`,`top` — square-to-round loft transition/duct (non-prismatic, T1.3; `finish:{shell}` hollows it)
- `housing(width,depth,height,wall,bore_d,bolt_d,bolt_circle,bolt_count,fillet)` → `base`,`top`,`bore_axis`,`bolt0..N` — open-top enclosure (filleted box, shelled, floor bore + mount holes; first 'rich' part, T1.5)
- `sheet_bracket(thickness,base_length,flange_length,width,bend_radius)` → `base`,`mount`,`flange_end`,`flange_face`,`bend` — constant-thickness sheet-metal L-bracket, one 90° bend; `bend_radius` rounds the inner edge (fail-soft, T1.4)
- `panel(width,depth,height)` → `base`,`top`,`center`,`x_pos`/`x_neg`/`y_pos`/`y_neg` — plain rectangular board/member/box; the architectural primitive (stud/plate/floor/sheathing), used by the `architectural` library (T6.3)
- `link(length,width,thickness)` → `p0`,`p1`,`center` — planar linkage bar (rounded ends + pivot bores); the interface primitive for posing mechanisms as assemblies (T10.2: a 4-bar = 4 links placed by the geometric solver)
- `spacer(outer_d,bore_d,height)` → `bottom`,`top` (enables chained stacks plate→spacer→gear)
- `shaft(diameter,length)` → `base`,`top`,`mid`,`key` (off-axis marker; mate into a bore via `revolute`)
- `threaded_rod(major_diameter,pitch,length)` → `base`,`top`,`axis` — a REAL helical thread
  (bd_warehouse `IsoThread`) whose pitch is driven by the value; with a `designation` param + `resolve_specs`
  the builder injects the spec-resolved major_diameter+pitch (spec fact → geometry). ~slow (heavy thread)
- `four_bar(ground,crank,coupler,rocker,input_angle_deg)` → joints `A`,`B`,`C`,`D` — a planar 4-bar posed
  by the SolveSpace solve (bars+pins in z=0, a single connected closed loop); raises if it can't close
- `bolt(shank_d,length,head_d,head_h)` → `seat`(under-head),`tip`,`head_top` (head up, shank down −Z)
- `nut(af,height,bore_d)` → `bottom`,`top` (hex, `af`=across-flats) · `washer(outer_d,bore_d,thick)` → `bottom`,`top`
- `bearing(outer_d,bore_d,width)` → `bore_base`,`bore_top` (⚠ SIMPLIFIED envelope, no balls/races — fit/mass only)

**Catalog parts** (`parts_bd.py::PART_GENS_BD`, build123d/bd_warehouse — real standard parts, merged into
the same dispatch via `builder.ALL_PART_GENS`; converted to cadquery through the shared TopoDS):
- `extrusion(rail_size,length)` → `base`,`top`,`center` — aluminum V-slot ('20x20'…'40x40'), along +Z
- `ball_bearing(size,bearing_type)` → `bore_base`,`bore_top` — real deep-groove bearing (size 'M8-22-7'=608)
- `screw(size,length,simple,fastener_type)` → `seat`,`tip`,`head_top` — ISO socket-head cap screw
  (`simple=True` skips thread geometry for speed). bd_warehouse is imported lazily per-generator.
- `bearing_block(width,depth,height,bore_d,bolt_d,bolt_spacing)` → JOINTS `mount`,`top`,`bore`,`bolt0`,`bolt1`
  (build123d RigidJoints; a pillow block — back face mounts to a rail slot, vertical bore holds a bearing,
  two through-holes for mounting screws)
- `sprocket(num_teeth,chain_pitch,thickness,bore_d)` → `face_a`,`face_b`,`center` — roller-chain sprocket
- `flange(nps,flange_class,kind)` → `bore_base`,`bore_top` — ASME B16.5 pipe flange (weld_neck/slip_on/blind)
- `pipe(nps,length,material,identifier)` → `end_a`,`end_b`,`center` — straight standard pipe (plumbing)
- `extrusion` also exposes slot-mount joints `slot_xp/xn/yp/yn`; `ball_bearing` also `bore_mid`.
- Still available in bd_warehouse, not yet wired: real-thread fasteners, `HexNut`, lead screws, V-wheels.

## Joints — oriented mate frames (the durable grounding)

`joints.py` makes mate frames **first-class, authored, oriented**: a joint is a named `cq.Location` whose
**+Z is the mate axis** (bore axis, screw shaft, face normal). Two sources, identical downstream:
- build123d parts author real `RigidJoint`s; `joints_from_b123d(part)` extracts them as cq.Locations.
- cadquery-native parts build the same frame with `frame(origin, z_axis, x_axis)` (pin `x_axis` to control
  roll — unpinned roll lets a mate rotate the moving part about the mate axis; e.g. a side-slot mount needs
  `x_axis=(0,0,1)` to keep the block upright).

**Why this matters:** joint names are recomputed from params, never selector-derived, so
`block.joints["bore"]` is stable across any parameter change — no `.faces(">Z")` drift. And because frames
carry orientation, the existing `coincident`/`rigid` solve (`a_world * b_local.inverse`) aligns the **axes**,
not just the points. This is the fix for grounding mate intent to durable references. Proven by
`test_joint_names_survive_param_changes` and `test_target_assembly_*` (screw fastens a bearing block to a
2020 extrusion: block→slot, bearing→bore, screw→bolt hole, all axes aligned, all gaps exact).

Any part spec may carry opt-in composable `"finish"` post-ops applied shell→chamfer→fillet (`builder._apply_finish`, fail-soft per op): `{"shell"?: t, "open_face"?: sel, "chamfer"?: r, "fillet"?: r, "edges"?: sel}` — `shell` hollows to wall thickness t (sealed; `open_face` removes a face), fillet/chamfer default to ALL edges. Stable *named* edge selection across regen is still research (R1.a).

`joints.derive_ports(wp)` DERIVES oriented ports from a *built* solid (top/bottom/center/bore_axis, via face queries + OCC cylinder-axis) — geometry-grounded interfaces interchangeable with authored joints. **T2.3 proves** (`test_derived_ports_survive_param_changes`) that across *topology-preserving* parameter changes the derived name set + each port's axis are stable and origins track the params by closed form — the regime intent-mating relies on. The R2.a boundary remains: a feature appearing/disappearing changes the derivable set (authored joints keep the name; a derived port can't fabricate an absent feature).

**Mate-by-intent (`mate_intent.py`, T2.2):** the bridge from semantic intent to the deterministic kernel. A mate may say HOW parts connect (`{"a","b","intent"}`) instead of naming frames; `resolve_intent` DETERMINISTICALLY (no LLM) maps a small vocabulary — `seat_on` (coincident, B base→A top/mount), `insert` (rigid, B axis→A bore; revolute/cylindrical override for a spinning shaft), `fasten` (rigid, fastener seat→A bolt hole, opt `hole_index`), `mesh` (two gears) — onto the parts' AUTHORED anchors first, falling back to the T2.3 derived ports (a chosen derived frame is injected into the part's anchors for the frame-based solver). Honest: an unknown intent, a disallowed type override, or an unsatisfiable role RAISE (never a silently-wrong mate). `builder.normalize_mates` resolves intents before placement, so an intent build is geometrically IDENTICAL to the hand-authored explicit-frame build (`test_mate_intent_equivalent_to_explicit`). Planner emits `intent` (schema enum + SYSTEM prompt); explicit a_joint/b_joint still work and override the auto-pick.

Mates (`mates.py` + builder dispatch): `coincident`/`rigid` (lands B's frame exactly on A's — `rigid`
signals an oriented joint mate and aligns axes), `contact` (drops B so its bbox bottom rests on A's plane —
the placement floor/crown-seat, generalized), `mesh` (two
spur_gears only — places B at the exact involute center distance `m*(za+zb)/2`, coplanar, half-tooth
phased, at `angle_deg` around A; gears must share module), `revolute` (KINEMATIC 1-DOF: rigid + rotate B
about the shared axis by `angle_deg` — a shaft spinning in a bore, a hinge), `cylindrical` (revolute +
axial `slide`). Mates apply **in order**, B relative to A's
current world position, so chains compose. To add a part/mate: extend `PART_GENS` / `mates.solve` (or the
builder mesh-style branch for geometry/param-aware mates) and the planner's `SPEC_SCHEMA` enum + `SYSTEM`
prompt together.

**2D parts** (`parts2d.py::PART2D_GENS`, Phase 1) — flat footprints with named 2D anchor points, for
DXF/laser/CNC: `rect_panel(width,height,hole_d,hole_inset)` (anchors center/edges/corners), `disc(diameter,
bore_d)`, `bracket(arm,width)`. The 2D mate is `coincident` with an optional `offset:[dx,dy]` gap (e.g.
right-edge→left-edge + [10,0] = "beside with 10mm gap"); each part may carry a `rotate` param. Same
deterministic in-order solve; `layout.py` exports DXF (one layer per part) + a matplotlib PNG preview.

## atlas (Neo4j) graph model

Shared community DB (single `neo4j` database), so everything is namespaced with `Ck*` labels +
`project="constraint-kit"` (verified isolated: our nodes are a small island in the shared graph). Writes
are **parameterized Cypher** (no injection). Model:

```
(:CkRun)-[:PRODUCED]->(:CkAssembly)-[:HAS_PART]->(:CkPart)-[:OF_TYPE]->(:CkPartType)   # 3D
(:CkPart)-[:MATE {type,a_joint,b_joint,seq}]->(:CkPart)        # a -> b, ordered
(:CkRun {kind:'layout2d'})-[:PRODUCED]->(:CkLayout)-[:HAS_PART]->(:CkPart2D)-[:OF_TYPE]->(:CkPartType)  # 2D
(:CkPart2D)-[:MATE {a_joint,b_joint,offset,seq}]->(:CkPart2D)
```
`CkAssembly` carries total_mass_g, bbox_size, glb/step/render_png paths, vlm_verdict; `CkLayout` carries
sheet_size, dxf/png paths, vlm_verdict. **Design versioning (T8.1):** `(:CkAssemblyTree)-[:HAS_VERSION]->
(:CkDesignVersion {id, git_sha, params, mass_g, part_count})` — the version id hashes the resolved
parameter values (idempotent; a parametric edit creates a new linked version). `store.record_design_version`
/ `GET /design/versions?name=`. Inspect:
`docker exec n4j_atlas cypher-shell -u neo4j -p microdrama-local "MATCH (a:CkAssembly) RETURN a"`.

## Hard-won gotchas

- **`cq_gears` is GitHub-only**, not on PyPI → `pip install "cq_gears @ git+https://github.com/meadiode/cq_gears.git"` (needs `git` in the image).
- **cadquery `Location.inverse` is a PROPERTY**, not a method (`loc.inverse`, never `loc.inverse()`).
- **This vLLM ignores top-level `guided_json`** → use OpenAI-standard `response_format:{type:"json_schema",...}` (honored). qwen3.6 is a reasoning model → send `chat_template_kwargs:{enable_thinking:false}` or the answer lands in `reasoning` and `content` is null.
- **`cadkit` needs `network_mode: host`** — the planner/VLM and comfy services bind `127.0.0.1`, unreachable via port-mapping; host netns also reaches `rtx0` over tailscale.
- **`OUTPUT_DIR` is the shared host path** mounted identically in the container, so returned glb/step paths are valid on the host for the Blender render step (no path translation).
- **cadquery `Assembly.save` is deprecated** (FutureWarning) but works; GLB export falls back to STL→trimesh→GLB if native GLTF fails.
- **Parallel builds use PROCESSES, not threads** (T3.3): cq_gears' involute math is Python-heavy and OCC holds the GIL, so a thread pool gives NO speedup (measured ~slower); a process pool does. Two entry points: `builder.build_assemblies_parallel` (batch of flat specs, geometry crosses as BREP) and **`assembly.build_tree(..., prewarm=True)`** — the recursive-tree integration (R-T3.3 resolved): `prewarm_cache` generates the tree's DISTINCT parts in a pool and populates the T3.1 cache, so the recursive build + the validated density-weighted mass-props run UNCHANGED against a warm cache (sidesteps the per-leaf-shape problem entirely; prewarmed tree == serial tree, exact).
- **Process pools MUST use the `spawn` start method, not `fork`** — the caller has already loaded cadquery/OCC (which run threads), and fork-after-threads DEADLOCKS the workers (cost me a hung suite). `ProcessPoolExecutor(mp_context=multiprocessing.get_context("spawn"))`. Spawn re-imports per worker (slower) but safe; the test file's `if __name__=="__main__"` guard keeps spawn from re-running the suite.
- Verify placement with **geometric ground truth** (anchor z == seat z), not just the VLM — the VLM is a categorical second opinion (it called a spacer-stack "seated on the boss"; true seating is confirmed by the bbox/anchor math).

## Tests

`tests/test_kernel.py` — dependency-free regression suite (plain asserts, no pytest) asserting the
geometric ground truths: seating zero-gap, contact seat, gear-mesh center distance `m*(z1+z2)/2`,
module-mismatch rejection, mass-vs-density, STEP/GLB export, id/name alias, 2D row placement + sheet
size, DXF validity (ezdxf audit), and the atlas round-trip (or fail-soft when the DB is down). Run:

```bash
tests/run.sh            # docker exec into cadkit; exit 0 = all pass (gates changes)
```

76 tests, all passing. Add a test alongside any new part/mate (assert the invariant, not just "it runs").
Lesson baked into the suite: verify with **geometry** (anchor/bbox math), not the VLM — qwen27b is a
categorical second opinion that misreads counts/angles (it called a bolted plate "no gear" from a `hero`
angle, then "2 bolts, yes gear" from `three_quarter`); the deterministic tests are ground truth.

## trailmark (standing instruction: use it for code-structure work)

`~/.local/bin/trailmark analyze constraint_kit --summary | entrypoints | --complexity N | diff <before> <after>`.
Snapshot before a change (`cp -r constraint_kit /tmp/ckit_baseline`) to get a real structural diff after.
Current: 8 API entrypoints (all `untrusted_external/high`, host-only bound); hotspot
`builder.build_assembly` (complexity 11).

## Phases

- **Phase 0 — 3D assembly** ✅ plate/spur_gear/spacer, coincident+contact mates, STEP+GLB, Blender render, qwen27b QA, atlas.
- **Phase 1 — 2D layout → DXF** ✅ rect_panel/disc/bracket, coincident+offset, DXF (per-part layers) + PNG preview, qwen27b QA, atlas.
- **Phase 2 (cadquery-native) — engineering parts & mates.** ✅ `mesh` gear-mate (exact involute center
  distance), native fasteners (bolt/nut/washer) + per-hole plate anchors, simplified bearing, multi-gear
  trains (chained mesh).
- **Phase 2b — build123d/bd_warehouse catalog parts.** ✅ The OCP-conflict fear was tested and DID NOT
  occur (build123d 0.10 + cadquery 2.7 share cadquery-ocp 7.8.1.1) → no second image. Added real catalog
  parts in the same image: aluminum `extrusion`, `ball_bearing`, ISO `screw`, converted via shared TopoDS
  and flowing through the same builder/export/mass/atlas pipeline. LLM-driven catalog build proven. Next
  here: wire bd_warehouse `pipe`/`flange` (plumbing), `sprocket`, real-thread fasteners.
- **Phase 2c — build123d Joints as first-class oriented mate frames.** ✅ `joints.py` + `bearing_block`
  (RigidJoints) + extrusion slot frames + the `rigid` mate. Proven on "screw fastens a bearing block to a
  2020 extrusion" with all axes aligned, gaps exact, and joint names stable across param changes. This is
  the durable grounding for mate intent (no selector drift).
- **Phase 2d — kinematic joints + catalog completion.** ✅ `revolute`/`cylindrical` mates (1/2-DOF) + a
  `shaft` part (off-axis `key` marker so rotation is testable); `sprocket`/`flange`/`pipe` catalog wrappers
  (plumbing + chain drive). LLM builds a shaft journalled in a block bore with a sprocket on top. 29 tests.
  Planner now told to anchor the first mate on the fixed base part.
## Three solver classes (by loop type)

constraint-kit honestly uses the RIGHT solver for each topology — that's the design, not a limitation:

| topology | solver | where |
|---|---|---|
| **tree** assemblies (parts attach to a parent) | deterministic ordered mate kernel | `builder.py` / `mates.py` |
| **gear** loops (planet meshes sun+ring) | analytical epicyclic mobility (Willis) | `planetary.py` → `/dof/planetary` |
| **geometric** loops (4-bar, slider-crank) | python-solvespace constraint solver | `linkage.py` → `/dof/four_bar`, `/dof/slider_crank` |

`builder.find_loops` detects when a spec's mate graph closes a loop (so the tree solver can't) and routes
the question to the right DOF solver. Each DOF result reports mobility two independent ways that must
agree (e.g. SolveSpace solved-DOF == Gruebler/Kutzbach for linkages; Willis == geometric closure for gears).
NB: SolveSpace's `create_2d_base` workplane floats with a fixed base DOF (measured once in
`linkage._base_dof`); mechanism mobility = solved_dof − base_dof.

## Closed loops & DOF (the planetary benchmark)

A planetary set is the canonical closed loop: each planet meshes BOTH the sun (external) and the ring
(internal), so it carries two simultaneous mesh constraints. The deterministic tree solver places each
part by ONE mate and **cannot close the loop** — `builder.find_loops(spec)` detects this (any mate whose
target is already positioned), `/assemble` reports it as `loop_constraints`/`needs_dof_solver`. The
closed-loop validation + DOF live in `planetary.py`:
- `validate()` — the consistency conditions the tree solver can't check: assembly condition
  `(Zs+Zr) % N == 0`, equal sun-planet/ring-planet centre distances (geometric loop closure), no
  adjacent-planet interference. `planetary_gearset` RAISES if these fail.
- `mobility()` — epicyclic (Willis) DOF: a planetary train is 2-DOF; grounding a member → 1-DOF
  mechanism. Reports `gear_dof`, `loop_consistent`, `loop_closing_constraints` (= N). `/dof/planetary`.
- Geometry ground truth: `cq_gears.PlanetaryGearset` builds the meshed **5-solid** set (sun+3 planets+ring),
  outer radius = `m·Zr/2`+rim — verified by solid-count + radial-material checks (the VLM mis-reads gear
  renders; geometry is truth, as always here).

**On FreeCAD (resolved):** FreeCAD's assembly solver is a *geometric* constraint solver and does NOT model
gear-ratio constraints — so it was the wrong tool for the gear-train DOF (correct method = the analytical
Willis mobility here). The geometric-loop class is now covered by **python-solvespace** (the same SolveSpace
engine FreeCAD's Assembly3 wraps) — lighter, no GUI, scriptable in the one CPU image — via `linkage.py` on
the 4-bar/slider-crank. We deliberately did NOT stand up FreeCAD just to use FreeCAD (that would be fake
progress); SolveSpace is the honest geometric DOF solver. FreeCAD remains optional only if interactive/GUI
B-rep assembly editing is ever wanted.

Deferred / lower-leverage: real-thread fasteners, more bd_warehouse wrappers, 2D nesting.
- **Phase 3 — FreeCAD container** — closed kinematic loops (planetary/linkage) need a DOF solver; the
  kernel here solves assembly *trees* deterministically.
- **Backlog:** 2D non-overlap packing/nesting (an optimizer, not the deterministic kernel); central-column
  contact seat (vs bbox) for overhanging parts; STEP/STL re-import for retrieval-to-CAD.
