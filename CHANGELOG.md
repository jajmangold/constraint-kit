# CHANGELOG

Newest first. Tags: `[feat]` `[fix]` `[test]` `[docs]` `[decision]` `[broke]` `[perf]`.

Read this first every session; write it last. Log **decisions**, not just code — a decision that
is not written down gets re-litigated, and the reason it was made is the expensive part.

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
