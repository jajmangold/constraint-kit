"""build123d / bd_warehouse part generators — real catalog mechanical parts.

These deliver the original constraint-kit vision (aluminum extrusions, plumbing, standard fasteners,
bearings) via bd_warehouse, which co-resolves with cadquery on the SAME cadquery-ocp (no conflict, no
second image). build123d objects are converted to a cadquery.Workplane via the shared TopoDS, so they
flow through the existing builder/export/mass/atlas/test pipeline unchanged — same (Workplane, anchors)
contract as parts.py.

bd_warehouse is imported lazily inside each generator: service start stays fast and a failure in one
catalog part can't take down the others (mirrors the cq_gears fallback pattern).
"""
from __future__ import annotations

import cadquery as cq

from .joints import frame, joints_from_b123d
from .parts import _loc


def _to_cq(bd_part) -> cq.Workplane:
    """Wrap a build123d part's TopoDS shape as a cadquery Workplane (same OCP kernel, zero-copy)."""
    return cq.Workplane("XY").add(cq.Shape.cast(bd_part.wrapped))


def extrusion(rail_size: str = "20x20", length: float = 100.0, slot_z: float | None = None):
    """Aluminum V-slot / T-slot extrusion (bd_warehouse open_builds), profile centered on the origin,
    extruded along +Z. rail_size in {'20x20','20x40','20x60','20x80','40x40'}.

    joints: 'base'(z=0), 'top'(z=length), 'center'; and oriented SLOT-MOUNT frames on each side face
    'slot_xp/xn/yp/yn' (+Z = outward face normal, roll pinned to world-up) at height `slot_z`
    (default mid-length) — a part's 'mount' joint coincides with one of these to bolt onto the rail."""
    from bd_warehouse.open_builds import VSlotLinearRail
    wp = _to_cq(VSlotLinearRail(rail_size=rail_size, length=length))
    bb = wp.val().BoundingBox()
    zc = length / 2.0 if slot_z is None else float(slot_z)
    up = (0, 0, 1)  # pin roll so a mounted part stays upright (its +Z -> world +Z)
    return wp, {
        "base": _loc(0, 0, 0), "top": _loc(0, 0, length), "center": _loc(0, 0, length / 2.0),
        "slot_xp": frame((bb.xmax, 0, zc), z_axis=(1, 0, 0), x_axis=up),
        "slot_xn": frame((bb.xmin, 0, zc), z_axis=(-1, 0, 0), x_axis=up),
        "slot_yp": frame((0, bb.ymax, zc), z_axis=(0, 1, 0), x_axis=up),
        "slot_yn": frame((0, bb.ymin, zc), z_axis=(0, -1, 0), x_axis=up),
    }


def ball_bearing(size: str = "M8-22-7", bearing_type: str = "SKT"):
    """A real single-row deep-groove ball bearing (bd_warehouse), axis = Z. size = 'bore-OD-width'
    (e.g. 'M8-22-7' = 608). anchors: 'bore_base' (z=0 face), 'bore_top'."""
    from bd_warehouse.bearing import SingleRowDeepGrooveBallBearing
    wp = _to_cq(SingleRowDeepGrooveBallBearing(size=size, bearing_type=bearing_type))
    bb = wp.val().BoundingBox()
    return wp, {"bore_base": _loc(0, 0, bb.zmin), "bore_top": _loc(0, 0, bb.zmax),
                "bore_mid": _loc(0, 0, (bb.zmin + bb.zmax) / 2.0)}  # axis-aligned center seat


def screw(size: str = "M5-0.8", length: float = 16.0, simple: bool = True,
          fastener_type: str = "iso4762"):
    """An ISO socket-head cap screw (bd_warehouse), head up (z>0), shank down (-Z), under-head at z=0.
    `simple=True` skips the helical thread geometry (fast); set False for real threads.
    anchors: 'seat' (under-head, z=0 -> lands on a plate top/hole), 'tip', 'head_top'."""
    from bd_warehouse.fastener import SocketHeadCapScrew
    wp = _to_cq(SocketHeadCapScrew(size=size, length=length, simple=simple,
                                   fastener_type=fastener_type))
    bb = wp.val().BoundingBox()
    return wp, {"seat": _loc(0, 0, 0), "tip": _loc(0, 0, bb.zmin),
                "head_top": _loc(0, 0, bb.zmax)}


def threaded_rod(major_diameter: float = 6.0, pitch: float = 1.0, length: float = 20.0,
                 hand: str = "right", designation: str | None = None):
    """A REAL externally-threaded rod: a core cylinder fused with a `bd_warehouse.IsoThread` whose helix
    is parameterized by `pitch` — so a spec-compiler-resolved pitch literally drives the geometry. Axis Z,
    z in [0, length]. anchors: 'base','top','axis'. (`designation` is accepted/ignored here; the builder
    injects major_diameter+pitch from it when resolve_specs is set.) ~slow: thread geometry is heavy."""
    from build123d import Align, Cylinder
    from bd_warehouse.thread import IsoThread
    it = IsoThread(major_diameter=major_diameter, pitch=pitch, length=length, external=True,
                   hand=hand, end_finishes=("fade", "fade"))
    core = Cylinder(radius=it.min_radius, height=length, align=(Align.CENTER, Align.CENTER, Align.MIN))
    wp = _to_cq(core + it)
    return wp, {"base": _loc(0, 0, 0), "top": _loc(0, 0, length), "axis": _loc(0, 0, length / 2.0)}


def bearing_block(width: float = 40.0, depth: float = 20.0, height: float = 40.0,
                  bore_d: float = 22.0, bolt_d: float = 5.0, bolt_spacing: float = 28.0):
    """A pillow-style bearing block (build123d, with first-class RigidJoints). Body spans x∈[-W/2,W/2],
    y∈[0,depth] (the BACK face y=0 mounts to a rail slot; FRONT face y=depth carries screw heads),
    z∈[0,height]. A vertical bore (Z) through the front portion holds a bearing; two horizontal (Y)
    through-holes carry mounting screws.

    Joints (oriented, authored — names stable across params):
      'mount'  back face center, +Z = outward (−body) normal, roll pinned up → coincides with a rail slot
      'top'    front face center (where screw heads seat)
      'bore'   bore center, +Z = vertical bore axis → a bearing's 'bore_mid' aligns to it
      'bolt0','bolt1'  front-face hole centers, +Z = screw axis (along the through-hole, i.e. into the rail)
    """
    from build123d import (Align, Box, BuildPart, Cylinder, Location, Locations, Mode, Plane,
                           RigidJoint)
    W, T, H = width, depth, height
    with BuildPart() as bp:
        Box(W, T, H, align=(Align.CENTER, Align.MIN, Align.MIN))
        with Locations((0, T / 2.0, 0)):                      # vertical bore (Z) through the block
            Cylinder(bore_d / 2.0, H + 2, mode=Mode.SUBTRACT)
        with Locations(Location((bolt_spacing / 2.0, 0, H / 2.0), (-90, 0, 0)),
                       Location((-bolt_spacing / 2.0, 0, H / 2.0), (-90, 0, 0))):
            Cylinder(bolt_d / 2.0, T + 2, mode=Mode.SUBTRACT)  # Y-axis through screw holes
    p = bp.part
    # roll pinned to world-up (x_dir=+Z) so a mounted block stays upright (vertical bore stays vertical)
    RigidJoint("mount", p, Location(Plane(origin=(0, 0, H / 2.0), x_dir=(0, 0, 1), z_dir=(0, 1, 0))))
    RigidJoint("top", p, Location(Plane(origin=(0, T, H / 2.0), x_dir=(0, 0, 1), z_dir=(0, 1, 0))))
    RigidJoint("bore", p, Location((0, T / 2.0, H / 2.0)))                 # +Z = vertical bore axis
    RigidJoint("bolt0", p, Location((bolt_spacing / 2.0, T, H / 2.0), (-90, 0, 0)))
    RigidJoint("bolt1", p, Location((-bolt_spacing / 2.0, T, H / 2.0), (-90, 0, 0)))
    return _to_cq(p), joints_from_b123d(p)


def sprocket(num_teeth: int = 16, chain_pitch: float = 12.7, thickness: float = 6.0,
             bore_d: float = 8.0):
    """A roller-chain sprocket (bd_warehouse), axis = Z, thickness centered on z=0.
    anchors: 'face_a','face_b','center' (+Z = axis)."""
    from bd_warehouse.sprocket import Sprocket
    wp = _to_cq(Sprocket(num_teeth=int(num_teeth), chain_pitch=chain_pitch,
                         thickness=thickness, bore_diameter=bore_d))
    bb = wp.val().BoundingBox()
    return wp, {"face_a": _loc(0, 0, bb.zmin), "face_b": _loc(0, 0, bb.zmax),
                "center": _loc(0, 0, (bb.zmin + bb.zmax) / 2.0)}


def flange(nps: str = "1", flange_class: int = 150, kind: str = "weld_neck"):
    """An ASME B16.5 pipe flange (bd_warehouse), axis = Z (bore along Z). nps e.g. '1','2','4';
    flange_class in {150,300,...}; kind in {weld_neck, slip_on, blind}.
    anchors: 'bore_base'(z=0 face), 'bore_top'."""
    from bd_warehouse.flange import BlindFlange, SlipOnFlange, WeldNeckFlange
    cls = {"weld_neck": WeldNeckFlange, "slip_on": SlipOnFlange, "blind": BlindFlange}[kind]
    wp = _to_cq(cls(nps=nps, flange_class=flange_class))
    bb = wp.val().BoundingBox()
    return wp, {"bore_base": _loc(0, 0, bb.zmin), "bore_top": _loc(0, 0, bb.zmax)}


def pipe(nps: str = "1", length: float = 100.0, material: str = "steel", identifier: str = "40"):
    """A straight run of standard pipe (bd_warehouse), axis = Z, z in [0, length]. nps e.g. '1','2';
    material/identifier select the schedule (e.g. steel/'40'). anchors: 'end_a','end_b','center'."""
    from build123d import Edge
    from bd_warehouse.pipe import Pipe
    path = Edge.make_line((0, 0, 0), (0, 0, length))
    wp = _to_cq(Pipe(nps=nps, material=material, identifier=identifier, path=path))
    return wp, {"end_a": _loc(0, 0, 0), "end_b": _loc(0, 0, length),
                "center": _loc(0, 0, length / 2.0)}


PART_GENS_BD = {
    "extrusion": extrusion,
    "ball_bearing": ball_bearing,
    "screw": screw,
    "threaded_rod": threaded_rod,
    "bearing_block": bearing_block,
    "sprocket": sprocket,
    "flange": flange,
    "pipe": pipe,
}
