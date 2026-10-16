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
from ckwb.mates import (MateError, align, mesh_center_distance, offset, seat,  # noqa: E402
                        solve)

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
def test_plate_takes_a_centred_bore_and_removes_exactly_a_cylinder():
    """A hole in the middle of a plate.

    This gap was found by the planner, not by me: asked for "a plate 120 by 80 by 10 with a 20mm
    bore through the middle" the model tried a bolt circle of radius zero, was refused, and came
    back saying the feature could not be expressed and it would rather drop it than fake it. It
    was right. A plate with a hole in it is not an exotic request.
    """
    w, d, t, bore = 120.0, 80.0, 10.0, 20.0
    plain, _ = P.plate(width=w, depth=d, thickness=t)
    holed, frames = P.plate(width=w, depth=d, thickness=t, bore_d=bore)
    removed = plain.Volume - holed.Volume
    expect = math.pi * (bore / 2.0) ** 2 * t
    assert abs(removed - expect) < 1e-5, (removed, expect)
    assert len(holed.Solids) == 1 and holed.isValid() and holed.isClosed()
    assert "bore" in frames, sorted(frames)
    axis = frames["bore"]
    assert axis.Base.Length < TOL, axis.Base
    assert axis.Rotation.multVec(FreeCAD.Vector(0, 0, 1)).z > 0.999


@test
def test_plate_bore_coexists_with_a_bolt_circle():
    """The two hole features are independent; a bored plate can still be bolted down."""
    w, d, t = 100.0, 100.0, 8.0
    both, frames = P.plate(width=w, depth=d, thickness=t, bore_d=25.0,
                           hole_d=6.0, bolt_circle=70.0, n_holes=4)
    expect = w * d * t - math.pi * 12.5 ** 2 * t - 4 * math.pi * 3.0 ** 2 * t
    assert abs(both.Volume - expect) < 1e-5, (both.Volume, expect)
    assert len(both.Solids) == 1
    assert {"bore", "hole_0", "hole_3", "top", "bottom"} <= set(frames), sorted(frames)


@test
def test_plate_refuses_a_bore_that_eats_the_plate():
    """A bore wider than the plate is not a plate with a hole, it is two pieces or none."""
    for bad in (80.0, 120.0):
        try:
            P.plate(width=60.0, depth=40.0, thickness=8.0, bore_d=bad)
        except ValueError as exc:
            assert "bore" in str(exc).lower(), str(exc)
        else:
            raise AssertionError("bore_d=%s should be refused on a 60 x 40 plate" % bad)


@test
def test_plate_refuses_a_bore_that_collides_with_its_bolt_circle():
    """Overlapping hole features would merge into one ragged opening; say so instead."""
    try:
        P.plate(width=100.0, depth=100.0, thickness=8.0, bore_d=60.0,
                hole_d=10.0, bolt_circle=64.0, n_holes=4)
    except ValueError as exc:
        assert "bolt circle" in str(exc).lower() or "bore" in str(exc).lower(), str(exc)
    else:
        raise AssertionError("a bore overlapping the bolt circle must be refused")


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


@test
def test_the_c_beam_is_an_80_by_40_section_with_a_full_length_channel():
    """The section a 2020 cannot express, and the reason a rail request had to be declined.

    80 wide, 40 deep, with a 40 x 20 channel down one long face. The channel runs the FULL
    length -- cutting a window in the middle of the face instead is a bug that shipped in
    gear-cutter's first beam model and made every upright wrong -- so the removed volume is
    exactly `40 * 20 * length`.
    """
    length = 200.0
    shape, frames = P.c_beam(length=length)
    assert len(shape.Solids) == 1, len(shape.Solids)
    bb = shape.BoundBox
    assert abs(bb.XLength - 80.0) < 1e-9, bb.XLength
    assert abs(bb.YLength - 40.0) < 1e-9, bb.YLength
    assert abs(bb.ZLength - length) < 1e-9, bb.ZLength
    removed = 80.0 * 40.0 * length - shape.Volume
    assert abs(removed - 40.0 * 20.0 * length) < 1e-6, ("channel is not full length", removed)
    for want in ("axis", "channel", "top", "side_x+", "side_x-", "end", "end_near"):
        assert want in frames, (want, sorted(frames))

    # The channel's own size is a PARAMETER, not a constant buried in the generator, because a
    # dimension that is not a parameter is one nothing downstream can reason about -- measured:
    # the planner refused to put anything in the channel while its size was hidden, saying so.
    defaults = P.defaults_for("c_beam")
    for key, want in (("width", 80.0), ("depth", 40.0),
                      ("channel_w", 40.0), ("channel_d", 20.0)):
        assert defaults[key] == want, (key, defaults[key])
    narrow, _f = P.c_beam(length=100.0, channel_w=20.0, channel_d=10.0)
    assert abs((80.0 * 40.0 * 100.0 - narrow.Volume) - 20.0 * 10.0 * 100.0) < 1e-6, narrow.Volume
    for bad in ({"channel_w": 80.0}, {"channel_d": 40.0}):
        try:
            P.c_beam(length=100.0, **bad)
        except ValueError:
            continue
        raise AssertionError("a channel that consumes the section must be refused: %s" % bad)


@test
def test_the_c_beam_channel_frame_sits_on_the_channel_FLOOR_not_its_mouth():
    """Where a screw actually mounts. Off by the channel depth is off by 20 mm."""
    shape, frames = P.c_beam(length=100.0)
    floor_y = -40.0 / 2.0 + 20.0        # 20 mm up from the open face
    assert abs(frames["channel"].Base.y - floor_y) < 1e-9, frames["channel"].Base
    # and it is really solid just above the floor, open just below it
    probe_in = Part.makeBox(1.0, 1.0, 1.0, FreeCAD.Vector(-0.5, floor_y + 0.5, 10.0))
    probe_out = Part.makeBox(1.0, 1.0, 1.0, FreeCAD.Vector(-0.5, floor_y - 1.5, 10.0))
    assert shape.common(probe_in).Volume > 0.9, "above the channel floor must be material"
    assert shape.common(probe_out).Volume < 1e-9, "below the channel floor must be open"
    # +Z runs along the beam, so an offset mate displaces ACROSS the channel, not along it
    along = frames["channel"].Rotation.multVec(FreeCAD.Vector(0, 0, 1))
    assert abs(along.z - 1.0) < 1e-9, along


@test
def test_an_offset_mate_puts_a_part_BESIDE_another_one():
    """The mate seat and align could not do, and the gap that forced an honest refusal.

    Both of those land the moving frame exactly ON the target frame, so everything ended up
    collinear. A rod 30 mm to the side of a screw is an offset mate and nothing else.
    """
    _rail, rf = P.c_beam(length=200.0)
    _rod, rodf = P.shaft(diameter=16.0, length=200.0)
    at_zero = offset(FreeCAD.Placement(), rf["axis"], rodf["axis"])
    assert at_zero.Base.Length < 1e-9, ("a zero offset is an align", at_zero.Base)
    moved = offset(FreeCAD.Placement(), rf["axis"], rodf["axis"], dx=30.0)
    assert abs(moved.Base.x - 30.0) < 1e-9, moved.Base
    assert abs(moved.Base.y) < 1e-9 and abs(moved.Base.z) < 1e-9, moved.Base
    both = offset(FreeCAD.Placement(), rf["axis"], rodf["axis"], dx=30.0, dy=-5.0, dz=12.0)
    assert abs(both.Base.x - 30.0) < 1e-9 and abs(both.Base.y + 5.0) < 1e-9 \
        and abs(both.Base.z - 12.0) < 1e-9, both.Base


@test
def test_an_offset_is_measured_in_the_TARGET_FRAME_and_not_the_world():
    """The property that makes it usable: the caller never does trigonometry.

    A beam turned on its side still has an `axis` frame whose +Z runs along it, so "30 mm to the
    side" stays 30 mm to the side of the beam. Measuring in world axes instead would put the rod
    somewhere that depends on how the assembly happens to be rotated.
    """
    _rail, rf = P.c_beam(length=200.0)
    _rod, rodf = P.shaft(diameter=16.0, length=200.0)
    # lay the beam down: its +Z now runs along world -Y
    turned = FreeCAD.Placement(FreeCAD.Vector(),
                               FreeCAD.Rotation(FreeCAD.Vector(1, 0, 0), 90.0))
    placed = offset(turned, rf["axis"], rodf["axis"], dx=30.0)
    # still 30 mm along the beam frame's own +X, whatever that is in world terms
    beam_x = turned.multiply(rf["axis"]).Rotation.multVec(FreeCAD.Vector(1, 0, 0))
    assert abs(placed.Base.dot(beam_x) - 30.0) < 1e-9, (placed.Base, beam_x)
    assert placed.Base.Length - 30.0 < 1e-9, "the offset must not pick up a world component"


@test
def test_the_solver_carries_offset_options_through_a_chain():
    length = 200.0
    parts = {
        "Beam": P.c_beam(length=length),
        "Screw": P.ball_screw(length=length),
        "RodA": P.shaft(diameter=16.0, length=length),
        "RodB": P.shaft(diameter=16.0, length=length),
    }
    places = solve(parts, "Beam", [
        ("align", "Beam", "channel", "Screw", "axis"),
        ("offset", "Beam", "channel", "RodA", "axis", {"dx": 30.0}),
        ("offset", "Beam", "channel", "RodB", "axis", {"dx": -30.0}),
    ])
    assert abs(places["Screw"].Base.Length - abs(places["Beam"].Base.Length)) < 20.0
    assert abs(places["RodA"].Base.x - 30.0) < 1e-9, places["RodA"].Base
    assert abs(places["RodB"].Base.x + 30.0) < 1e-9, places["RodB"].Base
    # the two rods are 60 mm apart and neither is on the screw's axis
    assert abs(places["RodA"].Base.x - places["RodB"].Base.x - 60.0) < 1e-9
    assert abs(places["Screw"].Base.x) < 1e-9, places["Screw"].Base


@test
def test_the_requested_rail_assembly_now_BUILDS_and_nothing_interferes():
    """The exact request that had to be refused: 200 mm rail, 1605 screw, two 16 mm rods, C-beam.

    This is the test that says the refusal is retired. The screw runs down the channel, a rod
    sits either side of it, and the solids are checked for real clearance -- because "it placed
    them" and "they fit" are different claims, and a stack-up that interferes is not a rail.
    """
    length = 200.0
    parts = {
        "Beam": P.c_beam(length=length),
        "Screw": P.ball_screw(nominal_d=16.0, lead=5.0, length=length),
        "Nut": P.ball_nut(nominal_d=16.0),
        "RodA": P.shaft(diameter=16.0, length=length),
        "RodB": P.shaft(diameter=16.0, length=length),
    }
    # The screw goes on the CHANNEL CENTRELINE, 10 mm in from the floor frame -- not on the
    # floor, which buries half of it, and this is the same constraint gear-cutter records for
    # real: a KFL08 flange is 27 mm tall in a 40 mm channel, so the screw sits mid-channel.
    # The rods run parallel to the beam on its flat face, which is why they mate to `axis`
    # (whose +Z runs along the length) and not to `top` (whose +Z points out of the face and
    # would lay them across the beam instead).
    places = solve(parts, "Beam", [
        ("offset", "Beam", "channel", "Screw", "axis", {"dy": -10.0}),
        ("align", "Screw", "nut_travel", "Nut", "bore"),
        ("offset", "Beam", "axis", "RodA", "axis", {"dx": 30.0, "dy": 30.0}),
        ("offset", "Beam", "axis", "RodB", "axis", {"dx": -30.0, "dy": 30.0}),
    ])
    assert len(places) == 5, sorted(places)

    placed = {}
    for name, (shape, _frames) in parts.items():
        copy = shape.copy()
        copy.Placement = places[name]
        placed[name] = copy

    # the screw lives in the channel, clear of the beam
    assert placed["Screw"].common(placed["Beam"]).Volume < 1e-6, "the screw cuts into the beam"
    # the two rods are 60 mm apart and clear of each other and of the screw
    for a, b in (("RodA", "RodB"), ("RodA", "Screw"), ("RodB", "Screw")):
        assert placed[a].common(placed[b]).Volume < 1e-6, ("interference", a, b)
    # the nut rides the screw
    assert placed["Nut"].distToShape(placed["Screw"])[0] < 1e-6, "the nut is not on the screw"


@test
def test_an_offset_option_the_mate_does_not_take_is_refused_by_name():
    parts = {"Beam": P.c_beam(length=100.0), "Rod": P.shaft(length=100.0)}
    try:
        solve(parts, "Beam", [("offset", "Beam", "axis", "Rod", "axis", {"dw": 5.0})])
    except MateError as exc:
        assert "dw" in str(exc) and "dx" in str(exc), str(exc)
    else:
        raise AssertionError("an unknown option must be refused")
    try:
        solve(parts, "Beam", [("align", "Beam", "axis", "Rod", "axis", {"dx": 5.0})])
    except MateError as exc:
        assert "dx" in str(exc), ("align takes no options", str(exc))
        return
    raise AssertionError("align must refuse an offset option rather than ignore it")


@test
def test_the_profile_rail_is_an_envelope_with_real_mounting_holes():
    """An envelope, declared as one, and still the right shape to lay out against.

    A real rail's function is two precision-ground raceways down its flanks and nothing here can
    express them, which is exactly what `ball_bearing` says about balls and races. What this has
    to get right is the envelope and the hole pitch, because those are what a carriage height, a
    clearance and a bolt pattern are measured from.
    """
    shape, frames = P.profile_rail(width=15.0, height=15.0, length=200.0, hole_pitch=60.0)
    assert len(shape.Solids) == 1, len(shape.Solids)
    bb = shape.BoundBox
    assert abs(bb.XLength - 15.0) < 1e-9 and abs(bb.YLength - 15.0) < 1e-9, bb
    assert abs(bb.ZLength - 200.0) < 1e-9, bb
    # three holes at 60 mm on a 200 mm rail, and they really are through holes
    solid_box = 15.0 * 15.0 * 200.0
    per_hole = math.pi * (4.5 / 2.0) ** 2 * 15.0
    assert abs((solid_box - shape.Volume) - 3 * per_hole) < 1.0, solid_box - shape.Volume
    for want in ("axis", "top", "base", "end", "end_near"):
        assert want in frames, (want, sorted(frames))
    assert P.profile_rail(hole_d=0.0)[0].Volume > shape.Volume, "hole_d 0 means a plain bar"


@test
def test_the_rail_carriage_STRADDLES_its_rail_with_real_clearance():
    """Composed from a box and a channel -- which is the honest answer to "surely you can make it".

    The ball tracks and end caps are not modelled. What is modelled is a block that occupies
    space over the rail with a stated clearance, so that a clash check between carriage and rail
    means something instead of being true by construction.
    """
    rail, rf = P.profile_rail(width=15.0, height=15.0, length=200.0)
    car, cf = P.rail_carriage(rail_width=15.0, rail_height=15.0, clearance=0.3)
    assert len(car.Solids) == 1, len(car.Solids)

    places = solve({"Rail": (rail, rf), "Carriage": (car, cf)}, "Rail",
                   [("align", "Rail", "axis", "Carriage", "rail")])
    a, b = rail.copy(), car.copy()
    a.Placement, b.Placement = places["Rail"], places["Carriage"]
    assert a.common(b).Volume < 1e-6, ("a carriage must not cut into its rail",
                                       a.common(b).Volume)
    assert abs(a.distToShape(b)[0] - 0.3) < 1e-6, ("the clearance must be the stated one",
                                                   a.distToShape(b)[0])
    # the carriage top is the height a gantry plate actually sits at, measured from the rail's
    # own mounting face -- the number that is easiest to get wrong by one rail height
    assert abs(cf["top"].Base.y - rf["base"].Base.y - 24.0) < 1e-9, cf["top"].Base
    # and `axis` means the same thing on the rail as on every other long part: the centreline
    assert abs(rf["axis"].Base.y) < 1e-9, ("the rail's axis is its centreline", rf["axis"].Base)
    for other in ("shaft", "ball_screw", "c_beam"):
        _s, frames = P.build(other, **P.defaults_for(other))
        assert abs(frames["axis"].Base.y) < 1e-9, (other, frames["axis"].Base)


@test
def test_a_carriage_that_cannot_wrap_or_clear_its_rail_is_refused():
    for kw, word in (({"width": 12.0}, "straddle"), ({"height": 10.0}, "clear")):
        try:
            P.rail_carriage(rail_width=15.0, rail_height=15.0, **kw)
        except ValueError as exc:
            assert word in str(exc), (kw, str(exc))
            continue
        raise AssertionError("a carriage that does not fit its rail must be refused: %s" % kw)


@test
def test_a_whole_LINEAR_AXIS_assembles_from_the_catalogue_and_nothing_interferes():
    """The request that started all this, built the way one is actually built.

    A C-beam, a profile rail bolted to its top face, a carriage riding the rail, and the ball
    screw down the channel. Every one of these was impossible a day ago: there was no C-beam, no
    profile rail, no carriage, and no mate that could place anything beside anything.

    The ball NUT is deliberately not in here, and the test below says why: it does not fit.
    """
    length = 200.0
    parts = {
        "Beam": P.c_beam(length=length),
        "Rail": P.profile_rail(width=15.0, height=15.0, length=length),
        "Carriage": P.rail_carriage(rail_width=15.0, rail_height=15.0, length=40.0),
        "Screw": P.ball_screw(nominal_d=16.0, lead=5.0, length=length),
    }
    places = solve(parts, "Beam", [
        # `axis` is the CENTRELINE on every long part here, so seating a 15 mm rail on a face
        # 20 mm up is 20 + 7.5. A live planner got this right for the round parts and wrong for
        # the rail while the rail's `axis` sat at its base instead -- hence the convention.
        ("offset", "Beam", "axis", "Rail", "axis", {"dy": 27.5}),
        ("align", "Rail", "axis", "Carriage", "rail"),
        # The screw sits on the channel CENTRELINE -- 10 mm in from the floor frame, not on the
        # floor, which would bury half of it. gear-cutter records the same constraint for real.
        ("offset", "Beam", "channel", "Screw", "axis", {"dy": -10.0}),
    ])
    assert len(places) == 4, sorted(places)

    placed = {}
    for name, (shape, _f) in parts.items():
        copy = shape.copy()
        copy.Placement = places[name]
        placed[name] = copy

    # the rail is ON the beam: touching it, and not inside it
    assert placed["Rail"].common(placed["Beam"]).Volume < 1e-6, "the rail cuts into the beam"
    assert placed["Rail"].distToShape(placed["Beam"])[0] < 1e-6, "the rail floats above the beam"
    # the carriage rides the rail at its running clearance and touches nothing else
    assert abs(placed["Carriage"].distToShape(placed["Rail"])[0] - 0.3) < 1e-6, \
        placed["Carriage"].distToShape(placed["Rail"])[0]
    names = sorted(placed)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            shared = placed[a].common(placed[b]).Volume
            assert shared < 1e-6, (a, b, shared)


@test
def test_an_SFU1605_FLANGED_NUT_DOES_NOT_FIT_inside_a_c_beam_channel():
    """A real hardware fact the geometry found, and the sort of thing this project is for.

    An SFU1605 single nut has a **48 mm flange**. A C-beam channel is **40 mm wide and 20 mm
    deep**. So a screw running down the channel cannot carry its own nut in there -- measured at
    7,092 mm3 of the nut buried in the beam with the screw on the channel centreline, which is
    where the screw belongs.

    It is not a modelling error and it does not go away by moving the screw about inside the
    channel. The nut clears only when the screw axis is a full flange RADIUS clear of the beam's
    material, which for this section is 24 mm below its open face -- the screw hanging under the
    beam rather than running in it. In a real C-beam gantry the nut is bolted to the moving plate
    outside the extrusion for exactly this reason.
    """
    flange_od = P.defaults_for("ball_nut")["flange_od"]
    assert flange_od == 48.0, flange_od
    channel_w, channel_d, half_depth = 40.0, 20.0, 20.0
    assert flange_od > channel_w, "the premise: the flange is wider than the channel"

    def overlap(dy):
        parts = {"Beam": P.c_beam(length=200.0),
                 "Screw": P.ball_screw(nominal_d=16.0, lead=5.0, length=200.0),
                 "Nut": P.ball_nut(nominal_d=16.0)}
        places = solve(parts, "Beam", [
            ("offset", "Beam", "channel", "Screw", "axis", {"dy": dy}),
            ("align", "Screw", "nut_travel", "Nut", "bore")])
        placed = {}
        for name, (shape, _f) in parts.items():
            copy = shape.copy()
            copy.Placement = places[name]
            placed[name] = copy
        return (placed["Screw"].common(placed["Beam"]).Volume,
                placed["Nut"].common(placed["Beam"]).Volume)

    # on the channel centreline, where the screw belongs: the screw is fine, the nut is not
    screw_in, nut_in = overlap(-10.0)
    assert screw_in < 1e-6, ("the screw itself fits the channel", screw_in)
    assert nut_in > 7000.0, ("the nut does not", nut_in)

    # and it does not come right by shuffling it down the channel
    assert overlap(-20.0)[1] > 1000.0, overlap(-20.0)
    assert overlap(-30.0)[1] > 1.0, overlap(-30.0)

    # It clears exactly where closed form says it does, and this is the satisfying part: the
    # binding constraint is not the flange's full radius but the CHANNEL CORNER at
    # (+/-channel_w/2, -half_depth). The flange clears once its circle passes outside that
    # corner, so the axis must drop
    #
    #     sqrt(flange_r^2 - (channel_w/2)^2)
    #
    # below the mouth -- 13.27 mm here, not the 24 mm a whole-radius argument gives. Measured
    # against the solids: 31.4 mm3 of overlap at -31, 10.2 at -32, 0.47 at -33, and 0.0000 at
    # -33.27. The relationship is the assertion; the numbers are this section and this nut.
    flange_r = flange_od / 2.0
    clears_at = -(half_depth + math.sqrt(flange_r ** 2 - (channel_w / 2.0) ** 2))
    assert abs(clears_at + 33.2665) < 1e-3, clears_at
    assert overlap(clears_at)[1] < 1e-6, (
        "clearing the channel corner must be enough", overlap(clears_at))
    assert overlap(clears_at + 1.0)[1] > 1e-6, (
        "and a millimetre less must not be, or this is not the real limit",
        overlap(clears_at + 1.0))
    assert abs(channel_d - half_depth) < 1e-9, (
        "this arithmetic assumes the channel floor is the section's mid-depth", channel_d)


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
