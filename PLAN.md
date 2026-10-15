# Plan: from drawing parts to optimizing machines

This is the active plan. `ROADMAP.md` stays as the original thesis (screw → car → home) and
`BACKLOG.md` as the epic list; where they disagree with this document on **ordering**, this one wins,
because the goal has changed. The goal is no longer "demonstrate NL → CAD". It is:

> **Build real machines — and have the tool tell me whether the machine is any good, and make it better.**

The machines are real and on the bench: the knee exoskeleton (`knee-exo`), the 4-axis gear cutter
(`gear-cutter`), the per-axis RP2040 controller (`axis_x`), and the enclosures around them.

---

## 1. Where we actually are

Measured, not recalled:

| | knee-exo | gear-cutter | constraint-kit |
|---|---|---|---|
| source of truth | `KneeExo_v*.FCStd` (binary) | `build_machine.py` (parametric) | the DSL program |
| lines to describe the machine | **45,399** across 376 scripts | **1,945** across 6 scripts | — |
| scripts that mutate `.Shape` in place | **181** | 0 | 0 |
| replayable from scratch | **no** (8 of 376 stages) | yes | yes |
| structural analysis | none | none | closed-form beam only |
| optimization | none | none | Z3 over discrete ratios |

Two further facts that set the agenda:

- `tools/build.sh` says it out loud: *"Each stage depends on every stage above it and destroys the
  work of the stages below."* A chain rebuild that re-ran `399_drivecap` without
  `502_interface_build` left **5.9 cm³** of interference in a shipped model.
- constraint-kit has **no geometry import at all** — STEP/IGES/FCStd export only. It cannot see a
  single part that already exists.

## 2. The one structural fact that gates everything

**You cannot optimize a blob.**

Optimization — of any kind, topology or parametric — is a loop that evaluates a *function* from
parameters to performance. knee-exo has no such function: it has a binary document and 376
one-way migrations applied to it. There is no parameter to sweep, and no way to evaluate a
variant, because producing a variant means replaying a destructive chain that cannot be replayed.

So "make the machine optimized" and "make the CAD stop taking forever" are not two goals. They
are the same goal, and a parametric source of truth is the precondition for both. Every hour
spent on a parametric rebuild buys the optimization loop as a side effect.

This is also why the four hard-won FreeCAD lessons exist (BoundBox bounds control polygons;
placement bakes into `Shape`; `foraminate` over `isInside`; boolean loops crash the instance).
Each is a technique for *interrogating an opaque solid* — archaeology that a parametric source
makes unnecessary, because the model already knows its own numbers.

## 3. Target architecture

FreeCAD's role changes and its value is preserved. It stops being the **model store** and becomes
the **viewer and the finishing line**: the engraving chain, the mark registry, the styled viewport
captures, the STL export, the render manifest — all of that is good work and all of it survives,
driven from STEP instead of from a mutated document.

```
  requirements  ──►  parametric source (per machine)   ◄── the only thing a human edits
                          │
                          ▼
                     kernel  (constraint_kit)
                     exact B-rep · oriented joints · mates · solvers
                          │
         ┌────────────────┼────────────────┬──────────────────┐
         ▼                ▼                ▼                  ▼
    geometric        manufacturing      physics            BOM / mass
    interference     overhang, wall,    stiffness,         rollups
    seating, fit     tool reach         stress, buckling
         └────────────────┴────────────────┴──────────────────┘
                          │   one verdict, structured, with causes
                          ▼
                   optimizer  (parameters → performance)
                          │
                          ▼
                 STEP ──► FreeCAD: engrave · style · render · STL ──► printer / mill
```

Five layers, and only one of them is new:

1. **Source** — a parametric program per machine. Parameters and requirements at the top.
2. **Kernel** — constraint-kit's existing geometry + mate kernel, extended.
3. **Verification** — three tiers that return one structured verdict. Geometric exists.
   Manufacturing exists in pieces (`411_printability.py`). **Physics does not exist. It is the gap.**
4. **Optimization** — parameters → performance, closed loop.
5. **Output** — STEP into the FreeCAD toolchain that already works.

## 4. What the research says

Searched October 2026. Four findings that change the plan, and several that confirm it.

**Physics in the loop is the measured difference between a model that draws and a model that
engineers.** [Physics-in-the-Loop](https://arxiv.org/abs/2605.19717) embeds verification tools
inside an agent's generate–simulate–refine loop and reports **4.2× the structural complexity** of
comparable agentic methods. [COSMO-Agent](https://arxiv.org/html/2604.05547) casts the whole
CAD→CAE→parse→revise cycle as the environment. The direction is unambiguous: the simulator belongs
*inside* the loop, as the reward, not downstream as a report. This is the single strongest argument
for building the physics tier before anything else.

**The field has routed around custom DSLs and fine-tunes.**
[Zero-to-CAD](https://arxiv.org/html/2604.24479) released ~1M executable **CadQuery** programs
covering booleans, fillets, chamfers, **lofts, sweeps, shells** — generated by an LLM that writes
code, executes it, reads the docs on failure, and revises until validation passes.
[FutureCAD](https://arxiv.org/html/2603.11831v2) trains on 140k real feature-based models and emits
executable CadQuery directly. CadQuery won because LLMs are already good at Python.
**Implication: the custom DSL and the Gemma fine-tune are a 2025 bet that 2026 has bypassed.**
Generating build123d/cadquery code against our own kernel, with an execute-and-repair loop, is
strictly simpler and better. That retires `dsl.py`, `training/`, and a large maintenance surface.

**Topology optimization still cannot hand you an editable part.** This is the finding that most
changes what I would otherwise have built. Converting a density field back to parametric CAD is an
open research problem with a 2026-era literature of its own —
[AMRTO](https://www.sciencedirect.com/science/article/abs/pii/S0045782524009277), NURBS
reconstruction via [harmonic mapping and rotation-minimizing
frames](https://academic.oup.com/jcde/article/12/9/162/8250036), [Catmull-Clark subdivision
surfacing](https://www.sciencedirect.com/science/article/pii/S0167839626000506) — and all of it
exists precisely because raw topopt output *"lacks compatibility with CAD systems, necessitating
time-consuming and laborious manual reconfiguration."* **So topopt is an oracle, not a generator:**
it tells us where material wants to be, and a human or an agent then expresses that as parametric
features. Anything else trades the parametric source of truth — the thing this whole plan is
about — for a mesh blob, which is how we got here.

**Overhang belongs in the objective, not in a post-check.** Self-supporting topology optimization
with the overhang constraint integrated into SIMP reports **35.2% less print time and 42.1% less
mass for 4.8% more compliance** ([enhanced TO for self-supporting
structures](https://link.springer.com/article/10.1007/s00170-025-17252-6), Jan 2026). Printability
is a constraint on the optimizer, not an audit afterwards.

**The open FEA stack is ready and agent-drivable.**
[JAX-FEM](https://github.com/deepmodeling/jax-fem) is differentiable, GPU-accelerated, ~10× a
commercial code at 7.7M DOF, and last updated 8 October 2026 — the right long-term target for
gradient-based work. Gmsh + CalculiX is the conventional STEP-in/results-out path and several
projects already wrap it for agents
([toolkit](https://github.com/Alekh-Panjwani/Python-Offline-Autonomous-FEA-User-and-Agentic-Toolkit),
[MCP](https://github.com/superWorldSavior/mcp-calculix),
[FeaGPT](https://arxiv.org/pdf/2510.21993)). **But neither is installed here, and both are heavy
dependencies for a loop that must run thousands of times.** Phase 0 therefore builds a small
voxel solver on numpy/scipy alone — zero install, runs inside FreeCAD's Python too — and we
graduate to Gmsh+CalculiX for final validation and JAX-FEM if gradients are needed.

**Confirmed, no change needed:** the topological naming problem remains unsolved across the
cadquery/build123d ecosystem in 2026, which vindicates constraint-kit's named-joint-frame design;
[CADIR](https://arxiv.org/pdf/2608.00891) is attacking the same problem with a cross-backend
editable IR and persistent naming.

## 5. Phases

Ordered by dependency, then by value per hour. Each phase ends in something that answers a
question about real hardware.

### Phase 0 — Physics core ✅ BUILT AND VERIFIED
numpy/scipy only, no CAD dependency, so it runs on the host and inside FreeCAD.
- `fea/hex8.py` — trilinear hex element, stiffness by Gauss quadrature.
- `fea/voxel.py` — occupancy grid → assembly → reduced linear solve → displacement, compliance,
  von Mises.
- `fea/sample.py` — occupancy from a triangle soup; binary STL reader (dependency-free, so the
  15 printed knee-exo parts and the gear-cutter enclosure are analysable today); OCC tessellation
  path for live shapes.
- Verified against ground truth, not opinion: element symmetry, exactly 6 rigid-body modes,
  uniform-strain patch test exact, axial extension exact to `PL/AE`, bending convergent to
  `PL³/3EI`.

**Verified 2026-10-14.** 19/19 against ground truth: element symmetry, exactly 6 rigid-body
modes, uniform-strain patch test exact, axial extension exact to `PL/AE` (rel. err < 1e-9),
von Mises exact to `P/A`, bending convergent to 95.7% / 97.7% / 98.5% of Timoshenko at 4 / 6 / 8
elements through thickness. Voxeliser cross-checked against exact signed-tetrahedra mesh volume
on all 20 knee-exo printed parts: within +-1.2% at 1 mm pitch, every part watertight.

**First real result (`P3_GantryPlate_Printed`, the documented 764 N belt load through the five
HTD-8M grooves, reacted at the ball-nut end plate),** over pitches 1.82 / 1.30 / 0.93 mm:

| | 1.82 mm | 1.30 mm | 0.93 mm |
|---|---|---|---|
| mass, g | 112.4 | 100.2 | 101.6 |
| p99 stress, MPa | 13.89 | 16.32 | 19.99 |
| safety factor | 1.08 | 0.92 | **0.75** |
| deflection, mm | 2.832 | 3.463 | 4.558 |

**The part does not pass its own documented load.** p99 stress exceeds the 15 MPa creep-aware
PETG allowable at every mesh below 1.8 mm pitch, and deflection is on the order of several
millimetres in series with the knee drive.

**And the repo had already reached the same conclusion by another route.** knee-exo's BOM moved
the ball nut into the bought aluminium DSG16H housing on sourcing grounds, noting that aluminium
"does not creep under 764 N the way a printed pocket does", and that `P3_GantryPlate_Printed`
must be redrawn around it. The FEA agrees from geometry and a material allowable alone, knowing
nothing of that decision. Two independent methods reaching the same verdict is the first real
evidence this tier earns its keep.

**What it still cannot tell you, and why.** The ladder is not converging — deflection increments
*grow* (0.63 then 1.09 mm) — so 4.56 mm is a lower bound rather than an answer. A 2% pitch change
moves it by double figures, because the 3.45 mm groove walls and thin V-wheel arms are three or
four elements through and whether a wall lands on element centres matters more than the
refinement does. Refining out of it is impractical: 0.93 mm is already 336k DOF and 139 s.
**A converged number for this part needs a body-fitted mesh** (Gmsh + CalculiX, issue #3) — which
is exactly the division of labour this phase assumed: the voxel tier for the loop, a conventional
solver for the final figure.

Two notes on believing any of it. The boundary conditions are an assumption — mine, stated on the
command line, not inferred and not taken from the repo. And the Y restraint at the V-wheel arms
carries no load at all; releasing it moved the answer by nothing, which locates the load path as
nut -> plate body -> groove wall.

**An earlier version of this section reported SF 2.9 and "stress is comfortable". That was wrong**
and is retracted: `Model.load` was dividing the applied force across the grid's empty padding as
well as its material, so roughly a quarter of the intended 764 N reached the structure. The solve
converged cleanly throughout. See CHANGELOG 2026-10-15.

### Phase 1 — Parametric source of truth
The precondition. Per machine, one program; the document becomes a build artifact.
- gear-cutter is nearly there — `build_machine.py` already wipes and rebuilds. Finish the job:
  stop forking files (`bench.py` → `bench_v2.py` → `bench_v3.py`, `MachineV2/V3.FCStd`) and make
  the variant an argument.
- axis_x's enclosure is small and should be converted first as the pattern reference.
- knee-exo is the hard call: extend `build_headless.py`'s `CHAIN` from 8 stages back to a
  `newDocument` root (the roots exist — `02_base`, `51_v3base`, `63_v4base`, `701_mirror_build`),
  making every stage idempotent. Weeks of grinding for ~60 parts. **Decide explicitly whether
  this is v9's job or v10's before starting.**
- A `params.py` per machine and named datum frames, so no dimension is ever measured off geometry
  again.

### Phase 2 — Kernel gaps that block real machines
- **STEP/IGES import.** Not in any of the nine epics, and it is the thing that lets the kernel
  touch existing work and lets Phase 0 see Phase 1's output. Smallest high-value item in the plan.
- **The missing joints.** `mates.py` implements six: `coincident`, `rigid`, `contact`, `mesh`,
  `revolute`, `cylindrical`. The hardware needs **`prismatic`** (linear rails), **`screw`** (the
  1605 ballscrew, 5 mm lead), **`belt`** (HTD-8M knee drive, the A-axis two-stage 9:1 GT2), and
  `gear_rack`. Without these the machines cannot even be *described*, let alone swept.
- **Freeform geometry.** `parts.py` is 429 lines of catalog primitives and `construct.py` caps at
  48 ops with no spline and no loft-through-sections. The conical cuffs, the blended motor pod, the
  31° ramp are not expressible. Zero-to-CAD's op coverage (loft/sweep/shell) is the target set.

### Phase 3 — Closed-loop parametric optimization
The payoff, and the reason Phase 0 comes first.
- An objective over the parametric program: minimise mass subject to tip deflection, or maximise
  stiffness-to-mass, with the physics tier as the evaluator.
- Derivative-free first (`scipy.optimize.differential_evolution`, CMA-ES) over a handful of
  parameters — plate thicknesses, rib counts, web positions. Gradients (JAX-FEM) only if the
  parameter count demands it.
- Build caching keyed on a content hash of the parameter vector, or the loop is dead on arrival.

### Phase 4 — Manufacturing truth as optimizer constraints
Not a new audit — constraints the optimizer must respect.
- Overhang angle, minimum wall, minimum feature, bridging span, tool reach and drill access.
- Fold in `411_printability.py`, which already encodes the rules that actually bite on his printer.

### Phase 5 — Topology optimization as an oracle
SIMP on the Phase 0 assembly, with the overhang constraint in the objective per the 2026 result.
Output is a **material-layout map that informs parametric features** — never a mesh promoted to a
part. Keeps the source of truth parametric by construction.

### Phase 6 — Structured diagnosis and repair
`validate.py` already returns `{ok, n_parts, pairs_total, pairs_exact_checked, clashes:[{a,b,
overlap_volume_mm3}]}` — the structure is there, the *cause* and the *ranked fix* are not. Add
cause attribution and repair proposals, re-verified before being offered. The 5.9 cm³ incident is
the worked example: that failure knew its own cause and said nothing.

### Phase 7 — Agent surface
An MCP server over the kernel, so this is drivable the way the FreeCAD RPC already is. Code
generation against the kernel with an execute-and-repair loop, per Zero-to-CAD/FutureCAD —
**not** a custom DSL and not a fine-tune.

## 6. Deliberately not building

Each of these is defensible work that does not move a machine onto the bench:

- GD&T feature control frames, datum schemes, surface-finish specification.
- Process profiles for SLA, sheet metal, injection moulding. He FDM-prints and mills.
- Cost and lead-time estimation.
- A public benchmark, a web playground, TypeScript types, an adoption surface.
- A forty-part catalog expansion before the port/interface model exists.
- More spec-compiler kinds. It is ~1,100 lines resolving facts from the web, and the values that
  bind on these machines are already measured and in memory.
- Any further LLM fine-tuning. `training/` is a finished research result; it is not on this path.

## 7. Status

Tracked in GitHub issues, milestoned by phase. This table is the index; the issues carry the
acceptance criteria.

| phase | state | issues |
|---|---|---|
| 0 — physics core | **done** — `constraint_kit/fea/`, 35 tests verified against ground truth, driver in `drivers/fea_part.py` | [#1](../../issues/1) buckling · [#2](../../issues/2) anisotropy · [#3](../../issues/3) Gmsh/CalculiX cross-check · [#4](../../issues/4) load-case envelope · [#5](../../issues/5) AMG |
| 1 — parametric source | not started; gear-cutter nearly there. **Blocked on a scope call: [#8](../../issues/8)** | [#6](../../issues/6) gear-cutter variants · [#7](../../issues/7) axis_x pattern · [#8](../../issues/8) knee-exo v9/v10 · [#9](../../issues/9) params + datums |
| 2 — kernel gaps | not started | [#10](../../issues/10) STEP import · [#11](../../issues/11) prismatic · [#12](../../issues/12) screw · [#13](../../issues/13) belt · [#14](../../issues/14) gear_rack · [#15](../../issues/15) freeform |
| 3 — closed-loop optimization | not started; needs Phase 1 for a machine to sweep | [#16](../../issues/16) build cache · [#17](../../issues/17) the optimizer |
| 4 — manufacturing constraints | pieces exist in knee-exo `411_printability.py` | [#18](../../issues/18) |
| 5 — topopt oracle | not started; `Grid.density` and SIMP interpolation already in place | [#19](../../issues/19) |
| 6 — diagnosis / repair | structure exists in `validate.py`, causes do not | [#20](../../issues/20) |
| 7 — agent surface | **MCP delivered** ([#21](../../issues/21) closed); planner not wired | [#26](../../issues/26) planner in the Describe panel · [#22](../../issues/22) retire the DSL |
| — the workbench | **built** — `workbench/`, 124 tests, installed via `install.py` | [#27](../../issues/27) Qt panel coverage · [#28](../../issues/28) Assembly joints · [#29](../../issues/29) requirements · [#30](../../issues/30) icons |
| — housekeeping | | [#23](../../issues/23) closed · [#24](../../issues/24) dependency pins |

**The workbench changed what Phase 2 means.** FreeCAD 1.0.2's Assembly solver already ships
`Slider`, `Screw`, `Belt` and `RackPinion` — the four mates [#11](../../issues/11)–[#14](../../issues/14)
were filed to implement — and `Part` already has `makeLoft`, `makePipeShell`, `makeThread` and the
rest of the op set [#15](../../issues/15) wanted. All of it is scriptable headlessly. So Phase 2
is largely wiring now rather than building, which is why the workbench was worth doing before it.

**The critical path to an optimized machine is 1 -> 3.** Everything else is either already done
(0), a prerequisite for describing the hardware at all (2), or an improvement to a loop that does
not exist yet (4, 5). Phase 1 on `axis_x` ([#7](../../issues/7)) is days of work and establishes
the pattern; `gear-cutter` ([#6](../../issues/6)) is close already. knee-exo waits on
[#8](../../issues/8).
