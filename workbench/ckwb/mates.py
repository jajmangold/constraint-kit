# -*- coding: utf-8 -*-
"""The mate kernel — oriented frames in, placements out, deterministically.

A mate here is not a constraint handed to a solver. It is an exact placement computed from two
named frames, which is why seating comes out at a gap of *exactly* zero rather than within some
tolerance, and why the same inputs always give the same answer.

**The frame convention, which everything depends on: a frame's +Z points out of the material.**
Two consequences follow, and they are the whole kernel:

* `seat` puts two outward normals nose to nose — origins coincident, Z anti-parallel — so the
  faces touch and the parts do not interpenetrate.
* `align` puts them Z-parallel, which is what you want to run a shaft down a bore or stack
  parts on a shared axis.

Named frames are also what makes a mate survive a parameter change. A mate against "the face
OCC happens to call Face6" breaks the moment a feature is added; a mate against `top` does not.
That is the topological-naming problem, and declaring frames in the generator is how this
workbench sidesteps it rather than fighting it.
"""
from __future__ import annotations

import math

import FreeCAD

#: Rotation that reverses a frame's Z while keeping its X — used to face two frames together.
_FLIP = FreeCAD.Placement(FreeCAD.Vector(), FreeCAD.Rotation(FreeCAD.Vector(1, 0, 0), 180))


class MateError(ValueError):
    """A mate that cannot be solved: unknown frame, unplaced target, or an over-constraint.

    Always raised, never worked around. Every one of these conditions has a silent failure mode
    that produces a plausible-looking assembly, which is worse than no assembly.
    """


def frame(position, z_dir=(0, 0, 1), x_dir=None) -> FreeCAD.Placement:
    """Build an oriented frame. `z_dir` is the outward normal; `x_dir` fixes the roll.

    If `x_dir` is omitted a stable perpendicular is chosen, which is fine whenever nothing about
    the mate cares which way "up" is around the axis. Pass it explicitly for anything keyed,
    flatted or bolted to a pattern, where roll is meaningful.
    """
    z = FreeCAD.Vector(*z_dir)
    if z.Length < 1e-12:
        raise MateError(f"z_dir must be a non-zero vector, got {z_dir}")
    z.normalize()
    if x_dir is None:
        helper = FreeCAD.Vector(1, 0, 0) if abs(z.x) < 0.9 else FreeCAD.Vector(0, 1, 0)
        x = helper - z * helper.dot(z)
    else:
        x = FreeCAD.Vector(*x_dir)
        x = x - z * x.dot(z)          # project onto the plane normal to z
        if x.Length < 1e-12:
            raise MateError("x_dir is parallel to z_dir, so the roll is undefined")
    x.normalize()
    y = z.cross(x)
    rot = FreeCAD.Rotation(x, y, z, "XYZ") if hasattr(FreeCAD.Rotation, "__call__") else None
    # Build from the three axes explicitly; FreeCAD.Rotation(x, y, z) takes column vectors.
    rot = FreeCAD.Rotation(x, y, z)
    return FreeCAD.Placement(FreeCAD.Vector(*position), rot)


def seat(target_placement: FreeCAD.Placement, target_frame: FreeCAD.Placement,
         moving_frame: FreeCAD.Placement) -> FreeCAD.Placement:
    """Placement that lands `moving_frame` face-to-face on `target_frame`. Gap is exactly zero.

    `target_placement` is where the target part already sits in the assembly, so the target
    frame is taken in world terms and the moving part is solved against it.
    """
    world = target_placement.multiply(target_frame)
    return world.multiply(_FLIP).multiply(moving_frame.inverse())


def align(target_placement: FreeCAD.Placement, target_frame: FreeCAD.Placement,
          moving_frame: FreeCAD.Placement) -> FreeCAD.Placement:
    """Placement that makes the two frames coincident and co-directed (shared axis)."""
    world = target_placement.multiply(target_frame)
    return world.multiply(moving_frame.inverse())


KINDS = {"seat": seat, "align": align}


def mesh_center_distance(module: float, teeth_a: int, teeth_b: int) -> float:
    """Centre distance for a standard external spur pair: m (z1 + z2) / 2.

    Exists so no caller ever eyeballs a gear spacing. This is the number that makes gears
    actually mesh rather than merely look meshed.
    """
    if module <= 0:
        raise MateError(f"module must be positive, got {module}")
    if teeth_a < 1 or teeth_b < 1:
        raise MateError(f"tooth counts must be positive, got {teeth_a} and {teeth_b}")
    return module * (teeth_a + teeth_b) / 2.0


def solve(parts: dict, ground: str, mates: list) -> dict:
    """Place every part from a chain of mates, starting at `ground`.

    `parts` maps name -> (shape, frames). `mates` is a list of
    `(kind, target_part, target_frame, moving_part, moving_frame)` applied in order.

    Returns name -> Placement. Refuses, rather than guessing, when:
      * a mate targets a part that has no placement yet (the chain is out of order),
      * a part would be placed by two mates (an over-constraint with no defined winner),
      * a frame name does not exist on the part.
    """
    if ground not in parts:
        raise MateError(f"ground part {ground!r} is not in the assembly; have {sorted(parts)}")
    placements = {ground: FreeCAD.Placement()}

    for i, spec in enumerate(mates):
        if len(spec) != 5:
            raise MateError(
                f"mate {i} must be (kind, target_part, target_frame, moving_part, moving_frame), "
                f"got {spec!r}")
        kind, tgt, tgt_f, mov, mov_f = spec
        if kind not in KINDS:
            raise MateError(f"mate {i}: unknown kind {kind!r}; have {sorted(KINDS)}")
        for part in (tgt, mov):
            if part not in parts:
                raise MateError(f"mate {i}: no part named {part!r}; have {sorted(parts)}")
        if tgt not in placements:
            raise MateError(
                f"mate {i}: target {tgt!r} has no placement yet — mates are applied in order, "
                f"so place {tgt!r} before mating onto it")
        if mov in placements:
            raise MateError(
                f"mate {i}: {mov!r} is already placed; a second mate on it would be an "
                "over-constraint with no defined winner")

        frames_t, frames_m = parts[tgt][1], parts[mov][1]
        if tgt_f not in frames_t:
            raise MateError(
                f"mate {i}: {tgt!r} has no frame {tgt_f!r}; it has {sorted(frames_t)}")
        if mov_f not in frames_m:
            raise MateError(
                f"mate {i}: {mov!r} has no frame {mov_f!r}; it has {sorted(frames_m)}")

        placements[mov] = KINDS[kind](placements[tgt], frames_t[tgt_f], frames_m[mov_f])

    unplaced = set(parts) - set(placements)
    if unplaced:
        raise MateError(
            f"no mate places {', '.join(sorted(unplaced))}; every part needs one or it floats "
            "at the origin, which looks like a modelling error and is one")
    return placements


def gap(shape_a, shape_b) -> float:
    """Shortest distance between two placed shapes. 0.0 means touching or interfering."""
    return float(shape_a.distToShape(shape_b)[0])


def axis_distance(point, axis_origin, axis_dir) -> float:
    """Perpendicular distance from a point to an infinite axis — used to measure radii exactly."""
    p = FreeCAD.Vector(*point) - FreeCAD.Vector(*axis_origin)
    d = FreeCAD.Vector(*axis_dir)
    d.normalize()
    return float(math.sqrt(max(p.Length ** 2 - p.dot(d) ** 2, 0.0)))
