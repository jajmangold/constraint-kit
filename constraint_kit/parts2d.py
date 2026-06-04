"""2D footprint generators for the layout solver (Phase 1).

Each generator returns {"entities", "anchors", "bbox"} in the part's LOCAL frame:
  - entities : list of primitive dicts, drawn to DXF/preview. Two kinds:
        {"type":"polyline","points":[(x,y),...],"closed":bool}
        {"type":"circle","center":(x,y),"r":float}
  - anchors  : dict[str,(x,y)] of NAMED 2D mate points (center, edge midpoints, corners) -- the
               planar analog of the 3D anchor frames in parts.py.
  - bbox     : (minx,miny,maxx,maxy).

This is the 2D reduction the README calls the cheapest first cut: DXF out -> laser/CNC. The mate kernel
(layout.py) is the same deterministic "land B's anchor on A's anchor" logic, with z dropped.
"""
from __future__ import annotations


def _rect_anchors(w: float, h: float) -> dict:
    hw, hh = w / 2.0, h / 2.0
    return {
        "center": (0.0, 0.0),
        "left": (-hw, 0.0), "right": (hw, 0.0), "top": (0.0, hh), "bottom": (0.0, -hh),
        "tl": (-hw, hh), "tr": (hw, hh), "bl": (-hw, -hh), "br": (hw, -hh),
    }


def rect_panel(width: float = 80.0, height: float = 50.0,
               hole_d: float = 0.0, hole_inset: float = 8.0):
    """Rectangular panel centered on origin, optional 4 corner holes."""
    hw, hh = width / 2.0, height / 2.0
    ents = [{"type": "polyline",
             "points": [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)], "closed": True}]
    if hole_d and hole_d > 0:
        r = hole_d / 2.0
        for sx in (-1, 1):
            for sy in (-1, 1):
                ents.append({"type": "circle",
                             "center": (sx * (hw - hole_inset), sy * (hh - hole_inset)), "r": r})
    return {"entities": ents, "anchors": _rect_anchors(width, height),
            "bbox": (-hw, -hh, hw, hh)}


def disc(diameter: float = 40.0, bore_d: float = 0.0):
    """A disc, optional center bore. Edge anchors on the rim."""
    r = diameter / 2.0
    ents = [{"type": "circle", "center": (0.0, 0.0), "r": r}]
    if bore_d and bore_d > 0:
        ents.append({"type": "circle", "center": (0.0, 0.0), "r": bore_d / 2.0})
    anchors = {"center": (0.0, 0.0), "left": (-r, 0.0), "right": (r, 0.0),
               "top": (0.0, r), "bottom": (0.0, -r)}
    return {"entities": ents, "anchors": anchors, "bbox": (-r, -r, r, r)}


def bracket(arm: float = 50.0, width: float = 16.0):
    """An L-bracket footprint: two arms of length `arm`, thickness `width`, sharing the inner corner
    at the origin. Anchors at the corner and the two arm ends."""
    a, w = arm, width
    pts = [(0, 0), (a, 0), (a, w), (w, w), (w, a), (0, a)]
    ents = [{"type": "polyline", "points": pts, "closed": True}]
    anchors = {
        "corner": (0.0, 0.0),
        "x_end": (a, w / 2.0), "y_end": (w / 2.0, a),
        "center": (w / 2.0, w / 2.0),
    }
    return {"entities": ents, "anchors": anchors, "bbox": (0.0, 0.0, a, a)}


PART2D_GENS = {
    "rect_panel": rect_panel,
    "disc": disc,
    "bracket": bracket,
}
