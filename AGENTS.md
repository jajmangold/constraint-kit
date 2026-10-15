# AGENTS.md

Operational protocol for this repo. **Architecture lives in [`CLAUDE.md`](CLAUDE.md); the active
plan lives in [`PLAN.md`](PLAN.md); what changed and why lives in
[`CHANGELOG.md`](CHANGELOG.md).** This file is only how to work here.

## WHAT THIS IS

Two things that share a doctrine. `constraint_kit/` is the kernel and `workbench/` is a FreeCAD
workbench built on FreeCAD's own `Part` kernel, where the document is the live editing surface
and a text params file is the source of truth.

A kernel for building real machines: exact parametric B-rep geometry, a deterministic mate
kernel, four solver classes, and a physics tier that says whether the thing is any good.
The LLM decides *meaning*; deterministic math decides *geometry*. The model never places a vertex.

The machines it serves are real and on a bench — a knee exoskeleton, a 4-axis gear cutter, a
per-axis CNC controller and the enclosures around them. **A change that does not help get a
machine built is not a priority**, however interesting. `PLAN.md` §6 lists what we deliberately
do not build; read it before proposing work.

## RULES

1. **Read `CHANGELOG.md` first. Every session.** Write it last, every change. Log decisions, not
   just code — say WHY, briefly. A decision not written down gets re-litigated.
2. **TDD. The test comes first and it must fail first.** See the section below; it is not
   optional and it is not satisfied by writing tests afterwards.
3. **Verify against ground truth outside this repo** — closed-form algebra, published tables,
   measured hardware. Never against a stored snapshot of our own output, and never against a
   vision model's opinion. A snapshot test on a solver proves only that it still does whatever
   it did before.
4. **Fail loudly, never plausibly.** A half-converged solve, a leaking mesh, a selector that
   matched nothing: raise or warn with the reason. The expensive failures in this codebase have
   all been things that returned a believable number.
5. **No dead code, no silent truncation, no silent caps.** If a limit is hit, say so.
6. **Boundary conditions, loads and material allowables are inputs, never inferred.** Choosing
   them is the engineering. A tool that guesses produces something that looks like analysis.
7. **Units are mm / N / MPa / g** throughout, consistently. Stress comes out in MPa directly.
8. **Quote converged numbers with their error bar**, or say they are not converged. One voxel-FEA
   value is not a result (`fea/convergence.py` exists for exactly this).
9. **Track work in GitHub issues.** One issue per discrete deliverable, labelled by phase.
   Reference the issue in the commit; close it from the commit when it is actually done.
10. **Don't guess hardware facts.** Numbers measured on the bench live in the machine repos and
    in `PLAN.md`; prefer a measured value over a datasheet one, and say which it is.
11. **Ask before adding a dependency.** The physics tier's value is that it needs only numpy and
    scipy; that property is easy to lose and hard to get back.

## TDD, CONCRETELY

The order is not negotiable:

1. Write the test. State in its docstring what ground truth it checks and why that truth holds
   independently of our implementation.
2. **Run it. Watch it fail, and read the failure** — it must fail because the behaviour is
   missing, not because of a typo or a bad import. A test that passes before the code exists is
   testing nothing.
3. Write the minimum code to pass it.
4. Run the whole suite, not just the new test.
5. Record it in `CHANGELOG.md`.

When a test and the code disagree, **work out which is wrong before changing either.** Both
directions happen here: during the convergence work, one test used pitches `2.4 / 2.0 / 1.6` and
failed against *correct* code, because that ladder is not geometric and the constant-ratio
precondition fired first. The fix was to the test, and the reason is now recorded in the test's
own docstring. Silently relaxing the assertion would have deleted a real guarantee.

Where the method genuinely cannot reach an analytic number — a trilinear hex locks in bending —
**assert the convergence trend under refinement, not a tolerance the formulation cannot meet.**
A loose tolerance on a single mesh passes for the wrong reasons.

## COMMANDS

```bash
tools/ci.sh                      # every available tier; gates pushes. exit 0 = green
CK_GUI=1 CK_LIVE=1 tools/ci.sh   # ...plus a FreeCAD window and the configured model

# host tier - numpy + scipy only, runs anywhere
python tests/test_fea.py                   # physics        (23)
python tests/test_convergence.py           # convergence    (16)
python tests/test_cases.py                 # load cases     (16)
python tests/test_optimise.py              # the optimizer  (22)
python tests/test_buckling.py              # stability      (12)
python tests/test_printed.py               # FDM anisotropy (15)
python tests/test_crosscheck.py            # vs gmsh+ccx    (16, skips if they are absent)
python workbench/tests/test_core.py        # wb core        (32)

# freecad tier - needs freecadcmd
freecadcmd workbench/tests/test_geometry.py # parts, mates   (30)
freecadcmd workbench/tests/test_document.py # doc, checks    (16)
freecadcmd workbench/tests/test_commands.py # commands, API  (42)
freecadcmd workbench/tests/test_planner.py  # plan + repair  (15)
freecadcmd workbench/tests/test_import.py   # STEP import    (14)
freecadcmd workbench/tests/test_motion.py   # travel, sweep  (17)
python workbench/tests/test_agent_end_to_end.py  # MCP, cross-process (4)

# gui tier - opens a real FreeCAD window for ~30 s
python workbench/tests/run_gui_tests.py    # qt layer       (17)

# live tier - calls the configured endpoint, costs money
CK_LIVE=1 freecadcmd workbench/tests/test_planner_live.py   # NL -> built  (4)

# container tier
bash tests/run.sh                          # kernel        (113, needs cadkit)

python workbench/install.py            # link the workbench into FreeCAD's Mod dir
git config core.hooksPath .githooks    # enable the pre-push gate, once per clone
```

**Five test tiers, on purpose.** The host tier needs nothing but numpy and scipy. The FreeCAD
tier runs under `freecadcmd`. `test_kernel.py` needs `build123d`, `bd_warehouse`, `cq_gears`,
`z3`, `python_solvespace` and `trimesh`, which live in the container — on a bare host it reports
86/113 with the other 27 failing on imports, and that is expected, not a regression. `tools/ci.sh`
knows the difference and reports a tier it could not run as SKIPPED, never as passed.

**The Qt layer has its own tier**, because the untestable layer is where the real bugs were: two
shipped in one day (InitGui raising on `__file__`, nine missing icons) and both were invisible to
150 passing headless tests. `run_gui_tests.py` launches a real FreeCAD, drives the toolbar and all
three panels, fakes the model call to exercise the worker-thread path, and quits. It is opt-in
(`CK_GUI=1`) only because it opens a window — **run it before calling any GUI change done.**

**The live tier is the only one that asks the real question.** `test_planner_live.py` (`CK_LIVE=1`)
runs plain English through the configured endpoint, the validator, the repair loop and `build()`, and
asserts on OUR side of the contract: a stated dimension reaches the geometry, unstated values stay
tagged as guesses, an impossible request declines rather than substituting, and two parts come out
mated rather than piled. It deliberately does NOT assert which generator the model picks or how it
words its notes — that would be a test of today's weights, and it would fail for the wrong reason the
day they change.

It also earns its keep as a gap-finder, which was not the plan. Asked for a plate with a centred
bore, the model tried a bolt circle of radius zero, was refused by the validator, and came back
saying it would rather drop the feature than fake it. It was right: `plate` could not express a hole
in the middle of a plate. **A live model exercising the catalogue finds holes in the catalogue that no
unit test will, because a unit test only asks for what the author already thought of.**

Write GUI assertions that can fail. The responsiveness test originally counted event-loop spins
*after* `submit()` returned, which proves nothing about blocking; it now times `submit()` itself,
and that was verified by mutation — making the worker synchronous reports "submit() took 2.30s
with a 2s model".

## GOTCHAS THAT HAVE COST TIME

- **Never give CG a non-symmetric preconditioner.** ILU seems like the upgrade from Jacobi and is
  invalid: it breaks Krylov orthogonality and fails by *stagnating*, not erroring. Measured:
  42 s to residual 8e-3, versus Jacobi at 1.6 s to 1e-9. See `fea/voxel.py::_preconditioner`.
- **`maxiter` must not scale with DOF count.** `20 * n` at 100k DOF is two million iterations.
- **A voxel grid's element count is not its node count.** An off-by-one there leaves a dead layer
  at the top of every axis and surfaces much later as "no material on the max face".
- **Ray-parity voxelisation cannot see a leak in a wall parallel to the ray.** Vertical walls
  contribute no crossings at all, so a hole in one is invisible to the watertightness check.
- **An absolute stress peak off a voxel model is a staircase artefact** as much as a structural
  fact. Use the 99th percentile; it moves with the structure.
- **Voxel bending stress is systematically OPTIMISTIC by `pitch/h`, and that is a ceiling, not
  an error.** Stress is evaluated at element *centres*; in bending it peaks at the *surface*. So
  the largest stress a grid can even observe is the value at `h/2 − pitch/2` from the neutral
  axis, which is `1 − pitch/h` of the extreme fibre. Measured against CalculiX on a 10 mm beam:
  0.447 / 0.740 / 0.863 at pitch 5 / 2.5 / 1.25, against a prediction of 0.500 / 0.750 / 0.875.
  **A safety factor read off a voxel bending stress is too high by that factor.** Four elements
  through a thickness still over-states SF by a quarter. See
  `tests/test_crosscheck.py::test_the_voxel_stress_bias_is_the_predicted_1_minus_pitch_over_thickness`.
- **A linear solve reports deflection it has no right to.** Linear elasticity assumes rotations
  stay small; past about a tenth of the span they are not, and the real part stiffens as it
  deflects so the linear figure OVERSTATES the movement. Measured: a 4 mm bracket read 124 mm on
  a 120 mm part, with the composure of a 5 mm answer. `physics` now reports `linear_valid`. The
  criterion is deflection over SPAN, not over thickness — thickness was tried first and flagged a
  valid 4.5%-of-span answer, which is how a warning gets trained out of a reader.
- **A stress check is BLIND to buckling**, and an optimizer asked for "lightest" goes looking
  for exactly that. Measured on a real strut: stress safety factor 19 while within a factor of
  two of folding. Ask `analyse` for `buckling=1` on anything slender and constrain
  `buckling_factor >= 2` -- two, not one, because linear buckling is an upper bound on a perfect
  member and a printed part has a bowed wall.
- **ARPACK does not work inside FreeCAD, and the reason is not ours.** FreeCAD 1.0 bundles a
  numpy whose `numpy.core._dtype.__repr__` recurses through `issubdtype`, and scipy's ARPACK
  wrapper formats a dtype on the way in -- so `eigs` dies before its first matvec. `fea/buckling`
  uses pure-numpy subspace iteration for that reason. Do not "optimise" it back to ARPACK.
- **`method="auto"` probes the direct solver; do not assume it.** FreeCAD's compiled SuperLU is
  built against a different numpy ABI, so every direct solve in there fails with `argument 3 must
  be numpy.ndarray, not numpy.ndarray`. `fea.voxel.direct_solver_available` is the check, and it
  lives in the tier so `auto` respects it -- the bridge used to know privately and `auto` stayed
  broken for everyone else.
- **A voxel mesh QUANTISES the geometry, so an optimizer sees a staircase.** At 4 mm pitch a
  plate 7 mm thick and one 10 mm thick sample to the same 300 elements and report the same safety
  factor to three decimals. A search tolerance finer than the pitch is chasing a plateau, and the
  answer is resolved to about one pitch -- `optimise` warns when `tol < pitch/2`. The same reason
  `mass_g` now comes from the exact B-rep volume: a voxel-derived mass is a step function and was
  up to 10% out.
- **A BINARY constraint carries no gradient and a feasibility filter gets it backwards.** Every
  infeasible point has violation 1.0, so the objective decides among them and the walk goes to
  the lightest *infeasible* design. `minimise` scans the parameter bounds before walking, which
  is what fixes it; if you write a new search, do the same.
- **A pure coordinate search stalls on a curved constraint boundary**, where the improving
  direction is neither axial nor diagonal. Use `minimise_sized` when one parameter is pure sizing
  and the rest are shape -- it reduces the dimension and gets the closed-form answer.
- **von Mises is the wrong allowable check for a printed part.** It presumes a material with no
  preferred direction; an FDM wall is a stack of welds at roughly half the in-plane strength
  across them. Use `fea.printed.check` on the layer plane. Measured on the gantry plate: the
  welds govern at **SF 0.74 printed on its end** against 1.63 printed flat, from the same load
  and the same geometry. Print orientation is a structural decision, and it is one a von Mises
  number cannot express.
- **Printed-part output must be ASCII.** These drivers print to a Windows cp1252 console, where
  an em-dash arrives as a replacement character and makes a correct sentence read like an
  encoding failure in the analysis. Prose goes in docstrings; `tests/test_printed.py` has the
  guard.
- **A non-converging voxel ladder on a thin-walled part is not a refinement problem.** Whether a
  wall lands on element centres matters more than the pitch does. A 3.45 mm wall is 2.7 elements
  at 1.30 mm pitch, and bending compliance goes as `t⁻³`, so gaining or losing one lattice layer
  off that wall swings its local compliance by 4×. Refining does not fix this; it re-rolls the
  dice. **Use `drivers/fea_part.py --reference`** — a body-fitted mesh puts nodes on the wall
  faces, so the wall is the thickness it is at every pitch.
- **`DIRECT_DOF_LIMIT` is shape-dependent and cannot be right for everything.** A slender beam
  factorises nearly banded and stays cheap far past the threshold; a cube is the opposite.
- **Load applied to empty nodes silently vanishes.** A voxel grid is padded, so a selector like
  `x >= 59` catches empty nodes past the part; they are dropped from the reduced system, so any
  share of the load given to them disappears. Measured: a cantilever read 0.288 mm against
  0.968 mm of beam theory because 200 N arrived as 60.5 N — converged, residual 9e-10, plausible,
  and wrong by three. `Model.load` now divides over material nodes only. The general lesson: when
  a selector feeds a physical quantity, check what it actually caught.
- **`freecadcmd` imports a script as a module named after its file.** `__name__` is
  `"test_geometry"`, never `"__main__"`, so a guarded `main()` never runs and a suite exits 0
  having tested nothing. The FreeCAD-side suites call `main()` unconditionally and say why.
- **`sys.argv[1]` under `freecadcmd` is the script path**, not your first argument. Pass
  parameters through the environment.
- **scipy's sparse DIRECT solver does not work inside FreeCAD 1.0.2.** The bundle pairs numpy
  1.26.4 with scipy 1.16.0 whose compiled SuperLU extension was built against a different numpy
  ABI, so `spsolve` and `factorized` fail with `argument 3 must be numpy.ndarray, not
  numpy.ndarray`. Pure-Python iterative solvers (`cg`, `minres`) and sparse matvec are fine.
  `ckwb.physics.direct_solver_available()` probes this once rather than hardcoding it.
- **Two OCCT builds cannot share a process.** FreeCAD 1.0.2 has OCCT 7.8.1; the host stack has
  OCP 7.9.3. Never import cadquery or build123d inside FreeCAD. The workbench is built on
  FreeCAD's own `Part` kernel for exactly this reason.
- The repo's own hard-won geometry gotchas are in `CLAUDE.md`; the FreeCAD-side ones (BoundBox
  bounds control polygons, placement bakes into `Shape`) are in `PLAN.md` §2.
