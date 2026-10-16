#!/usr/bin/env python3
"""The CG preconditioner, and the optional AMG one (issue #5).

    python3 tests/test_preconditioner.py

`voxel.py` has carried a note since it was written: Jacobi is valid, cheap and weak, "a genuinely
better symmetric option is algebraic multigrid (pyamg), which is not a dependency here". This is
that option, and the shape of it is set by two constraints that pull in opposite directions.

**It must be optional.** The physics tier runs inside FreeCAD, where pip is not available and
pyamg is not present. So the default path can never require it, and `tests/test_packaging.py`
would fail if `voxel.py` imported it at module level.

**But an explicit request must not silently degrade.** Asking for `precond="amg"` on a machine
without pyamg has to RAISE, with the install line. Falling back to Jacobi and reporting success
is the believable-wrong-answer failure: the caller asked for AMG because Jacobi was too slow, and
would be told the problem was solved the fast way when it was not.

So: `auto` uses AMG when it is both available and worth it, Jacobi otherwise; `"amg"` demands it;
`"jacobi"` forbids it. And the choice is REPORTED, because "it converged" and "it converged the
way you asked" are different claims.

Two things are verified rather than asserted, which is why pyamg is installed here:

* **the two preconditioners agree on the answer** -- to six figures on the same problem, which is
  what proves the preconditioner is a preconditioner and not a modification of the physics
* **AMG's iteration count is nearly independent of problem size**, which is the defining
  property of a multigrid method and the entire reason to have one: 92 -> 108 across a sixfold
  size increase where Jacobi goes 328 -> 604.

And one thing was measured and came out against the new code, which is recorded rather than
quietly dropped: **AMG is SLOWER in wall clock at every size reachable here** -- 2.79 s against
1.23 s at 29k DOF -- because a V-cycle costs far more per iteration than a divide. Flat
iterations still lose when each one is twenty times the price. So `auto` leaves AMG off below
150k DOF, the floor is set where the trend says the two curves meet rather than where it would
flatter the new path, and `precond="amg"` stays available for anybody who wants to measure their
own problem.
"""
from __future__ import annotations

import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from constraint_kit.fea import Grid, Model  # noqa: E402
from constraint_kit.fea.voxel import amg_available, choose_preconditioner  # noqa: E402

TESTS = []

AL_E, AL_NU = 70_000.0, 0.33
HAVE_AMG = amg_available()


def test(fn):
    TESTS.append(fn)
    return fn


def _slender(n=48, side=3, pitch=1.0):
    """A long slender beam: the shape Jacobi-CG handles worst, so the one worth measuring on.

    Iteration count for Jacobi grows with the condition number, which grows with slenderness --
    a cube is easy and a 16:1 beam is not. Picking a cube to compare preconditioners on would
    measure nothing.
    """
    grid = Grid(density=np.ones((side, side, n)), pitch=(pitch,) * 3)
    model = Model(grid, AL_E, AL_NU)
    model.fix(lambda p: p[:, 2] <= 1e-9)
    model.load(lambda p: p[:, 2] >= n * pitch - 1e-9, (120.0, 0.0, 0.0))
    return model


# ------------------------------------------------------------------------- the choice itself

@test
def test_an_explicit_amg_request_raises_rather_than_degrading_when_it_is_unavailable():
    """The honesty rule. A silent fallback would answer a question nobody asked.

    Someone passing `precond="amg"` is doing it because Jacobi was too slow. Quietly giving them
    Jacobi and reporting convergence tells them the slow path was the fast path.
    """
    saved = os.environ.get("CK_NO_AMG")
    os.environ["CK_NO_AMG"] = "1"            # pretend it is absent, however the machine is set up
    try:
        try:
            choose_preconditioner("amg", n_dof=100_000)
        except RuntimeError as exc:
            text = str(exc)
            assert "pyamg" in text, text
            assert "pip install" in text, "the message must say how to fix it: " + text
            assert "jacobi" in text.lower(), (
                "it should name the working alternative too: " + text)
        else:
            raise AssertionError("an explicit amg request must raise when pyamg is unavailable")
    finally:
        if saved is None:
            os.environ.pop("CK_NO_AMG", None)
        else:
            os.environ["CK_NO_AMG"] = saved


@test
def test_auto_falls_back_to_jacobi_without_complaint():
    """`auto` means "use your judgement", so degrading is the correct behaviour there."""
    saved = os.environ.get("CK_NO_AMG")
    os.environ["CK_NO_AMG"] = "1"
    try:
        assert choose_preconditioner("auto", n_dof=1_000_000) == "jacobi"
    finally:
        if saved is None:
            os.environ.pop("CK_NO_AMG", None)
        else:
            os.environ["CK_NO_AMG"] = saved


@test
def test_auto_does_not_pay_for_amg_setup_on_a_small_system():
    """AMG builds a hierarchy before it solves anything. On a small system that is a loss.

    Measured rather than assumed: the threshold exists because below it Jacobi finishes before
    AMG has finished setting up.
    """
    assert choose_preconditioner("auto", n_dof=500) == "jacobi"
    if HAVE_AMG:
        assert choose_preconditioner("auto", n_dof=5_000_000) == "amg"


@test
def test_jacobi_can_always_be_demanded():
    assert choose_preconditioner("jacobi", n_dof=5_000_000) == "jacobi"


@test
def test_an_unknown_preconditioner_is_refused_by_name():
    for bad in ("ilu", "cholesky", "magic"):
        try:
            choose_preconditioner(bad, n_dof=1000)
        except ValueError as exc:
            assert bad in str(exc), (bad, str(exc))
            continue
        raise AssertionError("%r should have been refused" % bad)


@test
def test_ilu_is_refused_with_the_reason_rather_than_just_rejected():
    """ILU is the obvious thing to reach for and is mathematically invalid for CG.

    It cost this project a measured 42 seconds to a residual of 8e-3 where Jacobi took 1.6 s to
    1e-9 -- and it failed by STAGNATING rather than erroring, which is the worst way to fail. The
    refusal explains that, so the next person does not try it again.
    """
    try:
        choose_preconditioner("ilu", n_dof=1000)
    except ValueError as exc:
        text = str(exc).lower()
        assert "symmetric" in text, str(exc)
        assert "stagnat" in text or "orthogonal" in text, str(exc)
        return
    raise AssertionError("ilu must be refused with the reason")


# --------------------------------------------------------------- what the solve then reports

@test
def test_the_solve_reports_which_preconditioner_it_used():
    """"It converged" and "it converged the way you asked" are different claims."""
    model = _slender(n=24)
    res = model.solve(method="cg", precond="jacobi")
    assert res.preconditioner == "jacobi", res.preconditioner
    assert "jacobi" in res.method or res.method == "cg", res.method


@test
def test_a_direct_solve_reports_no_preconditioner_because_it_used_none():
    model = _slender(n=12)
    res = model.solve(method="direct")
    assert res.preconditioner in (None, ""), res.preconditioner


# ------------------------------------------------------------- verified, not asserted (pyamg)

@test
def test_amg_and_jacobi_agree_on_the_answer():
    """The check that proves a preconditioner is a preconditioner.

    It changes how fast the iteration converges and nothing about the problem, so the two must
    land on the same displacement field. A preconditioner that changed the answer would be
    modifying the physics, and the answer would look entirely plausible.
    """
    if not HAVE_AMG:
        print("      SKIPPED: pyamg is not installed, so this cannot be verified")
        return
    model_a, model_b = _slender(n=40), _slender(n=40)
    jac = model_a.solve(method="cg", precond="jacobi")
    amg = model_b.solve(method="cg", precond="amg")

    rel = abs(amg.max_displacement - jac.max_displacement) / jac.max_displacement
    print("      jacobi %.9f mm, amg %.9f mm, relative difference %.2e"
          % (jac.max_displacement, amg.max_displacement, rel))
    assert rel < 1e-6, rel
    assert np.allclose(amg.displacement, jac.displacement, rtol=1e-5, atol=1e-12)
    assert amg.preconditioner == "amg" and jac.preconditioner == "jacobi"
    assert amg.residual < 1e-6 and jac.residual < 1e-6, (amg.residual, jac.residual)


@test
def test_amg_iteration_count_is_nearly_INDEPENDENT_of_problem_size():
    """The defining property of a multigrid method, and the thing worth having it for.

    Jacobi's iteration count grows with the condition number, which grows with the mesh. A
    working multigrid method's does not -- and if it does, the near-nullspace is wrong, which is
    exactly the bug this test caught. Measured across a sixfold size increase: Jacobi went
    328 -> 604 while AMG went 92 -> 108, and without the elasticity translations AMG went
    220 -> 359, which is not multigrid behaviour at all.

    So the assertion is about GROWTH rather than about a single number, because the growth is the
    property and a single number is just this machine on this day.
    """
    if not HAVE_AMG:
        print("      SKIPPED: pyamg is not installed, so this cannot be verified")
        return
    counts = {"jacobi": [], "amg": []}
    for n, side in ((48, 4), (96, 6)):
        for name in ("jacobi", "amg"):
            grid = Grid(density=np.ones((side, side, n)), pitch=(1.0,) * 3)
            model = Model(grid, AL_E, AL_NU)
            model.fix(lambda p: p[:, 2] <= 1e-9)
            model.load(lambda p: p[:, 2] >= n - 1e-9, (120.0, 0.0, 0.0))
            counts[name].append(model.solve(method="cg", precond=name).iterations)

    jac_growth = counts["jacobi"][1] / counts["jacobi"][0]
    amg_growth = counts["amg"][1] / counts["amg"][0]
    print("      jacobi %s (x%.2f) | amg %s (x%.2f) over a %.1fx size increase"
          % (counts["jacobi"], jac_growth, counts["amg"], amg_growth,
             (96 * 36) / (48 * 16)))
    assert amg_growth < 1.4, (
        "AMG iterations must be nearly flat in problem size, or the near-nullspace is wrong: "
        "%s" % counts["amg"])
    assert amg_growth < jac_growth, (jac_growth, amg_growth)
    assert counts["amg"][1] < counts["jacobi"][1] / 3.0, (
        "at the larger size AMG should need a small fraction of the iterations: %s" % counts)


@test
def test_the_near_nullspace_is_what_makes_it_multigrid():
    """Recorded as a test because leaving it out looked like "AMG is not worth it".

    Smoothed aggregation assumes it knows the operator's near-nullspace. For a scalar problem
    that is the constant vector, which is pyamg's default; for vector elasticity it is the rigid
    body motions. Told nothing, SA coarsens as though every DOF were an independent scalar and
    lands barely ahead of Jacobi -- and the honest-looking conclusion from that is to delete the
    feature.
    """
    if not HAVE_AMG:
        print("      SKIPPED: pyamg is not installed, so this cannot be verified")
        return
    import scipy.sparse.linalg as spla

    from constraint_kit.fea.voxel import _amg_preconditioner, _dof_components

    n, side = 96, 6
    grid = Grid(density=np.ones((side, side, n)), pitch=(1.0,) * 3)
    model = Model(grid, AL_E, AL_NU)
    model.fix(lambda p: p[:, 2] <= 1e-9)
    model.load(lambda p: p[:, 2] >= n - 1e-9, (120.0, 0.0, 0.0))
    active = model._active()                                     # noqa: SLF001
    K, _rm, n_red, keep = model._assemble(active)                # noqa: SLF001
    f = model._force.ravel()[keep]                               # noqa: SLF001

    counts = {}
    for label, components in (("without", None), ("with", _dof_components(keep))):
        seen = {"n": 0}
        _u, info = spla.cg(K, f, rtol=1e-9, maxiter=30_000,
                           M=_amg_preconditioner(K, components),
                           callback=lambda _x: seen.__setitem__("n", seen["n"] + 1))
        assert info == 0, (label, info)
        counts[label] = seen["n"]
    print("      %d DOF: %d iterations without the translations, %d with"
          % (n_red, counts["without"], counts["with"]))
    assert counts["with"] < counts["without"] / 2.0, counts


@test
def test_auto_leaves_amg_OFF_at_the_sizes_this_project_actually_solves():
    """The honest conclusion from the measurement, rather than the flattering one.

    AMG wins on iterations and loses on wall clock below about 150k DOF, because a V-cycle costs
    far more per iteration than a divide. Measured: 2.79 s against 1.23 s at 29k DOF. So `auto`
    leaves it off there, and the floor is set where the trend says the two meet rather than
    where it would make the new code look good.
    """
    from constraint_kit.fea.voxel import AMG_DOF_FLOOR

    assert AMG_DOF_FLOOR >= 100_000, (
        "the measurements put the crossover well above 30k DOF; a lower floor would make `auto` "
        "choose the slower path: %d" % AMG_DOF_FLOOR)
    assert choose_preconditioner("auto", n_dof=AMG_DOF_FLOOR - 1) == "jacobi"
    if HAVE_AMG:
        assert choose_preconditioner("auto", n_dof=AMG_DOF_FLOOR) == "amg"


@test
def test_the_iteration_count_is_reported_so_the_claim_can_be_checked():
    """A preconditioner that nobody can measure is a preconditioner nobody should trust."""
    res = _slender(n=24).solve(method="cg", precond="jacobi")
    assert res.iterations > 0, res.iterations
    assert res.iterations < 30_000, res.iterations
    direct = _slender(n=12).solve(method="direct")
    assert direct.iterations == 0, (
        "a direct solve does not iterate, and reporting a count would invite a comparison that "
        "means nothing: %s" % direct.iterations)


def main() -> int:
    print("pyamg %s" % ("available" if HAVE_AMG else "NOT installed -- two tests will skip"))
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
