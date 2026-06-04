"""2D drawing export (T7.2) — section + orthographic projection views on the cadquery/OCC stack.

A manufacturing drawing needs more than a 3D model: a cross-SECTION (the exact planar cut, for internal
features and machining) and orthographic PROJECTIONS (front/top/right). This produces both from a built
assembly via the exact OCC kernel: `section` cuts the world compound with an axis-aligned plane and exports
the planar face(s) to DXF (ezdxf-valid); `projection_svg` renders a hidden-line-removed orthographic view
to SVG. Overall bounding dimensions are reported alongside.

Honest scope: this generates the section + projection geometry and the OVERALL dimensions. Full GD&T —
tolerance frames, datums, per-feature leader dimensioning — is NOT generated (a much larger effort, and
faking dimension callouts would mislead); the toleranced VALUES already live in iso286/the spec compiler.
"""
from __future__ import annotations

import cadquery as cq

_PLANES = {"XY", "XZ", "YZ"}
_VIEW_DIRS = {"top": (0, 0, 1), "bottom": (0, 0, -1), "front": (0, -1, 0), "back": (0, 1, 0),
              "right": (1, 0, 0), "left": (-1, 0, 0), "iso": (1, -1, 1)}


def _to_compound(obj):
    if isinstance(obj, cq.Assembly):
        return obj.toCompound()
    if isinstance(obj, cq.Workplane):
        return obj.val()
    return obj


def overall_dimensions(obj) -> dict:
    """Overall bounding-box dimensions (mm) of a built object — the drawing's principal dimensions."""
    bb = _to_compound(obj).BoundingBox()
    return {"width_x": round(bb.xlen, 3), "depth_y": round(bb.ylen, 3), "height_z": round(bb.zlen, 3),
            "min": [round(bb.xmin, 3), round(bb.ymin, 3), round(bb.zmin, 3)],
            "max": [round(bb.xmax, 3), round(bb.ymax, 3), round(bb.zmax, 3)]}


def section_dxf(obj, out_path: str, plane: str = "XZ", height: float = 0.0) -> dict:
    """Cut `obj` with an axis-aligned plane (XY/XZ/YZ, offset `height` along its normal) and export the
    planar cross-section to DXF. Returns section area + face count + bbox + path."""
    if plane not in _PLANES:
        raise ValueError(f"plane must be one of {sorted(_PLANES)} (got {plane!r})")
    sec = cq.Workplane(plane).add(_to_compound(obj)).section(height)
    cq.exporters.export(sec, out_path)
    faces = sec.faces().vals()
    bb = sec.val().BoundingBox()
    return {"path": out_path, "plane": plane, "height": height, "n_faces": len(faces),
            "section_area_mm2": round(sum(f.Area() for f in faces), 3),
            "bbox": {"size": [round(bb.xlen, 3), round(bb.ylen, 3), round(bb.zlen, 3)]}}


def projection_svg(obj, out_path: str, view: str = "front") -> dict:
    """Render an orthographic (hidden-line-removed) projection of `obj` to SVG, viewed along `view`
    (top/bottom/front/back/right/left/iso)."""
    pdir = _VIEW_DIRS.get(view)
    if pdir is None:
        raise ValueError(f"view must be one of {sorted(_VIEW_DIRS)} (got {view!r})")
    cq.exporters.export(_to_compound(obj), out_path, exportType=cq.exporters.ExportTypes.SVG,
                        opt={"projectionDir": pdir, "showAxes": False})
    return {"path": out_path, "view": view, "projection_dir": list(pdir)}


def drawing(obj, out_base: str, plane: str = "XZ", height: float = 0.0,
            views: tuple = ("front", "top", "right")) -> dict:
    """A drawing set: one section DXF (cut on `plane`) + an orthographic SVG per `view` + overall
    dimensions. Honest scope note included: overall dims only, no GD&T tolerance frames."""
    return {
        "dimensions": overall_dimensions(obj),
        "section": section_dxf(obj, f"{out_base}_section.dxf", plane, height),
        "projections": [projection_svg(obj, f"{out_base}_{v}.svg", v) for v in views],
        "scope": "section + orthographic projections + overall dimensions; GD&T tolerance frames / "
                 "per-feature leader dimensioning are NOT generated (toleranced values live in iso286/specs)",
    }
