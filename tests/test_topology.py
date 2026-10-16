#!/usr/bin/env python3
"""SIMP topology optimisation -- an ORACLE, never a generator (issue #19).

    python3 tests/test_topology.py

The issue title is the design. What comes out of this is a **density field**, and a density field
is not a manufacturable design: it is a statement about where material earns its keep, which a
human then redraws as parametric geometry. Everything else in this project goes the other way --
parameters in, verified geometry out -- and conflating the two would be the whole doctrine
inverted. So `Topology` has no `.step()` and no `.shape()`, deliberately, and `suggestion()`
returns prose rather than a solid.

The groundwork was laid for it on purpose. `voxel.Model` has taken `density_floor` and `penal`
since the day it was written, with the comment that they exist "so a topology optimizer can reuse
this assembly unchanged" -- and it does. Nothing in `voxel.py` changed to support this.

**What is verifiable here, and it is more than it looks.** There is no closed-form optimal
topology for an arbitrary problem, so the tests assert properties of the METHOD rather than a
shape:

* the volume constraint holds **exactly**, every iteration
* compliance **falls** -- and the final design beats the uniform start at the same volume, which
  is the defining property of the whole exercise
* a **symmetric** problem gives a symmetric answer, which is exact and catches an asymmetric
  filter or an off-by-one in the element indexing
* a loaded cantilever puts material at the **top and bottom** and hollows the middle, which is
  the I-section beam theory predicts and is a real prediction rather than a tautology
* **checkerboarding appears without the filter and vanishes with it**, which tests that the
  filter does the one job it exists for
"""
from __future__ import annotations

import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from constraint_kit.fea import Grid, Model  # noqa: E402
from constraint_kit.fea.topology import Topology, checkerboard_score, topology  # noqa: E402

TESTS = []

E_AL, NU = 70_000.0, 0.33


def test(fn):
    TESTS.append(fn)
    return fn


def _cantilever(nx=20, ny=4, nz=8, pitch=1.0):
    """A design domain held at one end and pushed down at the other. The classic fixture."""
    grid = Grid(density=np.ones((nx, ny, nz)), pitch=(pitch,) * 3)
    return dict(grid=grid, E=E_AL, nu=NU,
                fix=lambda p: p[:, 0] <= 1e-9,
                load=(lambda p: (p[:, 0] >= nx * pitch - 1e-9)
                      & (np.abs(p[:, 2] - nz * pitch / 2.0) <= pitch * 0.51),
                      (0.0, 0.0, -400.0)))


# -------------------------------------------------------------------------- the hard guarantees

@test
def test_the_volume_constraint_holds_exactly_at_every_iteration():
    """Not approximately. The optimality-criterion update bisects until it does.

    A drifting volume is the failure that makes every other number meaningless: a design that
    halved its compliance by quietly using 60% of the material instead of 40% has not optimised
    anything.
    """
    fraction = 0.35
    out = topology(**_cantilever(), volume_fraction=fraction, max_iter=12)
    for i, step in enumerate(out.history):
        assert abs(step["volume_fraction"] - fraction) < 1e-6, (i, step)
    assert abs(out.density.mean() - fraction) < 1e-6, out.density.mean()


@test
def test_the_optimised_design_beats_the_uniform_one_at_the_same_volume():
    """The defining property. If this fails, nothing else about the module matters."""
    fraction = 0.35
    args = _cantilever()
    out = topology(**args, volume_fraction=fraction, max_iter=25)

    uniform = Grid(density=np.full(args["grid"].shape, fraction), pitch=args["grid"].pitch)
    model = Model(uniform, args["E"], args["nu"])
    model.fix(args["fix"])
    model.load(args["load"][0], args["load"][1])
    start = model.solve().compliance

    print("      uniform %.1f -> optimised %.1f  (%.0f%% stiffer at the same mass)"
          % (start, out.compliance, 100.0 * (start / out.compliance - 1.0)))
    assert out.compliance < start, (out.compliance, start)
    assert out.compliance < 0.8 * start, (
        "a 35%% volume cantilever should be much stiffer optimised than spread uniformly: "
        "%.1f against %.1f" % (out.compliance, start))


@test
def test_compliance_falls_over_the_run():
    out = topology(**_cantilever(), volume_fraction=0.4, max_iter=20)
    first = out.history[0]["compliance"]
    last = out.history[-1]["compliance"]
    assert last < first, (first, last)
    # and it is a descent rather than a wander: the second half must beat the first half
    half = len(out.history) // 2
    early = min(s["compliance"] for s in out.history[:half])
    late = min(s["compliance"] for s in out.history[half:])
    assert late <= early, (early, late)


@test
def test_a_symmetric_problem_gives_a_symmetric_answer():
    """Exact, and it catches an asymmetric filter or an off-by-one in element indexing.

    The domain, the restraint and the load are all symmetric about the mid-plane in Y, so the
    density field must be too -- to floating point, not to a tolerance somebody chose.
    """
    nx, ny, nz = 16, 6, 6
    grid = Grid(density=np.ones((nx, ny, nz)), pitch=(1.0,) * 3)
    out = topology(grid=grid, E=E_AL, nu=NU,
                   fix=lambda p: p[:, 0] <= 1e-9,
                   load=(lambda p: p[:, 0] >= nx - 1e-9, (0.0, 0.0, -400.0)),
                   volume_fraction=0.4, max_iter=15)
    mirrored = out.density[:, ::-1, :]
    worst = float(np.abs(out.density - mirrored).max())
    print("      worst symmetry deviation %.3e" % worst)
    assert worst < 1e-9, worst


@test
def test_a_cantilever_hollows_its_middle_and_keeps_its_flanges():
    """Beam theory's prediction, and a real one rather than a tautology.

    Bending stress goes as distance from the neutral axis, so material in the middle of the
    depth earns almost nothing and material at the top and bottom earns the most. An optimiser
    that understands the physics must rediscover the I-section -- and one that does not will
    spread material evenly or clump it somewhere arbitrary.
    """
    out = topology(**_cantilever(nx=24, ny=4, nz=10), volume_fraction=0.4, max_iter=30)
    # average density per layer through the depth
    through = out.density.mean(axis=(0, 1))
    nz = len(through)
    flanges = float(np.mean([through[0], through[-1]]))
    middle = float(np.mean(through[nz // 2 - 1:nz // 2 + 1]))
    print("      flange density %.3f, mid-depth density %.3f" % (flanges, middle))
    assert flanges > middle, (
        "bending wants material at the extreme fibres: %s" % ["%.2f" % v for v in through])
    assert flanges > 1.5 * middle, (
        "the separation should be pronounced, not marginal: %.3f against %.3f"
        % (flanges, middle))


# ---------------------------------------------------------------------------- the filter

@test
def test_checkerboarding_appears_without_the_filter_and_vanishes_with_it():
    """The one job the filter exists for, tested by turning it off.

    Unfiltered SIMP produces alternating full and empty elements -- a numerical artefact of the
    element, not a structure, and famously the first thing that goes wrong. A filter radius of
    zero must reproduce the artefact and a radius of 1.5 elements must suppress it, or the filter
    is decoration.
    """
    args = _cantilever(nx=20, ny=4, nz=8)
    raw = topology(**args, volume_fraction=0.4, filter_radius=0.0, max_iter=20)
    smooth = topology(**args, volume_fraction=0.4, filter_radius=1.5, max_iter=20)

    rough = checkerboard_score(raw.density)
    clean = checkerboard_score(smooth.density)
    print("      checkerboard score: unfiltered %.4f, filtered %.4f" % (rough, clean))
    assert rough > clean, (rough, clean)
    assert clean < 0.5 * rough, (
        "the filter must substantially suppress checkerboarding, not merely reduce it: "
        "%.4f against %.4f" % (clean, rough))


@test
def test_the_checkerboard_score_recognises_a_checkerboard():
    """The measure gets its own test, because a measure nobody checked proves nothing."""
    n = 12
    idx = np.indices((n, n, n)).sum(axis=0)
    board = (idx % 2).astype(float)
    assert checkerboard_score(board) > 0.9, checkerboard_score(board)

    uniform = np.full((n, n, n), 0.4)
    assert checkerboard_score(uniform) < 1e-9, checkerboard_score(uniform)

    # a smooth gradient is not a checkerboard, however much it varies overall
    ramp = np.repeat(np.linspace(0.1, 0.9, n)[:, None, None], n, axis=1)
    ramp = np.repeat(ramp, n, axis=2)
    assert checkerboard_score(ramp) < 0.1, checkerboard_score(ramp)


# ------------------------------------------------------------------- honesty and refusals

@test
def test_the_result_says_it_is_a_suggestion_and_offers_no_geometry():
    """An oracle, never a generator. The distinction is the whole design.

    A density field is not a manufacturable design. If this class ever grows a `.shape()` the
    doctrine has been inverted: everything else here takes parameters and emits verified
    geometry, and a grey blob emitted as geometry would be a part nobody chose.
    """
    out = topology(**_cantilever(), volume_fraction=0.4, max_iter=6)
    assert not hasattr(out, "shape"), "a Topology must not offer geometry"
    assert not hasattr(out, "step"), "a Topology must not offer an exported solid"
    text = out.suggestion()
    assert "suggestion" in text.lower() or "redraw" in text.lower(), text
    assert "not a" in text.lower(), text

    report = out.report()
    for marker in ("volume", "compliance"):
        assert marker in report.lower(), report
    bad = sorted({c for c in report if ord(c) > 127})
    assert not bad, "non-ASCII %r in a console report:\n%s" % (bad, report)


@test
def test_the_field_can_be_handed_back_to_the_solver_for_a_real_check():
    """The one legitimate thing to do with it mechanically: re-analyse it.

    `as_grid` exists so the suggestion can be measured rather than admired -- and because the
    SIMP compliance is computed on a field with intermediate densities, which is not a part.
    """
    args = _cantilever()
    out = topology(**args, volume_fraction=0.4, max_iter=10)
    grid = out.as_grid(args["grid"].pitch, args["grid"].origin)
    assert isinstance(grid, Grid)
    assert grid.shape == args["grid"].shape
    model = Model(grid, args["E"], args["nu"])
    model.fix(args["fix"])
    model.load(args["load"][0], args["load"][1])
    res = model.solve()
    assert res.max_displacement > 0.0
    assert abs(res.compliance - out.compliance) < 1e-6 * out.compliance, (
        res.compliance, out.compliance)


@test
def test_a_volume_fraction_outside_zero_to_one_is_refused():
    for bad in (0.0, 1.0, -0.2, 1.5):
        try:
            topology(**_cantilever(), volume_fraction=bad, max_iter=2)
        except ValueError as exc:
            assert "volume_fraction" in str(exc), (bad, str(exc))
            continue
        raise AssertionError("volume_fraction=%s should have been refused" % bad)


@test
def test_a_penalisation_below_one_is_refused_because_it_inverts_the_method():
    """`p < 1` rewards intermediate density instead of punishing it, so nothing ever resolves."""
    try:
        topology(**_cantilever(), volume_fraction=0.4, penal=0.5, max_iter=2)
    except ValueError as exc:
        assert "penal" in str(exc), str(exc)
        return
    raise AssertionError("penal below 1 must be refused")


@test
def test_an_exhausted_iteration_budget_reports_not_converged():
    out = topology(**_cantilever(), volume_fraction=0.4, max_iter=3, tol=1e-12)
    assert out.converged is False, out.report()
    assert len(out.history) == 3, len(out.history)
    assert "not converged" in out.report().lower(), out.report()


@test
def test_the_same_inputs_give_the_same_field():
    """No hidden seed: a design decision cannot rest on an irreproducible optimisation."""
    a = topology(**_cantilever(), volume_fraction=0.4, max_iter=8)
    b = topology(**_cantilever(), volume_fraction=0.4, max_iter=8)
    assert np.array_equal(a.density, b.density)
    assert a.compliance == b.compliance


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
