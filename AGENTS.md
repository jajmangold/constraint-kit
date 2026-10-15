# AGENTS.md

Operational protocol for this repo. **Architecture lives in [`CLAUDE.md`](CLAUDE.md); the active
plan lives in [`PLAN.md`](PLAN.md); what changed and why lives in
[`CHANGELOG.md`](CHANGELOG.md).** This file is only how to work here.

## WHAT THIS IS

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
tools/ci.sh                      # everything available here; gates pushes. exit 0 = green
python tests/test_fea.py         # physics tier      (host-capable: numpy + scipy only)
python tests/test_convergence.py # convergence       (host-capable)
bash  tests/run.sh               # kernel, 113 tests (needs the cadkit container)

git config core.hooksPath .githooks   # enable the pre-push gate, once per clone
```

**Two test tiers, on purpose.** `test_fea.py` and `test_convergence.py` run anywhere Python does.
`test_kernel.py` needs `build123d`, `bd_warehouse`, `cq_gears`, `z3`, `python_solvespace` and
`trimesh`, which live in the container — on a bare host it reports 86/113 with the other 27
failing on imports, and that is expected, not a regression. `tools/ci.sh` knows the difference.

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
- The repo's own hard-won geometry gotchas are in `CLAUDE.md`; the FreeCAD-side ones (BoundBox
  bounds control polygons, placement bakes into `Shape`) are in `PLAN.md` §2.
