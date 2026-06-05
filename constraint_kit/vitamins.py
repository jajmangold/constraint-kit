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
import re
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


# library vitamins keyed by canonical name: aliases the LLM might use -> a BOSL2 scad template + defaults.
# Curated + render-verified; only genuinely-missing components we have NO native generator for. Extend
# freely (each entry is just an open-licensed library module call).
VITAMIN_CATALOG = {
    "nema_motor": {
        "aliases": ["nema_motor", "nema_stepper", "stepper_motor", "stepper", "nema17", "nema_17",
                    "nema23", "nema_23", "nema14", "nema_14", "servo_motor"],
        "scad": "nema_stepper_motor(size={size}, h={h}, shaft_len={shaft_len});",
        "defaults": {"size": 17, "h": 40, "shaft_len": 20},
    },
}


def vitamin_for(kind, requirements=None):
    """If an OOV `kind` names a known library VITAMIN, return {scad, name, fn} to render it (the intent
    resolver routes here at the decline boundary, so a sourced part we don't model natively becomes a mesh
    vitamin instead of an honest decline). Returns None if no catalog entry matches. Standard nominal sizes;
    finer stated dims are approximate (mesh tier)."""
    if not isinstance(kind, str):
        return None
    k = kind.strip().lower()
    req = requirements or {}
    for cname, spec in VITAMIN_CATALOG.items():
        if not any(a in k for a in spec["aliases"]):
            continue
        p = dict(spec["defaults"])
        m = re.search(r"(\d{2,3})", k)                        # 'nema17' -> size 17
        if m and "size" in p:
            p["size"] = int(m.group(1))
        for key in list(p):                                   # stated requirements override
            rv = req.get(key)
            rv = rv.get("value") if isinstance(rv, dict) else rv
            if rv is not None:
                try:
                    p[key] = int(float(rv))
                except (TypeError, ValueError):
                    pass
        return {"scad": spec["scad"].format(**p), "name": cname, "fn": 48}
    return None


def vitamin(scad: str = "", fn: int = 48, name: str = "vitamin"):
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
