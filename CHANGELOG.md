# CHANGELOG

Newest first. Tags: `[feat]` `[fix]` `[test]` `[docs]` `[decision]` `[broke]` `[perf]`.

## 2026-10-17 — several load cases, and the field that bounds all of them (closes #4)

A real machine part does not have *a* load. The gantry plate carries belt tension when the drive
pulls and a reaction when it pushes; the gear cutter sees cutting force one way and clamping force
another. Analysing one of those and quoting its safety factor is a guess dressed as an answer.

- `[feat]` **`Model.load(..., case="belt")` and `Model.solve_cases()` -> `Envelope`.** Two things
  make this more than a loop over `solve()`:
  - **The matrix is assembled and factorised ONCE.** Neither depends on the load, and assembly is
    the expensive part on a big grid, so four cases cost barely more than one. Two tests measure
    it rather than assuming it: one counts `_assemble` calls, the other counts `splu` calls and
    then checks the four answers still scale linearly with their loads, which is what proves the
    shared factorisation is not stale.
  - **The envelope is an ELEMENTWISE maximum, not the worst single case.** This is the whole
    engineering point: one region of a part can be governed by the belt pull and another by the
    reaction, so the design field exists in *none* of the individual cases.
    `governing_case_per_element()` keeps that visible, and there is a test that fails if anybody
    simplifies it back to `max(case.peak for case in cases)`.
- `[test]` **Superposition holds exactly**, which is what proves the multi-RHS path is right: in
  linear elasticity the response to A+B must equal the response to A plus the response to B. That
  is a property of the physics rather than of this code, so it catches the three ways a multi-RHS
  solve goes wrong -- a mis-mapped reduced vector, a factorisation reused after the matrix
  changed, and one case leaking into another. 16 tests in `tests/test_cases.py`.
- `[feat]` **`drivers/fea_part.py --load "belt=y>=188 : 0,764,0"`** -- a `NAME=` prefix makes a
  load a case. The `=` is only read as a case name when it is not part of a comparison, so
  `y>=188` still parses as a selector. On the gantry plate with belt pull, reaction and gravity:

  ```
  3 load case(s) on one 25977-DOF matrix (direct)
    case                      defl mm    p99 MPa   peak MPa
    belt pull                 10.7126      18.41      47.03
    reaction                  10.7126      18.41      47.03
    gravity                    0.0523       0.08       0.18
    ENVELOPE                        -      18.41      47.03
    governs by element: belt pull 100%
    envelope SF 0.81 against 15 MPa allowable
  ```

  Belt pull and reaction landing on identical stress is a correctness check, not a coincidence:
  von Mises is sign-blind and the reaction is the negated belt load. Gravity at 1 N against 764 N
  is correctly negligible, which is also worth being able to see rather than assume.
- `[feat]` **The workbench `analyse` command takes `cases`** as a list of `{name, load, force}`,
  and reports per-case rows plus `governing_case` and `governs_by_element`. Refused when mixed
  with a single `load`/`force`, refused when a case is unnamed, has no selector, has a malformed
  force, or has a zero force -- a case that is silently zero reports an infinite safety factor
  for a load somebody meant to apply.
- `[fix]` **`solve()` and `solve_cases()` now share `_guard` and `_finish`**, so there is one
  precondition check and one Result-building path rather than two that can drift. `solve()` also
  now refuses a model that has named cases, instead of silently analysing none of them.
- `[fix]` A declared-but-unloaded case is refused **by name**, and mixing a named case with an
  unnamed load is refused outright: is the unnamed load part of every case, or a case of its own?
  Both readings are defensible and they give different answers, so this picks neither.

**259 tests across 14 suites.**

## 2026-10-16 (finally) — connecting the loop: parameters to performance

The physics tier was verified, cross-validated against CalculiX, and **unreachable**. `stiffness`
existed in the workbench bridge and was tested against beam theory, and it was not a registered
command — so an agent driving the workbench could insert a part, mate it, check it for
interference and export it, and could not find out whether it would hold. A closed loop from
parameters to performance cannot exist while the loop is not connected.

- `[feat]` **`analyse` is now a registered command.** Reachable over MCP, in the palette, and in
  the MCP schema with its arguments documented. One sweep an agent can now do unaided, on a
  120 x 20 PETG bracket held at one end and pushed with 60 N at the other:

  | thickness | mass g | deflection | p99 MPa | SF | linear? |
  |---|---|---|---|---|---|
  | 4 mm | 12.2 | 124.26 mm | 51.81 | 0.29 | **NO** |
  | 6 mm | 18.3 | 40.51 mm | 32.80 | 0.46 | **NO** |
  | 8 mm | 24.4 | 17.76 mm | 21.46 | 0.70 | **NO** |
  | 12 mm | 36.6 | 5.44 mm | 10.84 | **1.38** | yes |

- `[feat]` **The verdict says when linear theory has been left behind** — which the table above
  is the reason for. A 4 mm bracket reported 124 mm of deflection on a 120 mm part with exactly
  the composure of a 5 mm answer. Linear elasticity assumes rotations stay small enough that the
  geometry does not change under load; at 103% of span that is simply false, and the real part
  stiffens as it deflects so the figure *overstates* the movement. Nothing in this tier solves
  the nonlinear problem and that is fine; reporting the linear number without saying so is the
  believable-wrong-answer failure that rule 4 exists to prevent. Only the 12 mm row is a number
  anyone should quote.
- `[fix]` **The criterion is deflection over SPAN, not over thickness.** Thickness was tried
  first and is wrong: rotation is what matters, and a cantilever can move through several times
  its own thickness at a few degrees. The thickness rule flagged a valid 4.5%-of-span answer,
  which is how a warning gets trained out of a reader. A tenth of the span is about 6 degrees.
- `[feat]` **A printed part gets its interlayer verdict without being asked**, because for a part
  made of welds the isotropic answer is the wrong default.
- `[feat]` **`physics.prepare` / `physics.solve_prepared`** split the bridge along the only line
  that matters: tessellation is OCCT and must run on the GUI thread, the solve is numpy and runs
  anywhere. `stiffness` still composes both, so nothing that called it changed.
- `[feat]` **The Analyse button now analyses.** It used to print a Python snippet to paste into
  the console — right about not inferring a restraint, wrong about what to ask of a person.
  `AnalyseDialog` pre-fills the selectors from the part's own bounding box along its longest
  axis, in editable fields, beside the span they refer to. **That is a proposal, not a guess: the
  difference is whether a human had to look at it before it ran**, and here they do. The solve
  then goes to `AnalyseWorker`, whose constructor tessellates on the GUI thread and whose `run()`
  touches nothing but numpy — the same freeze the planner already had, not repeated.
- `[test]` Six new tests (four headless, two in the real GUI), including that `analyse` is
  read-only and stamped like the other checks, that the dialog's proposals are editable, and that
  `worker.start()` returns immediately rather than solving in place.
- `[fix]` An existing test earned its keep: `test_every_registered_command_is_reachable_from_the_interface`
  failed the moment `analyse` was registered without a home in the UI, which is exactly the drift
  between the two surfaces it was written to prevent.

**243 tests across 13 suites.**


## 2026-10-16 (last) — printed parts are anisotropic and von Mises does not know that (closes #2)

- `[feat]` **`fea/printed.py`** — the check an isotropic stress measure cannot express. An FDM
  wall is a stack of welded beads; across the weld the polymer is a diffusion bond formed in
  under a second against an already-cooling surface, at roughly half the in-plane strength. So
  the number that decides whether a printed part survives is the **traction on the layer plane**
  — split into the part trying to peel the layers apart and the part trying to slide them over
  one another — and neither is recoverable from an invariant.
- `[feat]` **Both tiers now keep the full stress tensor.** `voxel.Result.stress` (element
  centres) and `tet.TetResult.stress` (nodes), as `(sxx, syy, szz, sxy, syz, szx)`. Both were
  computing it on the way to von Mises and throwing it away, which made the anisotropic question
  unanswerable for no saving at all. von Mises is now *derived* from the tensor, so the two
  cannot drift apart, and a test asserts it is recomputable from it.
- `[feat]` **`drivers/fea_part.py --layers x|y|z|0,0,1|all`.** `all` ranks the three printable
  orientations and names the one to use.

### The finding: print orientation is a structural decision on the gantry plate

Same part, same 764 N, three ways up. Both tiers, independently:

| layers normal to | peel SF (voxel / ref) | shear SF | governing |
|---|---|---|---|
| **Z** — printed flat | 2.15 / 1.87 | 2.93 / 1.63 | **2.15 / 1.63** |
| X — on its side | 1.86 / 1.41 | 2.53 / 1.49 | 1.86 / 1.41 |
| **Y** — on its end | **0.76 / 0.74** | 2.38 / 1.38 | **0.76 / 0.74** |

- `[decision]` **Printed flat, the welds are not the problem** — the in-plane von Mises verdict
  (SF 1.06) governs. **Printed on its end, the belt tension pulls straight across every weld and
  the part delaminates at three quarters of its design load.** The two tiers agree on that worst
  case to 3% (0.76 against 0.74), from completely different stress fields.
- `[note]` **The de-rate factors are an assumption and are carried with the answer.** 0.50
  tensile and 0.45 shear of in-plane: literature-typical for unannealed FDM, deliberately at the
  conservative end of the published 0.45–0.60 spread, and *not* measured for this filament,
  nozzle temperature, layer height or printer. `InterlayerCheck` stores the factors it used and
  `report()` says they are assumed, because a safety factor computed from assumed properties
  that travels without them gets quoted as though it were measured.
- `[note]` **Compression across layers is not peeling**, so only tensile normal traction counts.
  A weld in compression is as strong as the bulk; treating it as a peel load would condemn parts
  that are entirely fine. There is a test, and the report says so when it applies.
- `[fix]` An interlayer check is **refused on `al6061` and `steel`** rather than computed. They
  are isotropic to any accuracy that matters here and the number would mean nothing.
- `[fix]` **Console reports are now ASCII, with a test.** An em-dash in `--layers all` output
  arrived through a cp1252 code page as a replacement character, so "in the best orientation `?`
  print orientation is a structural decision" read like an encoding failure in the analysis
  rather than in the prose. Prose belongs in docstrings, which nobody pipes through a code page.
- `[fix]` Two defects in the day-old `tet.py`: an explicit `workdir` was not created (so the
  auditable path, the one that leaves the deck and the `.frd` behind, failed outright), and
  `traction_on_plane` was duplicated there rather than imported — it is generic stress algebra
  with nothing CalculiX about it, and the voxel tier needs it too.
- `[note]` One of my own test bugs worth recording: I asserted `SXX > 5 x SYY` on a cantilever
  and measured 2.4x. The code was right and the expectation was wrong — a clamped end is a real
  3-D restraint, and holding a face against its own Poisson contraction generates a transverse
  stress (28 MPa against 68) that beam theory has no way to represent.

**222 tests across 12 suites** in the default run, 241 with the GUI and live tiers.


## 2026-10-16 (later again) — a second solver that shares no code with ours (closes #3)

- `[feat]` **`fea/tet.py`** — an independent reference solver: Gmsh quadratic tetrahedra through
  CalculiX. **Both ship inside FreeCAD 1.0** at `<FreeCAD>/bin/{gmsh,ccx}.exe`, so this cost no
  new dependency at all; `CK_GMSH` / `CK_CCX` override, and a missing toolchain raises rather
  than skipping, because a cross-check that quietly does not run reads as agreement.
- `[feat]` **`fea/crosscheck.py`** — measures the gap and *attributes* it. Independent in every
  way that matters: a different element family (C3D10 against our trilinear hexes), a different
  mesh topology (conforming against staircased), a different assembly, a different linear solver
  (SPOOLES against our CG/SuperLU). It reports rather than asserts, because when two solvers
  disagree the useful output is the size and the direction of the gap.
- `[feat]` **`drivers/fea_part.py --reference`** — one command, both solvers, formal comparison.
- `[test]` **13 new tests**, in the order that matters: the reference is validated against closed
  form *first* (1.0028× `PL³/3EI` on deflection, 0.935× `Mc/I` on p99 stress), and only then
  used to judge anything. Plus `voxel.Result.applied_force`, so the comparison can tell a solver
  disagreement apart from two solvers being handed different loads — the check that would have
  caught the lost-load bug in one line instead of three days.

### A safety-relevant systematic, found and then quantified

- `[fix]` **Voxel bending stress is optimistic by `pitch/h`, and it is a ceiling rather than an
  error.** Stress is evaluated at element *centres*; in bending it peaks at the *surface*. The
  largest value a grid can observe is therefore the stress at `h/2 − pitch/2`, i.e. `1 − pitch/h`
  of the extreme fibre. Measured against CalculiX on a 10 mm beam: **0.447 / 0.740 / 0.863** at
  pitch 5 / 2.5 / 1.25, against a prediction of 0.500 / 0.750 / 0.875. Four elements through a
  thickness still over-states the safety factor by a quarter. Refinement improves it; nothing
  else does. Now a test, and a gotcha in `AGENTS.md`.

### The gantry plate, converged — and the third safety factor for it

- `[feat]` The question `PLAN.md` said needed this exact tool now has an answer. Body-fitted,
  same geometry (STL → solid → STEP, volume preserved to 0.2 mm³ of 79,967), same BCs:

  | | 4.00 mm | 2.80 mm | 2.00 mm |
  |---|---|---|---|
  | nodes / C3D10 | 104,695 / 57,367 | 148,883 / 83,968 | 228,143 / 133,876 |
  | deflection, mm | 4.477 | 4.647 | 4.572 |
  | p99 stress, MPa | 12.98 | 14.10 | 14.10 |
  | safety factor | 1.16 | 1.06 | **1.06** |

  **It converges**, where the voxel ladder did not. And the two tiers agree on stress to 0.5%:
  the voxel run at 1.30 mm pitch reads p99 **14.02 MPa** against the reference **14.10**.
- `[decision]` **Three published safety factors for one part, so each change is on the record.**
  SF 2.9 was *wrong* (`Model.load` lost three quarters of the force to padding). SF 0.75 was
  *not converged* (a staircase through a 3.45 mm wall, reported as a lower bound at the time and
  flagged as needing this analysis). **SF 1.06 is converged, body-fitted and cross-validated.**
- `[decision]` **The verdict on the part is unchanged; the reason is sharper.** It is not 25%
  overstressed. It has *no stress margin* — SF 1.06 on a creep-aware allowable under sustained
  belt tension in a printed part — and it gives **4.6 mm** in series with the knee drive. The
  compliance was always the real finding and is now a converged number rather than a floor.
- `[fix]` **And the cross-check diagnosed the voxel tier rather than merely disagreeing with it.**
  Voxel deflection read 83% of the reference at 1.82 mm pitch and 121% at 1.30 mm — straddling
  it, which no discretisation story explains, since a locking element does not become compliant.
  The mass column says why: **+10.7% material at 1.82 mm, −1.3% at 1.30 mm.** The staircase
  thickens some walls and thins others, and with bending compliance going as `t⁻³`, one lattice
  layer off a 2.7-element wall moves local compliance by **4×**. That is a *sampling* failure,
  not a convergence failure — refining re-rolls the dice rather than fixing it. Which is the
  honest reason this phase always assumed a second solver for the final figure.
- `[note]` Two traps worth keeping. **Gmsh native `.msh` node order for a 10-node tet is not
  CalculiX order** — the mid-edge nodes on the fourth vertex are swapped, and the symptom is
  `*ERROR in e_c3d: nonpositive jacobian determinant`, not a wrong answer. Rather than
  hand-permute, `tet.py` asks Gmsh for its own Abaqus output and reads the element block from
  that; and there is a test that a scrambled ordering still *fails* rather than being absorbed.
  **The `.frd` parser must be fixed-width** (3 / 10 / 12 characters): splitting on whitespace
  works until a six-figure node id meets a negative exponent and two fields run together.

**235 tests across 13 suites.**


Read this first every session; write it last. Log **decisions**, not just code — a decision that
is not written down gets re-litigated, and the reason it was made is the expensive part.

## 2026-10-16 (later still) — the planner works against a real model, and the model found a hole in the catalogue

- `[feat]` **`workbench/tests/test_planner_live.py`** + a `CK_LIVE=1` CI tier: plain English through
  the configured endpoint, the validator, the repair loop and `build()`. Four tests, all green
  against `deepseek-flash` in 1.6-9.6 s each. This closes #26 with evidence rather than with a
  wiring diagram: the panel had been wired for a while, but no live completion had ever been
  observed to return.
- `[fix]` **The dead completions were the provider, not us.** Earlier the endpoint answered
  `/v1/models` in 0.5 s and never answered a chat completion at all; it now answers the real
  4,045-character planner prompt in 4.5 s on the as-shipped configuration. Nothing in the client
  needed changing for that, which is worth recording so the next outage is not re-debugged as a
  bug.
- `[fix]` **An answer lost to reasoning is now diagnosed where the cause is knowable.** A reasoning
  model can return HTTP 200 with `content: ""` and the answer in `reasoning_content` —
  measured: `finish_reason: "length"`, 70 characters of reasoning, empty answer, because thinking
  consumed the whole output budget. `llm.chat` returned `""`, and the planner then reported "the
  model did not return valid JSON", which is true of an empty string and tells nobody anything.
  It now names the actual fault and what to do about it, with a different message for an empty
  answer carrying no reasoning at all (a model-name problem, not a thinking problem).

### `plate` could not put a hole in the middle of a plate

- `[feat]` **`plate` takes a `bore_d`**, a single centred through-bore, independent of the bolt
  circle; a plate can have both, which is the commonest plate there is. New frames `bore` and
  `bore_top`. Four tests, written first, including the two refusals: a bore that is not smaller
  than the plate, and a bolt circle that would run into the bore and merge with it into one ragged
  opening.
- `[note]` **The gap was found by the model, not by me.** Asked for "a plate 120 by 80 by 10 with a
  20mm bore through the middle", it tried a bolt circle of radius zero, was refused by the
  validator, and the repair came back with: *"No generator in this catalogue can put a central
  through-bore in a flat plate... that feature was dropped rather than faked."* It was right on
  every count, including about `block_with_bore` being the wrong substitute.

  Two things in that are worth keeping. The execute-and-repair loop did what it is for, on a real
  gap rather than a syntax slip. And **a live model exercising the catalogue finds holes a unit
  test will not, because a unit test only asks for what its author already thought of.** That is
  now written into `AGENTS.md` as the reason the live tier exists.
- `[feat]` With `bore_d` in place the same model produces, first try and with every value stated, a
  160 x 120 x 10 gantry plate with a 25 mm bearing bore and four holes on a 100 mm bolt circle —
  and volunteers that 5.0 mm is M5 tapping size rather than clearance, which is a correct and
  unprompted observation about the part.
- `[note]` Forward-compatible by construction: `paramset_of` reads each part's stored provenance
  metadata rather than the live catalogue, so a document saved before `bore_d` existed rebuilds
  byte-identically.

**209 tests across 12 suites** (five tiers), plus 113 in the container.

## 2026-10-16 (later) — coupled motion, and checking a mechanism through its travel

- `[feat]` **`ckwb/motion.py`**: slides, screw travel, belt arithmetic, pulley rotation, and
  `sweep` — a registered command that drives one part along a travel and checks interference at
  every station.
- `[feat]` **The sweep is the part that earns its keep.** A static clash check at either end of a
  travel passes a mechanism that collides in the middle, and the test for it is built exactly
  that way: a carriage on a shaft with a post beside it partway along. The static check passes —
  that it passes is the point — and the sweep reports which stations collide and with what.
  Declared read-only and keeps the promise: the moving part is restored in a `finally`, because a
  check that quietly leaves the model somewhere else is a side effect wearing a verdict as a
  disguise.
- `[feat]` Closed-form arithmetic in one place, because this is what goes wrong by hand and then
  goes wrong in the BOM: `pitch_diameter`, `belt_ratio`, `belt_length` (the exact two-pulley wrap,
  not the `2C + 1.57(D1+D2)` approximation), `belt_centre_distance` by bisection — the number you
  actually need, since you buy a 93-tooth belt and then have to place the pulleys —
  `screw_travel`, `screw_turns`, and `steps_per_mm`, which reproduces the measured 320 steps/mm
  for a 1605 at 1600 steps/rev and 160 for a 1610. The lead is carried, never inferred from a
  part name.

### Why not FreeCAD's Assembly solver — and a correction

I claimed on #11-#14, #28 and in PLAN.md that FreeCAD 1.0.2's Assembly solver is *"fully
scriptable headlessly"*. **That was too strong.** What probing actually established:

- `Assembly::AssemblyObject` creates headlessly and exposes `solve()` / `undoSolve()`. True.
- A `Slider` / `Screw` / `Belt` / `RackPinion` joint proxy **does** construct headlessly — but only
  once the joint object is already inside the assembly, since `getAssembly()` walks `InList`. And
  `JointObject.Joint(obj, index)` takes an **integer index** into `JointObject.JointTypes`, not a
  type name.
- Grounding through `GroundedJoint` works; `isPartGrounded` returns True.
- `Placement1` / `Placement2` / `Offset1` / `Offset2` are plain `App::PropertyPlacement`, so a
  joint **need not name "Face3"** — the topological-naming problem is avoidable, which was the
  main worry.
- **But `isPartConnected` stays False and driving the joint moves nothing.** The solver wants its
  `Reference` properties in a path form that is undocumented and that I did not crack.

- `[decision]` So: closed-form placements rather than the solver, for now. Every motion this
  hardware has is closed form — a nut at a travel, a carriage at a position, a pulley at an angle,
  a belt length for a centre distance. A numerical solver would return an approximation to what
  arithmetic gives exactly, and bring an iteration count and a convergence failure along with it.
  What that forgoes is real and worth naming: **mechanism DOF analysis and loop closure.** Nothing
  on this bench needs it yet, and `constraint_kit/linkage.py` already does four-bars through
  SolveSpace when it does.
- `[fix]` **A design gap the sweep test found: a slot's outward normal is not its travel axis.**
  `extrusion_2020` offered only outward slot normals, so sliding a carriage "along the rail"
  pushed it sideways off the face. The rail now has its own `axis` frame. Seating and travelling
  are different questions and need different frames.
- `[fix]` Three of my own test bugs, each worth the note: a belt-growth assertion that demanded
  more than the physics allows (doubling the centre distance adds slightly *less* than two
  straight runs, because the wrap angles shrink and give some back); `Shape.BoundBox` on something
  that was already a Shape; and `CenterOfMass` on a `Part.Compound`, which FreeCAD 1.0 does not
  expose — the bounding-box centre is exact for that check anyway, since 90 degrees is six whole
  tooth pitches on a 24-tooth gear.

**214 tests across 11 suites.**

## 2026-10-16 — the kernel can see geometry it did not make (closes #10)

Until now it could only reason about shapes it generated, which ruled out the most useful
geometry there is: a vendor STEP for a bearing, a profile, a motor. An approximation is fine for
mass and envelope and wrong for anything that has to fit.

- `[feat]` **`import_step`** reads STEP/IGES/BREP into the document, as a command so an agent can
  call it too. `split=True` makes one object per solid for a vendor assembly; left off, the file
  is kept exactly as supplied.
- `[decision]` **The real question was not "can we read a file" but "what IS an imported part".**
  It has no generator and no parameters, so it cannot be parametric; it has a shape, so it must
  be checked, weighed, analysed and exported like anything else. Hence two collections:
  `ck_parts()` stays parametric (it is what the params file can describe) and **`all_parts()` is
  everything with geometry**, which is now what checks, mass, export and the tree use. Getting
  that split wrong in the other direction would let a bought component escape clash detection —
  the single failure that makes a verdict worthless. There is a test for exactly that.
- `[decision]` **Derived frames are named `bbox_top`, never `top`.** A vendor STEP carries no
  statement about which face is a mounting face. Inventing one would be precisely the plausible
  lie this project exists to avoid, so the bounding box is offered under names that admit what
  they are. An import can still be seated on a generated part by `bbox_bottom`, with the exact
  zero gap the mate kernel always gives.
- `[decision]` **The one-solid rule now applies only to generated parts.** It is a correctness
  invariant there — a boolean that severs a part returns a *valid* shape with two solids rather
  than an error, and that has slipped downstream twice in this project. A vendor assembly
  legitimately holds many, and flagging it would train people to ignore the check, so those are
  reported under `multi_solid_imports` as information.
- `[feat]` **The payoff: the physics tier analyses imported geometry.** A 120x12x12 beam
  exported to STEP, re-imported and solved gives 0.9521 mm against 0.9676 mm of beam theory —
  98%, identical to the generated case. So a bought bearing or extrusion can now be structurally
  checked instead of stood in for by my approximation of it.
- `[test]` 14 tests: volume-preserving round trip through a real file written by the suite
  itself (no fixture to rot), the source path recorded on the object, refusal of a missing file
  and of a file with no solids, whole-versus-split, membership in the right collections, clash
  detection against a generated part, mass, the solids nuance, tree reporting, export, the
  derived frames, seating by one, and the registry command.
- `[broke]` Known: the provenance panel shows nothing for an imported part, since it has no
  parameters to show. Correct but unhelpful; it should show the source file instead.

**197 tests across 10 suites.**

## 2026-10-15 (night) — the untestable layer is now tested

Closes the gap that produced the day's two worst bugs. Both shipped while 150 headless tests
passed, because `FreeCADGui` cannot be imported without a display: `InitGui.py` raising on
`__file__` so the workbench never appeared in the selector, and nine named pixmaps with no files.

- `[test]` **A fourth CI tier that drives a real FreeCAD window.**
  `workbench/tests/run_gui_tests.py` launches the application, runs `test_gui.py` inside it, and
  reports; 15 tests in about 30 s, then it quits itself. Opt-in via `CK_GUI=1` only because it
  opens a window — not because it is optional.
- `[test]` Covers what had no coverage at all: the workbench registering and activating, the
  toolbar's eight buttons with **every icon asserted non-null**, the menu, `IsActive` greying out
  honestly with no document, the checks panel moving through *not-run → pass → stale*, a clash
  rendering as a child row, the provenance panel grouping by origin and accepting a guess, the
  settings dialog masking the key, and the Report view logging nothing.
- `[test]` **The Describe panel's worker-thread path, with the model faked.** Propose, show,
  approve, build; the repair round trip; a decline surfaced calmly with nothing built; and a
  client failure reported as a sentence. No network, no key, no non-determinism.
- `[decision]` **The settings lookup is patched, never written.** A GUI test that edited
  `user.cfg` would overwrite a real endpoint and API key.
- `[fix]` **The responsiveness test was weak and is now verified by mutation.** It originally
  counted event-loop spins *after* `submit()` returned, which proves nothing — a blocking
  `submit()` has finished blocking by then. It now times `submit()` itself, and both directions
  were checked: making `ChatWorker.start()` synchronous fails exactly one test, with
  *"submit() took 2.30s with a 2s model; it is blocking the GUI thread"*. A test that cannot fail
  is worth nothing, so the ones that matter get mutated.
- `[docs]` AGENTS.md gains the tier and the rule behind it: write GUI assertions that can fail,
  and run this before calling any GUI change done.

**169 tests across 9 suites.**

## 2026-10-15 (evening) — the planner stopped freezing FreeCAD

A key was configured and the first real call went out. It never came back, and finding out why
turned up two defects of mine plus one thing that is not mine.

- `[broke]` **The planner call ran on FreeCAD's GUI thread.** A slow model froze the entire
  application for as long as the request took — originally up to 180 s. The instance stopped
  answering `execute_code` and had to be restarted. An unresponsive CAD application is not a
  cosmetic problem: the user cannot even see what it is waiting for.
- `[fix]` **The split follows what is actually thread-safe, not what is convenient.** The obvious
  fix — run `plan()` in a worker — is wrong, because validation *builds the proposed parts* to
  check their frames and driving OCCT off the GUI thread is unsafe. So `PlanSession` now owns the
  fast, thread-bound half (prompting, parsing, validating, building) and `gui/worker.py` owns the
  slow, thread-safe half (one HTTP call). `plan()` drives the session on one thread; the panel
  drives the same object across a worker and a signal — one implementation, so the interactive
  and scripted paths cannot diverge. The repair loop needs no special handling: it is a second
  trip through the same pair.
- `[feat]` A **Stop waiting** button, which says plainly that it cannot interrupt a socket read
  already in flight — the request finishes in the background and its answer is discarded.
  Pretending to cancel would be worse than saying so.
- `[fix]` **`response_format: json_object` is only sent when the prompt contains "json".**
  DeepSeek returns HTTP 400 otherwise: *"Prompt must contain the word 'json' in some form"*. The
  planner's own prompt happens to satisfy it, which is luck rather than design, so it is now
  checked — and a test asserts the SYSTEM prompt keeps the word, since losing it would silently
  stop JSON mode being requested.
- `[fix]` **`DEFAULT_TIMEOUT` 180 s → 60 s, and the docstring now says what it means.** urllib's
  timeout is per socket operation, not a total deadline, so a server that accepts a connection
  and then sends nothing stalls for that long *per read*. 180 s was indistinguishable from a
  hang. A timeout now reports that the endpoint is reachable and the problem is not the URL or
  the key, because those three failures look identical from the call site and need different
  fixes.
- `[test]` 4 client tests in the host suite (`llm.py` imports no FreeCAD): the json-mode guard,
  the SYSTEM prompt keeping its json mention, refusal of an unconfigured endpoint before any
  network call, and the timeout staying short enough to notice.
- `[broke]` **Still no successful live completion, and it is not this code.**
  `https://api.deepseek.com/v1/models` answers in 0.5 s and lists both `deepseek-flash` and
  `deepseek-v4-pro`; `/v1/chat/completions` validates requests (a malformed one returned HTTP 400
  in 0.4 s) — but a *valid* completion returns nothing, on both models, bounded to 16 output
  tokens, timing out at 55 s. Route reachable, key accepted, request validated, no response. That
  points at the provider side: status, quota or balance.

## 2026-10-15 (later still) — the planner, and an SFU1605

Prompted by a user typing "a 500mm sfu1605 ball screw assembly" into the Describe panel and
being told the planner was not connected. Two separate gaps behind that, and neither was the
panel's fault.

- `[feat]` **`ckwb/parts.py`: `ball_screw` and `ball_nut`.** The request was unservable because
  the catalogue had no ball screw — a wired planner would have had to decline it. Defaults are
  SFU1605 from the measured hardware: 16 mm nominal, **5 mm lead**, turned-down ends for BK/BF
  supports, flanged single nut with its M6 bolt circle. `ball_screw_lead()` exists so nobody
  infers the lead from the name — SFU16**05** and SFU16**10** share a diameter and differ by 2x,
  and on this hardware the lead is what makes 1600 steps/rev into 320 steps/mm.
- `[decision]` The thread groove is **not** modelled. A 500 mm screw at 5 mm lead is a hundred
  turns of swept helix, costly geometry that no assembly check uses; the envelope, the mass and
  the end journals are what a bearing block and a clash check care about. Said at the definition
  rather than discovered later.
- `[decision]` The nut bore is modelled at the screw's **nominal** diameter. The balls carry the
  load, not the bore, so a nut that touched the screw would be wrong — and modelling it at
  nominal keeps a clash against the nut bore meaningful rather than reporting the running fit.
- `[feat]` **`ckwb/planner.py` + `ckwb/llm.py`: plain English to a reviewable program.** The
  model chooses parts, values and mates; the kernel builds. `plan()` returns a `Plan` a human
  reads, and `build()` is a separate explicit step, so nothing exists until Build is pressed.
- `[feat]` **Validated before it can build.** An unknown generator, parameter or frame is refused
  with a message naming what exists. Frames are checked **by building the proposed part**, so the
  check cannot drift from the geometry the way a maintained list would.
- `[decision]` **One repair attempt, not a loop.** A refusal is handed back once with the exact
  error, per the execute-and-repair pattern 2026 text-to-CAD work converged on. A model that
  cannot fix its program given the precise error will not fix it on the fifth try, and an
  unbounded loop spends money proving that.
- `[decision]` **A decline is never retried.** Retrying an honest refusal is how a refusal gets
  talked out of, which would destroy the one property that makes declining worth having.
- `[decision]` **The planner cannot launder its guesses.** Values the request actually stated
  become `stated`; values the model chose become `default` and show up as guesses in the property
  editor. The model is asked to mark each one and is told plainly not to claim the user's
  authority for its own choices.
- `[feat]` `ckwb/llm.py` uses `urllib` and nothing else — adding `requests` to a CAD application
  to make one POST is a poor trade, and FreeCAD's interpreter has no access to the host's
  site-packages anyway. Works against DeepSeek, OpenAI, OpenRouter, or a local vLLM / llama.cpp /
  Ollama / LM Studio, which is the case worth optimising for because it needs no key.
- `[feat]` `ckwb/settings.py` reads the endpoint from FreeCAD's parameter store and falls back to
  the environment (`LLM_BASE_URL`, `DEEPSEEK_API_KEY`, `MODEL_PLANNER` and friends), so a
  headless session or an agent runs without anyone opening a dialog.
- `[test]` 15 planner tests with the model call **injected**: no network, no key, no
  non-determinism. They cover the happy path, provenance mapping, declines, the single repair,
  unknown generators/params/frames, mate ordering, malformed JSON, fenced JSON, and building.
- `[broke]` The orphan guard earned its place immediately: registering `plan` and `describe`
  failed `test_every_registered_command_is_reachable_from_the_interface` until both were declared
  reachable. That is the test preventing exactly the GUI/agent drift it was written for.
- `[broke]` **Still unverified: a live model call.** There is no API key configured anywhere and
  no local server running on this machine, so `llm.py` has never made a real request. Everything
  above the network is tested; the HTTP call itself is not.

## 2026-10-15 (later) — the workbench did not load, and then its console was noisy

- `[broke]` **ConstraintKit never appeared in FreeCAD's workbench selector.** `InitGui.py`
  computed `os.path.abspath(__file__)` at module level, outside its try/except; `__file__` is
  undefined when FreeCAD execs these files, so it raised `NameError`, FreeCAD logged one line and
  carried on, and the workbench silently did not register.
- `[decision]` This trap was found earlier the same day, written into AGENTS.md, and fixed in
  `Init.py` — then left in `InitGui.py` two files away. It survived because `FreeCADGui` cannot
  be imported headlessly, so none of the then-124 tests could reach it. **Everything testable was
  tested; the one untestable thing was broken.** The lesson is not "write more tests" — it is that
  a layer no test can reach has to be exercised by hand before it is called done.
- `[fix]` `InitGui.py` now finds its own directory by searching `sys.path` for its package, and
  its entire module body is one `try`, so a failure is reported rather than removing the
  workbench.
- `[test]` Three guards, parsed with `ast` rather than grepped: the entry points may not reference
  `__file__`, `InitGui.py`'s module body must be exactly one `Try`, and `gui/spec.py` must stay
  free of Qt so it remains testable. (The first draft grepped and failed against correct code,
  because both files document the trap in prose.)
- `[fix]` **Nine `Cannot find icon` warnings on every startup.** The toolbar named pixmaps that
  did not exist. Not errors — but a console full of warnings is how real errors get overlooked,
  and blank buttons read as a broken install. `workbench/tools/make_icons.py` generates the nine
  SVGs from one declaration so the set stays consistent, and `InitGui.py` registers the directory
  **before** `addWorkbench`, since the workbench's own icon resolves at registration time.
- `[decision]` Icon strokes are a mid-tone blue (#4F8DBF) that holds contrast on FreeCAD's light
  *and* dark themes, with amber reserved for Describe — the one action that creates something.
  Picking a stroke against one theme only is how addon icons vanish for half their users.
- `[fix]` Registry entries for killed processes accumulated in `~/.ck/instances` (seven had piled
  up from test runs). A server now prunes unreachable entries when it starts. Liveness is decided
  by connect-probing, never by `os.kill(pid, 0)` — on Windows Python maps that to
  TerminateProcess, so the liveness check would kill the instance it was asking about.
- `[feat]` Verified in the real GUI, which is what should have happened before any of this was
  called done: the workbench registers, activates, builds its toolbar with 8 icons and a menu,
  opens three docks, and the Report view is clean apart from the FreeCADMCP addon's own lines.
  Six of eight buttons correctly sit disabled with no document open.

## 2026-10-15 — a FreeCAD workbench, and a load bug that mattered

### The workbench (`workbench/`)

- `[feat]` **A FreeCAD workbench built on FreeCAD's own `Part` kernel.** `python
  workbench/install.py` links it into the Mod directory. One process, millisecond rebuilds, fully
  headless-capable.
- `[decision]` **Not cadquery, and not an out-of-process service.** FreeCAD 1.0.2 bundles OCCT
  7.8.1; this stack runs OCP 7.9.3. Two OCCT builds cannot share a process, so importing cadquery
  into FreeCAD is impossible and a service would mean marshalling STEP on every parameter edit.
  Built on `Part` instead, a property change recomputes in milliseconds. Probed first: FreeCAD
  ships numpy 1.26.4 and scipy 1.16.0, which is why the physics tier runs in-process unchanged.
- `[decision]` **The `.FCStd` is a cache; `<Document>.params.json` is the source of truth.** This
  is the discipline that keeps the workbench from re-creating the problem `PLAN.md` exists to
  solve. Round-trip and byte-stability are tested as invariants, not assumed.
- `[feat]` **Provenance rides on the property group**, so FreeCAD's own property editor IS the
  provenance inspector — *You specified* / *Guessed for you* / *From a standard* / *Derived*, with
  a `4 of 11 guessed` header and an explicit "accept this guess" action. No custom widget.
- `[feat]` **One command registry for the toolbar, the palette and agents.** `ckwb/registry.py`
  validates arguments, refuses unexpected ones, wraps every write in a single undo transaction
  and aborts it on any exception, and generates the MCP tool list from itself. A test asserts
  every toolbar button is backed by a registered command *and* that no command is unreachable
  from the UI — so the two interfaces cannot drift.
- `[feat]` **Eight toolbar entries, and that is the whole toolbar** (`ckwb/gui/spec.py`, declared
  as data so it can be tested headlessly). The rest of the vocabulary is in the command palette.
- `[feat]` **Part generators on `Part`**: plate with a bolt circle, spacer, bored block, shaft,
  **involute spur gear**, 20x20 V-slot section, deep-groove bearing, ISO 4762 socket screw. Each
  returns named mate frames, which is how a mate survives a parameter change instead of breaking
  on a renamed face — the topological-naming problem sidestepped rather than fought.
- `[feat]` **Mate kernel**: `seat` (faces in contact, gap exactly 0) and `align` (shared axis),
  computed from frames rather than solved, so the same inputs always give the same placement. It
  refuses an unplaced target, a double placement, and an unknown frame — and names the real
  frames when it does.
- `[feat]` **Checks with a fingerprint, so `stale` is a first-class state.** A stored verdict
  carries a hash of the geometry it was computed against, including placements — moving a part
  changes interference without changing any volume, so volume alone would miss it.
- `[feat]` **Agent access**: MCP-shaped JSON-RPC on loopback, plus `workbench/mcp_bridge.py`
  (stdlib only) speaking MCP on stdio for Claude Code, Codex and opencode. `claude mcp add
  freecad -- python workbench/mcp_bridge.py`.
- `[decision]` **Discovery never assumes a port**, for a reason this project paid for: instances
  publish `~/.ck/instances/<pid>.json`, the port is found by connect-probing *before* binding, and
  `allow_reuse_address` is off on Windows. Two servers are tested not to land on the same port.
- `[test]` **124 tests across three tiers**, all green, wired into `tools/ci.sh` which reports a
  tier it could not run as SKIPPED rather than passed. Host 59 (physics 21, convergence 16,
  wb core 22) · FreeCAD 61 (geometry 21, document 16, commands 24) · cross-process 4.
- `[test]` The agent path is tested **end to end across two processes and two Python versions** —
  host 3.13 through the stdio bridge and a socket into FreeCAD's 3.11, building a 30-tooth gear
  and reading its provenance back. Nothing mocked, because the boundary is what breaks.
- `[broke]` **The Qt panels are not covered by any test.** The toolbar spec is tested and every
  GUI module is compile-checked, but the dock widgets and dialogs need a human to click.

### The load bug

- `[fix]` **`Model.load` was silently losing force to empty nodes, and this invalidated the one
  real result published yesterday.** A voxel grid is padded, so a selector like `x >= 59` catches
  empty nodes beyond the part; `load` divided the total force across all selected nodes, and the
  share landing on empty ones was dropped from the reduced system. Found by the workbench bridge,
  where a cantilever read **0.288 mm against 0.968 mm of beam theory** — 200 N had arrived as
  60.5 N. The solve converged to a residual of 9e-10 the whole time. Converged, plausible and
  wrong by a factor of three is the worst failure this code can produce. Force is now divided
  over material nodes only, and a restraint that holds nothing but padding raises.
- `[test]` Two regression tests, one proving a padded grid and a bare grid now agree to within
  2%, one proving a load with no material under it raises.
- `[decision]` The lesson generalises past this bug: **when a selector feeds a physical quantity,
  check what it actually caught.** The selector language was doing exactly what it was asked.
- `[broke]` **The gantry-plate result published yesterday was wrong, and the corrected answer
  reverses its conclusion.** Re-run with the load fix, same load case, pitches 1.82 / 1.30 / 0.93 mm:

  | pitch mm | mass g | deflection mm | p99 MPa | SF |
  |---|---|---|---|---|
  | 1.82 | 112.4 | 2.832 | 13.89 | 1.08 |
  | 1.30 | 100.2 | 3.463 | 16.32 | 0.92 |
  | 0.93 | 101.6 | 4.558 | 19.99 | **0.75** |

  Yesterday's figures (deflection 1.17 mm, p99 5.25 MPa, SF 2.9) were produced with roughly a
  quarter of the intended force, because the load selector caught the empty padding around the
  part. **Retract "stress is comfortable".** p99 exceeds the 15 MPa creep-aware PETG allowable at
  every mesh below 1.8 mm, and the driver's own failure line now fires.
- `[decision]` **Independent corroboration, which is the first real evidence this tier earns its
  keep.** knee-exo's own BOM already concluded, on sourcing grounds, that the ball nut needed the
  bought aluminium DSG16H housing because an aluminium face "does not creep under 764 N the way a
  printed pocket does" — and that `P3_GantryPlate_Printed` has to be redrawn around it. The FEA
  reaches the same verdict from geometry and a material allowable, with no knowledge of that
  decision. Two independent methods agreeing is worth more than either alone.
- `[broke]` Still not converged, and now adversely: deflection increments GROW (0.63 then 1.09
  mm), so 4.56 mm is a lower bound and the trend is the wrong way. The caveats stand — the
  boundary conditions are an assumption, not from the repo, and the analysed STL is a design the
  repo has already superseded. A converged number needs a body-fitted mesh (issue #3).

## 2026-10-15 — the docs now match the code

- `[docs]` **README was stale and is corrected.** It claimed "113 regression tests" in two places
  (now 148), had **zero mention** of the physics tier, and its project-structure block omitted
  `fea/` entirely. Added a physics section in the README's own register — including the honest
  non-result, because a tool that declines is the thing worth showing — plus a two-tier
  `Running tests` section and a Documentation table pointing at PLAN / CLAUDE / AGENTS / CHANGELOG
  / ROADMAP / BACKLOG.
- `[fix]` `ROADMAP.md` claimed **57 green tests**, which was stale before any of this work started.
  Now 148, with the physics tier listed among what exists, and a header note saying `PLAN.md`
  supersedes it on ordering so a reader is not misled by a plan that is no longer the plan.
- `[fix]` **`BACKLOG.md`'s done-list corrected (closes #23).** It listed T4.1 CG+inertia, T7.3 BOM
  export, T1.5 parametric housing and T6.1 library loader as "suggested next" long after they were
  finished — `test_mass_properties_cg_and_inertia`, `test_bom_document`, `test_housing_part`,
  `test_library_loader` and `test_build_cache` all exist and were re-verified passing before this
  edit. Also records what is in **no** epic: STEP/IGES import (#10).
- `[decision]` Doc ownership is now explicit, because four planning documents disagreeing is worse
  than one being incomplete: **PLAN.md owns ordering, issues own the live queue, BACKLOG.md is the
  epic map, ROADMAP.md is the original thesis.** Each says so in its own header.

## 2026-10-14 — physics tier (PLAN.md Phase 0)

- `[feat]` **`constraint_kit/fea/`: linear elastostatics on voxel grids, numpy + scipy only.**
  No CAD stack, no solver install, no meshing step — so it runs on the bare host, inside
  FreeCAD's bundled Python, and hundreds of times inside an optimizer loop without a mesher that
  can fail. `hex8.py` (trilinear element by Gauss quadrature), `voxel.py` (grid, chunked reduced
  assembly, solve, von Mises), `sample.py` (triangle soup → occupancy by ray parity, with a
  dependency-free binary/ASCII STL reader and an OCC tessellation path).
- `[decision]` **Voxels, not body-fitted tets.** A structured grid cannot fail to mesh, every
  element shares one stiffness matrix, and the occupancy array is already the representation a
  topology optimizer wants. Cost is a stair-stepped boundary, so stress is reported at element
  centres and `von_mises_percentile(99)` is the figure to quote, never the absolute peak.
- `[decision]` **Element stiffness by quadrature, not a pasted 24×24 table.** A table is
  verifiable only against the source it was copied from; quadrature is verifiable against
  algebra, and the suite does exactly that.
- `[test]` **19 verification tests, all against external ground truth.** Element symmetry;
  exactly 6 rigid-body modes (catches spurious/hourglass modes and every sign error in B or D);
  uniform-strain patch test exact; axial extension exact to `PL/AE` (rel. err < 1e-9); von Mises
  exact to `P/A`; bending **convergent to 95.7 / 97.7 / 98.5 %** of Timoshenko at 4 / 6 / 8
  elements through thickness. Bending asserts the *trend*, not a tolerance — a loose tolerance on
  one mesh passes for the wrong reasons.
- `[test]` Voxeliser cross-checked against exact signed-tetrahedra mesh volume on **all 20
  knee-exo printed parts: within ±1.2 % at 1 mm pitch, every part watertight.**
- `[fix]` **ILU preconditioner with CG was invalid and silently wrong.** CG requires a
  *symmetric* positive-definite preconditioner; `(LU)⁻¹` is not symmetric, which breaks Krylov
  orthogonality. It failed by stagnating, not by erroring: 5,000 iterations / 42 s / relative
  residual 8e-3 at 14k DOF, where Jacobi-CG reached 1e-9 in 1.6 s. Now Jacobi only, with the
  reasoning recorded in `_preconditioner` so nobody "improves" it back.
- `[fix]` `maxiter` defaulted to `20 * n_dof` — two million iterations at 100k DOF, which is
  indistinguishable from a hang. Now a finite `CG_MAX_ITER`.
- `[perf]` `DIRECT_DOF_LIMIT` 40k → **80k**, measured on bulky (worst-case fill-in) geometry:
  direct beat Jacobi-CG at every size it could handle — 1.0 s vs 4.2 s at 25k DOF, 7.8 s vs
  16.8 s at 72k — and the two paths agreed to six significant figures, which cross-validates them.
- `[fix]` Voxeliser grid shape was one layer too large per axis (node count vs element count),
  leaving a dead layer at the top of every axis that surfaced as "no material on the max face".
- `[feat]` **`fea/convergence.py`: Richardson extrapolation with an honest refusal path.**
  Written test-first. Returns the extrapolated limit, observed order and remaining relative
  error; *declines* to extrapolate a non-monotone ladder, and raises on a non-geometric one
  rather than absorbing a ratio error into the exponent. Needed because an optimizer cannot read
  a trend off a table — it has to know whether a difference between two designs is structural or
  is mesh error.
- `[test]` 16 convergence tests against sequences with known limits by construction
  (`f = limit + C·h^p`), plus the pathologies: non-monotone, sign-flipping, stalled,
  non-geometric, too-short, unsorted.
- `[fix]` `--converge` built its ladder with 1.4× and 0.7×, and 0.7 is not 1/1.4 — a 2 % ratio
  error landing straight in the order estimate. Ladder is now exactly geometric `(h·r, h, h/r)`.
- `[feat]` **`drivers/fea_part.py`**: per-part stiffness/stress with a convergence bracket.
  Boundary conditions are command-line arguments, never inferred — choosing where a part is held
  and pushed *is* the engineering, and a tool that guesses produces something that looks like
  analysis and is not. Selector mini-language is parsed, not `eval`'d, and supports per-axis
  restraint (`x>=24@xz`) because a V-wheel, roller or journal reacts in some directions only.
- `[docs]` **`PLAN.md`** — the active plan, 8 phases, grounded in an October 2026 literature
  search. `ROADMAP.md` stays as the original screw→car→home thesis; `PLAN.md` wins on ordering.
- `[decision]` **Topology optimization is an oracle, not a generator.** Density-field → editable
  B-rep is an open research problem with its own 2026 literature (AMRTO, NURBS reconstruction,
  Catmull-Clark surfacing), all of which exists because raw topopt output is not CAD-compatible.
  So topopt will inform parametric features; it will never promote a mesh to a part. Doing
  otherwise trades the parametric source of truth for a blob.
- `[decision]` **The custom DSL and the Gemma fine-tune are off the critical path.** 2026 work
  (Zero-to-CAD's ~1M executable CadQuery programs, FutureCAD) generates CadQuery *code* with an
  execute-and-repair loop, because LLMs are already strong at Python. Generating code against
  this kernel is strictly simpler than maintaining `dsl.py` + `training/`.
- `[decision]` **Physics goes inside the loop, as the reward, not downstream as a report**
  (Physics-in-the-Loop, COSMO-Agent). That is why Phase 0 was built before anything else.
- `[feat]` First real result — `P3_GantryPlate_Printed` under the documented 764 N belt load
  through the five HTD-8M grooves, reacted at the ball-nut end plate. Pitch 1.82 / 1.30 / 0.93 mm:
  mass 112 / 100 / 102 g, p99 stress 3.42 / 4.73 / 6.06 MPa (**SF 4.4 / 3.2 / 2.5** against the
  15 MPa creep-aware PETG allowable), deflection **0.696 / 1.005 / 1.381 mm**.
  **Stress has margin at every mesh. Deflection is the finding and it is NOT converged** — the
  increments grow (+0.309 then +0.376), so `richardson` refuses to extrapolate. All we can say
  honestly: the plate gives on the order of a millimetre or more, in series with the knee drive,
  which no per-feature hand calc in knee-exo looks at.
- `[broke]` **The gantry plate is at the voxel tier's resolution limit and a converged number
  needs a body-fitted mesh.** A 2% pitch change (0.910 → 0.929 mm) moved deflection 18% and mass
  2.6%: the 3.45 mm groove walls and the thin V-wheel arms are only 3-4 elements through, so
  whether a wall lands on element centres changes the stiffness more than the refinement does.
  Refining further is impractical — 0.93 mm is already 336k DOF and 154 s. This raises the
  priority of the Gmsh + CalculiX cross-check (issue #3).
- `[decision]` **An earlier hand-extrapolation of this part to ~1.35 mm was wrong and is
  retracted.** It was computed off the non-geometric 1.82/1.30/0.91 ladder, whose ratio error
  made the increments look like they were shrinking. `fea/convergence.py` caught it on the first
  real part it was pointed at, which is the whole argument for refusing rather than smoothing.
- `[decision]` Releasing the Y restraint at the V-wheel arms changed the deflection by nothing
  (1.0048 mm either way, 1,980 DOF freed). Not a null result: it proves the groove wall reacts
  the whole 764 N and the V-wheels carry none of it. A restraint that can be released without
  changing the answer is a load-path finding.
- `[docs]` `AGENTS.md` added as the operational protocol; `CLAUDE.md` remains the architecture
  reference and now documents the physics tier.
- `[fix]` `tools/ci.sh` ran only the Docker suite, so it could not gate the host-capable physics
  tests. Now runs the host suites always and the container suite when `cadkit` is up.
- `[broke]` Known, unfixed: `BACKLOG.md`'s "done so far" list is stale — `test_build_cache`,
  `test_mass_properties_cg_and_inertia`, `test_bom_document` and `test_library_loader` all exist
  and pass, but the backlog still lists those epics as "suggested next".
