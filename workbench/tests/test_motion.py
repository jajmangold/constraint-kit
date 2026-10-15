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

What a solver would buy that this does not: mechanism DOF analysis and loop closure. Nothing on
this bench needs it yet; `linkage.py` already covers four-bars via SolveSpace when it does.

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
