#!/usr/bin/env python3
"""Part generators, named frames and the mate kernel — runs INSIDE FreeCAD.

    "C:/Program Files/FreeCAD 1.0/bin/freecadcmd.exe" workbench/tests/test_geometry.py

Written before ckwb/{parts,mates}.py exist. Every assertion is against geometry that can be
computed independently of this code: closed-form volumes, gear-tooth geometry from the module and
tooth count, and exact seating gaps. No assertion compares against a stored snapshot of our own
output.

Two deliberate choices show up in the tests and are worth stating:

* **Gear radii are measured off vertices, never off `Shape.BoundBox`.** BoundBox bounds a
  B-spline's control polygon rather than its surface, so it over-reports silently and with an
  unchanged volume — the trap that already put a part 0.6 mm proud in this project. The tip and
  root arcs end in real vertices at exactly the tip and root radii, so their distance from the
  axis is an exact measurement rather than an envelope.
* **A frame's +Z points out of the material.** So seating two frames face-to-face means
  coincident origins with anti-parallel Z, and the resulting gap is exactly zero rather than
  nearly zero. That is testable; "looks seated" is not.
"""
from __future__ import annotations

import math
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import FreeCAD  # noqa: E402
import Part  # noqa: E402

from ckwb import parts as P  # noqa: E402
from ckwb.mates import MateError, align, mesh_center_distance, seat, solve  # noqa: E402

TESTS = []
TOL = 1e-7


def test(fn):
    TESTS.append(fn)
    return fn


# --------------------------------------------------------------------------------- plate

@test
def test_plate_volume_is_exact():
    shape, _ = P.plate(width=60.0, depth=40.0, thickness=8.0)
    assert abs(shape.Volume - 60 * 40 * 8) < 1e-6, shape.Volume
    assert shape.isValid() and shape.isClosed()
    assert len(shape.Solids) == 1, len(shape.Solids)


@test
def test_plate_bolt_circle_removes_exactly_the_right_volume():
    """Four holes on a circle: the removed volume is closed-form, so this is an equality test."""
    w, d, t, hd, n = 60.0, 60.0, 8.0, 5.0, 4
    plain, _ = P.plate(width=w, depth=d, thickness=t)
    holed, _ = P.plate(width=w, depth=d, thickness=t, hole_d=hd, bolt_circle=40.0, n_holes=n)
    removed = plain.Volume - holed.Volume
    expect = n * math.pi * (hd / 2) ** 2 * t
    assert abs(removed - expect) < 1e-5, (removed, expect)
    assert len(holed.Solids) == 1, "a bolt circle must not sever the plate"


@test
def test_plate_frames_sit_on_the_faces_and_point_outward():
    _, frames = P.plate(width=60.0, depth=40.0, thickness=8.0)
    assert {"top", "bottom"} <= set(frames), sorted(frames)
    top, bot = frames["top"], frames["bottom"]
    assert abs(top.Base.z - 8.0) < TOL and abs(bot.Base.z) < TOL, (top.Base, bot.Base)
    # +Z out of the material on each face
    assert top.Rotation.multVec(FreeCAD.Vector(0, 0, 1)).z > 0.999
    assert bot.Rotation.multVec(FreeCAD.Vector(0, 0, 1)).z < -0.999


# -------------------------------------------------------------------------------- spacer

@test
def test_spacer_volume_matches_the_annulus_formula():
    od, idia, h = 20.0, 8.0, 6.0
    shape, _ = P.spacer(od=od, id_=idia, height=h)
    expect = math.pi / 4 * (od ** 2 - idia ** 2) * h
    assert abs(shape.Volume - expect) < 1e-5, (shape.Volume, expect)


@test
def test_spacer_refuses_a_bore_wider_than_the_body():
    try:
        P.spacer(od=10.0, id_=12.0, height=5.0)
    except ValueError as exc:
        assert "bore" in str(exc).lower() or "id" in str(exc).lower(), str(exc)
        return
    raise AssertionError("a bore larger than the outside diameter must be refused")


# ------------------------------------------------------------------------ block with bore

@test
def test_block_with_bore_volume_is_exact_and_bore_is_through():
    w, d, h, bd = 50.0, 30.0, 24.0, 22.0
    shape, frames = P.block_with_bore(width=w, depth=d, height=h, bore_d=bd)
    expect = w * d * h - math.pi * (bd / 2) ** 2 * d
    assert abs(shape.Volume - expect) < 1e-4, (shape.Volume, expect)
    assert "bore" in frames and "base" in frames, sorted(frames)
    # the bore axis runs along -Y..+Y through the block, so its frame faces +Y
    assert frames["bore"].Rotation.multVec(FreeCAD.Vector(0, 0, 1)).y > 0.999


# ----------------------------------------------------------------------------------- gear

@test
def test_spur_gear_radii_match_the_module_and_tooth_count():
    m, z, t = 2.0, 24, 10.0
    shape, frames = P.spur_gear(module=m, teeth=z, thickness=t)
    r_outer = m * (z + 2) / 2.0
    r_root = m * (z - 2.5) / 2.0

    # Measured off VERTICES, not Shape.BoundBox. The flanks are B-splines, and BoundBox bounds a
    # spline's control polygon rather than its surface -- it over-reports silently, with the same
    # volume, which is what makes it dangerous. That trap has already put a part 0.6 mm proud in
    # this project. The tip and root arcs terminate in real vertices at exactly r_outer and
    # r_root, so their distance from the axis is an exact measurement.
    radii = [math.hypot(v.Point.x, v.Point.y) for v in shape.Vertexes]
    assert abs(max(radii) - r_outer) < 1e-6, (max(radii), r_outer)
    assert abs(min(radii) - r_root) < 1e-6, (min(radii), r_root)
    assert abs(shape.BoundBox.ZLength - t) < 1e-9, shape.BoundBox.ZLength
    assert len(shape.Solids) == 1 and shape.isClosed()
    # material present just inside the root circle, absent just outside the tip
    assert shape.isInside(FreeCAD.Vector(r_root - 0.2, 0, t / 2), 1e-6, True)
    assert not shape.isInside(FreeCAD.Vector(r_outer + 0.2, 0, t / 2), 1e-6, True)
    assert "axis" in frames and "top" in frames, sorted(frames)


@test
def test_spur_gear_profile_has_four_edges_per_tooth():
    """Two flanks, a tip arc and a root arc, z times. A miscounted loop shows up here first."""
    z = 17
    wire = P.spur_gear_profile(module=1.5, teeth=z)
    assert wire.isClosed(), "the tooth profile must close"
    assert len(wire.Edges) == 4 * z, (len(wire.Edges), 4 * z)


@test
def test_spur_gear_volume_equals_its_profile_area_times_thickness():
    """Separates the 2-D profile from the extrusion: if they disagree, one of them is wrong.

    Relative tolerance, not absolute. Both numbers are numerically integrated over B-spline
    boundaries by different OCC routines -- a surface-area integral and a volume integral -- and
    they agree to about 4 parts in 10,000 rather than to the last bit. That is a property of the
    kernel's quadrature, not slack in the geometry, and the test still catches what it is for: a
    wrong extrusion length, a wrong direction, or a factor error.
    """
    m, z, t = 2.0, 20, 7.0
    wire = P.spur_gear_profile(module=m, teeth=z)
    area = Part.Face(wire).Area
    shape, _ = P.spur_gear(module=m, teeth=z, thickness=t)
    rel = abs(shape.Volume - area * t) / (area * t)
    assert rel < 1e-3, (shape.Volume, area * t, rel)


@test
def test_spur_gear_bore_is_subtracted():
    m, z, t, b = 2.0, 20, 7.0, 8.0
    solid, _ = P.spur_gear(module=m, teeth=z, thickness=t)
    bored, _ = P.spur_gear(module=m, teeth=z, thickness=t, bore_d=b)
    removed = solid.Volume - bored.Volume
    assert abs(removed - math.pi * (b / 2) ** 2 * t) < 1e-6, removed


@test
def test_spur_gear_refuses_too_few_teeth():
    try:
        P.spur_gear(module=2.0, teeth=4, thickness=5.0)
    except ValueError as exc:
        assert "teeth" in str(exc).lower(), str(exc)
        return
    raise AssertionError("an undercut-guaranteed tooth count must be refused")


@test
def test_mesh_centre_distance_is_the_textbook_formula():
    assert abs(mesh_center_distance(2.0, 24, 12) - 2.0 * (24 + 12) / 2.0) < 1e-12
    assert abs(mesh_center_distance(1.5, 20, 20) - 30.0) < 1e-12


# ------------------------------------------------------------------------------ extrusion

@test
def test_extrusion_2020_has_the_right_envelope_and_is_lighter_than_solid():
    shape, frames = P.extrusion_2020(length=100.0)
    bb = shape.BoundBox
    assert abs(bb.XLength - 20.0) < 1e-6 and abs(bb.YLength - 20.0) < 1e-6, (bb.XLength, bb.YLength)
    assert abs(bb.ZLength - 100.0) < 1e-9
    # V-slot stock is roughly half the mass of solid bar; anything near solid means no slots cut
    assert 0.35 < shape.Volume / (20.0 * 20.0 * 100.0) < 0.80, shape.Volume
    assert {"slot_x+", "slot_x-", "slot_y+", "slot_y-", "end"} <= set(frames), sorted(frames)


# ----------------------------------------------------------------------------- ball screw

@test
def test_ball_screw_volume_and_machined_ends_are_exact():
    """SFU1605: 16 mm nominal, 5 mm lead. Ends turned down for BK/BF supports."""
    shape, frames = P.ball_screw(nominal_d=16.0, lead=5.0, length=500.0,
                                 end_d=10.0, end_length=20.0)
    expect = (math.pi / 4 * 16.0 ** 2 * (500.0 - 40.0)
              + 2 * math.pi / 4 * 10.0 ** 2 * 20.0)
    assert abs(shape.Volume - expect) < 1e-3, (shape.Volume, expect)
    assert len(shape.Solids) == 1 and shape.isClosed()
    bb = shape.BoundBox
    assert abs(bb.ZLength - 500.0) < 1e-9, bb.ZLength
    assert abs(bb.XLength - 16.0) < 1e-6, bb.XLength
    assert {"axis", "end_a", "end_b", "nut_travel"} <= set(frames), sorted(frames)


@test
def test_ball_screw_reports_its_lead_so_a_screw_joint_can_use_it():
    """The lead is the number that turns rotation into travel; it must be carried, not implied."""
    _, frames = P.ball_screw(nominal_d=16.0, lead=5.0, length=300.0)
    assert abs(P.ball_screw_lead(nominal_d=16.0, lead=5.0) - 5.0) < 1e-12
    # 1600 steps/rev on a 5 mm lead is 320 steps/mm -- the measured figure for this hardware
    assert abs(1600.0 / P.ball_screw_lead(nominal_d=16.0, lead=5.0) - 320.0) < 1e-9


@test
def test_ball_screw_refuses_ends_that_consume_the_shaft():
    try:
        P.ball_screw(nominal_d=16.0, lead=5.0, length=30.0, end_d=10.0, end_length=20.0)
    except ValueError as exc:
        assert "end" in str(exc).lower(), str(exc)
        return
    raise AssertionError("machined ends longer than the screw must be refused")


@test
def test_ball_nut_volume_matches_its_flange_and_bores():
    shape, frames = P.ball_nut()
    body_od, flange_od, flange_t, ln = 28.0, 48.0, 10.0, 48.0
    bore, bc, bd, n = 16.0, 38.0, 6.5, 4
    expect = (math.pi / 4 * body_od ** 2 * ln
              + math.pi / 4 * (flange_od ** 2 - body_od ** 2) * flange_t
              - math.pi / 4 * bore ** 2 * ln
              - n * math.pi / 4 * bd ** 2 * flange_t)
    assert abs(shape.Volume - expect) < 1e-2, (shape.Volume, expect)
    assert len(shape.Solids) == 1, "the bolt circle must not sever the flange"
    assert {"flange", "bore", "back"} <= set(frames), sorted(frames)


@test
def test_a_nut_runs_on_its_screw_without_interfering():
    """The whole point of the pair: the nut's bore clears the screw's nominal diameter."""
    screw, sf = P.ball_screw(nominal_d=16.0, lead=5.0, length=200.0)
    nut, nf = P.ball_nut()
    placement = align(FreeCAD.Placement(), sf["nut_travel"], nf["bore"])
    moved = nut.copy()
    moved.Placement = placement
    assert screw.common(moved).Volume < 1e-6, "a 16.0 bore on a 16.0 screw must be modelled clear"


# ---------------------------------------------------------------------------- the registry

@test
def test_every_generator_is_registered_with_defaults_from_its_signature():
    """The defaults table IS the signature, so a new parameter cannot be forgotten in two places."""
    assert "plate" in P.PARTS and "spur_gear" in P.PARTS, sorted(P.PARTS)
    for name in P.PARTS:
        defaults = P.defaults_for(name)
        assert isinstance(defaults, dict) and defaults, name
        shape, frames = P.build(name, **defaults)
        assert shape.isValid() and shape.Volume > 0, name
        assert frames, f"{name} exposes no mate frames"


@test
def test_build_refuses_an_unknown_generator_and_a_bad_parameter():
    try:
        P.build("flux_capacitor")
    except KeyError:
        pass
    else:
        raise AssertionError("an unknown generator must raise")
    try:
        P.build("plate", width=60.0, nonsense=1.0)
    except TypeError:
        return
    raise AssertionError("an unknown parameter must raise, not be ignored")


# ------------------------------------------------------------------------------ mate kernel

@test
def test_seat_puts_two_faces_in_contact_with_exactly_zero_gap():
    base, bf = P.plate(width=60.0, depth=60.0, thickness=8.0)
    top, tf = P.plate(width=30.0, depth=30.0, thickness=5.0)
    placement = seat(FreeCAD.Placement(), bf["top"], tf["bottom"])
    moved = top.copy()
    moved.Placement = placement
    gap = base.distToShape(moved)[0]
    assert gap < 1e-9, f"seated faces must touch exactly, gap was {gap}"
    overlap = base.common(moved).Volume
    assert overlap < 1e-9, f"seating must not interpenetrate, overlap {overlap}"
    assert abs(moved.BoundBox.ZMin - 8.0) < 1e-9, moved.BoundBox.ZMin


@test
def test_align_runs_a_shaft_down_a_bore_on_one_axis():
    blk, bfr = P.block_with_bore(width=50.0, depth=30.0, height=24.0, bore_d=22.0)
    shaft, sfr = P.shaft(diameter=21.8, length=60.0)
    placement = align(FreeCAD.Placement(), bfr["bore"], sfr["axis"])
    moved = shaft.copy()
    moved.Placement = placement
    # the shaft's axis must coincide with the bore's axis (block centre, running along Y)
    c = moved.CenterOfMass
    assert abs(c.x - 0.0) < 1e-6 and abs(c.z - 12.0) < 1e-6, c
    assert blk.common(moved).Volume < 1e-6, "a 21.8 shaft must clear a 22.0 bore"


@test
def test_solver_places_a_chain_in_declaration_order():
    shapes = {
        "base": P.plate(width=60.0, depth=60.0, thickness=8.0),
        "mid": P.spacer(od=20.0, id_=8.0, height=6.0),
        "top": P.plate(width=20.0, depth=20.0, thickness=4.0),
    }
    placements = solve(
        shapes,
        ground="base",
        mates=[("seat", "base", "top", "mid", "bottom"),
               ("seat", "mid", "top", "top", "bottom")],
    )
    assert abs(placements["mid"].Base.z - 8.0) < 1e-9, placements["mid"].Base
    assert abs(placements["top"].Base.z - 14.0) < 1e-9, placements["top"].Base


@test
def test_solver_refuses_a_mate_against_an_unplaced_part():
    shapes = {"a": P.plate(width=10.0, depth=10.0, thickness=2.0),
              "b": P.plate(width=10.0, depth=10.0, thickness=2.0),
              "c": P.plate(width=10.0, depth=10.0, thickness=2.0)}
    try:
        solve(shapes, ground="a", mates=[("seat", "b", "top", "c", "bottom")])
    except MateError as exc:
        assert "b" in str(exc), str(exc)
        return
    raise AssertionError("mating onto a part that has no placement yet must raise")


@test
def test_solver_refuses_to_place_a_part_twice():
    """Two mates on one part is an over-constraint we will not silently pick a winner for."""
    shapes = {"a": P.plate(width=20.0, depth=20.0, thickness=2.0),
              "b": P.plate(width=10.0, depth=10.0, thickness=2.0)}
    try:
        solve(shapes, ground="a", mates=[("seat", "a", "top", "b", "bottom"),
                                         ("seat", "a", "bottom", "b", "bottom")])
    except MateError as exc:
        assert "already" in str(exc).lower(), str(exc)
        return
    raise AssertionError("placing one part from two mates must raise")


@test
def test_solver_refuses_an_unknown_frame_name():
    shapes = {"a": P.plate(width=20.0, depth=20.0, thickness=2.0),
              "b": P.plate(width=10.0, depth=10.0, thickness=2.0)}
    try:
        solve(shapes, ground="a", mates=[("seat", "a", "ceiling", "b", "bottom")])
    except MateError as exc:
        assert "ceiling" in str(exc), str(exc)
        return
    raise AssertionError("an unknown frame must raise and list what exists")


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
    if failed:
        sys.exit(1)


# NO `if __name__ == "__main__"` GUARD, deliberately. freecadcmd imports a script as a module
# named after its file -- __name__ is "test_geometry", never "__main__" -- so a guarded main()
# never runs and the suite exits 0 having tested nothing. That is the worst possible failure for
# a test runner, so it is called unconditionally.
main()
