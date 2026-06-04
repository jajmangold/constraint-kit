"""Parametric part generators.

Each generator returns (workplane, anchors) where:
  - workplane : a cadquery.Workplane holding one solid, built in its own local frame.
  - anchors   : dict[str, cadquery.Location] of NAMED MATE FRAMES on the part (the rig-equivalent
                the kernel mates against -- see ../README.md "semantic grounding").

Anchors are the part's contract with the mate solver. Keep frames simple and documented: origin +
(for Phase 0) identity orientation, so a coincident mate is a pure translation.
"""
from __future__ import annotations

import math

import cadquery as cq

# g / mm^3 -- multiply by part volume (mm^3) to get grams.
DENSITY_G_MM3 = {
    "steel": 0.00785, "stainless": 0.0080, "iron": 0.00787,
    "aluminum": 0.0027, "al": 0.0027, "brass": 0.0085, "copper": 0.00896,
    "abs": 0.00104, "pla": 0.00124, "petg": 0.00127, "nylon": 0.00114,
}


def _loc(x: float = 0.0, y: float = 0.0, z: float = 0.0) -> cq.Location:
    return cq.Location(cq.Vector(x, y, z))


def plate(width: float = 60.0, depth: float = 60.0, thick: float = 6.0,
          boss_d: float = 12.0, boss_h: float = 4.0,
          bolt_d: float = 4.0, bolt_circle: float = 40.0, bolt_count: int = 4):
    """A rectangular mounting plate, centered on the origin, with a centered top boss and a
    bolt circle. Box spans z in [-thick/2, +thick/2]; the boss rises from the +Z face.

    Anchors: 'mount' (top-center of the boss, the seat for a mating part), 'base' (bottom face center).
    """
    bolt_count = int(bolt_count)
    wp = cq.Workplane("XY").box(width, depth, thick)
    wp = wp.faces(">Z").workplane().circle(boss_d / 2.0).extrude(boss_h)
    if bolt_count > 0 and bolt_d > 0:
        holes = (cq.Workplane("XY")
                 .polarArray(bolt_circle / 2.0, 0, 360, bolt_count)
                 .circle(bolt_d / 2.0)
                 .extrude(thick * 2.0, both=True))
        wp = wp.cut(holes)
    anchors = {
        "mount": _loc(0, 0, thick / 2.0 + boss_h),
        "base":  _loc(0, 0, -thick / 2.0),
        "boss_axis": _loc(0, 0, thick / 2.0),
    }
    # per-hole anchors on the TOP face (z=+thick/2), matching the polarArray hole centers, so a bolt
    # head can seat in a specific hole: bolt.seat -> plate.bolt{i}. polarArray starts at 0deg, CCW.
    if bolt_count > 0:
        r = bolt_circle / 2.0
        for i in range(bolt_count):
            th = math.radians(i * 360.0 / bolt_count)
            anchors[f"bolt{i}"] = _loc(r * math.cos(th), r * math.sin(th), thick / 2.0)
    return wp, anchors


def spur_gear(module: float = 1.0, teeth: int = 20, width: float = 6.0,
              bore_d: float = 12.0, pressure_angle: float = 20.0):
    """An involute spur gear (cq_gears), axis = Z, built from z=0 up to z=width, bore centered.

    Anchors: 'bore_base' (bore center on the z=0 face -> seats onto a 'mount'),
             'bore_top' (bore center on the z=width face).
    Falls back to a plain bored cylinder ("gear blank", pitch-radius sized) if cq_gears is
    unavailable or the parameters are degenerate, so the pipeline still completes.
    """
    teeth = int(teeth)
    try:
        from cq_gears import SpurGear
        g = SpurGear(module=module, teeth_number=teeth, width=width,
                     bore_d=bore_d, pressure_angle=pressure_angle)
        built = g.build()
        wp = built if isinstance(built, cq.Workplane) else cq.Workplane("XY").add(built)
        fallback = False
    except Exception as exc:  # noqa: BLE001 -- any cq_gears failure -> blank, keep the slice alive
        pitch_r = max(module * teeth / 2.0, 1.0)
        wp = cq.Workplane("XY").circle(pitch_r).extrude(width)
        if bore_d and bore_d > 0:
            wp = wp.faces(">Z").workplane().hole(bore_d)
        fallback = True
        wp.metadata = {"gear_fallback": str(exc)}  # type: ignore[attr-defined]
    anchors = {
        "bore_base": _loc(0, 0, 0),
        "bore_top":  _loc(0, 0, width),
    }
    if 'fallback' in dir() and fallback:
        anchors["_fallback"] = _loc()
    return wp, anchors


def ring_gear(module: float = 1.0, teeth: int = 36, width: float = 6.0, rim_width: float = 3.0,
              pressure_angle: float = 20.0):
    """An internal (ring) gear, axis = Z, z in [0, width]. anchors: 'bore_base','bore_top' (the axis)."""
    from cq_gears import RingGear
    g = RingGear(module=module, teeth_number=int(teeth), width=width, rim_width=rim_width,
                 pressure_angle=pressure_angle)
    built = g.build()
    wp = built if isinstance(built, cq.Workplane) else cq.Workplane("XY").add(built)
    return wp, {"bore_base": _loc(0, 0, 0), "bore_top": _loc(0, 0, width),
                "axis": _loc(0, 0, width / 2.0)}


def planetary_gearset(module: float = 1.0, sun_teeth: int = 12, planet_teeth: int = 12,
                      width: float = 6.0, rim_width: float = 3.0, n_planets: int = 3,
                      pressure_angle: float = 20.0):
    """A complete meshed planetary set (cq_gears) — sun + N planets + ring, correctly phased. This is
    the GROUND-TRUTH geometry; the closed-loop constraints are validated separately (planetary.validate
    + the DOF service). Raises if the configuration is not a valid/assemblable planetary set.

    anchors: 'axis' (z mid), 'base', 'top' (the common rotation axis)."""
    from . import planetary
    errs = planetary.validate(module, sun_teeth, planet_teeth, n_planets)
    if errs:
        raise ValueError("invalid planetary set: " + "; ".join(errs))
    from cq_gears import PlanetaryGearset
    g = PlanetaryGearset(module=module, sun_teeth_number=int(sun_teeth),
                         planet_teeth_number=int(planet_teeth), width=width, rim_width=rim_width,
                         n_planets=int(n_planets), pressure_angle=pressure_angle)
    built = g.build()
    wp = built if isinstance(built, cq.Workplane) else cq.Workplane("XY").add(built)
    return wp, {"base": _loc(0, 0, 0), "top": _loc(0, 0, width), "axis": _loc(0, 0, width / 2.0)}


def spacer(outer_d: float = 14.0, bore_d: float = 6.0, height: float = 10.0):
    """A bored cylindrical spacer/standoff, axis = Z, built from z=0 up to z=height, bore centered.

    Anchors: 'bottom' (z=0 center -> seats onto a 'mount'/'top'),
             'top' (z=height center -> the next part seats here -> enables chained stacks).
    """
    wp = cq.Workplane("XY").circle(outer_d / 2.0).extrude(height)
    if bore_d and bore_d > 0:
        wp = wp.faces(">Z").workplane().hole(bore_d)
    anchors = {"bottom": _loc(0, 0, 0), "top": _loc(0, 0, height)}
    return wp, anchors


def four_bar(ground: float = 100.0, crank: float = 30.0, coupler: float = 90.0, rocker: float = 60.0,
             input_angle_deg: float = 60.0, link_width: float = 6.0, thickness: float = 4.0):
    """A planar 4-bar linkage POSED at `input_angle_deg` — the geometry comes from the SolveSpace
    geometric closed-loop solve (linkage.solve_four_bar), so the bars provably close the loop. Bars lie
    in the z=0 plane. anchors: the four joint pivots 'A','B','C','D'. Raises if the linkage can't close."""
    import math as _m

    from . import linkage
    sol = linkage.solve_four_bar(ground, crank, coupler, rocker, input_angle_deg)
    if not sol["ok"]:
        raise ValueError(f"4-bar cannot close at {input_angle_deg}deg (grashof={sol['grashof']})")
    P = sol["positions"]

    def _bar(p, q):
        (x1, y1), (x2, y2) = p, q
        L = _m.dist(p, q)
        ang = _m.degrees(_m.atan2(y2 - y1, x2 - x1))
        b = cq.Workplane("XY").box(L, link_width, thickness, centered=(False, True, True))
        return b.rotate((0, 0, 0), (0, 0, 1), ang).translate((x1, y1, 0))

    links = [("A", "B"), ("B", "C"), ("D", "C"), ("A", "D")]
    wp = _bar(P[links[0][0]], P[links[0][1]])
    for a, b in links[1:]:
        wp = wp.union(_bar(P[a], P[b]))
    # pivot pins at each joint (fill the corners; a real linkage pivots on these)
    for (x, y) in P.values():
        pin = cq.Workplane("XY").circle(link_width * 0.45).extrude(thickness * 1.5,
                                                                    both=True).translate((x, y, 0))
        wp = wp.union(pin)
    anchors = {k: _loc(P[k][0], P[k][1], 0) for k in P}
    return wp, anchors


def shaft(diameter: float = 8.0, length: float = 60.0):
    """A plain round shaft, axis = Z, z in [0, length]. Mates into a bore via a `revolute` joint so it
    can spin. anchors: 'base'(z=0), 'top'(z=length), 'mid'(z=length/2); 'key' is an OFF-AXIS marker
    frame at radius=diameter/2 (so a rotation about the shaft axis is observable/testable)."""
    from .joints import frame
    wp = cq.Workplane("XY").circle(diameter / 2.0).extrude(length)
    return wp, {
        "base": _loc(0, 0, 0), "top": _loc(0, 0, length), "mid": _loc(0, 0, length / 2.0),
        "key": frame((diameter / 2.0, 0, length / 2.0), z_axis=(0, 0, 1), x_axis=(1, 0, 0)),
    }


def bolt(shank_d: float = 5.0, length: float = 16.0, head_d: float = 9.0, head_h: float = 4.0):
    """A cap/cheese-head bolt, axis = Z, head ABOVE the seat plane (z in [0, head_h]) and shank BELOW
    (z in [-length, 0]) so it drops head-up into a hole. anchors: 'seat' (under-head, z=0 -> lands on a
    plate top / a plate 'bolt{i}' hole), 'tip' (z=-length), 'head_top' (z=head_h)."""
    head = cq.Workplane("XY").circle(head_d / 2.0).extrude(head_h)
    shank = cq.Workplane("XY").circle(shank_d / 2.0).extrude(-length)
    wp = head.union(shank)
    anchors = {"seat": _loc(0, 0, 0), "tip": _loc(0, 0, -length), "head_top": _loc(0, 0, head_h)}
    return wp, anchors


def nut(af: float = 8.0, height: float = 5.0, bore_d: float = 5.0):
    """A hex nut, axis = Z, z in [0, height], bore centered. `af` = width across flats.
    anchors: 'bottom' (z=0 bore center), 'top' (z=height)."""
    circ_d = af / math.cos(math.pi / 6.0)  # across-flats -> circumscribed (corner) diameter
    wp = cq.Workplane("XY").polygon(6, circ_d).extrude(height)
    if bore_d and bore_d > 0:
        wp = wp.faces(">Z").workplane().hole(bore_d)
    return wp, {"bottom": _loc(0, 0, 0), "top": _loc(0, 0, height)}


def washer(outer_d: float = 12.0, bore_d: float = 5.5, thick: float = 1.5):
    """A flat washer, axis = Z, z in [0, thick]. anchors: 'bottom', 'top' (bore center)."""
    wp = cq.Workplane("XY").circle(outer_d / 2.0).extrude(thick)
    if bore_d and bore_d > 0:
        wp = wp.faces(">Z").workplane().hole(bore_d)
    return wp, {"bottom": _loc(0, 0, 0), "top": _loc(0, 0, thick)}


def bearing(outer_d: float = 22.0, bore_d: float = 8.0, width: float = 7.0):
    """A SIMPLIFIED bearing envelope (solid ring OD->bore, no balls/races), axis = Z, z in [0, width].
    Good for fit/mass/layout; not a functional bearing model. anchors: 'bore_base','bore_top'."""
    wp = cq.Workplane("XY").circle(outer_d / 2.0).extrude(width)
    if bore_d and bore_d > 0:
        wp = wp.faces(">Z").workplane().hole(bore_d)
    return wp, {"bore_base": _loc(0, 0, 0), "bore_top": _loc(0, 0, width)}


def housing(width: float = 60.0, depth: float = 60.0, height: float = 40.0, wall: float = 3.0,
            bore_d: float = 20.0, bolt_d: float = 4.0, bolt_circle: float = 0.0, bolt_count: int = 4,
            fillet: float = 2.0):
    """An open-top rectangular ENCLOSURE — the first 'rich' native part (T1.5): a filleted box shelled to
    `wall` thickness, with a central bore through the floor (for a shaft/bearing) and an optional bolt
    circle of mounting holes. Box spans z in [0, height], centered in XY.

    Anchors: 'base' (floor bottom center), 'top' (rim opening center), 'bore_axis' (floor bore, +Z),
    'bolt0..N' (mount holes on the inner floor, matching the polar array)."""
    bolt_count = int(bolt_count)
    wp = cq.Workplane("XY").box(width, depth, height, centered=(True, True, False))   # z in [0, height]
    if fillet > 0:
        try:
            wp = wp.edges("|Z").fillet(fillet)        # round the four vertical edges first
        except Exception:  # noqa: BLE001 -- impossible fillet: keep square (fail-soft)
            pass
    wp = wp.faces(">Z").shell(-wall)                  # hollow it, open top
    if bore_d > 0:
        wp = wp.faces("<Z").workplane().hole(bore_d)  # central bore through the floor
    if bolt_count > 0 and bolt_d > 0 and bolt_circle > 0:
        holes = (cq.Workplane("XY").polarArray(bolt_circle / 2.0, 0, 360, bolt_count)
                 .circle(bolt_d / 2.0).extrude(wall * 3.0))   # through the floor
        wp = wp.cut(holes)
    anchors = {"base": _loc(0, 0, 0), "top": _loc(0, 0, height), "bore_axis": _loc(0, 0, 0)}
    if bolt_count > 0 and bolt_circle > 0:
        r = bolt_circle / 2.0
        for i in range(bolt_count):
            th = math.radians(i * 360.0 / bolt_count)
            anchors[f"bolt{i}"] = _loc(r * math.cos(th), r * math.sin(th), wall)
    return wp, anchors


def adapter(bottom_w: float = 40.0, bottom_d: float = 40.0, top_d: float = 24.0,
            height: float = 30.0):
    """A square-to-round TRANSITION (T1.3) built by LOFTING a rectangular base to a circular top — a
    non-prismatic part (duct/transition/reducer). Solid; `finish:{"shell": t}` makes it a hollow duct.
    Rect base at z=0, circular top at z=height, centered in XY. Anchors: 'base', 'top'."""
    wp = (cq.Workplane("XY").rect(bottom_w, bottom_d)
          .workplane(offset=height).circle(top_d / 2.0).loft(combine=True))
    return wp, {"base": _loc(0, 0, 0), "top": _loc(0, 0, height)}


# registry the planner/builder dispatch on; extend here as parts are added.
PART_GENS = {
    "plate": plate,
    "housing": housing,
    "adapter": adapter,
    "spur_gear": spur_gear,
    "ring_gear": ring_gear,
    "planetary_gearset": planetary_gearset,
    "spacer": spacer,
    "shaft": shaft,
    "four_bar": four_bar,
    "bolt": bolt,
    "nut": nut,
    "washer": washer,
    "bearing": bearing,
}
