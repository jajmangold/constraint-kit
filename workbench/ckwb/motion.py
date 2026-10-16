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

What that looked like it forwent: mechanism DOF analysis and loop closure. That turned out not
to need a solver either -- mobility is a property of the joint GRAPH rather than of the geometry,
and `mobility.py` counts it in closed form, with the one trap (a coupling is not a lower pair)
written down there.

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


# -------------------------------------------------------------------------------- rack maths

def rack_travel(teeth: int, module: float, turns: float) -> float:
    """How far a pinion travels along a rack for `turns` of rotation.

    One turn lays down every tooth the pinion has, so the travel per revolution is the pitch
    circumference `pi * m * z` -- equivalently the circular pitch times the tooth count. No
    `lead` enters it: unlike a screw, a rack's ratio is set by the pinion's size alone, which is
    why a rack drive is geared by changing the pinion.
    """
    _positive(module=module)
    if int(teeth) < 1:
        raise ValueError(f"teeth must be at least 1, got {teeth}")
    return math.pi * float(module) * int(teeth) * float(turns)


def rack_turns(teeth: int, module: float, distance: float) -> float:
    """Turns of the pinion for a given travel. The inverse of `rack_travel`."""
    return float(distance) / rack_travel(teeth, module, 1.0)


def rack_steps_per_mm(teeth: int, module: float, steps_per_rev: int) -> float:
    """Steps per millimetre for a stepper driving a rack through this pinion.

    Worth comparing against `steps_per_mm` for a screw before choosing one: a 20-tooth
    module-2 pinion on 3200 steps/rev gives 25.5 steps/mm, against the 1605 ballscrew's measured
    **320**. A rack buys unlimited travel and loses an order of magnitude of resolution.
    """
    _positive(steps_per_rev=steps_per_rev)
    return float(steps_per_rev) / rack_travel(teeth, module, 1.0)


def rack_centre_height(teeth: int, module: float) -> float:
    """Height of the pinion axis above the rack's PITCH LINE: the pinion's pitch radius.

    Above the pitch line, not above the tooth tips and not above the back face -- confusing
    those is the classic rack mistake, and it is off by a whole addendum, which jams the mesh.
    `gear_rack`'s `pitch` frame sits on the pitch line precisely so this is the only number
    needed.
    """
    _positive(module=module)
    if int(teeth) < 1:
        raise ValueError(f"teeth must be at least 1, got {teeth}")
    return float(module) * int(teeth) / 2.0


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


def rack_length(teeth: int, module: float) -> float:
    """Length of a rack of this many teeth: exactly `teeth * pi * m`, by `parts.gear_rack`.

    The same arithmetic as one turn of a `teeth`-tooth pinion, and not a coincidence: a pinion
    rolls its own tooth count per revolution, so a rack is as long as the pinion that would be
    its equal.
    """
    return rack_travel(teeth, module, 1.0)


def rack_drive(target_placement: FreeCAD.Placement, pitch_frame: FreeCAD.Placement,
               moving_frame: FreeCAD.Placement, turns: float, teeth: int, module: float,
               rack_teeth: int, drop: float = 0.0) -> FreeCAD.Placement:
    """Place a pinion on a rack at the position its own rotation has rolled it to.

    The coupling, and the counterpart of `screw_drive` -- with one difference that is not a
    detail. **A screw's rotation and travel share an axis; a rack's are perpendicular.** The
    pinion turns about an axis lying across the rack while travelling along it, so reusing
    `screw_drive` here would spin the pinion about its direction of travel, like a drill bit
    instead of a wheel.

    The sign is derived, not chosen: for the contact point not to slip, `v_centre = -omega x r`,
    which makes positive turns about the pitch frame's +X give travel along its +Z.

    The tooth phase is derived too. `gear_rack` begins mid-root-land and `spur_gear_profile`
    centres a tooth on its own +X, so the fixed quarter turn in here is what points that tooth at
    the rack. Rolling keeps the phase correct everywhere else for free -- that is what rolling
    without slip means.

    `rack_teeth` is not optional, because a rack without a length is not a rack: the travel is
    checked against it and running the pinion off the end is **refused**. That check exists
    because the failure is silent and reads backwards -- a pinion driven past the end of its rack
    interferes with nothing at all, so an interference check reports it as the cleanest position
    in the travel.

    `drop` lowers the pinion below the correct centre height, for testing a mesh rather than
    building one.
    """
    distance = rack_travel(teeth, module, turns)
    span = rack_length(rack_teeth, module)
    if not (-SUPPORT_TOL_MM <= distance <= span + SUPPORT_TOL_MM):
        raise MateError(
            f"{turns} turns of a {teeth}-tooth module-{module} pinion is {distance:.2f} mm of "
            f"travel, and a {rack_teeth}-tooth rack is only {span:.2f} mm long. Lengthen the "
            f"rack to at least {math.ceil(distance / (math.pi * float(module)))} teeth, or stay "
            f"within {rack_turns(teeth, module, span):.3f} turns.")
    height = rack_centre_height(teeth, module) - float(drop)
    world = target_placement.multiply(pitch_frame)
    # Two fixed rotations, then the spin: +90 about Y lays the pinion's own axis (+Z) onto the
    # frame's +X, and -90 about X swings its tooth-zero from the travel direction down onto the
    # rack. Both are about to be composed with the spin, which is also about X.
    spin = FreeCAD.Rotation(FreeCAD.Vector(1, 0, 0), 360.0 * float(turns) - 90.0).multiply(
        FreeCAD.Rotation(FreeCAD.Vector(0, 1, 0), 90.0))
    offset = FreeCAD.Placement(FreeCAD.Vector(0.0, height, distance), spin)
    return world.multiply(offset).multiply(moving_frame.inverse())


# ------------------------------------------------------------------------- travel limits

#: A travel is allowed to overhang its support by this much before it counts as off the end.
#: Not zero, because a carriage flush with the end of its rail is a legitimate end stop and
#: floating-point projection will put it a few nanometres either side of exact.
SUPPORT_TOL_MM = 1e-6


def axis_extent(shape, origin, axis, conservative: bool = False) -> tuple:
    """How far a shape reaches along `axis`, measured from `origin`. Returns `(lo, hi)`.

    Two measurements, and which one is safe depends on which part is being measured, so the
    caller says. The usable travel of a moving part along a support is

        d in [support_lo - moving_lo, support_hi - moving_hi]

    so **under**-estimating the SUPPORT and **over**-estimating the MOVING part both shrink that
    range, which is the direction that refuses a doubtful travel rather than allowing it.

    * `conservative=False` projects the shape's VERTICES, which is exact for prismatic stock --
      every rail, screw and rack here -- and can understate a shape whose furthest point along
      the axis is the middle of a curved face. Use it for the support.
    * `conservative=True` projects the eight corners of the bounding box, which never
      understates anything. Use it for the moving part.

    Bear in mind `Shape.BoundBox` bounds a B-spline's control polygon rather than the curve, so
    the conservative measure of a splined part can be loose. Loose in the safe direction, and the
    sweep's interference check remains the authority on actual contact.
    """
    if conservative:
        bb = shape.BoundBox
        points = [FreeCAD.Vector(x, y, z)
                  for x in (bb.XMin, bb.XMax)
                  for y in (bb.YMin, bb.YMax)
                  for z in (bb.ZMin, bb.ZMax)]
    else:
        points = [FreeCAD.Vector(v.Point) for v in shape.Vertexes]
    if not points:
        raise MateError("a shape with no vertices has no measurable extent along an axis")
    values = [(p - origin).dot(axis) for p in points]
    return min(values), max(values)


def travel_limits(support_lo: float, support_hi: float,
                  moving_lo: float, moving_hi: float, at: float = 0.0) -> dict:
    """The travel over which a moving part stays wholly on its support.

    `moving_lo/hi` are the moving part's extent measured at travel `at`, so the limits come out
    in the same travel coordinate the caller is already using. A moving part longer than its
    support has no valid travel at all, and that is reported rather than returned as a reversed
    range -- a long carriage on a short rail is a design error, not a travel of negative length.
    """
    lo = support_lo - (moving_lo - at)
    hi = support_hi - (moving_hi - at)
    span = (support_hi - support_lo) - (moving_hi - moving_lo)
    return {"min_mm": round(lo, 6), "max_mm": round(hi, 6), "usable_mm": round(span, 6),
            "fits": bool(span >= -SUPPORT_TOL_MM)}


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
