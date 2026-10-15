# CHANGELOG

Newest first. Tags: `[feat]` `[fix]` `[test]` `[docs]` `[decision]` `[broke]` `[perf]`.

Read this first every session; write it last. Log **decisions**, not just code — a decision that
is not written down gets re-litigated, and the reason it was made is the expensive part.

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
