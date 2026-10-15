#!/usr/bin/env python3
"""The voxel tier against an INDEPENDENT solver: Gmsh second-order tets through CalculiX.

    python3 tests/test_crosscheck.py

Why this suite exists (issue #3). Everything in `fea/` is ours: our element, our assembly, our
solver, our stress recovery. It is tested against closed-form elasticity, which is the right
first check and is not sufficient — a closed form only exists for the shapes that have one, and
those are exactly the shapes where an error is least likely to hide. The parts we actually care
about have no closed form, and on one of them (the knee-exo gantry plate) the voxel tier returns
a verdict that contradicts an earlier published result. There is no way to earn confidence in
that number except a second, independent implementation.

CalculiX 2.22 and Gmsh 4.13 ship inside FreeCAD 1.0, so this costs no new dependency. They are a
genuinely independent reference: a different element family (C3D10 quadratic tetrahedra against
our trilinear hexahedra), a different mesh topology (conforming tets against a staircased
lattice), a different assembly and a different linear solver (SPOOLES against our CG or SuperLU).
Two implementations that share no code agreeing on a number is evidence; one implementation
agreeing with itself is not.

The order of assertions matters and is deliberate:

1. the toolchain is reported missing, honestly, rather than silently skipped
2. CalculiX is validated against closed form FIRST — a reference that is not itself checked is
   not a reference, it is a second opinion of unknown quality
3. only then is the voxel tier compared against it
4. and the comparison REPORTS a disagreement rather than asserting agreement, because when two
   solvers disagree the interesting output is the size of the gap, not an exception
"""
from __future__ import annotations

import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from constraint_kit.fea import crosscheck, tet, voxel  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


# A 10:1 steel cantilever. Slender enough that beam theory is a fair reference, stubby enough
# that it meshes and solves in seconds.
L, B, H = 100.0, 10.0, 10.0
E_STEEL, NU = 210_000.0, 0.3
P = 100.0
I_BEAM = B * H ** 3 / 12.0
TIP_EXACT = P * L ** 3 / (3.0 * E_STEEL * I_BEAM)      # 0.190476 mm
WALL_STRESS_EXACT = P * L * (H / 2.0) / I_BEAM         # 60.0 MPa

WALL = staticmethod(lambda p: p[:, 0] <= 1e-9)


def _wall(p):
    return p[:, 0] <= 1e-9


def _tip(p):
    return p[:, 0] >= L - 1e-9


def _reference(pitch: float = 2.5, force: float = P):
    m = tet.mesh_box((L, B, H), pitch=pitch)
    return m, tet.solve(m, E=E_STEEL, nu=NU, fix=_wall,
                        load=(_tip, (0.0, 0.0, -force)))


# ------------------------------------------------------------------- the toolchain, honestly

@test
def test_a_missing_toolchain_is_named_not_guessed_at():
    """A cross-check that silently does not run is worse than none: it reads as agreement."""
    saved = dict(os.environ)
    try:
        os.environ["CK_GMSH"] = os.path.join("C:", "nope", "gmsh.exe")
        os.environ["CK_CCX"] = os.path.join("C:", "nope", "ccx.exe")
        try:
            tet.tools()
        except tet.ToolchainMissing as exc:
            text = str(exc)
            assert "CK_GMSH" in text and "CK_CCX" in text, text
            assert "FreeCAD" in text, "say where they usually live: " + text
        else:
            raise AssertionError("a bogus toolchain path must be refused")
    finally:
        os.environ.clear()
        os.environ.update(saved)


@test
def test_the_toolchain_is_found_and_reports_its_versions():
    gmsh_path, ccx_path = tet.tools()
    assert os.path.isfile(gmsh_path), gmsh_path
    assert os.path.isfile(ccx_path), ccx_path
    versions = tet.versions()
    assert versions["gmsh"] and versions["ccx"], versions
    print("      gmsh %s, CalculiX %s" % (versions["gmsh"], versions["ccx"]))


# ------------------------------------------------------------------------------- the mesh

@test
def test_gmsh_meshes_a_box_to_the_requested_size_and_order():
    m = tet.mesh_box((L, B, H), pitch=4.0)
    assert m.tets.shape[1] == 10, m.tets.shape
    assert m.nodes.shape[1] == 3
    assert len(m.tets) > 100, len(m.tets)
    lo, hi = m.bounds()
    assert np.allclose(lo, (0, 0, 0), atol=1e-6), lo
    assert np.allclose(hi, (L, B, H), atol=1e-6), hi
    # every tet references real nodes, and no node is orphaned
    assert m.tets.min() >= 0 and m.tets.max() < len(m.nodes)
    assert len(set(m.tets.ravel().tolist())) == len(m.nodes), "orphaned nodes in the mesh"


@test
def test_an_explicit_workdir_is_created_and_its_files_kept():
    """A caller who names a working directory wants the deck and the .frd afterwards.

    The temporary-directory path creates its own; the explicit path did not, and failed with a
    FileNotFoundError on the .geo. Keeping the files is the point of passing the argument at all:
    a stress result nobody can open the deck for is not auditable.
    """
    import tempfile

    parent = tempfile.mkdtemp(prefix="ck-wd-")
    work = os.path.join(parent, "does", "not", "exist", "yet")
    m = tet.mesh_box((L, B, H), pitch=8.0, workdir=work)
    assert os.path.isdir(work), work
    assert os.path.isfile(os.path.join(work, "model.geo"))
    assert os.path.isfile(os.path.join(work, "model.inp")), "the mesh must be left behind"

    tet.solve(m, E=E_STEEL, nu=NU, fix=_wall, load=(_tip, (0.0, 0.0, -P)), workdir=work)
    assert os.path.isfile(os.path.join(work, "job.inp")), "the deck must be left behind"
    assert os.path.isfile(os.path.join(work, "job.frd")), "the result must be left behind"


@test
def test_the_stress_tensor_survives_not_just_the_invariant():
    """von Mises assumes a material with no preferred direction. A printed part has one.

    An FDM part is markedly weaker across its layers than along them, so the question that
    decides whether such a part is safe is the normal and shear traction on the LAYER PLANE —
    which cannot be recovered from an invariant. The full tensor is parsed out of the .frd
    anyway; throwing it away made the anisotropic question unanswerable for no saving at all.
    """
    m, r = _reference(4.0)
    assert r.stress.shape == (len(m.nodes), 6), r.stress.shape

    # von Mises must be reproducible FROM the tensor, or one of the two is wrong
    sxx, syy, szz, sxy, syz, szx = r.stress.T
    recomputed = np.sqrt(0.5 * ((sxx - syy) ** 2 + (syy - szz) ** 2 + (szz - sxx) ** 2)
                         + 3.0 * (sxy ** 2 + syz ** 2 + szx ** 2))
    assert np.allclose(recomputed, r.von_mises, atol=1e-6), (
        "von Mises and the tensor it came from disagree")

    # A cantilever in bending about Y: SXX dominates, and it is signed. Only about 2x, not the
    # 10x beam theory implies — because a clamped end is a real 3-D restraint, and holding a
    # face against its own Poisson contraction generates a transverse stress that beam theory
    # has no way to represent. 28 MPa of SYY against 68 of SXX, all of it at the wall.
    assert abs(sxx).max() > 2.0 * abs(syy).max(), (abs(sxx).max(), abs(syy).max())
    assert sxx.min() < -1.0 and sxx.max() > 1.0, "bending puts one face in tension, one in compression"

    # the traction on a plane is extractable, which is the whole reason to keep the tensor
    for normal in ((0, 0, 1), (1, 0, 0)):
        tn, ts = tet.traction_on_plane(r.stress, normal)
        assert tn.shape == (len(m.nodes),) and ts.shape == (len(m.nodes),)
        assert np.all(ts >= -1e-12), "a shear magnitude cannot be negative"
    # on the Z plane of a beam bent about Y, the direct traction is small and SXX carries the load
    tn_z, _ = tet.traction_on_plane(r.stress, (0, 0, 1))
    assert abs(tn_z).max() < 0.5 * abs(sxx).max(), (abs(tn_z).max(), abs(sxx).max())
    # and on the X plane it is exactly SXX, because the normal is a coordinate direction
    tn_x, _ = tet.traction_on_plane(r.stress, (1, 0, 0))
    assert np.allclose(tn_x, sxx, atol=1e-9)


@test
def test_a_finer_pitch_gives_more_elements():
    coarse = tet.mesh_box((L, B, H), pitch=8.0)
    fine = tet.mesh_box((L, B, H), pitch=4.0)
    assert len(fine.tets) > 2 * len(coarse.tets), (len(coarse.tets), len(fine.tets))


@test
def test_the_mesh_volume_matches_the_box_volume():
    """A mesh whose volume is wrong is wrong before any physics happens."""
    exact = L * B * H
    for pitch in (8.0, 4.0):
        m = tet.mesh_box((L, B, H), pitch=pitch)
        assert abs(m.volume() - exact) / exact < 1e-6, (pitch, m.volume(), exact)


# ------------------------------------------- the REFERENCE, validated against closed form first

@test
def test_calculix_reproduces_beam_theory_on_deflection_and_stress():
    """Validate the reference before trusting it to judge anything else.

    Both numbers should land on their own side of beam theory, for known reasons: deflection a
    little HIGH because a 3-D solid adds the shear deflection Euler-Bernoulli omits, and peak
    stress a little LOW because the clamped end is a real 3-D restraint rather than a plane
    section held rigid.
    """
    m, r = _reference(2.5)

    ratio = r.max_displacement / TIP_EXACT
    print("      tip %.6f mm vs %.6f exact (x%.4f)" % (r.max_displacement, TIP_EXACT, ratio))
    assert 1.00 <= ratio < 1.03, (
        "CalculiX must agree with PL^3/3EI to a few percent, from above: %.4f" % ratio)

    s99 = r.von_mises_percentile(99.0)
    print("      von Mises p99 %.2f MPa vs %.2f exact" % (s99, WALL_STRESS_EXACT))
    assert 0.80 < s99 / WALL_STRESS_EXACT < 1.10, s99 / WALL_STRESS_EXACT

    assert r.n_nodes == len(m.nodes)
    assert r.von_mises.shape == (len(m.nodes),)


@test
def test_calculix_total_applied_load_is_what_was_asked_for():
    """A load spread over a node set must still sum to the load. Ours lost 70% of one once."""
    _, r = _reference(4.0)
    assert abs(r.applied_force[2] + P) < 1e-6 * P, r.applied_force
    assert abs(r.applied_force[0]) < 1e-9 and abs(r.applied_force[1]) < 1e-9, r.applied_force


@test
def test_a_scrambled_element_ordering_is_reported_not_absorbed():
    """CalculiX rejects a bad C3D10 node order with "nonpositive jacobian".

    Worth a test because it is the first thing that went wrong here: Gmsh native .msh node order
    for a 10-node tet is NOT CalculiX order, and the failure is an exit code rather than a wrong
    answer. If a future refactor starts swallowing that exit code, a wrong mesh silently becomes
    a plausible number.
    """
    m = tet.mesh_box((L, B, H), pitch=8.0)
    scrambled = tet.TetMesh(nodes=m.nodes, tets=m.tets[:, [0, 1, 2, 3, 5, 4, 6, 8, 7, 9]])
    try:
        tet.solve(scrambled, E=E_STEEL, nu=NU, fix=_wall, load=(_tip, (0.0, 0.0, -P)))
    except tet.SolverFailed as exc:
        assert "jacobian" in str(exc).lower(), str(exc)
    else:
        raise AssertionError("a scrambled element ordering must not return a result")


# ----------------------------------------------------------------- the cross-check itself

def _voxel_cantilever(pitch: float) -> voxel.Result:
    nx, ny, nz = (int(round(L / pitch)), int(round(B / pitch)), int(round(H / pitch)))
    grid = voxel.Grid(density=np.ones((nx, ny, nz)), pitch=(pitch, pitch, pitch))
    model = voxel.Model(grid, E=E_STEEL, nu=NU)
    model.fix(_wall)
    model.load(_tip, (0.0, 0.0, -P))
    return model.solve()


@test
def test_the_two_solvers_agree_on_the_cantilever():
    """The headline: two implementations sharing no code, on the same problem."""
    vox = _voxel_cantilever(2.5)
    _, ref = _reference(2.5)

    ag = crosscheck.compare(vox, ref, exact_displacement=TIP_EXACT)
    print("      " + ag.report().replace("\n", "\n      "))
    assert ag.agrees, ag.report()
    assert 0.85 < ag.displacement_ratio < 1.15, ag.displacement_ratio


@test
def test_the_comparison_reports_a_disagreement_rather_than_raising():
    """When two solvers disagree the useful output is the size of the gap, not an exception.

    A one-element-through-thickness voxel model of a bending beam is the honest example: a
    trilinear hex locks in bending, so a coarse grid reads far too stiff. The comparison must say
    so with a number rather than either crashing or quietly passing.
    """
    vox = _voxel_cantilever(10.0)          # one element through the thickness
    _, ref = _reference(2.5)

    ag = crosscheck.compare(vox, ref, exact_displacement=TIP_EXACT)
    assert not ag.agrees, ag.report()
    assert ag.displacement_ratio < 0.7, (
        "a single hex through the thickness should read much too stiff: %.3f"
        % ag.displacement_ratio)
    assert "stiff" in ag.report().lower(), ag.report()
    print("      correctly flagged: %.3f of the reference deflection" % ag.displacement_ratio)


@test
def test_refining_the_voxel_grid_closes_the_gap_to_the_reference():
    """The convergence direction, measured against something other than ourselves."""
    _, ref = _reference(2.5)
    ratios = []
    for pitch in (10.0, 5.0, 2.5):
        ratios.append(crosscheck.compare(_voxel_cantilever(pitch), ref).displacement_ratio)
    print("      ratios at pitch 10/5/2.5: %s" % ["%.3f" % r for r in ratios])
    assert ratios[0] < ratios[1] < ratios[2], ratios
    assert ratios[-1] > 0.9, ratios


@test
def test_the_voxel_stress_bias_is_the_predicted_1_minus_pitch_over_thickness():
    """A safety-relevant systematic, found by this cross-check and quantified by it.

    The voxel tier evaluates stress at ELEMENT CENTRES. In bending, stress varies linearly
    through the thickness and peaks at the surface — and an element centre sits half a pitch
    inside that surface. So the highest stress a voxel grid can even see is the stress at
    `h/2 - pitch/2` from the neutral axis, which is

        (h/2 - pitch/2) / (h/2)  =  1 - pitch/h

    of the true extreme-fibre value. Not a discretisation error that refinement merely improves:
    a geometric ceiling on what the sampling can observe, and one that is OPTIMISTIC. A safety
    factor quoted off a voxel bending stress is too high by this factor unless the pitch is small
    against the thickness.

    The ratio against the tetrahedral reference should therefore track `1 - pitch/h` closely,
    across pitches. If it does, the bias is understood and can be corrected for; if it does not,
    something else is wrong and the whole stress path needs looking at.
    """
    _, ref = _reference(2.5)
    reference_stress = ref.von_mises_percentile(99.0)
    rows = []
    for pitch in (5.0, 2.5, 1.25):
        vox = _voxel_cantilever(pitch)
        measured = vox.von_mises_percentile(99.0) / reference_stress
        predicted = 1.0 - pitch / H
        rows.append((pitch, measured, predicted))
        print("      pitch %.2f: measured %.3f, predicted 1-pitch/h = %.3f"
              % (pitch, measured, predicted))
    for pitch, measured, predicted in rows:
        assert abs(measured - predicted) < 0.08, (
            "the stress bias at pitch %.2f is %.3f, not the predicted %.3f -- the element-centre "
            "explanation does not hold and the stress path needs investigating"
            % (pitch, measured, predicted))
    # and it must be a one-sided bias: a voxel grid cannot see MORE than the surface stress
    assert all(measured < 1.02 for _, measured, _ in rows), rows


@test
def test_calculix_and_our_buckling_agree_on_a_column():
    """An independent check on an eigenvalue solver, which is where errors hide best.

    A geometric stiffness with a sign slip in one off-diagonal block still produces positive
    factors in roughly the right range, and still converges to Euler on the one case anybody
    tests. Nothing short of a second implementation catches that -- and CalculiX has `*BUCKLE`,
    quadratic tets, and no code in common with us.

    Both are checked against Euler first, for the same reason the static cross-check is: a
    reference nobody verified is a second opinion of unknown quality.
    """
    import math

    from constraint_kit.fea.buckling import buckling

    side, long_n, pitch = 4.0, 48.0, 1.0
    applied = 1000.0
    I = side ** 4 / 12.0
    euler = math.pi ** 2 * E_STEEL * I / (2.0 * long_n) ** 2       # fixed-free, k = 2

    m = tet.mesh_box((side, side, long_n), pitch=2.0)
    theirs = tet.buckle(m, E=E_STEEL, nu=NU,
                        fix=lambda p: p[:, 2] <= 1e-9,
                        load=(lambda p: p[:, 2] >= long_n - 1e-9, (0.0, 0.0, -applied)),
                        n_modes=2)
    their_critical = theirs[0] * applied

    n = int(round(side / pitch))
    grid = voxel.Grid(density=np.ones((n, n, int(long_n / pitch))), pitch=(pitch,) * 3)
    model = voxel.Model(grid, E=E_STEEL, nu=NU)
    model.fix(lambda p: p[:, 2] <= 1e-9)
    model.load_face("z", "max", (0.0, 0.0, -applied))
    ours = buckling(model, n_modes=1)
    our_critical = ours.factors[0] * applied

    print("      Euler %.1f N | CalculiX %.1f N (x%.3f) | voxel %.1f N (x%.3f)"
          % (euler, their_critical, their_critical / euler,
             our_critical, our_critical / euler))

    # the reference, against closed form, first
    assert 0.90 < their_critical / euler < 1.10, (
        "CalculiX must land near Euler before it can judge anything: %.4f"
        % (their_critical / euler))
    # ours, from above, because a trilinear hex locks
    assert our_critical / euler > 1.0, our_critical / euler
    # and the two against each other
    ratio = our_critical / their_critical
    assert 0.90 < ratio < 1.30, (
        "the two buckling solvers disagree by more than meshing explains: %.4f" % ratio)


@test
def test_the_comparison_refuses_to_compare_different_problems():
    """Two solvers given different loads disagree for a reason that is neither solver fault."""
    vox = _voxel_cantilever(5.0)
    _, ref = _reference(5.0, force=2.0 * P)
    try:
        crosscheck.compare(vox, ref)
    except crosscheck.NotComparable as exc:
        assert "load" in str(exc).lower(), str(exc)
    else:
        raise AssertionError("comparing different load cases must be refused")


def main() -> int:
    try:
        tet.tools()
    except tet.ToolchainMissing as exc:
        print("SKIPPED: %s" % exc)
        return 0

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
