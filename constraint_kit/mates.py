"""The deterministic mate kernel (Phase 0: coincident).

A mate consumes two NAMED anchor frames (one on each part, already lifted to world space for the
target) and returns the world cadquery.Location for the moving part B such that the requested
relation holds. No optimization, no solver iteration -- closed form, applied in order. This is the
domain-independent core extracted from the placement scripts (scale -> align -> coincident -> contact).

Phase 0 implements COINCIDENT as a frame-to-frame map. Anchors carry orientation, so this already
generalizes beyond pure translation: B is moved so b_anchor lands exactly on a_anchor.
"""
from __future__ import annotations

import math

import cadquery as cq

MATE_TYPES = ("coincident", "rigid", "contact", "mesh", "revolute", "cylindrical")


def _origin_axis(loc: cq.Location):
    """World origin and +Z direction of an oriented frame Location."""
    from OCP.gp import gp_Pnt
    t = loc.wrapped.Transformation()
    o = gp_Pnt(0, 0, 0).Transformed(t)
    z = gp_Pnt(0, 0, 1).Transformed(t)
    return (o.X(), o.Y(), o.Z()), (z.X() - o.X(), z.Y() - o.Y(), z.Z() - o.Z())


def gear_mesh_center_distance(module: float, teeth_a: int, teeth_b: int) -> float:
    """Standard involute spur-gear center distance: c = m*(z1+z2)/2 (sum of pitch radii)."""
    return module * (teeth_a + teeth_b) / 2.0


def coincident(a_anchor_world: cq.Location, b_anchor_local: cq.Location) -> cq.Location:
    """World Location for part B so its local frame `b_anchor_local` coincides with the
    already-world-space frame `a_anchor_world`.

    For a point p in B-local: world(p) = part_loc * p. We need part_loc * b_anchor = a_anchor,
    hence part_loc = a_anchor * b_anchor^-1.

    Note: cadquery's Location.inverse is a PROPERTY (returns a Location), not a method.
    """
    return a_anchor_world * b_anchor_local.inverse


def contact(a_anchor_world: cq.Location, b_wp: cq.Workplane) -> cq.Location:
    """Deterministic seat (the placement-pipeline 'contact' mate, generalized): drop B along +Z so
    its lowest extent rests exactly on the a_anchor plane, centered in XY on a_anchor.

    Geometry-aware (needs B's solid, not just an anchor) -- this is the floor/crown-seat from
    the placement scripts, with bbox standing in for the
    central-column trick (refine to central-column when overhangs appear, per README.md).
    """
    bb = b_wp.val().BoundingBox()
    (ax, ay, az), _ = a_anchor_world.toTuple()
    cx, cy = (bb.xmin + bb.xmax) / 2.0, (bb.ymin + bb.ymax) / 2.0
    return cq.Location(cq.Vector(ax - cx, ay - cy, az - bb.zmin))


def revolute(a_anchor_world: cq.Location, b_anchor_local: cq.Location,
             angle_deg: float = 0.0) -> cq.Location:
    """Kinematic 1-DOF rotational joint: coincide B's frame on A's, then rotate B about the shared
    axis (A-frame +Z) by `angle_deg`. The axis line and frame origin are preserved — only the roll
    about the axis changes. (A shaft spinning in a bore, a hinge, a gear on a journal.)"""
    placed = coincident(a_anchor_world, b_anchor_local)
    (px, py, pz), (dx, dy, dz) = _origin_axis(a_anchor_world)
    from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf
    trsf = gp_Trsf()
    trsf.SetRotation(gp_Ax1(gp_Pnt(px, py, pz), gp_Dir(dx, dy, dz)), math.radians(angle_deg))
    return cq.Location(trsf) * placed


def cylindrical(a_anchor_world: cq.Location, b_anchor_local: cq.Location,
                angle_deg: float = 0.0, slide: float = 0.0) -> cq.Location:
    """Kinematic 2-DOF joint: revolute + a slide of `slide` mm along the shared axis."""
    loc = revolute(a_anchor_world, b_anchor_local, angle_deg)
    (_, _, _), (dx, dy, dz) = _origin_axis(a_anchor_world)
    from OCP.gp import gp_Trsf, gp_Vec
    n = math.sqrt(dx * dx + dy * dy + dz * dz) or 1.0
    trsf = gp_Trsf()
    trsf.SetTranslation(gp_Vec(dx / n * slide, dy / n * slide, dz / n * slide))
    return cq.Location(trsf) * loc


def solve(mate_type: str, a_anchor_world: cq.Location,
          b_anchor_local: cq.Location | None = None,
          b_wp: cq.Workplane | None = None,
          angle_deg: float = 0.0, slide: float = 0.0) -> cq.Location:
    # 'rigid' = a joint-frame mate: identical to coincident, but the frames carry ORIENTATION (the +Z
    # mate axis), so it also aligns axes. Named distinctly to express joint intent in specs.
    if mate_type in ("coincident", "rigid"):
        if b_anchor_local is None:
            raise ValueError(f"{mate_type} needs b_anchor_local")
        return coincident(a_anchor_world, b_anchor_local)
    if mate_type == "contact":
        if b_wp is None:
            raise ValueError("contact needs b_wp (B's solid)")
        return contact(a_anchor_world, b_wp)
    if mate_type == "revolute":
        if b_anchor_local is None:
            raise ValueError("revolute needs b_anchor_local")
        return revolute(a_anchor_world, b_anchor_local, angle_deg)
    if mate_type == "cylindrical":
        if b_anchor_local is None:
            raise ValueError("cylindrical needs b_anchor_local")
        return cylindrical(a_anchor_world, b_anchor_local, angle_deg, slide)
    raise ValueError(f"unknown / unimplemented mate type {mate_type!r} (have {MATE_TYPES})")
