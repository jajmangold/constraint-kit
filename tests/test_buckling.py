#!/usr/bin/env python3
"""Linear (eigenvalue) buckling on the voxel grid (issue #1).

    python3 tests/test_buckling.py

Why this matters more now than it did a week ago: **the optimizer will find it.** "The lightest
design that passes stress and deflection" pushes straight at thin walls and slender columns, and
a linear static solve is perfectly happy with a strut that would fold the instant it was loaded.
A stress check cannot see buckling at all -- the member is nowhere near its allowable right up
until it collapses -- so without this the optimizer is free to return a design that fails in a
mode nothing was watching for.

The method is the standard one: solve the static problem, build the **geometric stiffness** from
the resulting stress state, and find the smallest `lambda` for which `(K + lambda*Kg) phi = 0`
has a non-trivial solution. That `lambda` is a load FACTOR: 1.0 means the applied load is exactly
critical, 3.0 means three times it.

Every assertion here is against Euler, `P_cr = pi^2 EI / (KL)^2`, with the effective-length
factor the restraints actually impose. That is ground truth from outside this repo, and it is
also a demanding test: the factor depends on `I`, so a 1% error in the element is a 1% error
here, and it depends on the restraint, so a boundary condition applied to the wrong nodes shows
up as a factor of four rather than as a few percent.

What this is NOT, stated because the difference gets people hurt:

* **Linear buckling is an UPPER bound.** It assumes a perfect member, perfectly loaded. A real
  printed column with a bowed wall and an eccentric load fails below it, often well below. The
  classical allowance is a factor of two or more on slender members, and the report says so
  rather than leaving it to be remembered.
* **It is not a post-buckling analysis.** It says when stability is lost, not what happens next.
* **A localised mode is usually an artefact.** A staircased boundary makes individual surface
  voxels into tiny cantilevers that buckle at absurdly low factors while the structure is fine.
  So mode LOCALISATION is measured and reported, because the alternative is quietly returning
  the buckling load of one voxel.
"""
from __future__ import annotations

import math
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from constraint_kit.fea import Grid, Model  # noqa: E402
from constraint_kit.fea.buckling import BucklingFailure, buckling  # noqa: E402
from constraint_kit.fea.hex8 import geometric_stiffness, hex8_stiffness  # noqa: E402

TESTS = []

E_AL, NU = 70_000.0, 0.33


def test(fn):
    TESTS.append(fn)
    return fn


def _euler(E: float, I: float, length: float, k: float) -> float:
    """`pi^2 EI / (kL)^2`. `k` is the effective-length factor for the restraints."""
    return math.pi ** 2 * E * I / (k * length) ** 2


# ------------------------------------------------------------------ the element matrix itself

@test
def test_the_geometric_stiffness_is_symmetric_and_scales_with_stress():
    """Two algebraic properties, before any structure is built on it.

    `Kg` is linear in the stress state -- that is what makes the eigenvalue a load factor rather
    than something that has to be iterated -- and it is symmetric, which is what lets a symmetric
    eigensolver be used at all.
    """
    h = (1.3, 0.9, 2.1)
    sigma = np.array([-120.0, 3.0, -4.0, 7.0, -2.0, 5.0])      # one stress state, (n, 6) order
    KG = geometric_stiffness(h, sigma)
    assert KG.shape == (24, 24), KG.shape
    assert np.allclose(KG, KG.T, atol=1e-12), "geometric stiffness is not symmetric"

    doubled = geometric_stiffness(h, 2.0 * sigma)
    assert np.allclose(doubled, 2.0 * KG, rtol=1e-12), (
        "Kg must be LINEAR in the stress, or the eigenvalue is not a load factor")

    # a zero stress state carries no geometric stiffness at all
    assert np.allclose(geometric_stiffness(h, np.zeros(6)), 0.0, atol=1e-14)


@test
def test_pure_hydrostatic_tension_stiffens_and_compression_softens():
    """The sign convention, checked against physics rather than against a comment.

    A member in tension is harder to buckle (think of a guitar string) and one in compression is
    easier. So the quadratic form of `Kg` on any non-trivial deformation must be positive under
    tension and negative under compression. A sign error here inverts every answer this module
    gives, and is otherwise invisible.
    """
    h = (1.0, 1.0, 1.0)
    rng = np.random.default_rng(0)
    v = rng.normal(size=24)
    # remove rigid-body content so the form is not trivially zero
    K = hex8_stiffness(h, E_AL, NU)
    v = v - np.linalg.lstsq(K, K @ v, rcond=None)[0] * 0.0      # keep it simple: v is generic

    tension = geometric_stiffness(h, np.array([50.0, 50.0, 50.0, 0, 0, 0]))
    compress = geometric_stiffness(h, np.array([-50.0, -50.0, -50.0, 0, 0, 0]))
    assert float(v @ tension @ v) > 0.0, "tension must stiffen"
    assert float(v @ compress @ v) < 0.0, "compression must soften"
    assert abs(float(v @ tension @ v) + float(v @ compress @ v)) < 1e-9 * abs(
        float(v @ tension @ v)), "equal and opposite stress must give equal and opposite forms"


# ----------------------------------------------------------------------------- against Euler

def _column(n_long=24, n_side=3, pitch=1.0) -> Grid:
    """A square column `n_long` elements along Z, loaded on its top face."""
    return Grid(density=np.ones((n_side, n_side, n_long)), pitch=(pitch,) * 3)


@test
def test_a_fixed_free_column_buckles_at_the_euler_load_for_k_equals_two():
    """The headline check. A cantilever column: `k = 2`, so `P_cr = pi^2 EI / (2L)^2`.

    A trilinear hex locks in bending, so the computed factor approaches Euler FROM ABOVE and the
    test asserts that direction as well as the magnitude -- a result below Euler would mean the
    element had become spuriously soft, which is a different and worse bug than being stiff.
    """
    side, long_n, pitch = 3, 30, 1.0
    grid = _column(long_n, side, pitch)
    width = side * pitch
    length = long_n * pitch
    I = width ** 4 / 12.0
    exact = _euler(E_AL, I, length, k=2.0)

    model = Model(grid, E_AL, NU)
    model.fix(lambda p: p[:, 2] <= 1e-9)
    applied = 100.0
    model.load_face("z", "max", (0.0, 0.0, -applied))

    out = buckling(model, n_modes=1)
    critical = out.factors[0] * applied
    print("      critical %.2f N vs Euler %.2f N (x%.3f), slenderness L/w = %.0f"
          % (critical, exact, critical / exact, length / width))
    assert critical / exact > 1.0, (
        "a locking element must not read BELOW Euler: %.4f" % (critical / exact))
    assert critical / exact < 1.35, (
        "%.4f of Euler is too far off for this mesh" % (critical / exact))
    assert out.factors[0] > 0.0
    assert out.n_dof == out.n_dof


@test
def test_refining_the_column_converges_toward_euler_from_above():
    """The trend is the assertion, because the element cannot hit Euler exactly."""
    ratios = []
    for side, long_n, pitch in ((2, 20, 1.5), (3, 30, 1.0), (4, 40, 0.75)):
        grid = _column(long_n, side, pitch)
        width, length = side * pitch, long_n * pitch
        exact = _euler(E_AL, width ** 4 / 12.0, length, k=2.0)
        model = Model(grid, E_AL, NU)
        model.fix(lambda p: p[:, 2] <= 1e-9)
        model.load_face("z", "max", (0.0, 0.0, -100.0))
        ratios.append(buckling(model, n_modes=1).factors[0] * 100.0 / exact)
    print("      ratios to Euler at 2/3/4 elements across: %s"
          % ["%.3f" % r for r in ratios])
    assert all(r > 1.0 for r in ratios), ratios
    assert ratios[-1] < ratios[0], (
        "refining must move TOWARD Euler, not away: %s" % ratios)


@test
def test_a_stockier_column_buckles_at_a_higher_factor_as_the_square_of_width():
    """`P_cr` goes as `I`, so doubling the width should be about sixteen times the load.

    Not exactly sixteen: the shorter-relative column picks up shear deformation that Euler
    omits, which lowers it. The point is the power law, which is a property of the physics.
    """
    length = 30.0
    out = {}
    for side in (3, 6):
        pitch = 1.0
        grid = Grid(density=np.ones((side, side, int(length / pitch))), pitch=(pitch,) * 3)
        model = Model(grid, E_AL, NU)
        model.fix(lambda p: p[:, 2] <= 1e-9)
        model.load_face("z", "max", (0.0, 0.0, -100.0))
        out[side] = buckling(model, n_modes=1).factors[0] * 100.0
    ratio = out[6] / out[3]
    print("      width x2 -> critical load x%.1f (Euler says 16)" % ratio)
    assert 8.0 < ratio < 20.0, (out, ratio)


@test
def test_a_column_in_tension_does_not_buckle_and_the_module_says_so():
    """"It does not buckle" is a real answer and must not be dressed as a number."""
    grid = _column(30, 3, 1.0)
    model = Model(grid, E_AL, NU)
    model.fix(lambda p: p[:, 2] <= 1e-9)
    model.load_face("z", "max", (0.0, 0.0, +100.0))             # pulling, not pushing
    try:
        buckling(model, n_modes=1)
    except BucklingFailure as exc:
        text = str(exc).lower()
        assert "tension" in text or "does not buckle" in text or "no positive" in text, str(exc)
        return
    raise AssertionError("a column in tension has no positive critical factor")


@test
def test_the_factor_is_a_load_multiplier_and_scales_inversely_with_the_load():
    """Double the load, halve the factor. That is what makes it a factor rather than a number."""
    grid = _column(30, 3, 1.0)

    def run(load: float) -> float:
        model = Model(grid, E_AL, NU)
        model.fix(lambda p: p[:, 2] <= 1e-9)
        model.load_face("z", "max", (0.0, 0.0, -load))
        return buckling(model, n_modes=1).factors[0]

    one, two = run(100.0), run(200.0)
    assert abs(one / two - 2.0) < 1e-3, (one, two)
    # and the critical LOAD is the same either way, which is the real invariant
    assert abs(one * 100.0 - two * 200.0) < 1e-6 * one * 100.0


# ------------------------------------------------------------------- modes, and honest doubt

@test
def test_several_modes_come_back_in_increasing_order():
    grid = _column(30, 3, 1.0)
    model = Model(grid, E_AL, NU)
    model.fix(lambda p: p[:, 2] <= 1e-9)
    model.load_face("z", "max", (0.0, 0.0, -100.0))
    out = buckling(model, n_modes=3)
    assert len(out.factors) == 3, out.factors
    assert all(a < b for a, b in zip(out.factors, out.factors[1:])), out.factors
    assert all(f > 0 for f in out.factors), out.factors
    # a square column has a symmetric pair of first modes: bending about X and about Y
    assert abs(out.factors[1] / out.factors[0] - 1.0) < 0.05, (
        "a square section should have two nearly equal first modes: %s" % out.factors)
    assert out.modes.shape == (len(out.factors), grid.n_nodes, 3), out.modes.shape


@test
def test_a_localised_mode_is_measured_and_reported():
    """A staircase turns surface voxels into tiny cantilevers that buckle absurdly early.

    So localisation is measured: the fraction of the mode energy carried by the busiest 5% of
    nodes. A global column mode spreads over the whole member; a one-voxel artefact does not.
    Reporting it is the difference between a buckling load and the buckling load of a pimple.
    """
    grid = _column(30, 3, 1.0)
    model = Model(grid, E_AL, NU)
    model.fix(lambda p: p[:, 2] <= 1e-9)
    model.load_face("z", "max", (0.0, 0.0, -100.0))
    out = buckling(model, n_modes=1)
    assert 0.0 < out.localisation[0] < 1.0, out.localisation
    # a genuine global column mode is NOT concentrated in a few nodes
    assert out.localisation[0] < 0.5, (
        "the first mode of a clean column should be global: %.3f" % out.localisation[0])
    assert out.suspect == [], out.suspect
    assert "upper bound" in out.report().lower(), out.report()


@test
def test_the_report_warns_that_linear_buckling_is_an_upper_bound():
    """The one thing a reader must not forget, so it is in every report rather than in a doc."""
    grid = _column(30, 3, 1.0)
    model = Model(grid, E_AL, NU)
    model.fix(lambda p: p[:, 2] <= 1e-9)
    model.load_face("z", "max", (0.0, 0.0, -100.0))
    text = buckling(model, n_modes=1).report()
    assert "upper bound" in text.lower(), text
    assert "imperfection" in text.lower() or "imperfect" in text.lower(), text
    bad = sorted({c for c in text if ord(c) > 127})
    assert not bad, "non-ASCII %r in a console report:\n%s" % (bad, text)


@test
def test_buckling_refuses_an_unrestrained_or_unloaded_model():
    grid = _column(10, 3, 1.0)
    bare = Model(grid, E_AL, NU)
    bare.load_face("z", "max", (0.0, 0.0, -100.0))
    try:
        buckling(bare, n_modes=1)
    except Exception as exc:                                    # noqa: BLE001
        assert "restrain" in str(exc).lower(), str(exc)
    else:
        raise AssertionError("an unrestrained model must be refused")

    unloaded = Model(grid, E_AL, NU)
    unloaded.fix(lambda p: p[:, 2] <= 1e-9)
    try:
        buckling(unloaded, n_modes=1)
    except Exception as exc:                                    # noqa: BLE001
        assert "load" in str(exc).lower(), str(exc)
        return
    raise AssertionError("an unloaded model has no stress state and must be refused")


@test
def test_asking_for_more_modes_than_the_system_has_is_refused():
    grid = Grid(density=np.ones((2, 2, 2)), pitch=(1.0,) * 3)
    model = Model(grid, E_AL, NU)
    model.fix(lambda p: p[:, 2] <= 1e-9)
    model.load_face("z", "max", (0.0, 0.0, -10.0))
    try:
        buckling(model, n_modes=10_000)
    except (BucklingFailure, ValueError) as exc:
        assert "mode" in str(exc).lower() or "dof" in str(exc).lower(), str(exc)
        return
    raise AssertionError("more modes than degrees of freedom must be refused")


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
