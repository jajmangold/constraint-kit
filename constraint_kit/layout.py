"""Deterministic 2D layout solver + DXF/preview export (Phase 1).

Same kernel as the 3D mates, with z dropped: place each part by landing its named anchor on a target
part's anchor (+ optional [dx,dy] gap), applied in order so chains compose (A->B->C). No optimization.

Spec:
{
  "parts": [ {"id","type","params":{...,"rotate":deg?},"material"?} , ... ],
  "mates": [ {"a","a_joint","b","b_joint","offset":[dx,dy]?} , ... ]   # ordered, all "coincident"
}
"""
from __future__ import annotations

import math

from .parts2d import PART2D_GENS


def _rot(pt, theta):
    c, s = math.cos(theta), math.sin(theta)
    x, y = pt
    return (c * x - s * y, s * x + c * y)


def _xform(pt, place):
    tx, ty, theta = place
    rx, ry = _rot(pt, theta)
    return (rx + tx, ry + ty)


def _pid(ps: dict) -> str:
    pid = ps.get("id") or ps.get("name")
    if not pid:
        raise ValueError(f"part missing id/name: {ps}")
    return pid


def solve(spec: dict) -> dict:
    if not spec.get("parts"):
        raise ValueError("layout spec has no parts")
    parts: dict[str, dict] = {}
    order: list[str] = []
    for ps in spec["parts"]:
        ptype = ps["type"]
        if ptype not in PART2D_GENS:
            raise ValueError(f"unknown 2D part type {ptype!r}; known: {sorted(PART2D_GENS)}")
        params = dict(ps.get("params", {}))
        rotate = math.radians(float(params.pop("rotate", 0.0)))
        fp = PART2D_GENS[ptype](**params)
        pid = _pid(ps)
        parts[pid] = {"fp": fp, "theta": rotate, "type": ptype,
                      "material": ps.get("material", "acrylic")}
        order.append(pid)

    # part 0 fixed at origin (with its own rotation); then apply mates in order.
    places: dict[str, tuple] = {order[0]: (0.0, 0.0, parts[order[0]]["theta"])}
    for m in spec.get("mates", []):
        a, b = m["a"], m["b"]
        if a not in parts or b not in parts:
            raise ValueError(f"mate references unknown part: {m}")
        if a not in places:
            raise ValueError(f"mate uses part {a!r} before it is placed (order matters)")
        a_world = _xform(parts[a]["fp"]["anchors"][m["a_joint"]], places[a])
        b_theta = parts[b]["theta"]
        b_rot = _rot(parts[b]["fp"]["anchors"][m["b_joint"]], b_theta)
        ox, oy = (m.get("offset") or [0.0, 0.0])[:2]
        tx, ty = a_world[0] + ox - b_rot[0], a_world[1] + oy - b_rot[1]
        places[b] = (tx, ty, b_theta)

    # any part never placed by a mate -> leave at origin (flag it)
    for pid in order:
        places.setdefault(pid, (0.0, 0.0, parts[pid]["theta"]))

    sheet = _sheet_bbox(parts, places)
    return {"parts": parts, "places": places, "order": order, "sheet_bbox": sheet}


def _entity_points(ent, place):
    if ent["type"] == "polyline":
        return [_xform(p, place) for p in ent["points"]]
    if ent["type"] == "circle":
        cx, cy = _xform(ent["center"], place)
        r = ent["r"]
        return [(cx - r, cy - r), (cx + r, cy + r)]
    return []


def _sheet_bbox(parts, places):
    xs, ys = [], []
    for pid, p in parts.items():
        for ent in p["fp"]["entities"]:
            for (x, y) in _entity_points(ent, places[pid]):
                xs.append(x); ys.append(y)
    if not xs:
        return [0, 0, 0, 0]
    return [round(min(xs), 2), round(min(ys), 2), round(max(xs), 2), round(max(ys), 2)]


def export_dxf(solved: dict, path: str) -> str:
    import ezdxf
    doc = ezdxf.new(setup=True)
    msp = doc.modelspace()
    for pid in solved["order"]:
        place = solved["places"][pid]
        layer = pid
        doc.layers.add(name=layer) if layer not in doc.layers else None
        for ent in solved["parts"][pid]["fp"]["entities"]:
            if ent["type"] == "polyline":
                pts = [_xform(p, place) for p in ent["points"]]
                msp.add_lwpolyline(pts, close=ent.get("closed", True), dxfattribs={"layer": layer})
            elif ent["type"] == "circle":
                cx, cy = _xform(ent["center"], place)
                msp.add_circle((cx, cy), ent["r"], dxfattribs={"layer": layer})
    doc.saveas(path)
    return path


def export_png(solved: dict, path: str) -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Circle, Polygon

    fig, ax = plt.subplots(figsize=(6, 6))
    colors = plt.cm.tab10.colors
    for i, pid in enumerate(solved["order"]):
        place = solved["places"][pid]
        col = colors[i % len(colors)]
        for ent in solved["parts"][pid]["fp"]["entities"]:
            if ent["type"] == "polyline":
                pts = [_xform(p, place) for p in ent["points"]]
                ax.add_patch(Polygon(pts, closed=ent.get("closed", True),
                                     fill=False, edgecolor=col, linewidth=1.5))
            elif ent["type"] == "circle":
                cx, cy = _xform(ent["center"], place)
                ax.add_patch(Circle((cx, cy), ent["r"], fill=False, edgecolor=col, linewidth=1.2))
        c = _xform(solved["parts"][pid]["fp"]["anchors"].get("center", (0, 0)), place)
        ax.annotate(pid, c, ha="center", va="center", fontsize=8, color=col)
    bb = solved["sheet_bbox"]
    pad = max((bb[2] - bb[0]), (bb[3] - bb[1])) * 0.08 + 5
    ax.set_xlim(bb[0] - pad, bb[2] + pad)
    ax.set_ylim(bb[1] - pad, bb[3] + pad)
    ax.set_aspect("equal")
    ax.grid(True, linestyle=":", alpha=0.4)
    ax.set_title("constraint-kit 2D layout")
    fig.savefig(path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return path


def report(solved: dict) -> dict:
    return {
        "sheet_bbox": solved["sheet_bbox"],
        "sheet_size": [round(solved["sheet_bbox"][2] - solved["sheet_bbox"][0], 2),
                       round(solved["sheet_bbox"][3] - solved["sheet_bbox"][1], 2)],
        "parts": [{"id": pid, "type": solved["parts"][pid]["type"],
                   "material": solved["parts"][pid]["material"],
                   "place": [round(v, 2) for v in solved["places"][pid][:2]],
                   "rotate_deg": round(math.degrees(solved["places"][pid][2]), 1)}
                  for pid in solved["order"]],
    }
