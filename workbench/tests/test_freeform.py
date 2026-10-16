#!/usr/bin/env python3
"""Loft, sweep and revolve: the shapes a prismatic catalogue cannot express (issue #15).

    "C:/Program Files/FreeCAD 1.0/bin/freecadcmd.exe" workbench/tests/test_freeform.py

This gap was identified by the live planner rather than by me, which is the second time that has
happened and the reason the live tier exists. Asked for a turbine blade it declined, and the
decline was precise:

    "the catalogue has no airfoil/twisted-blade generator ... Nothing here can express a lofted,
    twisted, tapered aerofoil section, let alone an internal cavity."

It was right on every count. Everything in the catalogue was a prism, a cylinder or a boolean of
the two, so a duct that changes section, a bend, a coil, a cone and a twisted blade were all
simply unsayable.

**Every assertion here is against a closed form**, which is the point of choosing these three
operations: each has an exact volume from classical geometry, computable without reference to
anything this code does.

* a **revolved** rectangle is a cylinder or an annulus -- `pi*(R2^2 - R1^2)*h`
* a **revolved** right triangle is a cone -- `pi*r^2*h/3`
* a **swept** circle along a circular arc is a torus segment, and Pappus gives
  `area * centroid path length` exactly
* a **lofted** stack of parallel sections is a prismatoid -- `h/6 * (A1 + 4*Am + A2)`, exact for
  any solid whose cross-sectional area is at most cubic in height, which a linear taper is

So these are equality tests to within OCCT's own tolerance, not plausibility checks. A loft that
is subtly wrong -- sections in the wrong order, a twist applied about the wrong point, a ruled
surface where a smooth one was asked for -- changes the volume, and the prismatoid formula
notices.
"""
from __future__ import annotations

import math
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import FreeCAD  # noqa: E402

from ckwb import parts as P  # noqa: E402

TESTS = []
TOL = 1e-6


def test(fn):
    TESTS.append(fn)
    return fn


def _rel(got: float, want: float) -> float:
    return abs(got - want) / abs(want) if want else abs(got)


# -------------------------------------------------------------------------------- revolve

@test
def test_a_revolved_rectangle_is_an_annulus_with_an_exact_volume():
    """`pi*(R2^2 - R1^2)*h`. The simplest case, and it pins the axis convention."""
    inner, outer, height = 10.0, 18.0, 12.0
    shape, frames = P.revolve(profile="rect", r_inner=inner, r_outer=outer, height=height,
                              angle=360.0)
    want = math.pi * (outer ** 2 - inner ** 2) * height
    assert _rel(shape.Volume, want) < 1e-6, (shape.Volume, want)
    assert shape.isValid() and shape.isClosed() and len(shape.Solids) == 1
    assert {"base", "top", "axis"} <= set(frames), sorted(frames)
    assert abs(frames["top"].Base.z - height) < TOL, frames["top"].Base


@test
def test_a_partial_revolve_is_exactly_its_fraction_of_the_full_one():
    """A 90 degree sweep is a quarter. Linear in the angle, so an error in it cannot hide."""
    full, _ = P.revolve(profile="rect", r_inner=8.0, r_outer=14.0, height=10.0, angle=360.0)
    quarter, _ = P.revolve(profile="rect", r_inner=8.0, r_outer=14.0, height=10.0, angle=90.0)
    assert _rel(quarter.Volume, full.Volume / 4.0) < 1e-6, (quarter.Volume, full.Volume)
    assert len(quarter.Solids) == 1


@test
def test_a_revolved_triangle_is_a_cone():
    """`pi*r^2*h/3`, which is a different formula from the annulus and catches a sign slip."""
    radius, height = 15.0, 24.0
    shape, _ = P.revolve(profile="taper", r_inner=0.0, r_outer=radius, height=height,
                         angle=360.0)
    want = math.pi * radius ** 2 * height / 3.0
    assert _rel(shape.Volume, want) < 1e-6, (shape.Volume, want)
    assert len(shape.Solids) == 1


@test
def test_revolve_refuses_a_profile_that_would_pass_through_the_axis():
    """A negative inner radius revolves the profile through itself. Not a solid, not an error."""
    for bad in dict(r_inner=-2.0, r_outer=10.0), dict(r_inner=12.0, r_outer=6.0):
        try:
            P.revolve(profile="rect", height=10.0, angle=360.0, **bad)
        except ValueError as exc:
            assert "r_inner" in str(exc) or "r_outer" in str(exc), str(exc)
            continue
        raise AssertionError("%s should have been refused" % bad)


# ---------------------------------------------------------------------------------- sweep

@test
def test_a_circle_swept_along_an_arc_is_a_torus_and_pappus_gives_its_volume():
    """`area * centroid path length`, exactly. Pappus is independent of how the sweep is built.

    A sweep that lets the profile scale, drift off the path or rotate when it should not changes
    the volume, and this notices -- which a bounding-box check would not.
    """
    tube, bend, angle = 6.0, 40.0, 90.0
    shape, frames = P.sweep(profile_d=tube, bend_radius=bend, angle=angle)
    area = math.pi * (tube / 2.0) ** 2
    path = 2.0 * math.pi * bend * (angle / 360.0)
    assert _rel(shape.Volume, area * path) < 2e-5, (shape.Volume, area * path)
    assert shape.isValid() and len(shape.Solids) == 1
    assert {"start", "end", "axis"} <= set(frames), sorted(frames)


@test
def test_a_full_sweep_closes_into_a_torus():
    tube, bend = 5.0, 30.0
    shape, _ = P.sweep(profile_d=tube, bend_radius=bend, angle=360.0)
    want = 2.0 * math.pi ** 2 * bend * (tube / 2.0) ** 2        # the closed-form torus
    assert _rel(shape.Volume, want) < 2e-5, (shape.Volume, want)
    assert len(shape.Solids) == 1


@test
def test_the_end_frame_sits_where_the_path_ends_and_points_along_it():
    """A sweep is only useful if the thing on the end of it can be mated to the end."""
    bend, angle = 40.0, 90.0
    _, frames = P.sweep(profile_d=6.0, bend_radius=bend, angle=angle)
    end = frames["end"]
    # a quarter bend of radius 40 starting at the origin along +Z ends 40 across and 40 up
    assert abs(end.Base.Length - bend * math.sqrt(2.0)) < 1e-3, end.Base
    out = end.Rotation.multVec(FreeCAD.Vector(0, 0, 1))
    assert abs(abs(out.x) - 1.0) < 1e-3, (
        "after a quarter turn the end should face along X: %s" % out)


@test
def test_a_helical_sweep_makes_a_spring_whose_volume_is_its_wire_length():
    """A coil is a sweep along a helix, and its volume is the wire cross-section times its length.

    The helix length is `turns * sqrt(circumference^2 + pitch^2)`, which is Pythagoras on the
    unrolled cylinder -- so this is still a closed form rather than a measurement.
    """
    wire, radius, pitch, turns = 3.0, 20.0, 8.0, 4.0
    shape, frames = P.sweep(profile_d=wire, bend_radius=radius, angle=0.0,
                            helix_pitch=pitch, helix_turns=turns)
    length = turns * math.sqrt((2.0 * math.pi * radius) ** 2 + pitch ** 2)
    want = math.pi * (wire / 2.0) ** 2 * length
    assert _rel(shape.Volume, want) < 5e-3, (shape.Volume, want)
    assert len(shape.Solids) == 1
    assert abs(frames["end"].Base.z - pitch * turns) < 1e-3, frames["end"].Base


@test
def test_sweep_refuses_a_bend_tighter_than_the_tube():
    """The profile would self-intersect through the inside of the bend. OCCT may or may not
    notice; saying so here is better than finding out from a broken solid."""
    try:
        P.sweep(profile_d=20.0, bend_radius=6.0, angle=90.0)
    except ValueError as exc:
        assert "bend_radius" in str(exc), str(exc)
        return
    raise AssertionError("a bend radius inside the tube radius must be refused")


# ----------------------------------------------------------------------------------- loft

@test
def test_a_loft_between_two_squares_matches_the_prismatoid_formula():
    """`h/6 * (A1 + 4*Am + A2)`, exact for a linear taper. The headline check.

    The prismatoid formula is exact for any solid whose cross-sectional area is at most cubic in
    height. A linear taper makes the area quadratic, so this is an equality -- and it is
    sensitive to the one thing most likely to be wrong, which is the sections being lofted in a
    different order or at different heights than asked for.
    """
    bottom, top, height = 40.0, 20.0, 60.0
    shape, frames = P.loft(profile="rect", bottom=bottom, top=top, height=height, sections=2)
    a1, a2 = bottom ** 2, top ** 2
    mid = ((bottom + top) / 2.0) ** 2
    want = height / 6.0 * (a1 + 4.0 * mid + a2)
    assert _rel(shape.Volume, want) < 1e-6, (shape.Volume, want)
    assert shape.isValid() and shape.isClosed() and len(shape.Solids) == 1
    assert {"base", "top", "axis"} <= set(frames), sorted(frames)
    assert abs(frames["top"].Base.z - height) < TOL


@test
def test_more_intermediate_sections_do_not_change_a_linear_taper():
    """A linear taper is already exactly represented, so subdividing it must change nothing.

    This is the check that the intermediate sections are placed by interpolation rather than by
    something that merely looks like it -- a section at the wrong height or the wrong size shows
    up immediately as a volume change where there should be none.
    """
    args = dict(profile="rect", bottom=40.0, top=20.0, height=60.0)
    two, _ = P.loft(sections=2, **args)
    six, _ = P.loft(sections=6, **args)
    assert _rel(six.Volume, two.Volume) < 1e-6, (two.Volume, six.Volume)


@test
def test_a_lofted_circle_taper_is_a_truncated_cone():
    """`pi*h/3 * (R1^2 + R1*R2 + R2^2)`. A different closed form, same machinery."""
    bottom, top, height = 30.0, 12.0, 45.0
    shape, _ = P.loft(profile="circle", bottom=bottom, top=top, height=height, sections=2)
    r1, r2 = bottom / 2.0, top / 2.0
    want = math.pi * height / 3.0 * (r1 ** 2 + r1 * r2 + r2 ** 2)
    assert _rel(shape.Volume, want) < 1e-5, (shape.Volume, want)
    assert len(shape.Solids) == 1


@test
def test_a_twisted_loft_turns_its_top_AND_interpolates_the_twist_through_the_stack():
    """The shape the planner could not express: tapered AND twisted.

    **Volume is the wrong instrument here, and that is worth stating because it is the obvious
    one to reach for.** A smooth loft through eight slightly-rotated squares stays almost exactly
    within the straight envelope: measured, the twisted solid is 0.000012% leaner. An assertion
    on that would pass on floating-point noise whether the twist worked or not -- which an
    earlier version of this test did, and it read as a real check.

    Two things discriminate properly:

    * the **mid-height section**, which must be at half the twist. 45 degrees straight against
      60 degrees for a 30 degree twist -- exactly 45 + 30/2. That is what proves the twist is
      *interpolated through the stack* rather than applied only at the ends, which is the
      difference between a twisted blade and a sheared one.
    * the **surface area**, which rises 0.157% because the side faces are warped. Three orders
      of magnitude more signal than the volume.
    """
    twist = 30.0
    args = dict(profile="rect", bottom=40.0, top=24.0, height=80.0, sections=8)
    shape, frames = P.loft(twist=twist, **args)
    straight, _ = P.loft(twist=0.0, **args)
    assert shape.isValid() and shape.isClosed() and len(shape.Solids) == 1, shape.Solids

    # the top turns by the full twist
    top_z = max(v.Point.z for v in shape.Vertexes)
    corners = [v.Point for v in shape.Vertexes if abs(v.Point.z - top_z) < 1e-6]
    assert len(corners) >= 4, len(corners)
    angles = sorted(math.degrees(math.atan2(c.y, c.x)) % 90.0 for c in corners)
    assert all(abs(a - (45.0 + twist) % 90.0) < 0.01 for a in angles), (
        "the top corners should all be at 45 + %g degrees; got %s"
        % (twist, ["%.2f" % a for a in angles]))

    # and the middle turns by HALF of it, which is what interpolated means
    for sh, want in ((straight, 45.0), (shape, 45.0 + twist / 2.0)):
        wires = sh.slice(FreeCAD.Vector(0, 0, 1), 40.0)
        assert len(wires) == 1, len(wires)
        mid = sorted(math.degrees(math.atan2(p.y, p.x)) % 90.0
                     for w in wires for v in w.Vertexes for p in [v.Point])
        assert all(abs(a - want % 90.0) < 0.01 for a in mid), (
            "the mid-height section should sit at %g degrees; got %s"
            % (want, ["%.2f" % a for a in mid]))

    # surface area is the aggregate that actually moves
    assert shape.Area > straight.Area * 1.001, (shape.Area, straight.Area)
    # and the volume, for the record, barely moves at all
    assert abs(shape.Volume / straight.Volume - 1.0) < 1e-5, (
        "if the volume has started moving, the twist is doing something other than twisting")


@test
def test_a_twist_needs_intermediate_sections_and_says_so():
    """Two sections and a twist is a ruled surface between them, not a twist. Refused.

    A quietly-ruled result would look like a twisted blade from a distance and be a different
    shape entirely -- which is worse than an error, because somebody would print it.
    """
    try:
        P.loft(profile="rect", bottom=40.0, top=24.0, height=80.0, sections=2, twist=30.0)
    except ValueError as exc:
        assert "twist" in str(exc).lower() and "section" in str(exc).lower(), str(exc)
        return
    raise AssertionError("a twist across only two sections must be refused")


@test
def test_loft_refuses_a_degenerate_stack():
    for bad, expect in ((dict(bottom=0.0, top=20.0), "bottom"),
                        (dict(bottom=20.0, top=-5.0), "top"),
                        (dict(bottom=20.0, top=20.0, height=0.0), "height"),
                        (dict(bottom=20.0, top=20.0, sections=1), "sections")):
        kwargs = dict(profile="rect", bottom=30.0, top=15.0, height=40.0, sections=3)
        kwargs.update(bad)
        try:
            P.loft(**kwargs)
        except ValueError as exc:
            assert expect in str(exc).lower(), (bad, str(exc))
            continue
        raise AssertionError("%s should have been refused" % bad)


@test
def test_an_unknown_profile_names_the_ones_that_exist():
    for fn, kwargs in ((P.loft, dict(bottom=30.0, top=15.0, height=40.0)),
                       (P.revolve, dict(r_inner=5.0, r_outer=10.0, height=10.0))):
        try:
            fn(profile="aerofoil", **kwargs)
        except ValueError as exc:
            assert "aerofoil" in str(exc), str(exc)
            assert "rect" in str(exc), "the real profiles should be listed: " + str(exc)
            continue
        raise AssertionError("an unknown profile must be refused, naming the real ones")


# --------------------------------------------------------------------- in the catalogue

@test
def test_all_three_are_registered_generators_with_frames_that_build():
    """A generator the catalogue does not list is one the planner cannot reach."""
    from ckwb import planner

    for name in ("loft", "sweep", "revolve"):
        assert name in P.PARTS, sorted(P.PARTS)
        defaults = P.defaults_for(name)
        assert defaults, name
        shape, frames = P.build(name, **defaults)
        assert shape.isValid() and len(shape.Solids) == 1, (name, len(shape.Solids))
        assert frames, name

    catalogue = planner.catalogue()
    for name in ("loft", "sweep", "revolve"):
        assert catalogue[name]["frames"], (
            "%s must publish frames in the catalogue, or the planner cannot mate it" % name)
        assert catalogue[name]["doc"], name


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


# No __main__ guard: freecadcmd imports a script as a module named after its file.
if main():
    sys.exit(1)
