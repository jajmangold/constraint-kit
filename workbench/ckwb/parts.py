# -*- coding: utf-8 -*-
"""Parametric generators on FreeCAD's own Part kernel, each returning (shape, named frames).

Why FreeCAD's kernel rather than cadquery: FreeCAD 1.0 bundles OCCT 7.8.1 and the host stack
runs OCP 7.9.3. Two OCCT builds cannot share a process, so a workbench that imported cadquery
would have to talk to an out-of-process service and marshal STEP on every rebuild. Built on
`Part` instead, the whole thing is one process, every parameter edit recomputes in milliseconds,
and it all runs headless under `freecadcmd` — which is also what makes it testable.

**Every generator returns frames, not just a shape.** A mate against "Face6" breaks the moment a
feature is added ahead of it; a mate against `top` or `bore` does not. Declaring frames in the
generator is how this sidesteps the topological-naming problem rather than fighting it, and it
is why `defaults_for()` reads the signature: the defaults table IS the signature, so a new
parameter cannot be added in one place and forgotten in another.

Honest about approximations, because a part that silently misrepresents hardware is worse than
no part: `ball_bearing` is an envelope and mass stand-in, not a rolling-element model;
`extrusion_2020` is a credible V-slot section, not a specific vendor's profile; the gear root is
a blended fillet where a real hobbed gear has a trochoid. Each says so at its definition.
"""
from __future__ import annotations

import inspect
import math

import FreeCAD
import Part

from .mates import frame

V = FreeCAD.Vector
_Z = V(0, 0, 1)


def _polar(r: float, a: float) -> V:
    return V(r * math.cos(a), r * math.sin(a), 0.0)


def _require_positive(**kw) -> None:
    for name, value in kw.items():
        if float(value) <= 0.0:
            raise ValueError(f"{name} must be positive, got {value}")


# ------------------------------------------------------------------------------------- plate

def plate(width: float = 60.0, depth: float = 40.0, thickness: float = 8.0,
          bore_d: float = 0.0, hole_d: float = 0.0, bolt_circle: float = 0.0,
          n_holes: int = 0):
    """Rectangular plate centred on the origin in XY, rising from z=0.

    Two independent hole features, because they answer different questions. `bore_d` is a single
    through-bore on the centre, which is what a bearing, a bushing or a shaft clearance wants.
    `hole_d`/`bolt_circle`/`n_holes` is a bolt circle, which is what mounting wants. A plate can
    have both: a bored boss ringed by fixings is the commonest plate there is.

    The centre bore was missing until the planner found it. Asked for a plate with a 20 mm bore
    through the middle, the model tried a bolt circle of radius zero, was refused, and reported
    that it would rather drop the feature than fake it. Correct of it, and a poor showing from
    the catalogue.
    """
    _require_positive(width=width, depth=depth, thickness=thickness)
    shape = Part.makeBox(width, depth, thickness, V(-width / 2.0, -depth / 2.0, 0.0))
    frames = {"top": frame((0, 0, thickness), (0, 0, 1)),
              "bottom": frame((0, 0, 0), (0, 0, -1))}

    if bore_d > 0.0:
        if bore_d >= min(width, depth):
            raise ValueError(
                f"bore_d={bore_d} is not smaller than the {width} x {depth} plate, so it would "
                f"not leave a plate behind")
        # Over-length tool so the cut is unambiguously through, never a skin-thin lid.
        shape = shape.cut(
            Part.makeCylinder(bore_d / 2.0, thickness + 2.0, V(0, 0, -1.0), _Z))
        frames["bore"] = frame((0, 0, 0), (0, 0, 1))
        frames["bore_top"] = frame((0, 0, thickness), (0, 0, 1))

    n = int(n_holes)
    if n > 0:
        if hole_d <= 0.0 or bolt_circle <= 0.0:
            raise ValueError("a bolt circle needs both hole_d and bolt_circle to be positive")
        r = bolt_circle / 2.0
        if bore_d > 0.0 and r - hole_d / 2.0 <= bore_d / 2.0:
            raise ValueError(
                f"bolt circle {bolt_circle} with {hole_d} holes runs into the {bore_d} centre "
                f"bore; the two would merge into one ragged opening rather than distinct holes")
        if r + hole_d / 2.0 > min(width, depth) / 2.0:
            raise ValueError(
                f"bolt circle {bolt_circle} with {hole_d} holes breaks the plate edge "
                f"({width} x {depth})")
        for i in range(n):
            a = 2.0 * math.pi * i / n
            c = _polar(r, a)
            # Over-length cylinder so the cut is unambiguously through, never a skin-thin lid.
            tool = Part.makeCylinder(hole_d / 2.0, thickness + 2.0, V(c.x, c.y, -1.0), _Z)
            shape = shape.cut(tool)
            frames[f"hole_{i}"] = frame((c.x, c.y, thickness), (0, 0, 1))
        if len(shape.Solids) != 1:
            raise ValueError("the bolt circle severed the plate into more than one solid")
    return shape, frames


# ------------------------------------------------------------------------------------ spacer

def spacer(od: float = 20.0, id_: float = 8.0, height: float = 6.0):
    """Plain annular spacer, axis on +Z, rising from z=0."""
    _require_positive(od=od, height=height)
    if id_ <= 0.0:
        raise ValueError(f"id_ must be positive, got {id_}")
    if id_ >= od:
        raise ValueError(f"bore id_={id_} must be smaller than od={od}")
    shape = Part.makeCylinder(od / 2.0, height).cut(
        Part.makeCylinder(id_ / 2.0, height + 2.0, V(0, 0, -1.0), _Z))
    return shape, {"top": frame((0, 0, height), (0, 0, 1)),
                   "bottom": frame((0, 0, 0), (0, 0, -1)),
                   "axis": frame((0, 0, 0), (0, 0, 1))}


# --------------------------------------------------------------------------- block with bore

def block_with_bore(width: float = 50.0, depth: float = 30.0, height: float = 24.0,
                    bore_d: float = 22.0):
    """Bearing-block style body: bore runs along Y through the block, centred on its height."""
    _require_positive(width=width, depth=depth, height=height, bore_d=bore_d)
    if bore_d >= min(width, height):
        raise ValueError(
            f"bore_d={bore_d} leaves no wall in a {width} x {height} block section")
    shape = Part.makeBox(width, depth, height, V(-width / 2.0, -depth / 2.0, 0.0))
    tool = Part.makeCylinder(bore_d / 2.0, depth + 2.0,
                             V(0.0, -depth / 2.0 - 1.0, height / 2.0), V(0, 1, 0))
    shape = shape.cut(tool)
    return shape, {
        "base": frame((0, 0, 0), (0, 0, -1)),
        "top": frame((0, 0, height), (0, 0, 1)),
        # +Y face of the bore, pointing out along the bore axis
        "bore": frame((0, depth / 2.0, height / 2.0), (0, 1, 0)),
        "bore_far": frame((0, -depth / 2.0, height / 2.0), (0, -1, 0)),
    }


# ------------------------------------------------------------------------------------- shaft

def shaft(diameter: float = 8.0, length: float = 60.0):
    """Plain round shaft, centred on the origin and running along Z.

    Centred rather than based at z=0 so that `align`-ing its `axis` frame into a bore puts the
    shaft symmetrically through it, which is what a journal usually wants.
    """
    _require_positive(diameter=diameter, length=length)
    shape = Part.makeCylinder(diameter / 2.0, length, V(0, 0, -length / 2.0), _Z)
    return shape, {"axis": frame((0, 0, 0), (0, 0, 1)),
                   "end_pos": frame((0, 0, length / 2.0), (0, 0, 1)),
                   "end_neg": frame((0, 0, -length / 2.0), (0, 0, -1))}


# -------------------------------------------------------------------------------- spur gear

def spur_gear_profile(module: float = 2.0, teeth: int = 20, pressure_angle: float = 20.0,
                      addendum: float = 1.0, dedendum: float = 1.25, samples: int = 18):
    """Closed involute tooth profile as a wire: 4 edges per tooth (2 flanks, tip arc, root arc).

    The flanks are B-splines interpolated through exact involute points. Below the base circle
    the involute does not exist, so those samples hold the base-circle angular width — a radial
    flank blended into the same spline. A real hobbed gear has a trochoidal fillet there; this is
    the standard simplification and it is geometrically inside the trochoid, so it never
    interferes with a mating tooth.

    Because the flanks are splines, do not measure this shape with `Shape.BoundBox` — it bounds
    the control polygon. The tip and root arcs end in vertices at exactly the tip and root radii.
    """
    m = float(module)
    z = int(teeth)
    _require_positive(module=m)
    if z < 7:
        raise ValueError(
            f"teeth={z} is below 7, where a standard 20-degree involute is severely undercut; "
            "use a larger tooth count or a profile-shifted gear")
    alpha = math.radians(float(pressure_angle))
    if not (0.0 < alpha < math.pi / 3):
        raise ValueError(f"pressure_angle must lie in (0, 60) degrees, got {pressure_angle}")

    r_pitch = m * z / 2.0
    r_base = r_pitch * math.cos(alpha)
    r_tip = r_pitch + addendum * m
    r_root = r_pitch - dedendum * m
    if r_root <= 0.0:
        raise ValueError(f"dedendum {dedendum} consumes the whole gear at module {m}, teeth {z}")

    theta_pitch = math.tan(alpha) - alpha
    half_pitch = math.pi / (2.0 * z)

    def half_width(r: float) -> float:
        """Half the tooth's angular width at radius r."""
        rr = max(float(r), r_base)
        phi = math.acos(min(1.0, max(-1.0, r_base / rr)))
        return half_pitch + theta_pitch - (math.tan(phi) - phi)

    if half_width(r_tip) <= 1e-6:
        raise ValueError(
            f"teeth come to a point at the tip for module {m}, teeth {z}, addendum {addendum}; "
            "reduce the addendum")

    n = max(6, int(samples))
    radii = [r_root + (r_tip - r_root) * i / (n - 1) for i in range(n)]
    pitch_angle = 2.0 * math.pi / z
    half_root, half_tip = half_width(r_root), half_width(r_tip)

    edges = []
    for k in range(z):
        base = k * pitch_angle
        right = [_polar(r, base - half_width(r)) for r in radii]
        left = [_polar(r, base + half_width(r)) for r in reversed(radii)]

        rf = Part.BSplineCurve()
        rf.interpolate(right)
        edges.append(rf.toShape())

        edges.append(Part.Arc(_polar(r_tip, base - half_tip),
                              _polar(r_tip, base),
                              _polar(r_tip, base + half_tip)).toShape())

        lf = Part.BSplineCurve()
        lf.interpolate(left)
        edges.append(lf.toShape())

        nxt = base + pitch_angle
        edges.append(Part.Arc(_polar(r_root, base + half_root),
                              _polar(r_root, base + pitch_angle / 2.0),
                              _polar(r_root, nxt - half_root)).toShape())

    wire = Part.Wire(edges)
    if not wire.isClosed():
        raise ValueError("the generated tooth profile did not close; this is a generator bug")
    return wire


def spur_gear(module: float = 2.0, teeth: int = 20, thickness: float = 8.0,
              bore_d: float = 0.0, pressure_angle: float = 20.0):
    """Involute spur gear, axis on +Z, rising from z=0. Mesh spacing: `mates.mesh_center_distance`."""
    _require_positive(thickness=thickness)
    wire = spur_gear_profile(module=module, teeth=teeth, pressure_angle=pressure_angle)
    shape = Part.Face(wire).extrude(V(0, 0, float(thickness)))
    if bore_d and bore_d > 0.0:
        r_root = module * teeth / 2.0 - 1.25 * module
        if bore_d / 2.0 >= r_root:
            raise ValueError(f"bore_d={bore_d} reaches the tooth roots; the gear would fall apart")
        shape = shape.cut(Part.makeCylinder(bore_d / 2.0, thickness + 2.0, V(0, 0, -1.0), _Z))
    return shape, {"axis": frame((0, 0, 0), (0, 0, 1)),
                   "top": frame((0, 0, thickness), (0, 0, 1)),
                   "bottom": frame((0, 0, 0), (0, 0, -1))}


# ------------------------------------------------------------------------------- V-slot stock

def extrusion_2020(length: float = 100.0):
    """20x20 V-slot aluminium section, running along +Z from z=0.

    A credible generic section, not a specific vendor's profile: 20x20 envelope, 4.2 mm centre
    bore, and a T-slot on each face (6.2 mm mouth, 2 mm deep, opening into a 10 x 3.6 mm
    channel). Cross-sectional area lands near 49% of solid bar, against roughly 51% for real
    2020 stock, so mass estimates are honest to a few percent. Use a vendor STEP file when a
    specific profile's exact slot geometry matters.
    """
    _require_positive(length=length)
    w = 20.0
    shape = Part.makeBox(w, w, length, V(-w / 2.0, -w / 2.0, 0.0))
    shape = shape.cut(Part.makeCylinder(4.2 / 2.0, length + 2.0, V(0, 0, -1.0), _Z))

    mouth_w, mouth_d, chan_w, chan_d = 6.2, 2.0, 10.0, 3.6
    for ux, uy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        if ux:  # slot on an X face: cut in along X
            mouth = Part.makeBox(mouth_d + 0.01, mouth_w, length + 2.0,
                                 V(ux * (w / 2.0 - mouth_d), -mouth_w / 2.0, -1.0))
            chan = Part.makeBox(chan_d, chan_w, length + 2.0,
                                V(ux * (w / 2.0 - mouth_d - chan_d) if ux > 0
                                  else ux * (w / 2.0 - mouth_d) - 0.0,
                                  -chan_w / 2.0, -1.0))
            if ux < 0:
                chan = Part.makeBox(chan_d, chan_w, length + 2.0,
                                    V(-(w / 2.0) + mouth_d, -chan_w / 2.0, -1.0))
        else:   # slot on a Y face: cut in along Y
            mouth = Part.makeBox(mouth_w, mouth_d + 0.01, length + 2.0,
                                 V(-mouth_w / 2.0, uy * (w / 2.0 - mouth_d), -1.0))
            chan = Part.makeBox(chan_w, chan_d, length + 2.0,
                                V(-chan_w / 2.0,
                                  (w / 2.0 - mouth_d - chan_d) if uy > 0
                                  else -(w / 2.0) + mouth_d, -1.0))
        shape = shape.cut(mouth).cut(chan)

    half = w / 2.0
    return shape, {
        # A slot's outward normal is for SEATING something on the face. Travelling along the rail
        # is a different direction entirely, so the axis is its own frame -- a distinction a
        # sweep test found the hard way by sliding a carriage sideways off the rail.
        "axis": frame((0, 0, 0), (0, 0, 1)),
        "slot_x+": frame((half, 0, length / 2.0), (1, 0, 0)),
        "slot_x-": frame((-half, 0, length / 2.0), (-1, 0, 0)),
        "slot_y+": frame((0, half, length / 2.0), (0, 1, 0)),
        "slot_y-": frame((0, -half, length / 2.0), (0, -1, 0)),
        "end": frame((0, 0, length), (0, 0, 1)),
        "end_near": frame((0, 0, 0), (0, 0, -1)),
    }


# --------------------------------------------------------------------------------- ball screw

def ball_screw_lead(nominal_d: float = 16.0, lead: float = 5.0) -> float:
    """The lead in mm per revolution. Exists so no caller ever infers it from the diameter.

    SFU**16**05 means 16 mm nominal and **5 mm lead**, and those two numbers are independent --
    an SFU1610 is the same diameter with twice the lead. Getting it from the part name is a
    coin flip, and on this hardware the lead is what sets 1600 steps/rev to 320 steps/mm.
    """
    _require_positive(nominal_d=nominal_d, lead=lead)
    return float(lead)


def ball_screw(nominal_d: float = 16.0, lead: float = 5.0, length: float = 500.0,
               end_d: float = 10.0, end_length: float = 20.0):
    """Rolled ball screw (SFU1605 by default), axis on +Z, with turned-down ends.

    The thread groove is **not** modelled. A 500 mm screw at 5 mm lead is a hundred turns of
    swept helix, which costs a great deal of geometry and buys nothing an assembly needs: the
    envelope, the mass and the end journals are what a bearing block, a nut and an interference
    check actually care about. `Part.makeHelix` is right there if a rendered thread is ever
    wanted for a drawing.

    The ends are turned down for BK/BF-style supports, which is how these are actually supplied.
    `lead` is carried as a parameter rather than implied, so a screw joint can read it.
    """
    _require_positive(nominal_d=nominal_d, lead=lead, length=length)
    if end_d > nominal_d:
        raise ValueError(f"end_d={end_d} cannot exceed the screw diameter {nominal_d}")
    if end_length < 0:
        raise ValueError(f"end_length must not be negative, got {end_length}")
    if 2.0 * end_length >= length:
        raise ValueError(
            f"machined ends of {end_length} each consume a {length} screw; "
            "reduce end_length or lengthen the screw")

    body = Part.makeCylinder(nominal_d / 2.0, length - 2.0 * end_length,
                             V(0, 0, end_length), _Z)
    shape = body
    if end_length > 0.0 and end_d > 0.0:
        shape = shape.fuse(Part.makeCylinder(end_d / 2.0, end_length, V(0, 0, 0), _Z))
        shape = shape.fuse(Part.makeCylinder(end_d / 2.0, end_length,
                                             V(0, 0, length - end_length), _Z))
    shape = shape.removeSplitter()
    return shape, {
        "axis": frame((0, 0, 0), (0, 0, 1)),
        "end_a": frame((0, 0, 0), (0, 0, -1)),
        "end_b": frame((0, 0, length), (0, 0, 1)),
        # Mid-travel, where a nut sits by default. Named so a mate does not have to do arithmetic.
        "nut_travel": frame((0, 0, length / 2.0), (0, 0, 1)),
    }


def ball_nut(nominal_d: float = 16.0, body_od: float = 28.0, flange_od: float = 48.0,
             flange_t: float = 10.0, length: float = 48.0, bolt_circle: float = 38.0,
             bolt_d: float = 6.5, n_bolts: int = 4):
    """Flanged single ball nut (SFU1605 by default), axis on +Z, flange at z=0.

    Bore is the screw's nominal diameter: the balls, not the bore, carry the load, so a nut that
    touched the screw would be wrong. Modelling it at nominal keeps a clash check meaningful --
    an interference against the nut bore then means a real collision, not the running fit.
    """
    _require_positive(nominal_d=nominal_d, body_od=body_od, flange_od=flange_od,
                      flange_t=flange_t, length=length)
    if body_od <= nominal_d:
        raise ValueError(f"body_od={body_od} must exceed the bore {nominal_d}")
    if flange_od <= body_od:
        raise ValueError(f"flange_od={flange_od} must exceed body_od={body_od}")
    if flange_t >= length:
        raise ValueError(f"flange_t={flange_t} must be less than the nut length {length}")

    shape = Part.makeCylinder(body_od / 2.0, length)
    shape = shape.fuse(Part.makeCylinder(flange_od / 2.0, flange_t))
    shape = shape.cut(Part.makeCylinder(nominal_d / 2.0, length + 2.0, V(0, 0, -1.0), _Z))

    n = int(n_bolts)
    if n > 0:
        r = bolt_circle / 2.0
        if r - bolt_d / 2.0 <= body_od / 2.0:
            raise ValueError(
                f"bolt circle {bolt_circle} with {bolt_d} holes cuts into the nut body "
                f"({body_od} OD)")
        if r + bolt_d / 2.0 >= flange_od / 2.0:
            raise ValueError(f"bolt circle {bolt_circle} breaks the flange rim ({flange_od} OD)")
        for i in range(n):
            a = 2.0 * math.pi * i / n + math.pi / n
            c = _polar(r, a)
            shape = shape.cut(Part.makeCylinder(bolt_d / 2.0, flange_t + 2.0,
                                                V(c.x, c.y, -1.0), _Z))
    shape = shape.removeSplitter()
    if len(shape.Solids) != 1:
        raise ValueError("the bolt circle severed the flange")
    return shape, {
        "flange": frame((0, 0, 0), (0, 0, -1)),
        "back": frame((0, 0, length), (0, 0, 1)),
        # The nut runs ON the screw, so its bore frame is centred for an `align` mate.
        "bore": frame((0, 0, length / 2.0), (0, 0, 1)),
    }


# ----------------------------------------------------------------------------------- vitamins

def ball_bearing(od: float = 22.0, id_: float = 8.0, width: float = 7.0):
    """Deep-groove ball bearing as an ENVELOPE and mass stand-in, not a rolling-element model.

    Two races with the ball track relieved between them. For a 608 (8x22x7) this lands at about
    13 g against roughly 12 g for the real part, so BOM mass and interference are trustworthy;
    the balls, cage and seals are not modelled and nothing here should be used to reason about
    load ratings. Press it with `align` on `bore`, and seat it with `face`.
    """
    _require_positive(od=od, width=width)
    if id_ <= 0.0 or id_ >= od:
        raise ValueError(f"id_={id_} must be positive and smaller than od={od}")
    shape = Part.makeCylinder(od / 2.0, width).cut(
        Part.makeCylinder(id_ / 2.0, width + 2.0, V(0, 0, -1.0), _Z))
    # Relieve the ball track from both faces, leaving a central web.
    r_mid = (od + id_) / 4.0
    track_half = max(0.8, (od - id_) / 8.0)
    web = max(width * 0.35, 1.2)
    relief_d = max((width - web) / 2.0, 0.2)
    for z0 in (0.0, width - relief_d):
        ring = Part.makeCylinder(min(r_mid + track_half, od / 2.0 - 0.6), relief_d, V(0, 0, z0), _Z)
        ring = ring.cut(Part.makeCylinder(max(r_mid - track_half, id_ / 2.0 + 0.6),
                                          relief_d + 2.0, V(0, 0, z0 - 1.0), _Z))
        shape = shape.cut(ring)
    return shape, {"face": frame((0, 0, width), (0, 0, 1)),
                   "face_far": frame((0, 0, 0), (0, 0, -1)),
                   "bore": frame((0, 0, 0), (0, 0, 1))}


#: d, head diameter, head height, hex socket across flats — ISO 4762 socket head cap screws.
_SCREWS = {"M3": (3.0, 5.5, 3.0, 2.5), "M4": (4.0, 7.0, 4.0, 3.0), "M5": (5.0, 8.5, 5.0, 4.0),
           "M6": (6.0, 10.0, 6.0, 5.0), "M8": (8.0, 13.0, 8.0, 6.0)}


def screw(size: str = "M5", length: float = 16.0):
    """ISO 4762 socket head cap screw. Head above z=0, shank running down -Z.

    The thread is not cut: a plain shank at the nominal diameter. Modelled threads cost a great
    deal of geometry for no engineering value in an assembly check, and `rules`-style engagement
    checks work off length and pitch rather than off helices. `Part.makeThread` is available if
    a rendered thread is ever actually wanted.

    Its `seat` frame is the head's bearing face pointing out of the clamped material, so
    `seat(plate_placement, plate_frames["top"], screw_frames["seat"])` drops it into the hole.
    """
    key = str(size).upper()
    if key not in _SCREWS:
        raise ValueError(f"unknown screw size {size!r}; have {', '.join(sorted(_SCREWS))}")
    _require_positive(length=length)
    d, hd, hh, af = _SCREWS[key]
    shape = Part.makeCylinder(hd / 2.0, hh)
    shape = shape.fuse(Part.makeCylinder(d / 2.0, float(length), V(0, 0, -float(length)), _Z))
    # hex socket, 60% of head height deep
    r_socket = af / math.sqrt(3.0)
    hexa = [_polar(r_socket, math.radians(60 * i)) for i in range(6)]
    hexa.append(hexa[0])
    socket = Part.Face(Part.makePolygon([V(p.x, p.y, hh - hh * 0.6) for p in hexa]))
    shape = shape.cut(socket.extrude(V(0, 0, hh * 0.6 + 1.0)))
    shape = shape.removeSplitter()
    return shape, {"seat": frame((0, 0, 0), (0, 0, -1)),
                   "head": frame((0, 0, hh), (0, 0, 1)),
                   "axis": frame((0, 0, 0), (0, 0, -1)),
                   "tip": frame((0, 0, -float(length)), (0, 0, -1))}


# ----------------------------------------------------------------------------------- registry

# ------------------------------------------------------------------- freeform: loft, sweep,
# revolve. The shapes a prismatic catalogue cannot express, and the gap the live planner found:
# asked for a turbine blade it declined, correctly, because "nothing here can express a lofted,
# twisted, tapered aerofoil section". Everything else in this file is a prism, a cylinder, or a
# boolean of the two.

#: Closed wire profiles, in the XY plane, centred on the origin. A profile is a function of one
#: size so a loft can interpolate it along the stack without the caller enumerating sections.
_PROFILES = ("rect", "circle")


def _profile_wire(kind: str, size: float, twist_deg: float = 0.0, z: float = 0.0):
    """One closed wire of the named profile, `size` across, rotated `twist_deg`, at height `z`."""
    if kind not in _PROFILES:
        raise ValueError(
            f"unknown profile {kind!r}; have {', '.join(_PROFILES)}")
    if size <= 0.0:
        raise ValueError(f"profile size must be positive, got {size}")
    if kind == "circle":
        wire = Part.Wire([Part.Circle(V(0, 0, z), _Z, size / 2.0).toShape()])
        return wire                              # a circle has no corners to twist
    half = size / 2.0
    pts = [V(-half, -half, z), V(half, -half, z), V(half, half, z), V(-half, half, z)]
    if twist_deg:
        rot = FreeCAD.Rotation(_Z, twist_deg)
        pts = [rot.multVec(V(p.x, p.y, 0.0)) + V(0, 0, z) for p in pts]
    edges = [Part.LineSegment(pts[i], pts[(i + 1) % 4]).toShape() for i in range(4)]
    return Part.Wire(edges)


def loft(profile: str = "rect", bottom: float = 40.0, top: float = 20.0,
         height: float = 60.0, sections: int = 2, twist: float = 0.0):
    """A solid lofted through a stack of parallel sections. Tapered, and optionally twisted.

    This is the generator the planner asked for and could not find. A duct that changes section,
    a tapered boss, a twisted blade: all of them are a stack of profiles at known heights with
    known sizes and known rotations, which is what this takes.

    `sections` is how many profiles the stack is built from, interpolated linearly between
    `bottom` and `top`. Two is a straight taper and is exactly representable, so adding
    intermediate sections to a straight taper changes nothing -- which is a test. A `twist` needs
    more than two, because a twist across a single span is a ruled surface between two rotated
    squares rather than a twist, and that is refused rather than silently delivered: it would
    look like a twisted blade from a distance and be a different shape entirely.
    """
    if profile not in _PROFILES:
        raise ValueError(f"unknown profile {profile!r}; have {', '.join(_PROFILES)}")
    if bottom <= 0.0:
        raise ValueError(f"bottom must be positive, got {bottom}")
    if top <= 0.0:
        raise ValueError(f"top must be positive, got {top}")
    _require_positive(height=height)
    n = int(sections)
    if n < 2:
        raise ValueError(f"a loft needs at least 2 sections, got {sections}")
    if twist and n < 3:
        raise ValueError(
            f"a twist of {twist} degrees needs at least 3 sections; across only two it is a "
            f"ruled surface between two rotated profiles rather than a twist, which would look "
            f"right and be a different shape")

    wires = []
    for i in range(n):
        t = i / float(n - 1)
        wires.append(_profile_wire(profile, bottom + (top - bottom) * t,
                                   twist_deg=twist * t, z=height * t))
    shape = Part.makeLoft(wires, True, False)      # solid, not ruled
    if len(shape.Solids) != 1 or not shape.isValid():
        raise ValueError(
            "the loft did not produce one valid solid; the section stack is probably "
            "self-intersecting (too much twist over too little height)")
    return shape, {
        "base": frame((0, 0, 0), (0, 0, -1)),
        "top": frame((0, 0, height), (0, 0, 1)),
        "axis": frame((0, 0, 0), (0, 0, 1)),
    }


def sweep(profile_d: float = 12.0, bend_radius: float = 40.0, angle: float = 90.0,
          helix_pitch: float = 0.0, helix_turns: float = 0.0):
    """A round profile swept along a circular bend or a helix: a pipe elbow, or a spring.

    Two paths from one generator because they are the same operation and the hardware is the
    same shape: a coolant bend and a compression spring are both a circle carried along a curve.
    `angle` sweeps a planar arc; `helix_turns` sweeps a helix instead and `angle` is then
    ignored.

    The frames are the point of a swept part. `start` and `end` sit at the two ends of the path
    with +Z along it, so the next thing in the line mates to the end of the bend rather than to
    a bounding box -- which is the whole reason not to approximate a bend with two cylinders.
    """
    _require_positive(profile_d=profile_d, bend_radius=bend_radius)
    if profile_d / 2.0 >= bend_radius:
        raise ValueError(
            f"bend_radius {bend_radius} is not larger than the {profile_d / 2.0} profile radius, "
            f"so the sweep would pass through itself on the inside of the bend")

    if helix_turns:
        if helix_pitch <= 0.0:
            raise ValueError(f"a helix needs a positive helix_pitch, got {helix_pitch}")
        path = Part.makeHelix(helix_pitch, helix_pitch * helix_turns, bend_radius)
        start_point = V(bend_radius, 0.0, 0.0)
        end_point = V(bend_radius * math.cos(2.0 * math.pi * helix_turns),
                      bend_radius * math.sin(2.0 * math.pi * helix_turns),
                      helix_pitch * helix_turns)
        start_dir = (0.0, 1.0, 0.0)
        end_dir = (-math.sin(2.0 * math.pi * helix_turns),
                   math.cos(2.0 * math.pi * helix_turns), 0.0)
    else:
        if not 0.0 < angle <= 360.0:
            raise ValueError(f"angle must lie in (0, 360], got {angle}")
        # A bend in the XZ plane: it starts at the origin running along +Z and curves toward +X,
        # so a straight tube mated to `start` continues the line it was already on.
        centre = V(bend_radius, 0.0, 0.0)
        axis = V(0.0, -1.0, 0.0)
        circle = Part.Circle(centre, axis, bend_radius)
        rad = math.radians(angle)
        path = Part.Wire([circle.toShape(0.0, rad)])
        start_point = V(0.0, 0.0, 0.0)
        end_point = V(bend_radius * (1.0 - math.cos(rad)), 0.0, bend_radius * math.sin(rad))
        start_dir = (0.0, 0.0, 1.0)
        end_dir = (math.sin(rad), 0.0, math.cos(rad))

    # The profile must be perpendicular to the path at its start, or the swept section is an
    # ellipse rather than the circle that was asked for.
    section = Part.Wire([Part.Circle(start_point, V(*start_dir), profile_d / 2.0).toShape()])
    shape = path.makePipeShell([section], True, True)      # make solid, and follow the Frenet
    if len(shape.Solids) != 1 or not shape.isValid():
        raise ValueError("the sweep did not produce one valid solid")
    return shape, {
        "start": frame((start_point.x, start_point.y, start_point.z),
                       (-start_dir[0], -start_dir[1], -start_dir[2])),
        "end": frame((end_point.x, end_point.y, end_point.z), end_dir),
        "axis": frame((0, 0, 0), (0, 0, 1)),
    }


def revolve(profile: str = "rect", r_inner: float = 10.0, r_outer: float = 20.0,
            height: float = 15.0, angle: float = 360.0):
    """A solid of revolution about Z: an annulus, a cone, a cup, a sector of any of them.

    `rect` revolves a rectangle and gives an annulus (or a plain cylinder at `r_inner = 0`);
    `taper` revolves a right triangle and gives a cone or a truncated cone. `angle` under 360
    gives a sector, which is what a segmented ring or a cable guide actually is.
    """
    kinds = ("rect", "taper")
    if profile not in kinds:
        raise ValueError(f"unknown profile {profile!r}; have {', '.join(kinds)}")
    _require_positive(height=height, r_outer=r_outer)
    if r_inner < 0.0:
        raise ValueError(
            f"r_inner must not be negative, got {r_inner}; a negative inner radius revolves the "
            f"profile through the axis and back out the other side")
    if r_inner >= r_outer:
        raise ValueError(f"r_inner {r_inner} must be smaller than r_outer {r_outer}")
    if not 0.0 < angle <= 360.0:
        raise ValueError(f"angle must lie in (0, 360], got {angle}")

    if profile == "rect":
        pts = [V(r_inner, 0, 0), V(r_outer, 0, 0), V(r_outer, 0, height), V(r_inner, 0, height)]
    else:
        # A right triangle: full width at the base, collapsing to r_inner at the top. Revolved,
        # that is a cone when r_inner is zero and a truncated cone otherwise.
        pts = [V(r_inner, 0, 0), V(r_outer, 0, 0), V(r_inner, 0, height)]
    edges = [Part.LineSegment(pts[i], pts[(i + 1) % len(pts)]).toShape()
             for i in range(len(pts))]
    face = Part.Face(Part.Wire(edges))
    shape = face.revolve(V(0, 0, 0), _Z, angle)
    if len(shape.Solids) != 1 or not shape.isValid():
        raise ValueError("the revolve did not produce one valid solid")
    return shape, {
        "base": frame((0, 0, 0), (0, 0, -1)),
        "top": frame((0, 0, height), (0, 0, 1)),
        "axis": frame((0, 0, 0), (0, 0, 1)),
    }



def gear_rack(module: float = 2.0, teeth: int = 12, width: float = 20.0,
              height: float = 20.0, pressure_angle: float = 20.0):
    """Involute gear rack: length along +X, teeth pointing +Y, pitch line at y = 0.

    A rack is the **limit of an involute gear as its radius goes to infinity**, and that is why
    its flanks are dead straight at the pressure angle -- the one gear profile that needs no
    involute at all. It is also the generating profile a hob cuts with, so a straight-flank rack
    and `spur_gear_profile` are exactly conjugate rather than approximately so.

    Dimensions are the ISO 53 basic rack: circular pitch `pi*m`, tooth thickness `pi*m/2` at the
    pitch line, addendum `1.0*m` above it and dedendum `1.25*m` below -- matching the pinion's
    `1.0/1.25` so a standard pinion meshes with no backlash and no root interference.

    **The length is exactly `teeth * pi * m` and the ends fall halfway along a root land**, so
    two racks butt into one unbroken tooth pattern with a correct full land across the joint.
    That matters because a long axis is several racks in a row, and a rack sized to its tooth
    tips instead would plant a half-pitch error at every joint.

    The `pitch` frame is the one to mate to: its origin sits on the pitch line at the rack's
    start, **+Z along the travel** and **+X along the pinion's rotation axis** -- which is
    perpendicular to the travel, unlike a screw, where the two share an axis.
    """
    m, z = float(module), int(teeth)
    _require_positive(module=m, width=width, height=height)
    if z < 1:
        raise ValueError(f"teeth={z} makes no rack; it needs at least one")
    alpha = math.radians(float(pressure_angle))
    if not (0.0 < alpha < math.pi / 3):
        raise ValueError(f"pressure_angle must lie in (0, 60) degrees, got {pressure_angle}")

    p = math.pi * m
    add, ded = 1.0 * m, 1.25 * m
    if height <= add + ded:
        raise ValueError(
            f"height={height} is not enough for a module-{m} rack, whose teeth alone are "
            f"{add + ded:.2f} mm from tip to root; it needs a back as well")

    t = math.tan(alpha)
    half_pitch, half_tip, half_root = p / 4.0, p / 4.0 - add * t, p / 4.0 + ded * t
    if half_tip <= 0.0:
        raise ValueError(
            f"a {pressure_angle} degree pressure angle brings the teeth to a point at module {m}")
    if 2.0 * half_root >= p:
        raise ValueError(
            f"a {pressure_angle} degree pressure angle closes the root land at module {m}")

    length = z * p
    pts = [V(0.0, -ded, 0.0)]
    for i in range(z):
        c = (i + 0.5) * p
        pts.append(V(c - half_root, -ded, 0.0))
        pts.append(V(c - half_tip, add, 0.0))
        pts.append(V(c + half_tip, add, 0.0))
        pts.append(V(c + half_root, -ded, 0.0))
    pts.append(V(length, -ded, 0.0))
    pts.append(V(length, add - height, 0.0))
    pts.append(V(0.0, add - height, 0.0))

    wire = Part.makePolygon(pts + [pts[0]])
    shape = Part.Face(wire).extrude(V(0, 0, float(width)))

    mid_z = width / 2.0
    return shape, {
        # +Z is the travel, +X is the pinion's rotation axis, so +Y points from the pitch line up
        # to the pinion centre. `motion.rack_centre_height` is the distance along that +Y.
        "pitch": frame((0.0, 0.0, mid_z), (1, 0, 0), (0, 0, -1)),
        "base": frame((length / 2.0, add - height, mid_z), (0, -1, 0)),
        "end_a": frame((0.0, 0.0, mid_z), (-1, 0, 0), (0, 0, -1)),
        "end_b": frame((length, 0.0, mid_z), (1, 0, 0), (0, 0, -1)),
        "front": frame((length / 2.0, 0.0, width), (0, 0, 1)),
    }


def c_beam(length: float = 200.0, width: float = 80.0, depth: float = 40.0,
           channel_w: float = 40.0, channel_d: float = 20.0):
    """OpenBuilds-style C-beam: 80 x 40 section running along +Z from z=0, channel facing -Y.

    The section `extrusion_2020` could not express, and the reason a linear-rail request had to
    be declined: a C-beam is 80 wide and 40 deep with a 40 x 20 channel down one long face, and
    the whole point of it is that the channel carries a screw or a rail while the flat opposite
    face takes the load. A 20 x 20 V-slot has neither.

    The defaults are Josh's actual stock, and the same numbers `gear-cutter`'s `machine.py`
    builds its frames from (CB_W 80, CB_D 40, CH_W 40, CH_D 20), so a part modelled here and a
    beam modelled there are the same beam.

    **The channel's own width and depth are PARAMETERS, not constants buried in here**, and that
    is because of a measured consequence of their being hidden: asked to put a ball screw down
    the channel, the planner answered *"the catalogue does not publish the channel's internal
    width/depth, so I did NOT put anything inside the C-channel - guessing there would risk a
    failed interference check"*. It was right to refuse, and the fix was to publish the numbers
    rather than to ask it to guess better. The catalogue a planner sees is built from these
    signatures, so a dimension that is not a parameter is a dimension nothing downstream can
    reason about.

    Credible rather than vendor-exact: the V-slots themselves are not cut, so the cross-section
    comes out heavier than real stock. Use a vendor STEP file when slot geometry matters.

    Frames, and which to mate to:
      `axis`     centreline of the whole section at z=0, +Z along the length. For a part that
                 runs the full length of the beam.
      `channel`  the channel FLOOR, centred, +Z still along the length so an `offset` mate
                 displaces across the channel rather than along it. This is where a ball screw
                 or a rail goes.
      `top`      the flat outer face opposite the channel, normal outward. For seating a plate.
      `side_x+` / `side_x-`  the two 40 mm faces.
      `end` / `end_near`     the cut ends, for butting beams.
    """
    _require_positive(length=length, width=width, depth=depth,
                      channel_w=channel_w, channel_d=channel_d)
    if channel_w >= width:
        raise ValueError(
            f"a {channel_w} mm channel in a {width} mm section leaves no side walls")
    if channel_d >= depth:
        raise ValueError(
            f"a {channel_d} mm deep channel cuts straight through a {depth} mm section")
    w, d, ch_w, ch_d = width, depth, channel_w, channel_d
    shape = Part.makeBox(w, d, length, V(-w / 2.0, -d / 2.0, 0.0))
    # The channel runs the FULL length, and it opens on -Y. Cutting a window instead of a
    # channel is a bug that shipped in gear-cutter's first beam model: every upright was wrong.
    shape = shape.cut(Part.makeBox(ch_w, ch_d, length + 2.0,
                                   V(-ch_w / 2.0, -d / 2.0, -1.0)))
    half_w, half_d = w / 2.0, d / 2.0
    return shape, {
        "axis": frame((0, 0, 0), (0, 0, 1)),
        # +Z along the length, so `offset` moves across the channel in dx/dy and along the beam
        # in dz -- the same convention as every other axis frame here.
        "channel": frame((0, -half_d + ch_d, 0), (0, 0, 1)),
        "top": frame((0, half_d, length / 2.0), (0, 1, 0)),
        "side_x+": frame((half_w, 0, length / 2.0), (1, 0, 0)),
        "side_x-": frame((-half_w, 0, length / 2.0), (-1, 0, 0)),
        "end": frame((0, 0, length), (0, 0, 1)),
        "end_near": frame((0, 0, 0), (0, 0, -1)),
    }


def profile_rail(width: float = 15.0, height: float = 15.0, length: float = 200.0,
                 hole_d: float = 4.5, hole_pitch: float = 60.0):
    """Profiled linear rail as an ENVELOPE, running along +Z. The thing a plain shaft is not.

    **The raceways are not modelled, and that is the whole declaration.** A real profile rail's
    function lives in two precision-ground gothic-arch grooves down its flanks, and nothing in
    this catalogue can express them. What IS modelled is the envelope, the counterbored mounting
    holes and the mass -- which is what a layout, a carriage height, a clearance and an
    interference check actually consume.

    So this is the same grade of part as `ball_bearing`, which says the same thing about balls
    and races, and it must never be mistaken for the real profile: it will not tell you a preload
    class, a rated load, or whether a carriage fits. Use a vendor STEP file for any of that.

    Dimensions are a credible generic rather than a specific vendor's: 15 x 15 is close to MGN15
    / HGR15 territory, and the defaults are parameters precisely so a measured rail replaces
    them. Mounting holes are a single row on the centreline at `hole_pitch`, which is how these
    are actually supplied.

    Frames:
      `axis`   the rail's own CENTRELINE, +Z along the length. Mate a carriage to this.
      `top`    the carriage-side face, normal outward, for measuring a stack-up from.
      `base`   the mounting face, normal outward -- what bolts to the beam.
      `end` / `end_near`   the cut ends.

    `axis` is the centreline and not the base, which looks like a detail and is not: every other
    long part here -- `shaft`, `ball_screw`, `c_beam` -- puts `axis` on its centreline, and a
    frame name that means one thing on four parts and something else on the fifth is a trap. It
    caught a live planner immediately: told to seat the rail on a face at y=20 it offset by 28,
    reasoning "radius 8 on a face at 20" exactly as it had for the 16 mm rods, and the rail
    floated. Use `base` to seat the rail on something.
    """
    _require_positive(width=width, height=height, length=length)
    if hole_d >= width:
        raise ValueError(f"hole_d={hole_d} will not fit across a {width} mm rail")
    # Length on +Z, like every other long part here, and centred across its width and height
    # so the `axis` frame is the rail's own centreline.
    shape = Part.makeBox(width, height, length, V(-width / 2.0, -height / 2.0, 0.0))
    if hole_d > 0.0 and hole_pitch > 0.0:
        n = max(int(length // hole_pitch), 1)
        first = (length - (n - 1) * hole_pitch) / 2.0
        for i in range(n):
            z = first + i * hole_pitch
            shape = shape.cut(Part.makeCylinder(hole_d / 2.0, height + 2.0,
                                                V(0, -height / 2.0 - 1.0, z),
                                                FreeCAD.Vector(0, 1, 0)))
    half_h = height / 2.0
    return shape, {
        "axis": frame((0, 0, 0), (0, 0, 1)),
        "top": frame((0, half_h, length / 2.0), (0, 1, 0)),
        "base": frame((0, -half_h, length / 2.0), (0, -1, 0)),
        "end": frame((0, 0, length), (0, 0, 1)),
        "end_near": frame((0, 0, 0), (0, 0, -1)),
    }


def rail_carriage(rail_width: float = 15.0, rail_height: float = 15.0, length: float = 40.0,
                  width: float = 34.0, height: float = 24.0, clearance: float = 0.3,
                  bolt_d: float = 4.5, bolt_dx: float = 26.0, bolt_dz: float = 26.0):
    """The block that rides a `profile_rail`: an ENVELOPE that straddles it, running along +Z.

    Composed from exactly what this catalogue has -- a box with a channel cut in it -- which is
    the honest answer to "surely it is made of parts you already have". The recirculating ball
    tracks and end caps are not modelled and cannot be; what is modelled is the block that
    occupies space above the rail, its mounting holes, and a channel with real `clearance` so
    that a clash check between carriage and rail MEANS something. Mate it with `align` from the
    rail's `axis` to this part's `rail` frame, then drive it with `motion.slide`.

    `height` is measured from the rail's own mounting face, so the `top` frame is the real
    carriage height a gantry plate would sit at -- the number a stack-up needs and the one that
    is easiest to get wrong by a rail height.
    """
    _require_positive(rail_width=rail_width, rail_height=rail_height, length=length,
                      width=width, height=height)
    if width <= rail_width:
        raise ValueError(
            f"a carriage {width} wide cannot straddle a {rail_width} rail; it has to wrap it")
    if height <= rail_height:
        raise ValueError(
            f"a carriage {height} tall cannot clear a {rail_height} rail it has to sit over")
    if clearance < 0.0:
        raise ValueError(f"clearance must not be negative, got {clearance}")

    # The block sits over the rail: its underside is the rail's mounting face.
    body = Part.makeBox(width, height, length, V(-width / 2.0, -rail_height / 2.0, 0.0))
    slot_w = rail_width + 2.0 * clearance
    slot_h = rail_height + clearance
    body = body.cut(Part.makeBox(slot_w, slot_h, length + 2.0,
                                 V(-slot_w / 2.0, -rail_height / 2.0, -1.0)))
    if bolt_d > 0.0:
        top_y = -rail_height / 2.0 + height
        for sx in (-1.0, 1.0):
            for sz in (-1.0, 1.0):
                body = body.cut(Part.makeCylinder(
                    bolt_d / 2.0, height,
                    V(sx * bolt_dx / 2.0, top_y + 1.0, length / 2.0 + sz * bolt_dz / 2.0),
                    FreeCAD.Vector(0, -1, 0)))
    if len(body.Solids) != 1:
        raise ValueError(
            f"this carriage came out as {len(body.Solids)} solids; the bolt holes or the rail "
            "channel have cut it in two")
    top_y = -rail_height / 2.0 + height
    return body, {
        # The frame that goes onto the rail's `axis`: the rail's own centreline as seen from
        # this block, so an `align` drops the carriage's channel over the rail.
        "rail": frame((0, 0, 0), (0, 0, 1)),
        "top": frame((0, top_y, length / 2.0), (0, 1, 0)),
        "centre": frame((0, top_y, length / 2.0), (0, 0, 1)),
        "end": frame((0, 0, length), (0, 0, 1)),
        "end_near": frame((0, 0, 0), (0, 0, -1)),
    }

PARTS = {
    "plate": plate,
    "spacer": spacer,
    "block_with_bore": block_with_bore,
    "shaft": shaft,
    "spur_gear": spur_gear,
    "gear_rack": gear_rack,
    "extrusion_2020": extrusion_2020,
    "c_beam": c_beam,
    "profile_rail": profile_rail,
    "rail_carriage": rail_carriage,
    "ball_bearing": ball_bearing,
    "screw": screw,
    "ball_screw": ball_screw,
    "ball_nut": ball_nut,
    # freeform: the shapes a prismatic catalogue cannot express
    "loft": loft,
    "sweep": sweep,
    "revolve": revolve,
}


def defaults_for(name: str) -> dict:
    """{param: default} straight off the generator signature — the single defaults table.

    The planner tags anything it takes from here as provenance `default`, which is what makes a
    guess visible in the UI instead of indistinguishable from a decision.
    """
    if name not in PARTS:
        raise KeyError(f"no generator named {name!r}; have {', '.join(sorted(PARTS))}")
    out = {}
    for pname, p in inspect.signature(PARTS[name]).parameters.items():
        if p.default is inspect.Parameter.empty:
            raise TypeError(
                f"generator {name!r} parameter {pname!r} has no default; every parameter needs "
                "one so the defaults table stays complete")
        out[pname] = p.default
    return out


def build(name: str, **params):
    """Build a part by generator name. Returns (shape, frames)."""
    if name not in PARTS:
        raise KeyError(f"no generator named {name!r}; have {', '.join(sorted(PARTS))}")
    return PARTS[name](**params)
