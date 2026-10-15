#!/usr/bin/env python3
"""Verification suite for the physics tier (PLAN.md Phase 0) — numpy/scipy only, no pytest:

    python tests/test_fea.py

Every assertion here is against ground truth that exists independently of this code: element
algebra (symmetry, rigid-body modes), closed-form elasticity (uniform strain, PL/AE, P/A), and
beam theory (PL^3/3EI with the Timoshenko shear term). Nothing is checked against a stored
snapshot of our own output, because a snapshot test on a solver only tells you it still does
whatever it did before.

Where the element genuinely cannot hit an analytic number -- bending, where full integration
makes a trilinear hex parasitically stiff -- the test asserts the CONVERGENCE TREND under
refinement rather than a tolerance the formulation cannot meet. A loose tolerance on a single
mesh would pass for the wrong reasons and hide a real error.
"""
from __future__ import annotations

import math
import os
import struct
import sys
import tempfile
import traceback
import warnings

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from constraint_kit.fea import (  # noqa: E402
    Grid, Model, SolveFailure, elasticity_matrix, hex8_stiffness, load_stl,
    occupancy_from_triangles,
)
from constraint_kit.fea.hex8 import NODE_NAT, hex8_b_centre  # noqa: E402

TESTS = []

AL_E, AL_NU = 70_000.0, 0.33        # aluminium, MPa
EPS = 1e-9


def test(fn):
    TESTS.append(fn)
    return fn


def box_triangles(lo, hi):
    """12-triangle closed box, for voxeliser tests without a CAD stack."""
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    v = np.array([[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
                  [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]], dtype=float)
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (2, 3, 7), (2, 7, 6), (1, 2, 6), (1, 6, 5), (0, 4, 7), (0, 7, 3)]
    return v[np.array(faces)]


# --------------------------------------------------------------------------- element algebra

@test
def test_element_stiffness_is_symmetric():
    KE = hex8_stiffness((1.0, 2.0, 3.5), AL_E, AL_NU)
    assert KE.shape == (24, 24), KE.shape
    assert np.array_equal(KE, KE.T), "element stiffness is not exactly symmetric"


@test
def test_element_has_exactly_six_rigid_body_modes():
    """A free element must have 6 zero-energy modes (3 translations, 3 rotations) and no more.

    This is the single most diagnostic element test there is: too many zero eigenvalues means a
    spurious (hourglass) mode from under-integration, too few means the element is locked, and a
    sign error anywhere in B or D moves them off zero.
    """
    KE = hex8_stiffness((1.3, 0.7, 2.1), AL_E, AL_NU)
    w = np.linalg.eigvalsh(KE)
    scale = w.max()
    n_zero = int(np.sum(np.abs(w) < 1e-9 * scale))
    assert n_zero == 6, f"expected 6 rigid-body modes, found {n_zero} (eigenvalues {w[:8]})"
    assert w[6] > 1e-6 * scale, "the 7th mode is near-zero: the element has a spurious mode"
    assert np.all(w > -1e-9 * scale), "stiffness is not positive semi-definite"


@test
def test_uniform_strain_patch_test():
    """Impose u = A x on all 8 nodes; the element must reproduce that strain and its energy exactly.

    A trilinear hex contains the complete linear field, so this is an equality test, not an
    approximation: any error here is a bug in B, D, or the quadrature.
    """
    h = (1.7, 2.3, 0.9)
    A = np.array([[0.003, 0.001, -0.002],
                  [0.0005, -0.004, 0.0015],
                  [-0.001, 0.002, 0.005]])
    coords = NODE_NAT * np.array(h) / 2.0
    ue = (coords @ A.T).ravel()

    eps = hex8_b_centre(h) @ ue
    want = np.array([A[0, 0], A[1, 1], A[2, 2],
                     A[0, 1] + A[1, 0], A[1, 2] + A[2, 1], A[0, 2] + A[2, 0]])
    assert np.allclose(eps, want, atol=1e-14), f"strain {eps} != {want}"

    D = elasticity_matrix(AL_E, AL_NU)
    KE = hex8_stiffness(h, AL_E, AL_NU)
    got = 0.5 * ue @ KE @ ue
    expect = 0.5 * math.prod(h) * (want @ D @ want)
    assert abs(got - expect) < 1e-8 * abs(expect), f"energy {got} != {expect}"


@test
def test_elasticity_matrix_refuses_impossible_poisson():
    for bad in (0.5, 0.7, -1.0, -2.0):
        try:
            elasticity_matrix(AL_E, bad)
        except ValueError:
            continue
        raise AssertionError(f"nu={bad} should have been rejected")
    try:
        elasticity_matrix(-1.0, 0.3)
    except ValueError:
        return
    raise AssertionError("negative modulus should have been rejected")


# ------------------------------------------------------------------- closed-form elasticity

def _bar(nx=20, ny=4, nz=4, pitch=1.0):
    return Grid(density=np.ones((nx, ny, nz)), pitch=(pitch,) * 3)


def _axial_bar(load=1000.0):
    """Bar in uniaxial tension on symmetry restraints, which permit free Poisson contraction.

    Clamping the whole x=0 face would suppress lateral contraction and make the exact uniform
    field inadmissible; roller restraints on the three origin planes remove the six rigid-body
    modes without adding any constraint the analytic solution violates.
    """
    grid = _bar()
    m = Model(grid, AL_E, AL_NU)
    m.fix(lambda p: p[:, 0] <= EPS, axes="x")
    m.fix(lambda p: p[:, 1] <= EPS, axes="y")
    m.fix(lambda p: p[:, 2] <= EPS, axes="z")
    m.load_face("x", "max", (load, 0.0, 0.0))
    return grid, m, m.solve()


@test
def test_axial_extension_is_exact():
    """Elongation must equal PL/AE to solver precision, not to within a few percent."""
    load = 1000.0
    grid, m, res = _axial_bar(load)
    L, A = 20.0, 16.0
    expect = load * L / (A * AL_E)

    coords = grid.node_coords()
    tip = np.flatnonzero(coords[:, 0] >= L - EPS)
    ux = res.displacement[tip, 0]
    assert np.ptp(ux) < 1e-12, f"tip face is not plane: spread {np.ptp(ux):.3e}"
    rel = abs(ux.mean() - expect) / expect
    assert rel < 1e-9, f"elongation {ux.mean():.9f} vs analytic {expect:.9f} (rel {rel:.2e})"
    assert res.method == "direct" and res.residual < 1e-10, (res.method, res.residual)


@test
def test_poisson_contraction_is_exact():
    grid, m, res = _axial_bar()
    coords = grid.node_coords()
    L, A, load = 20.0, 16.0, 1000.0
    eps_axial = load / (A * AL_E)

    far = np.flatnonzero(coords[:, 1] >= 4.0 - EPS)
    uy = res.displacement[far, 1]
    expect = -AL_NU * eps_axial * 4.0
    assert abs(uy.mean() - expect) < 1e-12, f"lateral {uy.mean():.3e} vs {expect:.3e}"


@test
def test_axial_von_mises_equals_P_over_A():
    """Stress recovery check: uniaxial tension must give exactly P/A everywhere."""
    load = 1000.0
    _, _, res = _axial_bar(load)
    expect = load / 16.0
    assert np.allclose(res.von_mises, expect, rtol=1e-9), (
        f"von Mises spread {res.von_mises.min():.6f}..{res.von_mises.max():.6f}, "
        f"expected {expect:.6f}")


@test
def test_compliance_is_twice_the_strain_energy():
    """f.u must equal u'Ku; if it does not, the load vector and the matrix disagree."""
    _, m, res = _axial_bar()
    active = res.active_elements
    nodes = m.grid.element_nodes(active)
    edof = (nodes[:, :, None] * 3 + np.arange(3)[None, None, :]).reshape(len(nodes), 24)
    KE = hex8_stiffness(m.grid.pitch, AL_E, AL_NU)
    u = res.displacement.ravel()
    energy = float(sum(u[e] @ KE @ u[e] for e in edof))
    assert abs(res.compliance - energy) < 1e-6 * abs(res.compliance), (
        f"compliance {res.compliance:.6e} vs u'Ku {energy:.6e}")


# ------------------------------------------------------------------------------ beam theory

@test
def test_cantilever_bending_converges_to_beam_theory():
    """Tip deflection must approach PL^3/3EI + PL/kAG from below as the mesh refines.

    The trend is the assertion. A trilinear hex under full integration locks in bending, so a
    coarse mesh under-predicts deflection badly; what proves the formulation right is that
    refinement monotonically closes the gap toward the analytic value rather than wandering or
    converging to something else.
    """
    L, side, load = 128.0, 8.0, 200.0
    I = side ** 4 / 12.0
    G = AL_E / (2.0 * (1.0 + AL_NU))
    analytic = load * L ** 3 / (3.0 * AL_E * I) + load * L / ((5.0 / 6.0) * side ** 2 * G)

    history = []
    for through in (4, 6, 8):
        pitch = side / through
        nx = int(round(L / pitch))
        grid = Grid(density=np.ones((nx, through, through)), pitch=(pitch,) * 3)
        m = Model(grid, AL_E, AL_NU)
        m.fix(lambda p: p[:, 0] <= EPS)                       # fully clamped root
        m.load_face("x", "max", (0.0, 0.0, -load))
        res = m.solve()
        coords = grid.node_coords()
        tip = np.flatnonzero(coords[:, 0] >= L - EPS)
        history.append(abs(res.displacement[tip, 2].mean()))

    print(f"        analytic {analytic:.4f} mm; mesh 4/6/8 through thickness -> "
          + " ".join(f"{d:.4f}" for d in history)
          + " (" + " ".join(f"{d / analytic:.1%}" for d in history) + ")")

    assert history[0] < history[1] < history[2], f"not converging monotonically: {history}"
    assert history[2] < analytic * 1.02, (
        f"finest mesh {history[2]:.4f} exceeds analytic {analytic:.4f}; a locking element "
        "should approach from below, so this means the load or restraint is wrong")
    assert history[2] > analytic * 0.80, (
        f"finest mesh {history[2]:.4f} is only {history[2] / analytic:.1%} of analytic "
        f"{analytic:.4f} -- worse than shear locking alone explains")
    # Refinement must actually be buying something, not creeping.
    assert (history[2] - history[0]) / analytic > 0.02, "refinement barely moved the answer"


# ------------------------------------------------------------------------------- honest failure

@test
def test_load_delivers_the_whole_force_even_when_the_selector_catches_empty_nodes():
    """A node with no material around it cannot carry load, and must not absorb a share of it.

    Found by the workbench bridge: a cantilever reported 0.288 mm where beam theory said 0.968.
    The solve was clean -- residual 9e-10 -- but the load selector had caught the empty pad nodes
    outside the part as well as the material ones, and `load` divided the total between all of
    them. The share landing on pad nodes was then dropped from the reduced system, so 200 N of
    requested force arrived as 60.5 N and the answer was quietly wrong by a factor of three.

    That is the worst failure shape there is: converged, plausible, and wrong. So the whole force
    is spread over the nodes that actually carry stiffness.
    """
    grid = Grid(density=np.ones((20, 4, 4)), pitch=(1.0,) * 3, origin=(0.0, 0.0, 0.0))
    # A padded grid, where the +x selector also catches a layer of empty nodes.
    padded = np.zeros((22, 4, 4))
    padded[:20] = 1.0
    gp = Grid(density=padded, pitch=(1.0,) * 3, origin=(0.0, 0.0, 0.0))

    def tip(g):
        m = Model(g, AL_E, AL_NU)
        m.fix(lambda p: p[:, 0] <= EPS)
        m.load(lambda p: p[:, 0] >= 19.0, (0.0, 0.0, -100.0))
        res = m.solve()
        return res.max_displacement

    bare, with_pad = tip(grid), tip(gp)
    rel = abs(with_pad - bare) / bare
    assert rel < 0.02, (
        f"empty nodes absorbed load: {bare:.6f} mm vs {with_pad:.6f} mm ({rel:.0%} apart)")


@test
def test_load_refuses_a_selector_that_touches_no_material():
    grid = Grid(density=np.ones((6, 4, 4)), pitch=(1.0,) * 3)
    m = Model(grid, AL_E, AL_NU)
    m.fix(lambda p: p[:, 0] <= EPS)
    try:
        m.load(lambda p: p[:, 2] > 100.0, (0.0, 0.0, -1.0))
    except ValueError as exc:
        assert "material" in str(exc).lower() or "no nodes" in str(exc).lower(), str(exc)
        return
    raise AssertionError("loading where there is no material must raise, not silently do nothing")


@test
def test_refuses_to_solve_an_unrestrained_model():
    m = Model(_bar(), AL_E, AL_NU)
    m.load_face("x", "max", (10.0, 0.0, 0.0))
    try:
        m.solve()
    except SolveFailure as exc:
        assert "rigid-body" in str(exc), str(exc)
        return
    raise AssertionError("an unrestrained model must not return a displacement field")


@test
def test_refuses_to_solve_an_unloaded_model():
    m = Model(_bar(), AL_E, AL_NU)
    m.fix(lambda p: p[:, 0] <= EPS)
    try:
        m.solve()
    except SolveFailure:
        return
    raise AssertionError("an unloaded model must not pretend to have a solution")


@test
def test_rejects_a_selector_that_matches_nothing():
    m = Model(_bar(), AL_E, AL_NU)
    try:
        m.fix(lambda p: p[:, 0] > 1e9)
    except ValueError as exc:
        assert "no nodes" in str(exc), str(exc)
        return
    raise AssertionError("a selector matching nothing must raise, not silently restrain nothing")


@test
def test_grid_rejects_out_of_range_density():
    for bad in (np.full((2, 2, 2), 1.5), np.full((2, 2, 2), -0.1)):
        try:
            Grid(density=bad, pitch=(1.0, 1.0, 1.0))
        except ValueError:
            continue
        raise AssertionError("density outside [0, 1] must be rejected")
    try:
        Grid(density=np.ones((2, 2, 2)), pitch=(1.0, 0.0, 1.0))
    except ValueError:
        return
    raise AssertionError("a zero pitch must be rejected")


# ---------------------------------------------------------------------------------- voxeliser

@test
def test_voxelises_a_cube_to_its_exact_volume():
    tri = box_triangles((0.0, 0.0, 0.0), (10.0, 10.0, 10.0))
    grid = occupancy_from_triangles(tri, 1.0)
    assert grid.shape == (12, 12, 12), grid.shape
    assert abs(grid.material_volume - 1000.0) < 1e-9, grid.material_volume
    # The pad must be genuinely empty, or face selectors have nothing to stand on.
    assert grid.density[0].sum() == 0 and grid.density[-1].sum() == 0


@test
def test_voxelises_an_offset_cube_and_keeps_its_position():
    tri = box_triangles((5.0, -3.0, 2.0), (9.0, 1.0, 6.0))
    grid = occupancy_from_triangles(tri, 0.5, pad=2)
    assert abs(grid.material_volume - 64.0) < 1e-9, grid.material_volume
    solid = grid.element_centres()[grid.density.ravel() > 0.5]
    assert solid[:, 0].min() > 5.0 and solid[:, 0].max() < 9.0
    assert solid[:, 1].min() > -3.0 and solid[:, 1].max() < 1.0
    assert solid[:, 2].min() > 2.0 and solid[:, 2].max() < 6.0


@test
def test_voxeliser_warns_on_a_leaking_mesh():
    """A hole must be reported, not quietly filled: a leak reads as a hollow stripe.

    The hole has to be punched in a HORIZONTAL face. Vertical walls are parallel to the sample
    ray and contribute no crossings at all, so deleting one of those is invisible to parity --
    which is itself worth knowing: this voxeliser cannot detect a leak in a wall it never hits.
    """
    tri = np.delete(box_triangles((0.0, 0.0, 0.0), (10.0, 10.0, 10.0)), 2, axis=0)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        occupancy_from_triangles(tri, 1.0)
    assert any("watertight" in str(w.message) for w in caught), (
        f"expected a watertightness warning, got {[str(w.message) for w in caught]}")


@test
def test_hollow_box_voxelises_hollow():
    """Parity must see both walls: a shell has to come back as a shell, not a solid block."""
    outer = box_triangles((0.0, 0.0, 0.0), (12.0, 12.0, 12.0))
    inner = box_triangles((3.0, 3.0, 3.0), (9.0, 9.0, 9.0))
    grid = occupancy_from_triangles(np.concatenate([outer, inner]), 0.5, pad=1)
    expect = 12.0 ** 3 - 6.0 ** 3
    assert abs(grid.material_volume - expect) < 1e-6, (grid.material_volume, expect)


@test
def test_round_trips_a_binary_stl():
    tri = box_triangles((0.0, 0.0, 0.0), (4.0, 4.0, 4.0))
    path = os.path.join(tempfile.gettempdir(), "ck_fea_cube.stl")
    with open(path, "wb") as fh:
        fh.write(b"\0" * 80 + struct.pack("<I", len(tri)))
        for t in tri:
            fh.write(struct.pack("<3f", 0.0, 0.0, 0.0))
            for v in t:
                fh.write(struct.pack("<3f", *v))
            fh.write(b"\0\0")
    try:
        back = load_stl(path)
        assert back.shape == tri.shape, (back.shape, tri.shape)
        assert np.allclose(back, tri), "binary STL round-trip changed the geometry"
        assert abs(occupancy_from_triangles(back, 0.5).material_volume - 64.0) < 1e-9
    finally:
        os.unlink(path)


@test
def test_end_to_end_voxelised_cantilever_is_sane():
    """The whole chain: triangles -> occupancy -> solve, against beam theory within locking error.

    Coarse on purpose (4 voxels through a 10 mm section). The assertion is an order-of-magnitude
    bracket, which is all a mesh this coarse earns -- it exists to catch a broken chain, not to
    produce a number worth quoting.
    """
    L, side, load = 80.0, 10.0, 100.0
    tri = box_triangles((0.0, 0.0, 0.0), (L, side, side))
    grid = occupancy_from_triangles(tri, 2.5, pad=0)
    m = Model(grid, AL_E, AL_NU)
    m.fix(lambda p: p[:, 0] <= grid.origin[0] + EPS)
    m.load_face("x", "max", (0.0, 0.0, -load))
    res = m.solve()
    analytic = load * L ** 3 / (3.0 * AL_E * (side ** 4 / 12.0))
    got = res.max_displacement
    print(f"        voxel cantilever {got:.4f} mm vs beam theory {analytic:.4f} mm "
          f"({got / analytic:.0%})")
    assert 0.5 * analytic < got < 1.05 * analytic, (got, analytic)
    assert res.peak_von_mises > res.von_mises_percentile(99) >= 0.0


@test
def test_the_voxel_tier_keeps_the_stress_tensor_and_can_resolve_it_onto_a_plane():
    """von Mises presumes isotropy. A printed part is the counter-example that matters here.

    An FDM wall is a stack of welded beads and the weld is the weak direction, so whether a
    printed part survives is decided by the traction on the LAYER PLANE — the part that peels
    the layers apart and the part that slides them over one another — and no invariant can
    recover either. The tensor is computed on the way to von Mises anyway.
    """
    from constraint_kit.fea.tet import traction_on_plane

    # The same bar fixture the exactness tests use, and for the same reasons: roller restraints
    # so the uniform field is admissible, and `load_face` so the nodal load vector is the
    # consistent one. Equal nodal forces on a face are NOT consistent with a uniform traction --
    # they leave a self-equilibrating end effect that reaches 2.6e-5 into this bar -- and a test
    # making exact claims about an element should not be absorbing that in its tolerance.
    load = 1000.0
    _, _, r = _axial_bar(load)
    area = 16.0

    assert r.stress.shape == (len(r.active_elements), 6), r.stress.shape
    sx, sy, sz, txy, tyz, tzx = r.stress.T
    assert np.allclose(sx, load / area, rtol=1e-9), (sx.min(), sx.max())
    for other in (sy, sz, txy, tyz, tzx):
        assert abs(other).max() < 1e-9 * (load / area), abs(other).max()

    # von Mises must be recoverable FROM the tensor, or one of the two is wrong
    recomputed = np.sqrt(0.5 * ((sx - sy) ** 2 + (sy - sz) ** 2 + (sz - sx) ** 2)
                         + 3.0 * (txy ** 2 + tyz ** 2 + tzx ** 2))
    assert np.allclose(recomputed, r.von_mises, atol=1e-9)

    # Printed flat (layers normal to Z): a pure X tension does not peel the layers at all.
    peel_z, shear_z = traction_on_plane(r.stress, (0, 0, 1))
    assert abs(peel_z).max() < 1e-9 * (load / area), abs(peel_z).max()
    assert abs(shear_z).max() < 1e-9 * (load / area), abs(shear_z).max()

    # Printed on its end (layers normal to X): the same load pulls straight across every weld.
    peel_x, shear_x = traction_on_plane(r.stress, (1, 0, 0))
    assert np.allclose(peel_x, load / area, rtol=1e-9), (peel_x.min(), peel_x.max())
    assert abs(shear_x).max() < 1e-9 * (load / area), "pure tension on a layer normal is not shear"

    # At 45 degrees the traction splits, and the split is exact: sigma/2 each.
    peel_45, shear_45 = traction_on_plane(r.stress, (1, 0, 1))
    assert np.allclose(peel_45, 0.5 * load / area, rtol=1e-9), (peel_45.min(), peel_45.max())
    assert np.allclose(shear_45, 0.5 * load / area, rtol=1e-9), (shear_45.min(), shear_45.max())


@test
def test_a_result_reports_the_force_it_actually_applied():
    """The accounting that would have caught the worst bug in this package.

    `Model.load` divides a force over the nodes a selector matched, and a padded grid lets a
    selector match empty space. Those nodes are dropped from the reduced system and their share
    of the load vanishes with them. Measured once: 200 N asked for, 60.5 N applied, a converged
    residual of 9e-10, and a cantilever deflection wrong by a factor of three. The division is
    now over material nodes only, and the total is reported so the next version of that mistake
    is visible in the output rather than inferable from a surprising answer.
    """
    grid = Grid(density=np.ones((8, 2, 2)), pitch=(1.0, 1.0, 1.0))
    model = Model(grid, E=210000.0, nu=0.3)
    model.fix(lambda p: p[:, 0] <= 0.0)
    model.load(lambda p: p[:, 0] >= 8.0, (0.0, 0.0, -200.0))
    r = model.solve()
    assert abs(r.applied_force[2] + 200.0) < 1e-9, r.applied_force
    assert abs(r.applied_force[0]) < 1e-12 and abs(r.applied_force[1]) < 1e-12, r.applied_force

    # two loads accumulate rather than replace
    model2 = Model(grid, E=210000.0, nu=0.3)
    model2.fix(lambda p: p[:, 0] <= 0.0)
    model2.load(lambda p: p[:, 0] >= 8.0, (0.0, 0.0, -200.0))
    model2.load(lambda p: p[:, 0] >= 8.0, (50.0, 0.0, 0.0))
    r2 = model2.solve()
    assert abs(r2.applied_force[0] - 50.0) < 1e-9, r2.applied_force
    assert abs(r2.applied_force[2] + 200.0) < 1e-9, r2.applied_force


def main():
    passed, failed = 0, []
    for fn in TESTS:
        try:
            fn()
            passed += 1
            print(f"  PASS  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(fn.__name__)
            print(f"  FAIL  {fn.__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{passed}/{passed + len(failed)} passed" + (f"; FAILED: {failed}" if failed else ""))
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
