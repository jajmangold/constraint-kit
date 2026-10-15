# -*- coding: utf-8 -*-
"""Coupled motion: slides, screws, belts, and sweeping a mechanism through its travel.

Deliberately NOT FreeCAD's Assembly solver, and the reasoning matters because the solver is
right there and ships with the application.

Reconnaissance on it, headless, 2026-10-16: an `Assembly::AssemblyObject` creates and exposes
`solve()`; a `Slider` / `Screw` / `Belt` / `RackPinion` joint proxy constructs once the joint
sits inside the assembly (`JointObject.Joint(obj, index)` takes an integer index into
`JointObject.JointTypes`, not a name); grounding via `GroundedJoint` works; and `Placement1` /
`Placement2` are plain `App::PropertyPlacement`, so a joint need not refer to "Face3" and the
topological-naming problem is avoidable. But `isPartConnected` stays False and driving the joint
moves nothing: the solver wants its `Reference` properties in a path form that is undocumented
and was not cracked.

More to the point, a solver is not what this hardware is missing. A ball nut at a given travel, a
carriage at a position, a pulley at an angle, a belt length for a centre distance — every one of
those is closed form. A numerical solver would return an approximation to something arithmetic
gives exactly, and bring an iteration count and a convergence failure with it. So these are exact
placements, like every other mate in this kernel.

What that forgoes, stated plainly: mechanism DOF analysis and loop closure. Nothing here needs it
yet, and `constraint_kit/linkage.py` already does four-bars through SolveSpace when it does.

**The sweep is the part that earns its keep.** A static check at either end of a travel passes a
mechanism that collides in the middle, and that is not a hypothetical — it is what a
`check_clearance` run on a gantry cannot see.
"""
from __future__ import annotations

import math

import FreeCAD

from .mates import MateError, align

#: Below this, a belt wrap angle calculation is meaningless and the inverse will not converge.
_MIN_CENTRE = 1e-6


# ------------------------------------------------------------------------------ screw maths

def screw_travel(lead: float, turns: float) -> float:
    """Axial travel for a number of turns. The lead is mm per revolution, nothing else."""
    _positive(lead=lead)
    return float(lead) * float(turns)


def screw_turns(lead: float, distance: float) -> float:
    """Turns needed for an axial distance."""
    _positive(lead=lead)
    return float(distance) / float(lead)


def steps_per_mm(lead: float, steps_per_rev: int) -> float:
    """Motor steps per millimetre of travel.

    The number a controller is actually configured with, and the reason `lead` is carried as a
    parameter rather than inferred from a part name: an SFU1605 and an SFU1610 are the same
    diameter, and at 1600 steps/rev they are 320 and 160 steps/mm respectively.
    """
    _positive(lead=lead, steps_per_rev=steps_per_rev)
    return float(steps_per_rev) / float(lead)


# ------------------------------------------------------------------------------- belt maths

def pitch_diameter(teeth: int, pitch: float) -> float:
    """Pitch diameter of a toothed pulley: p * N / pi."""
    _positive(teeth=teeth, pitch=pitch)
    return float(pitch) * int(teeth) / math.pi


def belt_ratio(teeth_driver: int, teeth_driven: int) -> float:
    """Reduction ratio: driven teeth over driver teeth. Greater than one is a reduction."""
    _positive(teeth_driver=teeth_driver, teeth_driven=teeth_driven)
    return float(teeth_driven) / float(teeth_driver)


def coupled_angle(angle: float, teeth_driver: int, teeth_driven: int) -> float:
    """How far the driven pulley turns when the driver turns by `angle` degrees."""
    return float(angle) / belt_ratio(teeth_driver, teeth_driven)


def belt_length(centre_distance: float, teeth_a: int, teeth_b: int, pitch: float) -> float:
    """Belt length around two pulleys at a given centre distance.

    The standard two-pulley wrap, exact rather than the `2C + 1.57(D1+D2) + ...` approximation:

        beta = asin((D1 - D2) / 2C)
        L    = 2C cos(beta) + D1 (pi/2 + beta) + D2 (pi/2 - beta)

    For equal pulleys beta is zero and this reduces to `2C + pi D`, which is the check the test
    suite pins it to. This is the arithmetic that goes wrong by hand and then goes wrong in the
    BOM, so it lives in one place.
    """
    _positive(centre_distance=centre_distance)
    d1 = pitch_diameter(max(teeth_a, teeth_b), pitch)
    d2 = pitch_diameter(min(teeth_a, teeth_b), pitch)
    c = float(centre_distance)
    ratio = (d1 - d2) / (2.0 * c)
    if abs(ratio) > 1.0:
        raise ValueError(
            f"a centre distance of {c} is too short for pulleys of {d1:.1f} and {d2:.1f} mm; "
            "they would overlap")
    beta = math.asin(ratio)
    return (2.0 * c * math.cos(beta)
            + d1 * (math.pi / 2.0 + beta)
            + d2 * (math.pi / 2.0 - beta))


def belt_centre_distance(belt_teeth: int, teeth_a: int, teeth_b: int, pitch: float,
                         tol: float = 1e-9) -> float:
    """Centre distance for a belt of a given tooth count. The number you actually need.

    You buy a stock belt — 93 teeth of HTD-8M, say — and then have to place the pulleys. Solved
    by bisection because `belt_length` is monotonic in the centre distance, which makes bisection
    both exact to tolerance and incapable of the surprises a Newton step can produce near the
    wrap limit.
    """
    _positive(belt_teeth=belt_teeth, pitch=pitch)
    target = float(belt_teeth) * float(pitch)
    d1 = pitch_diameter(max(teeth_a, teeth_b), pitch)
    d2 = pitch_diameter(min(teeth_a, teeth_b), pitch)

    low = (d1 - d2) / 2.0 + 1e-6          # any closer and the pulleys overlap
    shortest = belt_length(low, teeth_a, teeth_b, pitch)
    if target <= shortest:
        raise ValueError(
            f"a {belt_teeth}-tooth belt is {target:.1f} mm, too short to wrap pulleys of "
            f"{d1:.1f} and {d2:.1f} mm, which need at least {shortest:.1f} mm")

    high = max(low * 2.0, target)         # L grows faster than C, so this always overshoots
    while belt_length(high, teeth_a, teeth_b, pitch) < target:
        high *= 2.0
    for _ in range(200):
        mid = 0.5 * (low + high)
        if belt_length(mid, teeth_a, teeth_b, pitch) < target:
            low = mid
        else:
            high = mid
        if high - low < tol:
            break
    return 0.5 * (low + high)


# ------------------------------------------------------------------------------ placements

def slide(target_placement: FreeCAD.Placement, target_frame: FreeCAD.Placement,
          moving_frame: FreeCAD.Placement, distance: float) -> FreeCAD.Placement:
    """Share an axis, then offset by `distance` along it. A prismatic joint at one position.

    The travel is along the TARGET frame's own +Z, not world Z, so a rail lying along X slides
    along X without the caller doing any trigonometry.
    """
    world = target_placement.multiply(target_frame)
    offset = FreeCAD.Placement(FreeCAD.Vector(0, 0, float(distance)), FreeCAD.Rotation())
    return world.multiply(offset).multiply(moving_frame.inverse())


def screw_drive(target_placement: FreeCAD.Placement, axis_frame: FreeCAD.Placement,
                moving_frame: FreeCAD.Placement, turns: float, lead: float,
                rotate_with_screw: bool = False) -> FreeCAD.Placement:
    """Place a nut at the travel corresponding to `turns` of a screw of this `lead`.

    `rotate_with_screw` is False by default because a ball nut does not turn: the screw rotates
    and the nut translates. Set it for a plain nut on a threaded rod that turns with it.
    """
    distance = screw_travel(lead, turns)
    world = target_placement.multiply(axis_frame)
    spin = FreeCAD.Rotation(FreeCAD.Vector(0, 0, 1),
                            360.0 * float(turns)) if rotate_with_screw else FreeCAD.Rotation()
    offset = FreeCAD.Placement(FreeCAD.Vector(0, 0, distance), spin)
    return world.multiply(offset).multiply(moving_frame.inverse())


def rotate(target_placement: FreeCAD.Placement, target_frame: FreeCAD.Placement,
           moving_frame: FreeCAD.Placement, angle: float) -> FreeCAD.Placement:
    """Share an axis, then turn by `angle` degrees about it. A revolute joint at one position."""
    world = target_placement.multiply(target_frame)
    spin = FreeCAD.Placement(FreeCAD.Vector(),
                             FreeCAD.Rotation(FreeCAD.Vector(0, 0, 1), float(angle)))
    return world.multiply(spin).multiply(moving_frame.inverse())


# ---------------------------------------------------------------------------------- sweeps

def sweep_stations(start: float, stop: float, stations: int) -> list:
    """`stations` positions from start to stop, both ends included.

    Inclusive on purpose: the ends are where a mechanism is usually checked by hand, so leaving
    them out would make a sweep disagree with a static check for no good reason.
    """
    n = int(stations)
    if n < 2:
        raise ValueError(f"a sweep needs at least 2 stations, got {n}")
    if n == 2:
        return [float(start), float(stop)]
    step = (float(stop) - float(start)) / (n - 1)
    return [float(start) + step * i for i in range(n)]


def _positive(**kw) -> None:
    for name, value in kw.items():
        if float(value) <= 0.0:
            raise ValueError(f"{name} must be positive, got {value}")
