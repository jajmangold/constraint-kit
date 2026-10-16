# CHANGELOG

Newest first. Tags: `[feat]` `[fix]` `[test]` `[docs]` `[decision]` `[broke]` `[perf]`.

## 2026-10-19 (later still) — mobility, the rack, and a travel that had no end (closes #11, #12, #13, #14)

The four missing-mate issues, and the capability their comments said was blocked behind #28 and
FreeCAD's undocumented `Reference` path format. It was not blocked, and the reason is the whole
point: **mobility is a property of the joint GRAPH, not of the geometry.** Gruebler and Kutzbach
wrote it down in the 1880s and it is arithmetic, so `ckwb/mobility.py` has it in closed form and
#28 is no longer in the way of the thing it was blocking.

- `[feat]` **`ckwb/mobility.py`** — `M = d(n-1) - sum(constraints)`, planar or spatial, with the
  **loop count reported beside it** and the report saying the number is a *count rather than a
  proof* whenever a loop exists. A Bennett linkage counts as zero mobility and moves perfectly
  well; a mobility quoted without that caveat is the confident wrong answer this project spends
  most of its effort avoiding.
- `[feat]` A **floating body is named rather than summed**. Mobility is a sum, so one disconnected
  part with six free degrees of freedom and a mechanism over-constrained by six cancel exactly —
  the total looks perfect while the model is wrong.
- `[feat]` `mobility` registered as a command, defaulting its bodies to the document's parts.

### A coupling is not a lower pair, and getting that wrong turned a belt drive into a structure

- `[fix]` The first version had one `FREEDOMS` table, and so counted a belt as a joint "leaving one
  freedom" — which removes `(3-1) = 2` in the plane. A two-pulley belt drive then came out at
  **M = 0, a STRUCTURE**, for a mechanism anybody can turn by hand; the ballscrew axis came out at
  **M = -1**, over-constrained. The split is now explicit and is the module's main lesson:

  | | what it is | removes |
  |---|---|---|
  | `LOWER_PAIRS` | surface contact between two bodies (revolute, prismatic, bolt in a threaded hole) | `d - f` |
  | `COUPLINGS` | **one scalar equation** between bodies already connected (gear, belt, ballscrew nut) | **exactly 1**, whatever `d` is |

  Belt drive `3(3-1) - (2+2+1) = 1`, ballscrew axis `3(3-1) - (2+2+1) = 1`. Both correct now.
- `[decision]` **`screw` therefore means two different things and both are sayable.** `screw` is the
  lower pair — a bolt turning in a threaded hole, the only connection between two bodies.
  `screw_coupling` (aliases `nut`, `ballscrew`) is a ballscrew nut tying a shaft that already has a
  revolute to a carriage that already has a prismatic. Conflating them is what made a CNC axis read
  as over-constrained.

### The rack (#14), which needed a part as well as a mate

- `[feat]` **`parts.gear_rack`** — straight-flank ISO 53 basic rack. A rack is the involute's
  infinite-radius limit, which is why its flanks are dead straight and why it is **exactly
  conjugate** to `spur_gear_profile` rather than approximately so. Measured: the mesh overlap at
  the correct centre height is **1.6e-4 mm³** — numerical noise, i.e. tangential contact with no
  backlash and no root interference.
- `[decision]` **Length is exactly `teeth * pi * m`, with both ends halfway along a root land**, so
  two racks butt into one unbroken tooth pattern. A long axis is several racks in a row and a rack
  sized to its tooth tips would plant a half-pitch error at every joint.
- `[feat]` `motion.rack_travel / rack_turns / rack_steps_per_mm / rack_centre_height / rack_length /
  rack_drive`. The phase is **derived, not tuned**: `gear_rack` starts mid-root-land and
  `spur_gear_profile` centres a tooth on its own +X, so one fixed quarter turn points that tooth at
  the rack, and rolling holds the phase everywhere else for free. Verified at awkward fractions
  (0.03, 0.59 of a turn), not just whole teeth.
- `[fix]` **A rack's rotation and travel are PERPENDICULAR**, unlike a screw's, which share an axis.
  Reusing `screw_drive` would spin the pinion about its direction of travel — a drill bit instead
  of a wheel. The sign is derived too: for the contact point not to slip, `v_centre = -omega x r`.
- `[note]` **A rack drive is measurably coarser than the hardware already on the bench.** 25.5
  steps/mm for a 20-tooth module-2 pinion against the 1605 ballscrew's measured **320** — an eighth
  of the resolution. A rack buys unlimited travel, which is why neither machine here uses one.

### The travel that had no end: a measured false pass, in the dangerous direction

- `[fix]` **`sweep` reported `ok: True` for a carriage driven clean off its rail.** Measured before
  the fix: a spacer swept 0 → 900 mm along a shaft spanning −100..100 reported **ok, with its worst
  gap unchanged at 1.0 mm**. Running a carriage 800 mm into thin air read as the *cleanest* part of
  the travel, because a part that has left its rail interferes with nothing. The sweep now asks two
  questions — does it hit anything, and *is it still on the thing it is travelling along* — and
  reports `travel_limits`, `off_support` and a verdict that fails on either.
- `[feat]` `motion.axis_extent(..., conservative=)` and `motion.travel_limits`. Which measurement is
  safe depends on which part: **under**-estimating the support and **over**-estimating the moving
  part both shrink the usable range, which refuses a doubtful travel rather than allowing it. So the
  support is measured by vertex projection (exact for prismatic stock) and the moving part by its
  bounding box (never understates).
- `[decision]` **"Still on the support" is NOT the same question for the two motions.** A carriage
  must be *wholly* within its rail's extent or it is held up by nothing; a pinion need only keep its
  **centre** over the rack, because its body overhanging the end is what a pinion at the end of its
  travel always does. Demanding containment there would refuse the last tip radius of travel at each
  end for no physical reason. Both criteria are now tested, because they look like one criterion
  until a pinion meets them.
- `[feat]` `sweep` gained `motion="slide" | "rack"`, and the rack mode is not dressing: a pinion
  slid along a rack without turning ploughs its teeth through the rack's, so the same sound
  mechanism swept as a slide collides at **11 of 13 stations** and would be reported as a design
  fault. `rack_drive` refuses travel past the end of the rack by arithmetic, naming how many more
  teeth it would take.

**25 suites, 458 tests.** Motion went 17 → 34; mobility is 22 new.

## 2026-10-19 (later) — the optional AMG preconditioner, and a measurement that went against it (closes #5)

`voxel.py` has carried a note since it was written: Jacobi is valid, cheap and weak, and "a
genuinely better symmetric option is algebraic multigrid (pyamg), which is not a dependency here".
This is that option, built and then **measured** -- which is the interesting part, because the
measurement came out against the new code and is recorded rather than quietly dropped.

- `[feat]` **`precond="auto" | "jacobi" | "amg"`** on `solve` and `solve_cases`, with
  `amg_available()` probing for pyamg and `choose_preconditioner` resolving the request.
  `Result` now reports **which** preconditioner ran and **how many iterations** it took, because
  "it converged" and "it converged the way you asked" are different claims, and a preconditioner
  nobody can measure is a preconditioner nobody should trust.
- `[decision]` **An explicit `precond="amg"` RAISES when pyamg is absent**, with the install
  line. `auto` degrades silently and should -- it means "use your judgement" -- but somebody
  passing `"amg"` is doing it because Jacobi was too slow, and quietly giving them Jacobi while
  reporting convergence would tell them the slow path was the fast path.
- `[fix]` **`ilu` is refused by name, with the reason**, so the next person does not try it
  again: CG needs a symmetric positive-definite preconditioner and an incomplete LU is not one;
  it breaks Krylov orthogonality and fails by *stagnating* rather than erroring. Measured here
  long ago at 42 s to a residual of 8e-3 against Jacobi's 1.6 s to 1e-9.

### The near-nullspace was the whole difference, and leaving it out nearly got AMG deleted

- `[fix]` Smoothed aggregation coarsens by assuming it knows the operator's near-nullspace. For a
  scalar problem that is the constant vector, which is pyamg's default -- and this is a **vector
  elasticity** operator whose near-nullspace is the rigid-body motions. Told nothing, SA coarsens
  as though every degree of freedom were an independent scalar:

  | 14,112 DOF | iterations |
  |---|---|
  | AMG without the translations | **300** |
  | AMG with them | **99** |

  The first number is barely ahead of Jacobi, and the honest-looking conclusion from it would
  have been to delete the feature. There is now a test that measures both configurations, so the
  reason the code looks the way it does is recorded rather than inferable.

### What AMG actually buys, and what it does not

Measured on a slender beam at 1e-9 relative tolerance:

| DOF | Jacobi | AMG (+nullspace) |
|---|---|---|
| 4,800 | 328 it / 0.07 s | **92** it / 0.24 s |
| 14,112 | 522 it / 0.50 s | **99** it / 1.16 s |
| 29,160 | 604 it / 1.23 s | **108** it / 2.79 s |

- `[feat]` **The iteration count is essentially flat in problem size** -- 92 to 108 across a
  sixfold increase, against Jacobi's 328 to 604. That is the defining property of a working
  multigrid method, and it is what the tests assert: the *growth*, not a single number, because
  the growth is the property and a single number is this machine on this day.
- `[decision]` **And AMG is slower in wall clock at every size reachable here**, because a
  V-cycle costs far more per iteration than a divide. Flat iterations still lose when each one is
  twenty times the price. So `AMG_DOF_FLOOR` is **150,000** -- set where the trend says the two
  curves meet rather than where it would flatter the new path -- `auto` leaves AMG off below it,
  and there is a test asserting the floor is high enough that `auto` never chooses the slower
  option at the sizes this project actually solves.
- `[test]` **The two preconditioners agree to 3.6e-12**, which is what proves a preconditioner is
  a preconditioner and not a modification of the physics.
- `[note]` pyamg stays a **lazy** import inside the two functions that need it, so `voxel.py`
  still imports on numpy and scipy alone -- `tests/test_packaging.py` enforces that, and the
  physics tier has to work inside FreeCAD where pyamg is not present. Declared as the `amg`
  extra.

**24 suites, 419 tests.**

## 2026-10-19 — SIMP topology optimisation, an oracle and never a generator (closes #19)

The issue title is the design. What comes out is a **density field**, and a density field is not a
manufacturable design: it is a statement about where material earns its keep, which a human then
redraws as parametric geometry. Everything else in this project runs the other way -- parameters
in, verified geometry out, provenance on every number -- so `Topology` has no `.shape()` and no
`.step()`, deliberately, and `suggestion()` returns prose.

It is also why this is **separate from `constraint_kit.optimise`**, and that separation was argued
in #17's closing before either existed. The parametric optimizer searches a parameter vector and
answers with "thickness 17.2, depth 12, stiffness binds" -- something you can put on a drawing.
This searches a density field and answers with a picture.

- `[feat]` **`constraint_kit/fea/topology.py`** -- SIMP with sensitivity filtering and the
  optimality-criterion update, 13 tests.
- `[note]` **Nothing in `voxel.py` changed to support it.** `Model` has accepted `density_floor`
  and `penal` since the day it was written, with the comment that they exist "so a topology
  optimizer can reuse this assembly unchanged". They did.

### What is verifiable, and it is more than it looks

There is no closed form for the optimal topology of an arbitrary problem, so the tests assert
properties of the **method** rather than a shape -- and every one of them is a real prediction:

| property | measured |
|---|---|
| the optimised design beats the uniform one at the same volume | compliance **916.2 -> 108.7**, so **743% stiffer at the same mass** |
| the volume constraint holds exactly, every iteration | to 1e-6, by bisecting the Lagrange multiplier |
| a symmetric problem gives a symmetric answer | worst deviation **1.18e-12** |
| a cantilever hollows its middle and keeps its flanges | flange density **0.679** against mid-depth **0.242** |
| checkerboarding appears without the filter and is suppressed with it | **0.133 -> 0.066** |

The flange result is the one worth pausing on. Bending stress goes as distance from the neutral
axis, so material in the middle of the depth earns almost nothing -- and the optimiser rediscovers
the I-section from the physics alone, having been told nothing about beams.

### A measure that could not tell a defect from the thing you wanted

- `[fix]` **The first `checkerboard_score` was wrong, and it cost a test.** It took the mean
  difference between each element and its six neighbours -- large for a checkerboard, and *also*
  large for a legitimately sharp solid/void boundary, which is exactly what a well-resolved
  topology is made of. It scored a cleanly filtered field at 0.144 and an unfiltered one at
  0.251, so the filter looked only mildly effective. The real measure multiplies the field by the
  checkerboard parity `(-1)^(i+j+k)` and averages over 2x2x2 blocks: alternation survives the
  parity and anything else cancels. A smooth ramp now scores under 0.1 however much it varies
  overall, and a true checkerboard scores 1.0. **A measure that cannot distinguish a defect from
  the thing you were trying to achieve is not a measure of the defect.**

### Honesty, structurally rather than by convention

- `[decision]` A test asserts `Topology` has **no `shape` and no `step` attribute**. If either
  ever appears the doctrine has been inverted, and a grey blob emitted as geometry would be a
  part nobody chose. `as_grid()` exists -- the one legitimate mechanical use, re-analysing the
  suggestion so it can be measured rather than admired -- and `report()` names the
  **intermediate** fraction explicitly, because intermediate density is not a material and that
  fraction is the part a human still has to decide.
- `[feat]` A final solve is run on the field being **returned**, so `compliance` is the compliance
  of that field rather than of the one before the last update. Otherwise `as_grid()` and
  `compliance` would disagree and a reader could not check one against the other.
- `[fix]` `penal < 1` is refused: below 1 the interpolation *rewards* intermediate density instead
  of punishing it, so the field never resolves and the method does the opposite of its job.

**23 suites, 406 tests.**

## 2026-10-18 (finally) — cause attribution and ranked, re-verified repairs (closes #20)

The validator already returned structure. What it did not return was a **cause** and a **fix**. A
program with `thicknes` for `thickness` came back as a sentence, went to the model, and cost a
round trip -- for a misspelling with exactly one plausible correction. A clash came back as an
overlap volume and said nothing about which pair caused it.

- `[feat]` **`ckwb/diagnose.py`** -- a seven-cause taxonomy, deterministic repair candidates,
  ranking by certainty, and re-verification of every one. 17 tests.
- `[feat]` **The planner now tries a deterministic repair BEFORE asking the model**, and the test
  observes it: a reply with one misspelled parameter comes back as a usable plan with
  `session.attempt == 1`, meaning no second model call was made. The plan's notes say what was
  repaired and that it was re-validated, so the user is not being quietly corrected.
- `[decision]` **Nothing is offered that has not been re-verified.** The acceptance criterion on
  the issue, and the only defensible rule: a suggested fix that does not build is worse than no
  suggestion, because somebody will take it. Repairs are applied cumulatively to a copy and the
  whole program is re-diagnosed -- two fixes that are each fine and together are not would
  otherwise both be offered. There is a test that sabotages the verifier and asserts nothing
  comes back.
- `[decision]` **An unknown generator is NEVER repaired by substitution**, and that is the test to
  read first. `turbine_blade` is not a misspelling of `plate`, and a tool that quietly made it one
  would build the wrong object and report success. The output is a refusal naming what the
  catalogue has, so the choice stays the caller's.
- `[feat]` **The similarity threshold is the line this module exists to walk.** 0.72 admits
  `thicknes` -> `thickness` and `botom` -> `bottom`, and rejects `ceiling` -> `bottom`. A word
  within an edit or two of a real name is a typo; a word that is simply not in the vocabulary is
  a different request, and picking the alphabetically-first frame would be inventing an answer.
- `[feat]` **A repair keeps the provenance of what it renames.** A value the user stated is still
  `stated` after the parameter it was attached to is spelled correctly -- a repair that re-tagged
  it as a guess would have edited the record of who decided what.
- `[feat]` **Overriding a stated value ranks BELOW a plain rename** (0.6 against 0.95) and says
  so in its reason: *"you stated id_, so this changes a number you gave -- the generator's own
  rule says it cannot hold."* Changing somebody's number is not a free action and should not look
  like one.
- `[feat]` **The clash case from the issue.** `repair_document` attributes an interference to the
  overlapping pair, computes the separation from the **bounding-box overlap on the axis they
  overlap least** -- the smallest move that resolves it, rather than a guess -- applies it, and
  **re-runs the interference check**. If the model is not clean afterwards the move is undone
  rather than left in place, and the repair is reported as rejected. Measured on two 8 mm plates
  overlapping by 4 mm: separation exactly 4 mm, model clean, verified.
- `[note]` A `geometry_failed` repair is deliberately narrow. It parses the generator's own error
  -- "bore id_=20 must be smaller than od=10" names both the value to move and the bound -- so
  the fix is *determined*. A generator error that does not name its values gets no repair, which
  is correct: guessing which dimension to change would be designing rather than fixing.

### Three of my own test fixtures, each wrong in an instructive way

- `[fix]` **A fixture that broke two things while claiming to break one.** Overriding `parts`
  left the default mate pointing at a part that no longer existed, so every single-part fixture
  also produced a spurious `unknown_part_reference` -- and three tests failed against entirely
  correct code.
- `[fix]` **A taxonomy test asserting that a reachable cause was unreachable.** `interference` is
  the one cause that cannot come from a *program*, because it needs placed geometry, so the
  document path had to be walked too.
- `[fix]` **Two assertions looking for the wrong text.** The "you stated this" warning lives in a
  repair's `because` rather than its `summary` -- the summary is the one-line *what*, and "you
  asked for this and I overrode it" is a reason. And a refusal is recorded as an explicit
  *rejection* rather than an empty list, which is better: an empty list leaves a reader wondering
  whether the case was even considered.

**22 suites, 393 tests**, including the live tier against the configured model.

## 2026-10-18 (last) — freeform geometry, and the packaging made to match the architecture

Two things, one found by the live planner and one by reading our own claims.

### Loft, sweep and revolve (closes #15)

The gap was identified by the model rather than by me, which is the second time and the reason
the live tier exists. Asked for a turbine blade it declined, precisely: *"the catalogue has no
airfoil/twisted-blade generator ... Nothing here can express a lofted, twisted, tapered aerofoil
section."* It was right -- everything in the catalogue was a prism, a cylinder or a boolean of the
two, so a duct that changes section, a bend, a coil and a cone were all unsayable.

- `[feat]` **`loft`** -- a solid through a stack of parallel sections, tapered and optionally
  twisted; **`sweep`** -- a round profile along a circular bend or a helix, so a pipe elbow and a
  compression spring are one generator; **`revolve`** -- an annulus, a cone, or a sector of
  either.
- `[test]` **17 tests, every one against a closed form**, which is why these three operations
  were chosen: a revolved rectangle is `pi*(R2^2-R1^2)*h`, a revolved triangle is `pi*r^2*h/3`, a
  swept circle is Pappus (`area * centroid path length`), a lofted taper is the prismatoid
  formula `h/6*(A1+4Am+A2)`, and a helical sweep is its wire cross-section times the helix length
  by Pythagoras on the unrolled cylinder. Equality tests, not plausibility checks.
- `[feat]` **The live planner now builds all four of the things it used to decline**, first try,
  and three match their closed forms on the nose:

  | request | generator | volume | closed form |
  |---|---|---|---|
  | duct tapering 80 to 40 square over 120 | `loft` | 448,000 | 448,000 (prismatoid) |
  | 90 degree bend, 25 tube on 60 radius | `sweep` | 46,264 | 46,268 (Pappus) |
  | spring, 4 wire, 30 radius, 10 pitch, 6 turns | `sweep` | 14,212 | 14,232 (helix length) |
  | tapered twisted turbine blade, 35 degrees | `loft` | 131,250 | 131,250 |

  And the blade's notes are worth quoting, because they are the behaviour the whole design is
  for: *"the catalogue has no airfoil profile, so the cross-section is the generator's
  rectangular profile rather than a real blade aerofoil ... I also interpolated the twist through
  8 intermediate sections (the default would be 2) ... that number is my choice, not yours."*
- `[fix]` **A twist across only two sections is refused.** Two rotated squares with a ruled
  surface between them is a shear, not a twist -- and it would look like a twisted blade from a
  distance while being a different shape, which is worse than an error because somebody would
  print it.
- `[fix]` **A test of mine that would have passed on noise.** It asserted a twisted loft has less
  volume than a straight one. True, and by **0.000012%** -- a smooth loft through eight
  slightly-rotated squares stays almost exactly inside the straight envelope, so that assertion
  passed whether the twist worked or not. Replaced with two that discriminate: the **mid-height
  section must sit at half the twist** (45 degrees straight against 60 for a 30 degree twist,
  exactly 45 + 30/2), which is what proves the twist is *interpolated through the stack* rather
  than applied only at the ends; and the **surface area**, which moves 0.157%, three orders of
  magnitude more signal than the volume.

### The packaging contradicted the architecture (closes #24)

The issue was filed about a stale pin. The finding was worse.

- `[fix]` **`pyproject.toml` declared ONE dependency list for THREE environments.** So
  `pip install constraint-kit` pulled cadquery, fastapi, neo4j and langgraph on somebody who
  wanted only the physics tier -- **the tier whose entire value is that it needs none of them**,
  because it has to run inside FreeCAD's interpreter where pip is not available. The base install
  is now numpy and scipy alone, and everything heavier is an extra that says what it is for
  (`kernel`, `service`, `solver`, `gear`, `pdf`, `training`).
- `[test]` **`tests/test_packaging.py` (9 tests) makes the claims checkable**, by reading imports
  with `ast` rather than importing anything -- which matters, because a test that imports a
  module to find out what it imports can only run where everything is already installed. It
  distinguishes an **eager** import from a **lazy** one, because `fea/sample.py` legitimately
  needs OCP inside one optional function and an earlier version of the test condemned it, which
  would have pushed the fix in exactly the wrong direction.
- `[test]` And it compares the pinned kernel stack against `cadkit/Dockerfile` exactly, because
  the container is the only place that stack has ever run: 113 tests pass in there and nowhere
  else. A disagreement would mean the test count was measured against a different stack than the
  one declared.
- `[docs]` **CLAUDE.md now opens with the three stacks** -- interpreter, geometry kernel, numpy
  version, what runs there and what verifies it -- plus a table of the three things FreeCAD's
  bundled scipy has broken and what we do instead. Almost every environmental surprise in this
  project came from forgetting that there are three.
- `[fix]` `requires-python` was `>=3.10` and is now `>=3.11`, which is what FreeCAD 1.0 actually
  bundles. A floor above that would have declared the workbench unsupported by its own metadata.

**20 suites, 372 tests.**

## 2026-10-18 (later) — requirements drive parameters, and the design says whether it meets them (closes #29)

The workbench had a Requirements button that opened a dialog saying *"Requirements are not
implemented yet"*, which is the one kind of interface element worth less than nothing. More
importantly it had the dependency running the wrong way: a parameter was a number somebody typed,
and the reason for the number lived in their head. Change the belt tension and nothing moves;
change a plate and nothing notices the belt tension it was sized for.

- `[feat]` **`ckwb/requirements.py`** -- four things, in the order that matters:
  - a **requirement** is a named value with a unit and a reason: `belt_tension = 764 N, measured
    on the bench`
  - a **derived** value is an expression over requirements: `moment = load * span / 1000`
  - a parameter can be **driven** by an expression, which makes the dependency real -- edit the
    requirement and the geometry follows, with the chain in the document rather than in memory
  - a **target** on a measured quantity is checked against the physics and comes back met or
    unmet, because a requirement nobody checks is a comment
- `[feat]` **Provenance survives the whole way down.** A driven parameter is tagged `derived`,
  never `stated`, and its note names the expression it came from -- so FreeCAD's own property
  editor shows a computed value as computed. A design where a computed value and a chosen one
  cannot be told apart is a design nobody can review, and `set_param` would have tagged it
  `stated`, which is a lie the tool would be telling about its own work.
- `[feat]` **The verdict is real, and the test proves it comes from the geometry**: the same
  target against the same load on a 4 mm deck and a 16 mm one -- 15.4 mm missed, 2.39 mm met. If
  the verdict were merely restating the requirement, both would agree.
- `[feat]` **Six registered commands** (`set_requirement`, `derive_requirement`, `drive_param`,
  `release_param`, `get_requirements`, `verify_requirements`), so an agent drives the same surface
  the panel does. **And a RequirementsPanel** that shows the chain: requirements, what they
  derive, what they drive, the targets, and what is out of date.
- `[decision]` **Staleness is the state that matters in use**, and it gets its own row group and
  its own summary line. An edited requirement that has not propagated leaves the design in two
  states at once -- the number says one thing and the geometry another -- and a panel showing a
  green tick for that would be actively misleading. `stale()` reports rather than quietly
  correcting on read, because a getter with a side effect on the geometry is a trap.
- `[feat]` **The expression evaluator is reused, not rewritten.** `constraint_kit.parameters.safe_eval`
  is a strict AST whitelist that never calls Python `eval`, it is pure stdlib so it runs inside
  FreeCAD unchanged, and it is already what the kernel trusts with an expression that came from a
  language model. **Two independent guards** turn out to cover it: the free-name check rejects
  `__import__` and `range` before anything is evaluated at all, and the whitelist rejects an
  attribute access or a comprehension if one ever got past it. The test asserts the refusal and
  deliberately not which guard fired.
- `[fix]` A circular derivation is **named** rather than iterated to a limit: a pass that resolves
  nothing new while work remains is a cycle, and the message lists the values involved. An
  expression over an unknown name says which name, and lists the ones that do exist.
- `[note]` A derived value whose inputs do not exist yet is a normal intermediate state -- somebody
  is building the set up -- so `derive()` tolerates it and `solve()` is where it fails loudly.
  Recording an intention and asking for an answer are different operations.

**18 suites, 346 tests.**

## 2026-10-18 — manufacturing rules as constraints, not an audit (closes #18)

The point of the issue title. An optimizer told to minimise mass subject to strength will return a
0.9 mm wall at an 80 degree overhang with a 0.6 mm cooling channel, because nothing in its problem
statement said otherwise. Auditing that afterwards and sending it back is a loop with a human in
it. **The demonstration, on a real part:** asked for the lightest panel that carries 20 N, the
optimizer picks **1 mm**; with printability as a constraint against a 4 mm wall rule, **5 mm**.

- `[feat]` **`constraint_kit/manufacture.py`** -- overhang, thin wall, narrow channel and build
  volume, every one computed from **geometry** rather than from a parameter name. That is what
  makes it general: a wall is thin because the solid is thin there, not because something called
  `wall_thickness` is small, and a part with a thin region nobody named is exactly the part an
  audit misses.
- `[feat]` **`analyse(..., rules={...})`** reports them as plain numbers, so
  `{"metric": "printable", "op": ">=", "limit": 1.0}` is a constraint the search respects rather
  than a verdict somebody reads.
- `[decision]` **The three rules carry different confidence and that is stated rather than
  averaged away.** Overhang is EXACT -- a question about surface normals, which the tessellation
  has. Thin wall and narrow channel are morphological openings on the voxel grid and so are
  RESOLUTION-LIMITED, and a rule finer than two voxels is **refused** rather than answered,
  because that is the one place a printability check could most easily reassure somebody about a
  wall it physically cannot see.
- `[feat]` The thin-wall test is a morphological **opening**, which is a definition rather than a
  heuristic: a region can contain a ball of radius r if and only if it survives erosion by r
  followed by dilation by r, so the solid minus its own opening is *exactly* the set of places
  thinner than 2r. Narrow channels are the same question asked of the enclosed void -- with the
  outside air flood-filled away first, because the air around a part is not a channel.

### A geometric fact that had to become a design decision

- `[decision]` **An opening inevitably flags every convex edge, and that is geometry rather than a
  bug.** A ball of radius r centred inside a solid cannot cover the material right at a 90 degree
  corner, so by the ball-fitting definition that corner IS thinner than 2r. Measured: a
  20 x 20 x 8 mm slab against a 3 mm rule flags **6.7%** of its volume, all of it within one ball
  radius of an edge, while being 8 mm thick everywhere that matters -- and the same slab at 1.5 mm
  thick flags **100%**. So the verdict is a **fraction** with a threshold, not a count. Fudging
  the measure to hide the edge effect would also hide a real thin region that happened to sit
  near an edge.

### Three defects found, one in the code and two in my own fixtures

- `[fix]` **The build-plate exclusion used the lowest face CENTROID, not the lowest VERTEX.** A
  box's bottom face needs no support because it lies on the plate, so plate faces must be
  excluded -- but a 45 degree wedge touches the plate along a *line*, so no face centroid sits at
  the true minimum, and the lowest centroid belongs to a sloped face a third of the way up. Taking
  that as the plate height excluded half of the very overhang being measured: the wedge reported
  exactly half its hypotenuse, which is the kind of wrong that looks like a rounding problem and
  is not. A face is on the plate when ALL THREE vertices are.
- `[fix]` **My extruded-profile fixtures had inverted side-wall winding**, so the TOP face of an L
  reported a downward normal and the overhang check "found" 360 mm2 of unsupported roof on a part
  whose only real overhang was 240 mm2 of arm underside. The symptom reads exactly like a broken
  overhang check. One `_extrude` helper now DERIVES the orientation from the profile's signed
  area, and has its own test: a unit cube must have six 1 mm2 faces with an area-weighted normal
  summing to zero.
- `[fix]` **And that helper fan-triangulated a CONCAVE profile**, which is invalid -- a fan only
  works from a vertex that sees the whole shape, and the L has a reflex corner. It emitted a
  triangle lying partly outside the shape and wound backwards, 300 mm2 of it. Printed upright
  that was invisible, because cap faces point along Y and are never overhangs in a Z build;
  printed on its side it appeared as 300 mm2 of phantom unsupported area. **It would also have
  broken ray-parity voxelisation**, which counts crossings and cannot survive a face pointing the
  wrong way, so the thin-wall numbers on that fixture were suspect too. Replaced with ear
  clipping, and the fixture test now checks a concave profile explicitly.

### The third thing FreeCAD's bundled scipy cannot do

- `[fix]` **`scipy.ndimage` does not import inside FreeCAD 1.0.** It pulls in `scipy.special`,
  which raises `ValueError: All ufuncs must have type numpy.ufunc` from `_multiufuncs` because
  the bundled scipy and numpy disagree about what a ufunc is. That is the **third** distinct
  failure the same ABI mismatch has caused here, after the SuperLU direct solver and the
  recursive `dtype.__repr__` that ruled out ARPACK. So the morphology is implemented in about
  forty lines of numpy array shifting -- spherical structuring element, 6-neighbour flood fill --
  and produces numbers identical to the scipy version on every test while working in both
  interpreters. `manufacture.py` is now numpy-only like the rest of the tier.

**325 tests across 17 suites.**

## 2026-10-17 (last) — linear buckling, the failure a stress check cannot see (closes #1)

This mattered more the day the optimizer shipped than it did the day before. "The lightest design
that passes stress and deflection" pushes straight at thin walls and slender columns, and a linear
static solve is perfectly content with a strut that would fold the instant it was loaded. A stress
check is **blind** to stability: the member sits nowhere near its allowable right up until it
collapses. Measured on a real strut in this very changelog entry: **stress safety factor 19, and
within a factor of two of buckling.**

- `[feat]` **`hex8.geometric_stiffness`** and **`fea/buckling.py`**: solve the static problem,
  build the geometric stiffness from the stress state, find the smallest `lambda` at which
  `K + lambda*Kg` loses positive-definiteness. Because `Kg` is linear in the stress, that
  `lambda` is a load **factor** -- 1.0 means the applied load is exactly critical.
- `[feat]` **Verified three independent ways**, which an eigenvalue solver needs more than most
  code because its errors look plausible:

  | | result |
  |---|---|
  | against Euler `pi^2 EI/(2L)^2`, fixed-free | **1.066x** at 3 elements across, converging **1.149 / 1.066 / 1.037** from above as it refines |
  | the `I` power law, width doubled | critical load **x15.0** against Euler's 16 (the rest is shear) |
  | against **CalculiX `*BUCKLE`** on the same column | CalculiX **0.999x** Euler, ours **1.034x**; the two within 3.4% |
  | inside FreeCAD, a 10 mm round shaft | factor **2.1427** against Euler's **2.136**, and the two degenerate modes of a round section identical as they must be |

- `[feat]` **`analyse(..., buckling=N)`** and a constrainable `buckling_factor`, so the optimizer
  can be told to keep a margin. The demonstration is the point: the same strut, same load,
  optimised for mass, comes out **6 mm unconstrained and 8 mm with a 2x buckling margin.**
- `[test]` **The sign convention is tested against physics, not against a comment.** Tension
  stiffens (a guitar string is harder to deflect the tighter it is wound) and compression softens,
  so the quadratic form of `Kg` must be positive under one and negative under the other. A sign
  error there inverts every answer this module gives and is otherwise invisible.
- `[feat]` **Mode localisation is measured**, as the energy fraction in the busiest 5% of nodes. A
  staircased boundary turns surface voxels into tiny cantilevers that buckle at absurd factors
  while the structure is fine, so a localised mode is flagged SUSPECT -- the alternative is
  quietly reporting the buckling load of one voxel as the buckling load of the machine.
- `[feat]` **"It does not buckle" is a first-class answer.** A member in net tension has no
  positive factor, and that is reported as such rather than as a solver failure -- which is what
  it looked like before, because ARPACK hunting for an eigenvalue that does not exist runs to its
  iteration limit and complains about convergence.
- `[feat]` Every report says **linear buckling is an UPPER BOUND** on a perfect member, perfectly
  loaded. A real printed column with a bowed wall and an eccentric load fails below it, often well
  below. In the report rather than in a doc, because that is the one thing a reader must not
  forget.

### ARPACK does not work here, twice, for two unrelated reasons

Both environmental, both recorded at `_largest_eigenpairs` so nobody spends the afternoon again:

- `eigsh(B, M=K, Minv=...)` -- scipy's generalised symmetric mode re-wraps a supplied `Minv`, and
  with a LinearOperator in that slot it **recursed until the interpreter gave up**:
  `maximum recursion depth exceeded`, from an eigensolver, on a correctly posed problem.
- `eigs` on the collapsed operator `x -> K^-1 B x` -- the right reformulation, since its
  eigenvalues *are* the pencil's -- dies **inside FreeCAD before a single matvec**, because
  FreeCAD 1.0 bundles a numpy whose `numpy.core._dtype.__repr__` recurses through `issubdtype`
  and scipy's ARPACK wrapper formats a dtype on the way in. Nothing to do with this code and
  nothing this code can fix.

- `[decision]` So: **subspace iteration in pure numpy**, which is what conventional buckling
  solvers have used for forty years and suits the problem exactly -- near-degenerate modes are
  handled naturally, which matters because a square column has two almost identical first modes
  and single-vector power iteration would thrash between them. One code path that works in both
  interpreters beats a fast path that works in one. It costs 34 s on 6300 DOF inside FreeCAD,
  where every inner solve is a CG, and that is the honest price.

### And a footgun removed from the whole tier

- `[fix]` **`method="auto"` now probes whether the direct solver actually works.** FreeCAD 1.0.2
  pairs numpy 1.26.4 with a scipy whose compiled SuperLU was built against a different numpy ABI,
  so every direct solve in there fails with `argument 3 must be numpy.ndarray, not numpy.ndarray`.
  The workbench bridge knew and passed `method="cg"` explicitly, so the bridge worked -- **and
  `auto` stayed broken in there for every other caller**, which is exactly how the buckling solver
  failed the first time it ran inside FreeCAD. The probe now lives in `fea/voxel.py` where `auto`
  can respect it. A capability check that only one caller knows about is a trap for the next one.

**308 tests across 16 suites.**

## 2026-10-17 (later) — the loop closes: parameters to performance to better parameters (closes #16, #17)

The piece everything else was built toward, and deliberately the smallest piece. The hard parts
were already done: a parametric source of truth means a design *has* parameters to vary, and a
cross-validated physics tier means varying them produces a number you can believe.

- `[feat]` **`constraint_kit/optimise.py`** — compass search with a feasibility filter, plus
  `minimise_monotone` (bisection) and `minimise_sized` (nested). Nothing in it imports CAD or
  FEA: `evaluate` is any callable from parameters to metrics, which is what lets all 22 tests
  assert against closed-form optima rather than against a mesh.
- `[feat]` **`optimise` is a registered command**, so an agent can run the whole loop. On a
  120 x 24 PETG bracket, thickness and depth free on a 0.4 mm lattice, 60 N at the end,
  SF >= 1.5 and deflection <= 3 mm: **33 evaluations in 16 s**, 26 of 59 lookups cached, answer
  17.2 x 12 mm at 31.5 g, both constraints blocking, depth driven to its lower bound, and the
  document restored to exactly where it started.
- `[feat]` **The evaluation cache (#16)** is not a micro-optimisation, it is most of the speed: a
  compass search steps out, fails, halves, and steps back over ground it has covered. Keyed on
  the quantised parameter vector, so two genuinely different designs can never share an answer.
- `[decision]` **Why a compass search and not something more impressive.** Each evaluation is
  seconds, so the method must be frugal and no gradient is available at any price; the space is
  a handful of dimensions, so a population method would spend its budget on diversity nobody
  needs; and bounds and quantisation are hard constraints, because a wall cannot be 1.7 mm when
  the nozzle lays 0.4 mm lines and an optimizer that returns 1.73 mm has not answered the
  question.
- `[feat]` **It never claims to be global.** `report()` says "no improving move from here, at
  this step size" every time, not only when it happens to be wrong. There is a test that traps
  the search in the shallower of two wells on purpose and asserts the wording.

### Four real defects found by using it, each now a test

- `[fix]` **A pure coordinate search stalls on a curved constraint boundary.** Minimise `b*h`
  subject to `b*h^2/6 >= S` and it stops at area 170 against a true 80, because every axis move
  either raises the area or breaks the constraint while the improving direction runs *along* the
  boundary at `db/dh = -12S/h^3`. Diagonals (added up to 4 parameters, since 2^n) help and do not
  fix it. **`minimise_sized` fixes it** by reducing the dimension: one parameter is bisected to
  the boundary for every setting of the others, so every point the outer search sees is already
  a sized design. Exact closed-form answer, and a warm start — resuming the inner bisection from
  the previous boundary — takes it from 484 evaluations to 349 with the same answer to every
  digit. The stall itself is now a test, asserting the plain search still returns a *valid*
  design and still says "local".
- `[fix]` **A BINARY constraint carries no gradient, and the filter got it backwards.**
  `linear_valid >= 1` is 0 or 1, so every infeasible point has violation exactly 1.0, the filter
  cannot order them, and the objective decides — sending the walk to the lightest *infeasible*
  design. Measured: a 160 mm plate feasible only above 20 mm, started at 14, walked
  14 -> 10 -> 6 -> 4 and **refused a problem whose answer was 20**. Fixed by a **bound scan**:
  2n evaluations at the box extremes before the walk, which are the points a person would try
  first.
- `[fix]` **`mass_g` was coming from the voxel count, which is a staircase.** At 4 mm pitch a
  plate 7 mm thick and one 10 mm thick sample to the same 300 elements and weigh the same to the
  gram — so the objective was a step function and the search stopped on a plateau. Worse, that
  number was being *quoted*, and it is up to 10% out (+10.7% measured on the gantry plate). Mass
  now comes from the exact B-rep volume, which is free and smooth; `voxel_mass_g` and
  `voxel_volume_error_pct` are reported beside it, because their difference is a mesh-quality
  signal worth having.
- `[fix]` **"Which constraint binds" needed two different answers, and a slack threshold is
  neither.** On a quantised evaluation the optimum sits on a tread, strictly inside the
  constraint — measured 21% inside a safety factor it was unambiguously limited by. A 10% window
  called that unconstrained; a 30% window would call almost anything constrained; neither is a
  fact about the design. So: `active_constraints` is exact tightness (the mathematical notion,
  often legitimately empty), and **`blocking_constraints` is read off the history** — every
  point tried with a better objective was infeasible, so what those points violate is exactly
  what stands between here and a better design. Ordered by frequency, so the first is the
  dominant blocker. Evidence rather than a threshold.

### And two honesty guards

- `[feat]` **The command warns when the mesh is too coarse for the tolerance.** A 0.05 mm search
  tolerance against a 4 mm pitch is chasing a plateau, and the answer is resolved to about one
  pitch. Said, not enforced — a coarse search is a legitimate choice — but never left for the
  reader to discover.
- `[feat]` **Booleans are reported as 1.0 / 0.0 so they can be CONSTRAINED**, and
  `linear_valid >= 1.0` is the one that matters: "lightest" pushes straight at the thin designs
  where the linear solve no longer holds, and without that constraint the winner is very often
  exactly such a design. There is a test where the two runs differ by nothing but that
  constraint and the answers differ accordingly.
- `[feat]` **`optimise` is declared read-only and keeps it.** Every trial parameter is restored
  in a `finally`, including on the exception paths, because a search that leaves the model at
  its last trial value has edited the document while reporting a verdict.

**291 tests across 15 suites.**

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
