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
tools/ci.sh                      # all three tiers; gates pushes. exit 0 = green

# host tier - numpy + scipy only, runs anywhere
python tests/test_fea.py                   # physics        (21)
python tests/test_convergence.py           # convergence    (16)
python workbench/tests/test_core.py        # wb core        (22)

# freecad tier - needs freecadcmd
freecadcmd workbench/tests/test_geometry.py # parts, mates   (21)
freecadcmd workbench/tests/test_document.py # doc, checks    (16)
freecadcmd workbench/tests/test_commands.py # commands, API  (24)
python workbench/tests/test_agent_end_to_end.py  # MCP, cross-process (4)

# gui tier - opens a real FreeCAD window for ~30 s
python workbench/tests/run_gui_tests.py    # qt layer       (15)

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
