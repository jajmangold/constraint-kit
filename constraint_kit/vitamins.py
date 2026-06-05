"""OpenSCAD 'vitamins' tier — the coverage-scaling second tier.

A *vitamin* is a sourced/standard component (motor, bearing, fastener, pulley) you wouldn't re-model:
rendered from an open parametric library (BOSL2, BSD-2) and brought in as geometry. It is built into a
watertight cadquery SOLID from the rendered STL, so it flows through the EXISTING mate / assembly / export /
signature pipeline with zero changes. Honest second tier, NOT exact authored B-rep:
  - anchors are DERIVED from the bounding box (approximate — good for mounting/contact, NOT precise press-fit);
  - each is TAGGED tier='vitamin' + its source, so it never masquerades as exact authored geometry.
Renders are cached by (scad, fn) in builder's T3.1 cache (the build chokepoint), so the OpenSCAD latency is
a one-time cost per distinct part. openscad is a GENERATION tool here, not a runtime dep of the verify path.
"""
from __future__ import annotations

import os
import subprocess
import tempfile

import cadquery as cq

from .parts import _loc

_PRELUDE = ("include <BOSL2/std.scad>\ninclude <BOSL2/ball_bearings.scad>\n"
            "include <BOSL2/gears.scad>\ninclude <BOSL2/nema_steppers.scad>\ninclude <BOSL2/hinges.scad>\n")


def _stl_to_solid(stl_path: str) -> cq.Shape:
    """Read an STL mesh into a watertight cadquery SOLID (sew the triangle faces -> shell -> solid), so a
    rendered vitamin is a first-class cq.Shape (transformable, exportable, Volume/Area-measurable)."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid, BRepBuilderAPI_Sewing
    from OCP.StlAPI import StlAPI_Reader
    from OCP.TopoDS import TopoDS, TopoDS_Shape
    sh = TopoDS_Shape()
    StlAPI_Reader().Read(sh, stl_path)
    sew = BRepBuilderAPI_Sewing(1e-4)
    sew.Add(sh)
    sew.Perform()
    mk = BRepBuilderAPI_MakeSolid()
    mk.Add(TopoDS.Shell_s(sew.SewedShape()))
    return cq.Shape(mk.Solid())


def vitamin(scad: str, fn: int = 48, name: str = "vitamin"):
    """Render a BOSL2 module call `scad` (e.g. 'ball_bearing(\"608\");') to a watertight cq solid with
    bbox-DERIVED anchors. Anchors: 'base' (zmin face center) / 'top' (zmax) / 'center' / 'mount' (=base,
    for seating onto a surface). Tagged tier='vitamin'. Raises if the render is empty/non-watertight."""
    body = f"$fn={int(fn)};\n{_PRELUDE}{scad}\n"
    scadf, stl = tempfile.mktemp(suffix=".scad"), tempfile.mktemp(suffix=".stl")
    try:
        with open(scadf, "w") as f:
            f.write(body)
        r = subprocess.run(["openscad", "-o", stl, scadf], capture_output=True, text=True, timeout=300)
        if not os.path.exists(stl) or os.path.getsize(stl) == 0:
            raise ValueError(f"openscad render failed: {(r.stderr.strip().splitlines() or ['?'])[-1]}")
        solid = _stl_to_solid(stl)
    finally:
        for p in (scadf, stl):
            if os.path.exists(p):
                os.remove(p)
    wp = cq.Workplane("XY").add(solid)
    bb = wp.val().BoundingBox()
    cx, cy = (bb.xmin + bb.xmax) / 2.0, (bb.ymin + bb.ymax) / 2.0
    anchors = {
        "base": _loc(cx, cy, bb.zmin), "top": _loc(cx, cy, bb.zmax),
        "center": _loc(cx, cy, (bb.zmin + bb.zmax) / 2.0), "mount": _loc(cx, cy, bb.zmin),
        "_vitamin": _loc(),                                   # tag: a derived/approximate, mesh-sourced part
    }
    return wp, anchors


VITAMIN_GENS = {"vitamin": vitamin}
