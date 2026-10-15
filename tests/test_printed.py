#!/usr/bin/env python3
"""Printed parts are anisotropic, and von Mises does not know that (issue #2).

    python3 tests/test_printed.py

An FDM wall is a stack of welded beads. Along a bead the polymer is continuous; across the weld
it is a diffusion bond formed in under a second against a cooling surface. Published data puts
across-layer tensile strength at roughly half the in-plane value for PETG and ABS, and interlayer
shear lower again. So the von Mises stress — which presumes a material with no preferred
direction — is the wrong number to compare against an allowable for a part that was printed.

What decides whether a printed part survives is the traction on the LAYER PLANE, split into the
component trying to peel the layers apart and the component trying to slide them over one
another. Both are recoverable from the stress tensor and neither is recoverable from an
invariant, which is why both solver tiers now keep the tensor.

Two things this module is careful NOT to pretend:

* **The de-rate factors are an assumption, not a measurement.** They are literature-typical for
  the polymer, not a property of a particular filament, nozzle temperature, layer height or
  printer. `check` therefore carries them in its output, so a safety factor computed here always
  travels with the numbers it was computed from.
* **Compression across layers is not peeling.** A weld in compression is as strong as the bulk,
  so only tensile normal traction counts against the across-layer allowable. Treating a
  compressive normal traction as a peel load would condemn parts that are perfectly fine.
"""
from __future__ import annotations

import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from constraint_kit.fea import printed  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def _uniaxial(sigma: float, axis: int = 0, n: int = 50) -> np.ndarray:
    s = np.zeros((n, 6))
    s[:, axis] = sigma
    return s


def _pure_shear(tau: float, component: int = 3, n: int = 50) -> np.ndarray:
    s = np.zeros((n, 6))
    s[:, component] = tau          # 3=sxy, 4=syz, 5=szx
    return s


# ---------------------------------------------------------------------- the traction algebra

@test
def test_tension_along_the_layers_does_not_peel_them():
    """The orientation you want: load in the plane of the welds."""
    s = _uniaxial(40.0, axis=0)                      # sigma_xx
    peel, shear = printed.traction_on_plane(s, (0, 0, 1))
    assert abs(peel).max() < 1e-12, abs(peel).max()
    assert abs(shear).max() < 1e-12, abs(shear).max()


@test
def test_tension_across_the_layers_is_entirely_peel():
    s = _uniaxial(40.0, axis=0)
    peel, shear = printed.traction_on_plane(s, (1, 0, 0))
    assert np.allclose(peel, 40.0), (peel.min(), peel.max())
    assert abs(shear).max() < 1e-12, "pure tension on a layer normal is not shear"


@test
def test_at_forty_five_degrees_the_traction_splits_exactly_in_half():
    s = _uniaxial(40.0, axis=0)
    peel, shear = printed.traction_on_plane(s, (1, 0, 1))
    assert np.allclose(peel, 20.0), (peel.min(), peel.max())
    assert np.allclose(shear, 20.0), (shear.min(), shear.max())


@test
def test_pure_shear_in_the_layer_plane_is_pure_interlayer_shear():
    """A sigma_zx shear acts on the z plane, which is the layer plane of a part printed flat."""
    s = _pure_shear(12.0, component=5)               # szx
    peel, shear = printed.traction_on_plane(s, (0, 0, 1))
    assert abs(peel).max() < 1e-12, abs(peel).max()
    assert np.allclose(shear, 12.0), (shear.min(), shear.max())


@test
def test_hydrostatic_tension_peels_every_plane_equally():
    """A sanity check with no preferred direction in it: the answer must not have one either."""
    s = np.zeros((10, 6))
    s[:, :3] = 7.0
    for normal in ((1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 1), (1, -2, 3)):
        peel, shear = printed.traction_on_plane(s, normal)
        assert np.allclose(peel, 7.0), (normal, peel.min(), peel.max())
        assert abs(shear).max() < 1e-12, (normal, abs(shear).max())


@test
def test_the_normal_is_normalised_and_the_zero_vector_refused():
    s = _uniaxial(40.0, axis=0)
    a, _ = printed.traction_on_plane(s, (1, 0, 0))
    b, _ = printed.traction_on_plane(s, (17.0, 0, 0))
    assert np.allclose(a, b), "the normal must be normalised, not taken at face value"
    for bad in ((0, 0, 0), (0.0, 0.0, 0.0)):
        try:
            printed.traction_on_plane(s, bad)
        except ValueError:
            continue
        raise AssertionError("the zero vector is not a plane normal")
    try:
        printed.traction_on_plane(np.zeros((4, 3)), (0, 0, 1))
    except ValueError as exc:
        assert "6" in str(exc), str(exc)
    else:
        raise AssertionError("a (n, 3) array is not a stress tensor")


@test
def test_a_shear_magnitude_is_never_negative():
    """|t|^2 - direct^2 is non-negative in exact arithmetic and can dip below it in floats."""
    rng = np.random.default_rng(0)
    s = rng.normal(scale=50.0, size=(4000, 6))
    for _ in range(5):
        n = rng.normal(size=3)
        _, shear = printed.traction_on_plane(s, n)
        assert np.all(shear >= 0.0), shear.min()


# -------------------------------------------------------------------------- the check itself

@test
def test_compression_across_the_layers_is_not_a_peel_load():
    """A weld in compression is as strong as the bulk. Counting it would condemn good parts."""
    s = _uniaxial(-40.0, axis=2)                     # 40 MPa COMPRESSION across the z layers
    c = printed.check(s, (0, 0, 1), in_plane_allowable=15.0)
    assert c.peel == 0.0, c.peel
    assert c.sf_peel == float("inf"), c.sf_peel
    assert c.governing == float("inf"), c.report()
    assert "compression" in c.report().lower(), c.report()


@test
def test_the_check_reports_the_governing_mode_and_which_one_it_is():
    s = _uniaxial(10.0, axis=2)                      # 10 MPa tension across the layers
    c = printed.check(s, (0, 0, 1), in_plane_allowable=15.0)
    assert abs(c.peel - 10.0) < 1e-9, c.peel
    assert abs(c.allow_peel - 7.5) < 1e-9, c.allow_peel     # 0.50 of 15
    assert abs(c.sf_peel - 0.75) < 1e-9, c.sf_peel
    assert c.sf_shear == float("inf"), c.sf_shear
    assert abs(c.governing - 0.75) < 1e-9, c.governing
    assert c.governing_mode == "peel", c.governing_mode
    assert not c.passes
    assert "peel" in c.report().lower() and "0.75" in c.report(), c.report()


@test
def test_shear_can_be_the_governing_mode():
    s = _pure_shear(8.0, component=5)                # szx, on the z layer plane
    c = printed.check(s, (0, 0, 1), in_plane_allowable=15.0)
    assert c.sf_peel == float("inf"), c.sf_peel
    assert abs(c.allow_shear - 15.0 * printed.LAYER_FACTORS["shear"]) < 1e-9, c.allow_shear
    assert c.governing_mode == "shear", c.governing_mode
    assert c.governing < 1.0, c.governing


@test
def test_the_factors_travel_with_the_answer():
    """A safety factor from assumed properties must carry the assumption, or it will be quoted."""
    s = _uniaxial(10.0, axis=2)
    c = printed.check(s, (0, 0, 1), in_plane_allowable=15.0)
    assert c.factors == printed.LAYER_FACTORS, c.factors
    text = c.report()
    assert "assum" in text.lower(), "the report must say these are assumed: " + text
    assert "%.2f" % printed.LAYER_FACTORS["tensile"] in text, text

    custom = {"tensile": 0.80, "shear": 0.75}        # a well-characterised, annealed process
    c2 = printed.check(s, (0, 0, 1), in_plane_allowable=15.0, factors=custom)
    assert abs(c2.allow_peel - 12.0) < 1e-9, c2.allow_peel
    assert c2.sf_peel > c.sf_peel, "a better bond must give a better safety factor"
    assert c2.factors == custom


@test
def test_a_factor_outside_zero_to_one_is_refused():
    """Above 1.0 would claim a weld stronger than the bulk, which no FDM process does."""
    s = _uniaxial(10.0, axis=2)
    for bad in ({"tensile": 1.4, "shear": 0.5}, {"tensile": 0.0, "shear": 0.5},
                {"tensile": 0.5, "shear": -0.1}):
        try:
            printed.check(s, (0, 0, 1), in_plane_allowable=15.0, factors=bad)
        except ValueError:
            continue
        raise AssertionError("factors %s should have been refused" % bad)
    try:
        printed.check(s, (0, 0, 1), in_plane_allowable=15.0, factors={"tensile": 0.5})
    except ValueError as exc:
        assert "shear" in str(exc), str(exc)
    else:
        raise AssertionError("an incomplete factor dict should have been refused")


@test
def test_a_percentile_is_used_rather_than_the_peak():
    """Same reason as everywhere else here: a peak at one node is a mesh artefact."""
    s = _uniaxial(10.0, axis=2, n=1000)
    s[0, 2] = 500.0                                  # one outlier node
    c = printed.check(s, (0, 0, 1), in_plane_allowable=15.0)
    assert abs(c.peel - 10.0) < 1.0, c.peel
    assert c.peak_peel > 400.0, c.peak_peel          # reported, not used
    hot = printed.check(s, (0, 0, 1), in_plane_allowable=15.0, percentile=100.0)
    assert hot.peel > 400.0, hot.peel


@test
def test_the_best_orientation_is_the_one_with_the_largest_safety_factor():
    """A real decision this makes for you: which way up to print the part.

    A part loaded in one direction should be printed with its layers PARALLEL to that load, so
    the load runs along the beads rather than across the welds. `best_orientation` must find
    that by itself from the stress field, since it is the whole practical point.
    """
    s = _uniaxial(10.0, axis=1)                      # loaded along Y
    ranked = printed.best_orientation(s, in_plane_allowable=15.0)
    assert len(ranked) == 3, ranked
    worst_name, worst = ranked[-1]
    best_name, best = ranked[0]
    assert worst_name == "Y", "loading along Y must make layers normal to Y the worst choice"
    assert worst.governing < best.governing, (worst.governing, best.governing)
    assert best.governing == float("inf"), (best_name, best.governing)
    # and it must be sorted, best first
    assert [c.governing for _, c in ranked] == sorted(
        (c.governing for _, c in ranked), reverse=True)


@test
def test_every_report_is_plain_ascii():
    """A console report must survive the console it prints to.

    These run under `cmd.exe` and Git Bash on Windows, where stdout is a cp1252 code page: an
    em-dash arrives as a replacement character and the sentence it was punctuating reads as
    though something went wrong. Caught in review of real output, where
    "in the best orientation <?> print orientation is a structural decision" looked like an
    encoding failure in the analysis rather than in the prose. Prose belongs in the docstrings,
    which nobody pipes through a code page; printed output stays ASCII.
    """
    from constraint_kit.fea import crosscheck

    s = _uniaxial(10.0, axis=2)
    texts = [printed.check(s, (0, 0, 1), in_plane_allowable=15.0).report()]
    texts += [c.report() for _, c in printed.best_orientation(s, in_plane_allowable=15.0)]
    texts.append(crosscheck.Agreement(
        displacement_voxel=1.0, displacement_reference=2.0, stress_voxel=1.0,
        stress_reference=1.0, exact_displacement=1.5, n_dof_voxel=10,
        n_nodes_reference=20, n_elements_reference=5).report())
    texts.append(crosscheck.Agreement(
        displacement_voxel=3.0, displacement_reference=2.0, stress_voxel=1.0,
        stress_reference=1.0, exact_displacement=None, n_dof_voxel=10,
        n_nodes_reference=20, n_elements_reference=5).report())
    for text in texts:
        bad = sorted({c for c in text if ord(c) > 127})
        assert not bad, "non-ASCII %r in a printed report:\n%s" % (bad, text)


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
