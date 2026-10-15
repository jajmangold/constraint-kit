#!/usr/bin/env python3
"""The closed loop: parameters -> performance -> better parameters (issues #16, #17).

    python3 tests/test_optimise.py

Every assertion here is against an objective whose optimum is known in closed form, with no CAD
and no FEA in sight. That is deliberate and it is the only way this can be tested honestly: if
the optimizer were only ever exercised through a voxel solve, a test could not tell the
difference between a bad search and a bad mesh.

The search is a **compass (pattern) search with a feasibility filter**, chosen for what this
problem actually is rather than for what is fashionable:

* **Each evaluation is expensive** -- seconds, because it is a mesh and a linear solve -- so the
  method has to be frugal with them, and a gradient is not available at any price.
* **The space is small**, a handful of real dimensions, so a population method would spend its
  budget on diversity nobody needs.
* **Bounds and quantisation are hard constraints, not penalties.** A wall cannot be 1.7 mm when
  the nozzle lays 0.4 mm lines, and an optimizer that returns 1.73 mm has not answered the
  question. Compass search handles a bounded, quantised lattice natively.
* **It must be able to start infeasible and walk in**, because the honest starting point for
  "make this part lighter" is usually a part that already fails something.

And the thing it must never do is flatter itself. A pattern search finds a LOCAL optimum; on a
multi-modal objective it can be fooled, and there is a test below that fools it on purpose and
asserts the report says so. An optimizer that reports "optimal" is lying; one that reports "no
improving move from here, at this step size" is telling the truth.
"""
from __future__ import annotations

import math
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from constraint_kit.optimise import (  # noqa: E402
    Constraint, InfeasibleBox, Parameter, minimise, minimise_monotone, minimise_sized,
)

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


# ------------------------------------------------------------------ an analytic cantilever

# A rectangular cantilever, 120 mm long, 20 mm wide, PETG-ish. Mass goes as thickness; stress
# goes as 1/t^2 and deflection as 1/t^3, both in closed form, so the optimum is exact.
L, B, LOAD, E, ALLOW = 120.0, 20.0, 60.0, 2000.0, 15.0


def beam(x: dict) -> dict:
    t = x["thickness"]
    I = B * t ** 3 / 12.0
    return {
        "mass_g": B * L * t * 1.27e-3,
        "stress_mpa": LOAD * L * (t / 2.0) / I,
        "deflection_mm": LOAD * L ** 3 / (3.0 * E * I),
        "safety_factor": ALLOW / (LOAD * L * (t / 2.0) / I),
    }


def _t_for_sf_one() -> float:
    """Closed form: SF = 1 when 6*P*L/(B*t^2) = ALLOW."""
    return math.sqrt(6.0 * LOAD * L / (B * ALLOW))


@test
def test_the_lightest_beam_that_passes_is_the_one_where_the_constraint_is_exactly_tight():
    """The defining property of a constrained optimum: the binding constraint is active.

    Mass increases with thickness and safety factor increases with thickness, so the lightest
    safe beam is the thinnest safe beam, and that is where SF == 1 exactly. Known in closed
    form, which makes this an equality test rather than a plausibility one.
    """
    exact = _t_for_sf_one()
    out = minimise(
        beam,
        [Parameter("thickness", low=2.0, high=40.0)],
        objective="mass_g",
        constraints=[Constraint("strength", "safety_factor", ">=", 1.0)],
        tol=1e-4)

    assert out.best.feasible, out.report()
    got = out.best.x["thickness"]
    assert abs(got - exact) < 1e-3, (
        "optimum %.6f mm against the closed-form %.6f mm" % (got, exact))
    assert abs(out.best.metrics["safety_factor"] - 1.0) < 1e-4, out.best.metrics
    assert out.active_constraints == ["strength"], out.active_constraints


@test
def test_the_binding_constraint_is_named_and_it_is_the_design_insight():
    """Which constraint is tight is the useful output, more than the number itself.

    The same beam against a strength limit and against a stiffness limit optimises to different
    thicknesses, and knowing WHICH one stopped you is what tells you what to change: a stiffness
    bound says add depth or a rib, a strength bound says add material or change material.
    """
    strength = minimise(beam, [Parameter("thickness", 2.0, 40.0)], objective="mass_g",
                        constraints=[Constraint("strength", "safety_factor", ">=", 1.0)],
                        tol=1e-4)
    stiffness = minimise(beam, [Parameter("thickness", 2.0, 40.0)], objective="mass_g",
                         constraints=[Constraint("strength", "safety_factor", ">=", 1.0),
                                      Constraint("stiffness", "deflection_mm", "<=", 1.0)],
                         tol=1e-4)

    assert strength.active_constraints == ["strength"], strength.active_constraints
    assert stiffness.active_constraints == ["stiffness"], stiffness.active_constraints
    # The evidence-based reading agrees on a smooth problem, which is the check that the two
    # notions are two views of one thing rather than two different answers. It is the FIRST
    # entry that is the claim: the list is ordered by how often each constraint refused a better
    # design, and more than one can appear -- the low bound of the box violates both, so strength
    # legitimately blocked something too. The dominant blocker is the design insight.
    assert strength.blocking_constraints[0] == "strength", strength.blocking_constraints
    assert stiffness.blocking_constraints[0] == "stiffness", stiffness.blocking_constraints
    assert stiffness.best.x["thickness"] > strength.best.x["thickness"], (
        "a deflection limit of 1 mm must bind harder than a safety factor of 1 here")
    assert "stiffness" in stiffness.report(), stiffness.report()


@test
def test_blocking_constraints_are_read_off_what_the_search_was_refused():
    """Evidence, not a slack threshold -- and the difference shows on a quantised problem.

    A deliberately staircased evaluation: thickness is rounded to 2 mm before the physics, the
    way a voxel mesh rounds it. The optimum then sits well inside the constraint (there is no
    design ON it that the lattice can express), so no slack threshold identifies strength as the
    limit -- but every lighter design the search tried was refused by strength, which settles it.
    """
    def staircase(x: dict) -> dict:
        t = 2.0 * math.floor(x["thickness"] / 2.0)      # the mesh sees whole treads only
        t = max(t, 2.0)
        I = B * t ** 3 / 12.0
        return {"mass_g": B * L * x["thickness"] * 1.27e-3,
                "safety_factor": ALLOW / (LOAD * L * (t / 2.0) / I)}

    out = minimise(staircase, [Parameter("thickness", 4.0, 30.0)], objective="mass_g",
                   constraints=[Constraint("strength", "safety_factor", ">=", 1.5)], tol=1e-3)
    assert out.best.feasible, out.report()
    slack = out.best.metrics["safety_factor"] / 1.5 - 1.0
    assert slack > 0.05, (
        "this fixture needs the optimum to sit WELL inside the constraint, or it does not test "
        "the distinction: slack %.3f" % slack)
    assert out.active_constraints == [], (
        "nothing is exactly tight here, by construction: %s" % out.active_constraints)
    assert out.blocking_constraints == ["strength"], out.blocking_constraints
    assert "strength" in out.report(), out.report()


@test
def test_a_monotone_single_parameter_problem_is_bisected_not_searched():
    """When the physics is monotone, bisection is exact and needs a fraction of the evaluations.

    Thicker is both stronger and heavier, which is the common shape of a real sizing problem.
    Bisection on the constraint boundary then beats a pattern search outright, and this asserts
    both that the answers agree and that the cost does not.
    """
    exact = _t_for_sf_one()
    bisect = minimise_monotone(
        beam, Parameter("thickness", 2.0, 40.0), objective="mass_g",
        constraints=[Constraint("strength", "safety_factor", ">=", 1.0)], tol=1e-6)
    search = minimise(
        beam, [Parameter("thickness", 2.0, 40.0)], objective="mass_g",
        constraints=[Constraint("strength", "safety_factor", ">=", 1.0)], tol=1e-6)

    assert abs(bisect.best.x["thickness"] - exact) < 1e-5, bisect.best.x
    assert abs(bisect.best.x["thickness"] - search.best.x["thickness"]) < 1e-4
    assert bisect.evaluations < search.evaluations, (bisect.evaluations, search.evaluations)
    # Both land ON the boundary to within tolerance, which is the property that matters; which
    # of them lands a few parts in a billion closer is noise, and an earlier version of this
    # test asserted bisection always won that and failed against entirely correct code.
    for out in (bisect, search):
        assert abs(out.best.metrics["safety_factor"] - 1.0) < 1e-5, (out.method, out.best.metrics)
    assert bisect.best.feasible and bisect.converged, bisect.report()
    assert "bisect" in bisect.method, bisect.method


@test
def test_bisection_refuses_a_problem_it_cannot_prove_monotone():
    """A non-monotone constraint silently bisected would return a confident wrong answer."""
    def folded(x: dict) -> dict:
        t = x["thickness"]
        # deliberately non-monotone: passes in a band and fails on both sides of it
        return {"mass_g": t, "margin": 1.0 - abs(t - 10.0) / 3.0}

    try:
        minimise_monotone(folded, Parameter("thickness", 2.0, 40.0), objective="mass_g",
                          constraints=[Constraint("band", "margin", ">=", 0.0)], tol=1e-4)
    except ValueError as exc:
        assert "monoton" in str(exc).lower(), str(exc)
        return
    raise AssertionError("a non-monotone constraint must not be bisected")


# ------------------------------------------------------------------------- two parameters

@test
def test_a_two_parameter_optimum_is_found_where_the_closed_form_puts_it():
    """Minimise area subject to a fixed section modulus: the optimum is on the constraint.

    For `b*h` minimised subject to `b*h^2/6 >= S` with `h` bounded above, the solution pushes `h`
    to its ceiling and then takes the smallest `b` that satisfies the constraint -- because area
    grows linearly in both while the section modulus grows quadratically in `h`. That is a
    closed-form answer about the geometry, not about this code.

    It needs `minimise_sized` rather than `minimise`, and the test below says why: the optimum
    lies on a CURVED constraint boundary, where the improving direction is neither axial nor
    diagonal, and a pattern search stalls on it.
    """
    S, H_MAX = 400.0, 30.0

    def section(x: dict) -> dict:
        b, h = x["b"], x["h"]
        return {"area": b * h, "modulus": b * h ** 2 / 6.0}

    out = minimise_sized(section,
                         [Parameter("b", 2.0, 40.0), Parameter("h", 5.0, H_MAX)],
                         size="b", objective="area",
                         constraints=[Constraint("bending", "modulus", ">=", S)],
                         tol=1e-4)

    assert out.best.feasible, out.report()
    assert abs(out.best.x["h"] - H_MAX) < 1e-3, (
        "depth should be driven to its ceiling: %.4f" % out.best.x["h"])
    expect_b = 6.0 * S / H_MAX ** 2
    assert abs(out.best.x["b"] - expect_b) < 2e-3, (out.best.x["b"], expect_b)
    assert abs(out.best.metrics["modulus"] - S) < 1e-2, out.best.metrics


@test
def test_a_pattern_search_stalls_on_a_curved_boundary_and_says_so():
    """The limitation that `minimise_sized` exists to work around, recorded as a test.

    On the same section problem, a plain compass search cannot reach the optimum: at a point on
    the curved constraint boundary, every axis move either raises the area or breaks the
    constraint, and the diagonals improve it only marginally before the step halves below
    usefulness. The improving direction runs ALONG the boundary at a ratio (db/dh = -12S/h^3)
    that no fixed direction set contains.

    The point of this test is not that the stall happens -- it is that the result is honest
    about it. It returns a feasible, correct, local optimum and the report says "local". A
    result that silently claimed this was the best design would be the real failure.
    """
    S, H_MAX = 400.0, 30.0

    def section(x: dict) -> dict:
        b, h = x["b"], x["h"]
        return {"area": b * h, "modulus": b * h ** 2 / 6.0}

    plain = minimise(section, [Parameter("b", 2.0, 40.0), Parameter("h", 5.0, H_MAX)],
                     objective="area",
                     constraints=[Constraint("bending", "modulus", ">=", S)], tol=1e-4)
    nested = minimise_sized(section, [Parameter("b", 2.0, 40.0), Parameter("h", 5.0, H_MAX)],
                            size="b", objective="area",
                            constraints=[Constraint("bending", "modulus", ">=", S)], tol=1e-4)

    assert plain.best.feasible, "the stalled answer must still be a valid design"
    assert plain.best.objective > nested.best.objective * 1.5, (
        "this fixture is meant to show a big gap: %.2f against %.2f"
        % (plain.best.objective, nested.best.objective))
    assert "local" in plain.report().lower(), plain.report()

    # And the warm start is worth measuring rather than asserting: the inner boundary moves only
    # slightly as the outer shape changes, so resuming from the last one should cost a fraction
    # of rescanning. Both must reach the same answer, or the shortcut is not a shortcut.
    cold = minimise_sized(section, [Parameter("b", 2.0, 40.0), Parameter("h", 5.0, H_MAX)],
                          size="b", objective="area",
                          constraints=[Constraint("bending", "modulus", ">=", S)],
                          tol=1e-4, warm_start=False)
    warm = minimise_sized(section, [Parameter("b", 2.0, 40.0), Parameter("h", 5.0, H_MAX)],
                          size="b", objective="area",
                          constraints=[Constraint("bending", "modulus", ">=", S)],
                          tol=1e-4, warm_start=True)
    assert abs(warm.best.objective - cold.best.objective) < 1e-3, (
        "the warm start changed the answer, which makes it a bug rather than a shortcut: "
        "%.6f against %.6f" % (warm.best.objective, cold.best.objective))
    assert warm.evaluations < 0.75 * cold.evaluations, (
        "the warm start saved little: %d against %d" % (warm.evaluations, cold.evaluations))
    print("      warm %d evaluations against cold %d, same answer"
          % (warm.evaluations, cold.evaluations))


@test
def test_it_can_start_infeasible_and_walk_into_the_feasible_region():
    """The honest starting point for "make this lighter" is usually a part that already fails."""
    out = minimise(beam, [Parameter("thickness", 2.0, 40.0)], objective="mass_g",
                   constraints=[Constraint("strength", "safety_factor", ">=", 1.0)],
                   start={"thickness": 2.0},        # fails badly
                   tol=1e-4)
    assert out.best.feasible, out.report()
    assert out.history[0].feasible is False, "the fixture must actually start infeasible"
    assert abs(out.best.x["thickness"] - _t_for_sf_one()) < 1e-3, out.best.x


# ------------------------------------------------------------------------- honest refusals

@test
def test_a_binary_constraint_does_not_send_the_search_the_wrong_way():
    """A 0/1 constraint carries no gradient, and the filter alone gets it backwards.

    Every infeasible point has violation exactly 1.0, so the filter cannot order them and the
    objective decides -- which walks to the lightest INFEASIBLE design and then reports the
    problem impossible. Measured on exactly this shape before the bound scan existed: feasible
    only above 20, started at 14, walked 14 -> 10 -> 6 -> 4 and refused a problem whose answer
    was 20.

    The bound scan is what fixes it: the high bound is evaluated before the walk, found
    feasible, and the walk then comes DOWN to the lightest feasible design.
    """
    def binary(x: dict) -> dict:
        t = x["thickness"]
        return {"mass_g": t, "valid": 1.0 if t >= 20.0 else 0.0}

    out = minimise(binary, [Parameter("thickness", 4.0, 24.0, step=2.0)], objective="mass_g",
                   constraints=[Constraint("validity", "valid", ">=", 1.0)],
                   start={"thickness": 14.0}, tol=0.5)
    assert out.best.feasible, out.report()
    assert abs(out.best.x["thickness"] - 20.0) < 1e-9, (
        "the lightest feasible lattice point is 20; got %s" % out.best.x)
    assert out.blocking_constraints == ["validity"], out.blocking_constraints


@test
def test_the_bound_scan_costs_two_evaluations_per_parameter_and_no_more():
    """It is a fixed, predictable price -- not a search that can run away."""
    calls = []

    def counted(x: dict) -> dict:
        calls.append(x["thickness"])
        return beam(x)

    minimise(counted, [Parameter("thickness", 2.0, 40.0)], objective="mass_g",
             constraints=[Constraint("strength", "safety_factor", ">=", 1.0)],
             tol=1e-4)
    # start, then both bounds, in that order
    assert calls[0] == 21.0, calls[:3]
    assert set(calls[1:3]) == {2.0, 40.0}, calls[:3]


@test
def test_an_impossible_requirement_is_refused_with_the_best_achievable():
    """"No" is a useful answer; "0.98" presented as success is not.

    Asked for a safety factor of 10 from a beam whose box tops out at 40 mm, there is no answer,
    and the useful output is the best achievable plus which constraint cannot be met -- that is
    what tells you to change the material, the depth, or the requirement.
    """
    try:
        minimise(beam, [Parameter("thickness", 2.0, 4.0)], objective="mass_g",
                 constraints=[Constraint("strength", "safety_factor", ">=", 10.0)], tol=1e-4)
    except InfeasibleBox as exc:
        text = str(exc)
        assert "strength" in text, text
        assert "4" in text, "the bound that was hit should be named: " + text
        best = exc.best
        assert best is not None and not best.feasible
        assert abs(best.x["thickness"] - 4.0) < 1e-6, (
            "the best achievable must sit on the bound it was stopped by: %s" % best.x)
        assert best.metrics["safety_factor"] < 10.0
        return
    raise AssertionError("an unachievable requirement must be refused, not approximated")


@test
def test_a_constraint_on_a_metric_the_evaluation_does_not_report_is_refused():
    """Silently skipping an unknown constraint would optimise against the wrong problem."""
    try:
        minimise(beam, [Parameter("thickness", 2.0, 40.0)], objective="mass_g",
                 constraints=[Constraint("buckling", "buckling_load", ">=", 500.0)])
    except (KeyError, ValueError) as exc:
        assert "buckling_load" in str(exc), str(exc)
        return
    raise AssertionError("a constraint naming an unreported metric must be refused")


@test
def test_an_objective_the_evaluation_does_not_report_is_refused():
    try:
        minimise(beam, [Parameter("thickness", 2.0, 40.0)], objective="cost_gbp",
                 constraints=[])
    except (KeyError, ValueError) as exc:
        assert "cost_gbp" in str(exc), str(exc)
        return
    raise AssertionError("an unreported objective must be refused")


@test
def test_bad_parameters_and_bad_constraints_are_refused():
    # Built inside the try, because `Parameter` validates in its own constructor -- an eagerly
    # built tuple of bad parameters raises before the test can catch it, which is how an earlier
    # version of this test failed while the code was entirely correct.
    for make, expect in (
            (lambda: Parameter("t", 10.0, 2.0), "low"),
            (lambda: Parameter("t", 2.0, 10.0, step=-1.0), "step"),
            (lambda: Parameter("t", 2.0, 10.0, step=50.0), "range"),
            (lambda: Parameter("", 2.0, 10.0), "name")):
        try:
            minimise(beam, [make()], objective="mass_g", constraints=[])
        except ValueError as exc:
            assert expect in str(exc).lower(), (expect, str(exc))
            continue
        raise AssertionError("a parameter failing on %r should have been refused" % expect)
    try:
        Constraint("c", "safety_factor", "~=", 1.0)
    except ValueError as exc:
        assert "~=" in str(exc) or "operator" in str(exc).lower(), str(exc)
    else:
        raise AssertionError("an unknown operator must be refused")


@test
def test_an_evaluation_that_raises_is_treated_as_infeasible_not_fatal():
    """A geometry that will not build is a real outcome in a sizing sweep, not a crash.

    But it must be RECORDED as such: a silently skipped failure would let the search wander
    through a region nobody knows is unbuildable.
    """
    # Fails below 10 mm, and the optimum is at 12 mm, so the search genuinely probes into the
    # unbuildable region on its way down and the failures have to be counted rather than lost.
    def fragile(x: dict) -> dict:
        if x["thickness"] < 10.0:
            raise RuntimeError("the boolean failed at this thickness")
        return beam(x)

    out = minimise(fragile, [Parameter("thickness", 2.0, 40.0)], objective="mass_g",
                   constraints=[Constraint("strength", "safety_factor", ">=", 1.0)],
                   start={"thickness": 20.0}, tol=1e-3)
    assert out.best.feasible, out.report()
    assert out.best.x["thickness"] >= 10.0 - 1e-9
    assert out.failures > 0, "the failed evaluations must be counted, not discarded"
    assert "fail" in out.report().lower(), out.report()


# --------------------------------------------------------------------------- the cache (#16)

@test
def test_a_repeated_parameter_vector_is_never_evaluated_twice():
    """Issue #16, and it is not an optimisation of the optimizer -- it is most of its speed.

    A compass search revisits points constantly: it steps out, fails, halves the step, and steps
    back over ground it has already covered. Without a cache a two-parameter search re-solves the
    same mesh dozens of times.
    """
    calls = {"n": 0}

    def counted(x: dict) -> dict:
        calls["n"] += 1
        return beam(x)

    out = minimise(counted, [Parameter("thickness", 2.0, 40.0)], objective="mass_g",
                   constraints=[Constraint("strength", "safety_factor", ">=", 1.0)], tol=1e-5)
    assert calls["n"] == out.evaluations, (calls["n"], out.evaluations)
    assert out.cache_hits > 0, "a compass search must revisit points; none were cached"
    assert out.evaluations + out.cache_hits == out.lookups, out


@test
def test_the_cache_key_is_the_quantised_vector_so_it_cannot_collide():
    """Two genuinely different designs must never share a cached answer."""
    seen = []

    def recording(x: dict) -> dict:
        seen.append(round(x["thickness"], 9))
        return beam(x)

    minimise(recording, [Parameter("thickness", 2.0, 40.0)], objective="mass_g",
             constraints=[Constraint("strength", "safety_factor", ">=", 1.0)], tol=1e-5)
    assert len(seen) == len(set(seen)), (
        "the same point was evaluated twice, so the cache key is wrong: %d calls, %d distinct"
        % (len(seen), len(set(seen))))


# ------------------------------------------------------------------- quantisation and budget

@test
def test_a_quantised_parameter_lands_on_its_lattice():
    """A 1.73 mm wall is not an answer when the nozzle lays 0.4 mm lines."""
    out = minimise(beam, [Parameter("thickness", 2.0, 40.0, step=0.8)], objective="mass_g",
                   constraints=[Constraint("strength", "safety_factor", ">=", 1.0)], tol=1e-4)
    t = out.best.x["thickness"]
    assert abs((t - 2.0) / 0.8 - round((t - 2.0) / 0.8)) < 1e-9, t
    assert out.best.feasible, out.report()
    # and it must be the SMALLEST lattice point that passes
    below = t - 0.8
    assert below < 2.0 or beam({"thickness": below})["safety_factor"] < 1.0, (
        "a lighter lattice point also passes, so this is not the optimum: %.3f" % below)


@test
def test_an_exhausted_budget_reports_not_converged_rather_than_optimal():
    out = minimise(beam, [Parameter("thickness", 2.0, 40.0)], objective="mass_g",
                   constraints=[Constraint("strength", "safety_factor", ">=", 1.0)],
                   tol=1e-12, max_evaluations=6)
    assert out.evaluations <= 6, out.evaluations
    assert out.converged is False, out.report()
    assert "budget" in out.report().lower() or "not converged" in out.report().lower(), \
        out.report()


@test
def test_the_result_never_claims_to_be_a_global_optimum():
    """A pattern search finds a local optimum. Saying otherwise is the one unforgivable lie here.

    This objective has two wells and the search is started in the shallower one on purpose, so
    the returned answer is genuinely not the best. The test asserts the honest wording rather
    than a better answer, because a local method cannot give one and should not pretend to.
    """
    def two_wells(x: dict) -> dict:
        t = x["thickness"]
        # minima near 6 (value 1.0) and near 30 (value 0.2)
        shallow = 1.0 + 0.05 * (t - 6.0) ** 2
        deep = 0.2 + 0.05 * (t - 30.0) ** 2
        return {"cost": min(shallow, deep), "ok": 1.0}

    out = minimise(two_wells, [Parameter("thickness", 2.0, 40.0)], objective="cost",
                   constraints=[Constraint("sane", "ok", ">=", 1.0)],
                   start={"thickness": 5.0}, tol=1e-5)
    assert abs(out.best.x["thickness"] - 6.0) < 0.2, (
        "the fixture must trap the search in the shallow well: %s" % out.best.x)
    text = out.report().lower()
    assert "local" in text, "the report must say the optimum is local:\n" + out.report()
    assert "global" not in text or "not" in text, out.report()


@test
def test_the_same_inputs_give_the_same_answer():
    """An optimizer with a hidden random seed cannot be used to justify a design decision."""
    def run():
        return minimise(beam, [Parameter("thickness", 2.0, 40.0)], objective="mass_g",
                        constraints=[Constraint("strength", "safety_factor", ">=", 1.0)],
                        tol=1e-6)

    a, b = run(), run()
    assert a.best.x == b.best.x, (a.best.x, b.best.x)
    assert a.evaluations == b.evaluations, (a.evaluations, b.evaluations)


@test
def test_the_report_is_plain_ascii_and_names_the_numbers_it_used():
    out = minimise(beam, [Parameter("thickness", 2.0, 40.0)], objective="mass_g",
                   constraints=[Constraint("strength", "safety_factor", ">=", 1.0)], tol=1e-4)
    text = out.report()
    bad = sorted({c for c in text if ord(c) > 127})
    assert not bad, "non-ASCII %r in a console report:\n%s" % (bad, text)
    assert "thickness" in text and "mass_g" in text and "strength" in text, text
    assert "%d" % out.evaluations in text, text


def main() -> int:
    failed = []
    for fn in TESTS:
        try:
            fn()
            print("  PASS  %s" % fn.__name__)
        except Exception as exc:  # noqa: BLE001
            failed.append(fn.__name__)
            print("  FAIL  %s: %s" % (fn.__name__, exc))
            traceback.print_exc()
    print("\n%d/%d passed" % (len(TESTS) - len(failed), len(TESTS)))
    if failed:
        print("FAILED: %s" % failed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
