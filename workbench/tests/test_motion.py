#!/usr/bin/env python3
"""Coupled motion: slides, screws, belts — and checking a mechanism through its travel.

    "C:/Program Files/FreeCAD 1.0/bin/freecadcmd.exe" workbench/tests/test_motion.py

This is the deliberate alternative to wiring FreeCAD's Assembly solver, and the reasoning is
worth stating because the solver is *right there*.

Reconnaissance on it (2026-10-16, headless): an `Assembly::AssemblyObject` creates and exposes
`solve()`; a `Slider`/`Screw`/`Belt`/`RackPinion` joint proxy constructs once the joint sits
inside the assembly; grounding works; and `Placement1`/`Placement2` are plain placements, so a
joint need not name "Face3" — the topological-naming problem is avoidable. But `isPartConnected`
stays False and driving the joint moves nothing: the solver wants its `Reference` properties in a
path form that is not documented and was not cracked.

And for the hardware here, a solver is not what is missing. A ball nut at a given travel, a
carriage at a given position, a pulley at a given angle, a belt length for a centre distance —
all of that is **closed form**. A numerical solver would return an approximation to an answer
arithmetic gives exactly, and bring an iteration count and a convergence failure mode along with
it. So these are exact placements, like every other mate in this kernel.

What a solver looked like it would buy that this did not: mechanism DOF analysis and loop
closure. That turned out not to need a solver either -- mobility is a property of the joint
GRAPH, and `ckwb/mobility.py` counts it in closed form. See that module for the one real trap.

The payoff test is the last one. A static check at the ends of a travel passes a mechanism that
collides in the middle, which is the whole reason to sweep.
"""
from __future__ import annotations

import math
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import FreeCAD  # noqa: E402

from ckwb import checks as C  # noqa: E402
from ckwb import commands as K  # registers commands  # noqa: E402,F401
from ckwb import document as D  # noqa: E402
from ckwb import motion as M  # noqa: E402
from ckwb import parts as P  # noqa: E402
from ckwb.registry import registry  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def new_doc(name="motion"):
    for existing in list(FreeCAD.listDocuments()):
        FreeCAD.closeDocument(existing)
    doc = FreeCAD.newDocument(name)
    FreeCAD.setActiveDocument(name)
    return doc


# ------------------------------------------------------------------------ the arithmetic

@test
def test_screw_travel_is_turns_times_lead():
    assert abs(M.screw_travel(lead=5.0, turns=1.0) - 5.0) < 1e-12
    assert abs(M.screw_travel(lead=5.0, turns=12.5) - 62.5) < 1e-12
    assert abs(M.screw_travel(lead=10.0, turns=-2.0) + 20.0) < 1e-12


@test
def test_screw_turns_and_steps_match_the_measured_hardware():
    """1605 at 1600 steps/rev is 320 steps/mm — measured on the bench, not derived here."""
    assert abs(M.screw_turns(lead=5.0, distance=62.5) - 12.5) < 1e-12
    assert abs(M.steps_per_mm(lead=5.0, steps_per_rev=1600) - 320.0) < 1e-9
    # an SFU1610 is the same diameter and half the steps/mm; the lead is what decides it
    assert abs(M.steps_per_mm(lead=10.0, steps_per_rev=1600) - 160.0) < 1e-9


@test
def test_screw_arithmetic_refuses_a_zero_lead():
    for bad in (0.0, -5.0):
        try:
            M.screw_turns(lead=bad, distance=10.0)
        except ValueError:
            continue
        raise AssertionError(f"lead {bad} must be refused")


@test
def test_belt_ratio_is_driven_over_driver():
    assert abs(M.belt_ratio(teeth_driver=20, teeth_driven=60) - 3.0) < 1e-12
    assert abs(M.belt_ratio(teeth_driver=60, teeth_driven=20) - (1.0 / 3.0)) < 1e-12
    # the A-axis two-stage 9:1 is 3:1 twice
    assert abs(M.belt_ratio(20, 60) * M.belt_ratio(20, 60) - 9.0) < 1e-12


@test
def test_belt_length_reduces_to_the_closed_form_for_equal_pulleys():
    """Two equal pulleys: L = 2C + pi*D exactly. A formula that misses this is wrong."""
    pitch, teeth, centre = 8.0, 30, 120.0
    d = M.pitch_diameter(teeth, pitch)
    expect = 2.0 * centre + math.pi * d
    got = M.belt_length(centre_distance=centre, teeth_a=teeth, teeth_b=teeth, pitch=pitch)
    assert abs(got - expect) < 1e-9, (got, expect)


@test
def test_pitch_diameter_is_pitch_times_teeth_over_pi():
    # HTD-8M, 30 teeth: pitch diameter 76.39 mm
    assert abs(M.pitch_diameter(30, 8.0) - 8.0 * 30 / math.pi) < 1e-12
    assert abs(M.pitch_diameter(30, 8.0) - 76.3944) < 1e-3


@test
def test_belt_length_grows_with_centre_distance_and_is_invertible():
    """The number you actually need: you buy a belt, then you need the centre distance."""
    pitch, a, b = 8.0, 20, 60
    short = M.belt_length(100.0, a, b, pitch)
    long = M.belt_length(200.0, a, b, pitch)
    # Doubling the centre distance adds TWO straight runs, so the growth approaches 2 * delta C
    # from below: the wrap angles shrink as the pulleys separate and give a little back. An
    # earlier version of this test demanded > 190 mm and failed against correct arithmetic.
    growth = long - short
    assert 180.0 < growth < 200.0, (short, long, growth)

    for teeth in (80, 93, 120):
        length = teeth * pitch
        centre = M.belt_centre_distance(belt_teeth=teeth, teeth_a=a, teeth_b=b, pitch=pitch)
        back = M.belt_length(centre, a, b, pitch)
        assert abs(back - length) < 1e-6, (teeth, centre, back, length)


@test
def test_belt_centre_distance_refuses_a_belt_that_cannot_wrap():
    """A belt shorter than the pulleys themselves has no solution; say so rather than return one."""
    try:
        M.belt_centre_distance(belt_teeth=10, teeth_a=60, teeth_b=60, pitch=8.0)
    except ValueError as exc:
        assert "short" in str(exc).lower() or "wrap" in str(exc).lower(), str(exc)
        return
    raise AssertionError("an impossible belt must raise")


# ---------------------------------------------------------------------------- placements

@test
def test_slide_moves_exactly_the_requested_distance_along_the_shared_axis():
    rail, rf = P.extrusion_2020(length=300.0)
    car, cf = P.plate(width=40.0, depth=40.0, thickness=10.0)
    at_zero = M.slide(FreeCAD.Placement(), rf["slot_x+"], cf["bottom"], 0.0)
    at_fifty = M.slide(FreeCAD.Placement(), rf["slot_x+"], cf["bottom"], 50.0)
    delta = at_fifty.Base.sub(at_zero.Base)
    assert abs(delta.Length - 50.0) < 1e-9, delta.Length
    # the slot frame points along +X, so travel is along the rail's own axis, not world Z
    assert abs(delta.x - 50.0) < 1e-9, delta


@test
def test_a_nut_travels_along_its_screw_by_lead_times_turns():
    screw, sf = P.ball_screw(nominal_d=16.0, lead=5.0, length=400.0)
    nut, nf = P.ball_nut()
    turns = 20.0
    placement = M.screw_drive(FreeCAD.Placement(), sf["axis"], nf["bore"],
                              turns=turns, lead=5.0)
    moved = nut.copy()
    moved.Placement = placement
    bb = moved.BoundBox          # a Shape, not a document object: no .Shape on it
    centre = (bb.ZMin + bb.ZMax) / 2.0
    assert abs(centre - turns * 5.0) < 1e-6, centre
    assert screw.common(moved).Volume < 1e-6, "the nut must still clear the screw"


@test
def test_rotate_turns_a_pulley_about_its_own_axis_without_moving_it():
    gear, gf = P.spur_gear(module=2.0, teeth=24, thickness=8.0, bore_d=8.0)
    placement = M.rotate(FreeCAD.Placement(), gf["axis"], gf["axis"], 90.0)
    moved = gear.copy()
    moved.Placement = placement
    # Bounding-box centre, not CenterOfMass: a Part.Compound does not expose one in FreeCAD 1.0.
    # Exact here anyway -- 90 degrees is six whole 15-degree tooth pitches on a 24-tooth gear, so
    # the rotated gear occupies precisely the same box.
    a, b = gear.BoundBox, moved.BoundBox
    for lo, hi in (("XMin", "XMax"), ("YMin", "YMax"), ("ZMin", "ZMax")):
        assert abs((getattr(a, lo) + getattr(a, hi)) / 2.0
                   - (getattr(b, lo) + getattr(b, hi)) / 2.0) < 1e-6, (lo, a, b)

    # A 24-tooth gear turned by exactly one tooth pitch maps onto itself. That is a real property
    # of the geometry, not of this code, which makes it a good check on both.
    one_tooth = M.rotate(FreeCAD.Placement(), gf["axis"], gf["axis"], 360.0 / 24.0)
    spun = gear.copy()
    spun.Placement = one_tooth
    assert abs(spun.Volume - gear.Volume) < 1e-6
    assert spun.common(gear).Volume > 0.98 * gear.Volume, \
        "turning by one tooth pitch should land back on itself"


@test
def test_a_coupled_pair_turns_by_the_ratio():
    """Driver 20T, driven 60T: three driver turns per driven turn, in the opposite sense."""
    assert abs(M.coupled_angle(angle=360.0, teeth_driver=20, teeth_driven=60) - 120.0) < 1e-9
    assert abs(M.coupled_angle(angle=90.0, teeth_driver=60, teeth_driven=20) - 270.0) < 1e-9


# ------------------------------------------------------------------------------ the sweep

@test
def test_sweep_stations_cover_the_travel_inclusively():
    stations = M.sweep_stations(0.0, 100.0, 5)
    assert stations == [0.0, 25.0, 50.0, 75.0, 100.0], stations
    assert M.sweep_stations(10.0, 10.0, 3) == [10.0, 10.0, 10.0]
    try:
        M.sweep_stations(0.0, 10.0, 1)
    except ValueError:
        return
    raise AssertionError("a sweep needs at least two stations")


@test
def test_a_sweep_finds_a_collision_that_both_endpoints_miss():
    """The reason to sweep at all.

    A spacer rides a shaft; a post stands beside the shaft partway along. Checked at either end
    of the travel the mechanism is clean, and halfway along the spacer hits the post. No amount
    of examining the ends reveals it, which is exactly the trap a static clearance check sets.
    """
    doc = new_doc()
    shaft = D.make_part(doc, "shaft", None, name="Shaft")
    shaft.Diameter, shaft.Length = 10.0, 300.0          # centred: z -150 .. 150
    carriage = D.make_part(doc, "spacer", None, name="Carriage")
    carriage.Od, carriage.Id, carriage.Height = 40.0, 12.0, 10.0
    post = D.make_part(doc, "plate", None, name="Post")
    post.Width, post.Depth, post.Thickness = 12.0, 12.0, 60.0
    doc.recompute()

    # Beside the shaft (x 19..31, within the carriage radius of 20) and clear of z=0, so the
    # untravelled model is clean.
    post.Placement = FreeCAD.Placement(FreeCAD.Vector(25.0, 0.0, 60.0), FreeCAD.Rotation())
    doc.recompute()

    static = registry.call("run_checks")
    assert static["checks"]["interference"]["ok"] is True, \
        "the static check must pass here; that it does is the whole point"

    res = registry.call("sweep", moving="Carriage", target="Shaft", target_frame="axis",
                        moving_frame="axis", start=-140.0, stop=130.0, stations=28)
    assert res["ok"] is False, res
    assert res["clash_stations"], res
    assert min(res["clash_stations"]) > 0.0, res["clash_stations"]
    assert max(res["clash_stations"]) < 120.0, res["clash_stations"]
    assert all(c["clashes"][0]["with"] == "Post" for c in res["clashes"]), res["clashes"]


@test
def test_a_sweep_with_clearance_everywhere_passes_and_reports_the_worst_gap():
    doc = new_doc()
    shaft = D.make_part(doc, "shaft", None, name="Shaft")
    shaft.Diameter, shaft.Length = 10.0, 300.0
    carriage = D.make_part(doc, "spacer", None, name="Carriage")
    carriage.Od, carriage.Id, carriage.Height = 40.0, 12.0, 10.0
    doc.recompute()

    res = registry.call("sweep", moving="Carriage", target="Shaft", target_frame="axis",
                        moving_frame="axis", start=-140.0, stop=130.0, stations=9)
    assert res["ok"] is True, res
    assert res["stations"] == 9
    assert res["clash_stations"] == []
    # a 12 mm bore on a 10 mm shaft is 1 mm of radial clearance
    assert abs(res["min_gap_mm"] - 1.0) < 1e-3, res["min_gap_mm"]


@test
def test_the_sweep_restores_the_part_to_where_it_started():
    """A check must not leave the model moved; that would be a side effect masquerading as a test."""
    doc = new_doc()
    D.make_part(doc, "shaft", None, name="Shaft")
    car = D.make_part(doc, "spacer", None, name="Carriage")
    doc.recompute()
    before = tuple(round(v, 6) for v in car.Placement.Base)
    registry.call("sweep", moving="Carriage", target="Shaft", target_frame="axis",
                  moving_frame="axis", start=0.0, stop=80.0, stations=5)
    after = tuple(round(v, 6) for v in car.Placement.Base)
    assert before == after, (before, after)


@test
def test_sweep_is_a_registered_read_only_command():
    by_name = {t["name"]: t for t in registry.schema()}
    assert "sweep" in by_name, sorted(by_name)
    assert by_name["sweep"]["writes"] is False, \
        "a sweep inspects the model and restores it; it is not a write"
    required = by_name["sweep"]["input_schema"]["required"]
    for arg in ("moving", "target", "target_frame", "moving_frame", "start", "stop"):
        assert arg in required, required


# ------------------------------------------------------------------------------ gear rack

@test
def test_rack_travel_is_the_pitch_circumference_per_turn():
    """One turn of the pinion rolls its whole pitch circumference along the rack.

    So the travel per revolution is `pi * m * z`, which is the circular pitch times the tooth
    count -- the pinion lays down exactly as many teeth as it has.
    """
    m, z = 2.0, 20
    per_turn = M.rack_travel(teeth=z, module=m, turns=1.0)
    assert abs(per_turn - math.pi * m * z) < 1e-12, per_turn
    assert abs(per_turn - 125.6637) < 1e-3, per_turn
    assert abs(M.rack_travel(teeth=z, module=m, turns=2.5) - 2.5 * per_turn) < 1e-9
    assert M.rack_travel(teeth=z, module=m, turns=-1.0) == -per_turn, "it must run backwards too"


@test
def test_rack_turns_inverts_rack_travel():
    for turns in (0.25, 1.0, 7.5):
        d = M.rack_travel(teeth=17, module=1.5, turns=turns)
        assert abs(M.rack_turns(teeth=17, module=1.5, distance=d) - turns) < 1e-12, turns


@test
def test_a_rack_drive_is_far_COARSER_than_the_ballscrew_and_the_numbers_say_why():
    """Why neither machine on this bench uses a rack, in one comparison.

    A 20-tooth module-2 pinion on a 1.8 degree motor at 16x microstepping gives 25.5 steps/mm.
    The 1605 ballscrew on the same motor gives **320** -- the figure measured on the bench in
    #12 -- so the rack has about an **eighth** of the resolution. A rack earns its place on long
    travels where a screw would whip, not on these.
    """
    rack = M.rack_steps_per_mm(teeth=20, module=2.0, steps_per_rev=3200)
    assert abs(rack - 3200.0 / (math.pi * 2.0 * 20)) < 1e-9, rack
    assert abs(rack - 25.46) < 0.01, rack
    screw = M.steps_per_mm(lead=5.0, steps_per_rev=1600)
    assert abs(screw - 320.0) < 1e-9, screw
    assert screw > 12.0 * rack, (screw, rack)


@test
def test_the_pinion_axis_sits_at_the_PITCH_RADIUS_above_the_pitch_line():
    """Pitch-line contact: the one dimension that makes a rack mesh rather than jam or slip.

    Standard centre height is the pinion's pitch radius, `m*z/2`, measured from the rack's pitch
    line -- NOT from its tooth tips and NOT from its back face. Getting this wrong by the
    addendum is the classic rack mistake, and here it is a single named function.
    """
    assert abs(M.rack_centre_height(teeth=20, module=2.0) - 20.0) < 1e-12
    assert abs(M.rack_centre_height(teeth=17, module=1.5) - 12.75) < 1e-12


@test
def test_a_pinion_ROLLS_on_a_rack_rather_than_sliding_along_it():
    """The coupling: travel and rotation are locked, and the contact point does not slip.

    `rack_drive` places the pinion at the travel its own rotation has earned. Positive turns
    about the frame's +X give travel along the frame's +Z, which is the rolling-without-slip
    sign and is derived rather than chosen: for the contact point to be stationary,
    `v_centre = -omega x r`.
    """
    doc = new_doc()
    rack = D.make_part(doc, "gear_rack", None, name="Rack")
    pinion = D.make_part(doc, "spur_gear", None, name="Pinion")
    doc.recompute()

    _shape, rack_frames = P.gear_rack()
    _pshape, pin_frames = P.spur_gear()
    for turns in (0.0, 0.5, 1.0, 2.0):
        placed = M.rack_drive(rack.Placement, rack_frames["pitch"], pin_frames["axis"],
                              turns=turns, teeth=20, module=2.0, rack_teeth=40)
        travelled = placed.Base.x
        assert abs(travelled - M.rack_travel(teeth=20, module=2.0, turns=turns)) < 1e-6, \
            (turns, travelled)


@test
def test_rack_drive_spins_the_pinion_about_an_axis_PERPENDICULAR_to_the_travel():
    """The difference between a rack and a screw, and it is not a detail.

    A screw's rotation and travel share one axis. A rack's are perpendicular: the pinion turns
    about an axis across the rack while moving along it. A `screw_drive` reused for a rack would
    spin the pinion about the direction of travel -- a wheel turning like a drill bit.
    """
    _shape, rack_frames = P.gear_rack()
    _pshape, pin_frames = P.spur_gear()
    at_zero = M.rack_drive(FreeCAD.Placement(), rack_frames["pitch"], pin_frames["axis"],
                           turns=0.0, teeth=20, module=2.0, rack_teeth=40)
    quarter = M.rack_drive(FreeCAD.Placement(), rack_frames["pitch"], pin_frames["axis"],
                           turns=0.25, teeth=20, module=2.0, rack_teeth=40)
    # The pinion's own axis must not have tilted: it stays the rack frame's X, whatever the turn.
    axis0 = at_zero.Rotation.multVec(FreeCAD.Vector(0, 0, 1))
    axis1 = quarter.Rotation.multVec(FreeCAD.Vector(0, 0, 1))
    assert axis0.getAngle(axis1) < 1e-9, (axis0, axis1)
    # And it must have actually turned: a marker off the axis moves.
    off = FreeCAD.Vector(10.0, 0, 0)
    moved = at_zero.Rotation.multVec(off).getAngle(quarter.Rotation.multVec(off))
    assert abs(math.degrees(moved) - 90.0) < 1e-6, math.degrees(moved)


@test
def test_a_rack_is_an_exact_whole_number_of_pitches_so_two_butt_end_to_end():
    """A long axis is several racks in a row, and the joint has to keep the pitch.

    The rack is generated `teeth * pi * m` long, starting and ending halfway along a root land,
    so butting two gives one correct land across the joint and an unbroken tooth pattern. A rack
    sized to its tooth tips instead would put a half-pitch error at every joint.
    """
    m, z = 2.0, 12
    shape, frames = P.gear_rack(module=m, teeth=z)
    bb = shape.BoundBox
    assert abs(bb.XLength - z * math.pi * m) < 1e-6, (bb.XLength, z * math.pi * m)
    # The pitch frame is on the pitch line, at the rack's start, travel along +Z.
    assert abs(frames["pitch"].Base.y) < 1e-9, frames["pitch"].Base
    travel = frames["pitch"].Rotation.multVec(FreeCAD.Vector(0, 0, 1))
    assert abs(travel.x - 1.0) < 1e-9, travel
    axis = frames["pitch"].Rotation.multVec(FreeCAD.Vector(1, 0, 0))
    assert abs(abs(axis.z) - 1.0) < 1e-9, ("the pinion axis must be across the rack", axis)


@test
def test_the_rack_teeth_are_on_the_pitch_line_at_the_circular_pitch():
    """Geometry, not arithmetic: slice the solid on its pitch line and measure what is there.

    At the pitch line the material is the teeth and nothing else -- the root lands are a
    dedendum below it -- so a thin slice there falls into exactly `teeth` separate solids, each
    half a circular pitch wide, spaced one circular pitch apart.
    """
    import Part  # noqa: E402

    m, z = 2.0, 10
    shape, _frames = P.gear_rack(module=m, teeth=z)
    assert len(shape.Solids) == 1, len(shape.Solids)

    slab = Part.makeBox(1000.0, 0.02, 1000.0, FreeCAD.Vector(-1.0, -0.01, -1.0))
    teeth_solids = shape.common(slab).Solids
    assert len(teeth_solids) == z, (len(teeth_solids), z)

    p = math.pi * m
    centres = sorted(s.BoundBox.Center.x for s in teeth_solids)
    for s in teeth_solids:
        # The slice is 0.02 mm tall, so the flank taper makes it a hair wider than p/2 at the
        # bottom; that is the profile being right, not a measurement error.
        assert p / 2.0 - 1e-9 <= s.BoundBox.XLength < p / 2.0 + 0.02, s.BoundBox.XLength
    for a, b in zip(centres, centres[1:]):
        assert abs((b - a) - p) < 1e-6, (b - a, p)
    assert abs(centres[0] - 0.5 * p) < 1e-6, ("the first tooth is half a pitch in, which is what "
                                              "puts the rack's END halfway along a root land",
                                              centres[0])


@test
def test_a_pinion_at_the_pitch_height_TOUCHES_the_rack_and_lower_DIGS_IN():
    """Pitch-line contact proved against the solids, and proved SHARP.

    One assertion that the mesh is clean would not be worth much -- a pinion held a mile above
    the rack passes it. What makes this a measurement is the curve: at the correct centre height
    the overlap is numerical noise, and it grows monotonically from the first fifty microns of
    error. So the height is right to a fraction of a millimetre rather than merely plausible.

    That the overlap at contact is ~1e-4 mm3 rather than merely small is the straight-flank rack
    and the involute pinion being **exactly conjugate**: a rack is the involute's infinite-radius
    limit, so the two touch tangentially with no backlash and no root interference.
    """
    m, z = 2.0, 20
    doc = new_doc()
    rack = D.make_part(doc, "gear_rack", None, name="Rack")
    rack.Module, rack.Teeth = m, 12
    pinion = D.make_part(doc, "spur_gear", None, name="Pinion")
    pinion.Module, pinion.Teeth, pinion.Thickness = m, z, 10.0
    doc.recompute()

    _rshape, rack_frames = P.gear_rack(module=m, teeth=12)
    _pshape, pin_frames = P.spur_gear(module=m, teeth=z, thickness=10.0)

    def overlap(turns=0.0, drop=0.0):
        pinion.Placement = M.rack_drive(rack.Placement, rack_frames["pitch"],
                                        pin_frames["axis"], turns=turns, teeth=z, module=m,
                                        rack_teeth=12, drop=drop)
        doc.recompute()
        return rack.Shape.common(pinion.Shape).Volume

    touching = overlap()
    assert touching < 0.01, ("at the pitch height the gears touch, they do not interfere",
                             touching)

    # Measured 0.19 / 1.53 / 6.30 / 25.1 mm3 at 0.05 / 0.2 / 0.5 / 1.0 mm low. The assertion is
    # the SHAPE of that curve -- monotone, and already real material at fifty microns -- because
    # the exact volumes are this profile on this OCCT and the trend is the property.
    drops = [0.05, 0.2, 0.5, 1.0]
    volumes = [overlap(drop=d) for d in drops]
    assert volumes[0] > 0.05, ("fifty microns low must already bite", volumes[0])
    for a, b in zip(volumes, volumes[1:]):
        assert b > a * 1.5, ("interference must grow with the error", list(zip(drops, volumes)))
    assert volumes[-1] > 200.0 * touching, (touching, volumes[-1])


@test
def test_the_mesh_stays_clean_through_FRACTIONAL_turns_not_just_whole_teeth():
    """The ratio and the phase, checked where a wrong phase would actually show.

    A rack drive that only meshes at whole tooth pitches would pass a test at turns 0, 1, 2 and
    jam everywhere between. These are deliberately awkward fractions: at 0.03 and 0.77 of a turn
    the pinion sits mid-tooth over the rack, which is where a phase error puts tooth on tooth
    instead of tooth in space.
    """
    m, z = 2.0, 20
    doc = new_doc()
    rack = D.make_part(doc, "gear_rack", None, name="Rack")
    rack.Module, rack.Teeth = m, 12
    pinion = D.make_part(doc, "spur_gear", None, name="Pinion")
    pinion.Module, pinion.Teeth, pinion.Thickness = m, z, 10.0
    doc.recompute()
    _rshape, rack_frames = P.gear_rack(module=m, teeth=12)
    _pshape, pin_frames = P.spur_gear(module=m, teeth=z, thickness=10.0)

    # A 12-tooth module-2 rack is 75.40 mm and a turn of this pinion is 125.66 mm, so these
    # stations stop at 0.59 of a turn. Going further is refused, by the test below.
    for turns in (0.0, 0.03, 0.125, 0.25, 0.5, 0.59):
        pinion.Placement = M.rack_drive(rack.Placement, rack_frames["pitch"],
                                        pin_frames["axis"], turns=turns, teeth=z,
                                        module=m, rack_teeth=12)
        doc.recompute()
        vol = rack.Shape.common(pinion.Shape).Volume
        assert vol < 0.01, ("tooth on tooth at %s turns -- the phase is wrong" % turns, vol)
        # and it really is engaged at that station, not hovering clear of the rack
        assert rack.Shape.distToShape(pinion.Shape)[0] < 1e-6, (turns, "not in contact")


@test
def test_driving_a_pinion_off_the_end_of_its_rack_is_REFUSED():
    """The failure that reads backwards, and so has to be refused rather than reported.

    A pinion driven past the end of its rack interferes with nothing at all, so a clearance
    check calls the worst position in the travel the cleanest one. `rack_drive` therefore
    insists on knowing the rack's tooth count and does the arithmetic itself.
    """
    _shape, rf = P.gear_rack(module=2.0, teeth=12)
    _p, pf = P.spur_gear()
    # 12 teeth of module 2 is 75.40 mm; one turn of a 20-tooth pinion is 125.66 mm.
    assert abs(M.rack_length(12, 2.0) - 75.398) < 1e-3, M.rack_length(12, 2.0)
    M.rack_drive(FreeCAD.Placement(), rf["pitch"], pf["axis"], turns=0.59,
                 teeth=20, module=2.0, rack_teeth=12)       # 74.1 mm, on the rack
    try:
        M.rack_drive(FreeCAD.Placement(), rf["pitch"], pf["axis"], turns=1.0,
                     teeth=20, module=2.0, rack_teeth=12)
    except Exception as exc:
        msg = str(exc)
        assert "75.40" in msg and "125.66" in msg, msg
        assert "20 teeth" in msg or "20-tooth" in msg, msg
        assert "teeth" in msg, ("say how long a rack it would take", msg)
        return
    raise AssertionError("running off the end of the rack must be refused")


@test
def test_a_sweep_that_runs_the_carriage_OFF_ITS_RAIL_fails_even_with_nothing_to_hit():
    """The measured false pass this check exists for.

    A 10 mm shaft spanning z -100..100, a spacer riding it, and a travel of 0 to 900 mm. Every
    station past the end of the shaft touches nothing whatsoever, so before this check the sweep
    reported **ok: True with its worst gap unchanged at 1.0 mm** -- running a carriage 800 mm
    into thin air read as the cleanest part of the travel.
    """
    doc = new_doc()
    shaft = D.make_part(doc, "shaft", None, name="Shaft")
    shaft.Diameter, shaft.Length = 10.0, 200.0
    car = D.make_part(doc, "spacer", None, name="Carriage")
    car.Od, car.Id, car.Height = 40.0, 12.0, 10.0
    doc.recompute()
    assert abs(shaft.Shape.BoundBox.ZMin + 100.0) < 1e-9, shaft.Shape.BoundBox.ZMin

    res = registry.call("sweep", moving="Carriage", target="Shaft", target_frame="axis",
                        moving_frame="axis", start=0.0, stop=900.0, stations=10)
    assert res["ok"] is False, res
    assert res["clash_stations"] == [], ("nothing is hit; that is the point", res)
    assert res["off_support_stations"], res
    # The shaft ends at +100 and the carriage is 10 mm tall, so it leaves at 90 mm of travel.
    assert min(res["off_support_stations"]) > 90.0, res["off_support_stations"]
    assert 100.0 in res["off_support_stations"], res["off_support_stations"]
    assert res["off_support"][-1]["overhang_mm"] > 700.0, res["off_support"]
    limits = res["travel_limits"]
    assert abs(limits["max_mm"] - 90.0) < 1e-6, limits
    assert abs(limits["min_mm"] + 100.0) < 1e-6, limits
    assert limits["min_mm"] < res["start_mm"] + 1e-6, "the sweep started inside the limits"
    assert abs(limits["usable_mm"] - 190.0) < 1e-6, limits
    assert limits["fits"] is True, limits


@test
def test_a_sweep_WITHIN_the_travel_limits_still_passes():
    """The other half: the new check must not fail an honest travel."""
    doc = new_doc()
    shaft = D.make_part(doc, "shaft", None, name="Shaft")
    shaft.Diameter, shaft.Length = 10.0, 200.0
    car = D.make_part(doc, "spacer", None, name="Carriage")
    car.Od, car.Id, car.Height = 40.0, 12.0, 10.0
    doc.recompute()
    res = registry.call("sweep", moving="Carriage", target="Shaft", target_frame="axis",
                        moving_frame="axis", start=-100.0, stop=90.0, stations=11)
    assert res["ok"] is True, res
    assert res["off_support"] == [], res["off_support"]
    # Both ends are flush with the ends of the shaft: the limits are the travel, exactly.
    assert abs(res["travel_limits"]["max_mm"] - 90.0) < 1e-6, res["travel_limits"]


@test
def test_a_carriage_LONGER_than_its_rail_has_no_valid_travel_and_says_so():
    """A design error, not a travel of negative length."""
    limits = M.travel_limits(support_lo=0.0, support_hi=50.0, moving_lo=0.0, moving_hi=80.0)
    assert limits["fits"] is False, limits
    assert limits["usable_mm"] < 0.0, limits
    ok = M.travel_limits(support_lo=0.0, support_hi=200.0, moving_lo=0.0, moving_hi=10.0)
    assert ok["fits"] is True and abs(ok["usable_mm"] - 190.0) < 1e-9, ok


@test
def test_a_RACK_DRIVE_sweeps_through_its_travel_with_the_pinion_turning():
    """#14's rack, swept the way #12 asks a drive train to be swept.

    The pinion has to TURN as it travels or its teeth plough through the rack's, so the sweep
    takes a motion kind. Run as a plain slide the very same mechanism collides at nearly every
    station -- and that is checked here too, because a mode that silently did the wrong
    kinematics would report a sound rack drive as a design fault.
    """
    m, z, rz = 2.0, 20, 24
    doc = new_doc()
    rack = D.make_part(doc, "gear_rack", None, name="Rack")
    rack.Module, rack.Teeth = m, rz
    pinion = D.make_part(doc, "spur_gear", None, name="Pinion")
    pinion.Module, pinion.Teeth, pinion.Thickness = m, z, 10.0
    doc.recompute()

    span = M.rack_length(rz, m)                  # 150.80 mm
    assert abs(span - 150.796) < 1e-3, span

    rolled = registry.call("sweep", moving="Pinion", target="Rack", target_frame="pitch",
                           moving_frame="axis", start=0.0, stop=span, stations=13,
                           motion="rack", teeth=z, module=m, rack_teeth=rz)
    assert rolled["motion"] == "rack", rolled
    assert rolled["ok"] is True, rolled
    assert rolled["clash_stations"] == [], rolled["clashes"][:2]

    # A pinion at either end of its travel OVERHANGS the rack by most of its radius, and that is
    # what a pinion does rather than a fault. So the rack's support test is that its CENTRE stays
    # over the rack -- unlike a carriage, which has to be wholly on its rail or it is held up by
    # nothing. Checked here because the two criteria look like one until a pinion meets them.
    pinion.Placement = M.rack_drive(rack.Placement, P.gear_rack(module=m, teeth=rz)[1]["pitch"],
                                    P.spur_gear(module=m, teeth=z, thickness=10.0)[1]["axis"],
                                    turns=0.0, teeth=z, module=m, rack_teeth=rz)
    doc.recompute()
    assert pinion.Shape.BoundBox.XMin < rack.Shape.BoundBox.XMin - 20.0, (
        "the pinion really does hang off the end here", pinion.Shape.BoundBox.XMin)
    assert rolled["off_support"] == [], rolled["off_support"]
    assert rolled["travel_limits"]["max_mm"] == round(span, 6), rolled["travel_limits"]

    slid = registry.call("sweep", moving="Pinion", target="Rack", target_frame="pitch",
                         moving_frame="axis", start=0.0, stop=span, stations=13)
    assert slid["ok"] is False, "a pinion dragged along a rack without turning must collide"
    assert len(slid["clash_stations"]) > 8, slid["clash_stations"]


@test
def test_a_rack_sweep_that_runs_PAST_the_rack_is_refused_by_the_drive_itself():
    """The travel limit, reached through the command layer rather than the function."""
    m, z, rz = 2.0, 20, 12
    doc = new_doc()
    rack = D.make_part(doc, "gear_rack", None, name="Rack")
    rack.Module, rack.Teeth = m, rz
    pinion = D.make_part(doc, "spur_gear", None, name="Pinion")
    pinion.Module, pinion.Teeth, pinion.Thickness = m, z, 10.0
    doc.recompute()
    try:
        registry.call("sweep", moving="Pinion", target="Rack", target_frame="pitch",
                      moving_frame="axis", start=0.0, stop=200.0, stations=5,
                      motion="rack", teeth=z, module=m, rack_teeth=rz)
    except Exception as exc:
        assert "75.40" in str(exc), str(exc)
        return
    raise AssertionError("sweeping past the end of the rack must be refused")


@test
def test_a_rack_sweep_without_the_pinion_size_is_refused_by_name():
    doc = new_doc()
    D.make_part(doc, "gear_rack", None, name="Rack")
    D.make_part(doc, "spur_gear", None, name="Pinion")
    doc.recompute()
    try:
        registry.call("sweep", moving="Pinion", target="Rack", target_frame="pitch",
                      moving_frame="axis", start=0.0, stop=10.0, motion="rack")
    except ValueError as exc:
        for name in ("teeth", "module", "rack_teeth"):
            assert name in str(exc), (name, str(exc))
        return
    raise AssertionError("a rack sweep needs the pinion and rack sizes")


@test
def test_a_CARRIAGE_RIDES_THE_FULL_LENGTH_of_its_rail_without_touching_it():
    """What makes a linear rail a linear rail rather than a block sitting on a bar.

    Swept, not checked at the ends, because a static check at either end of a travel passes a
    mechanism that binds in the middle -- and the travel limit is checked too, because a
    carriage driven off the end of its rail touches nothing at all and so reads as the cleanest
    station in the sweep.
    """
    doc = new_doc()
    rail = D.make_part(doc, "profile_rail", None, name="Rail")
    rail.Length = 200.0
    car = D.make_part(doc, "rail_carriage", None, name="Carriage")
    doc.recompute()

    res = registry.call("sweep", moving="Carriage", target="Rail", target_frame="axis",
                        moving_frame="rail", start=0.0, stop=160.0, stations=17)
    assert res["ok"] is True, res
    assert res["clash_stations"] == [], res["clashes"][:2]
    assert res["off_support"] == [], res["off_support"]
    # the stated running clearance, held at every station of the travel
    assert abs(res["min_gap_mm"] - 0.3) < 1e-3, res["min_gap_mm"]
    # a 200 mm rail and a 40 mm carriage is 160 mm of travel, and the limits say so
    assert abs(res["travel_limits"]["max_mm"] - 160.0) < 1e-6, res["travel_limits"]
    assert abs(res["travel_limits"]["usable_mm"] - 160.0) < 1e-6, res["travel_limits"]


@test
def test_driving_the_carriage_PAST_the_end_of_the_rail_fails_the_sweep():
    doc = new_doc()
    rail = D.make_part(doc, "profile_rail", None, name="Rail")
    rail.Length = 200.0
    D.make_part(doc, "rail_carriage", None, name="Carriage")
    doc.recompute()
    res = registry.call("sweep", moving="Carriage", target="Rail", target_frame="axis",
                        moving_frame="rail", start=0.0, stop=260.0, stations=14)
    assert res["ok"] is False, res
    assert res["clash_stations"] == [], ("it hits nothing out there; that is the point", res)
    assert res["off_support_stations"], res
    assert min(res["off_support_stations"]) > 160.0, res["off_support_stations"]


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


# No __main__ guard: freecadcmd imports a script as a module named after its file.
main()
