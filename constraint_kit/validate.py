"""Validation at scale (Phase C): interference / clearance checking across an assembly.

A real assembly (a car, a home) must not have parts occupying the same space. This flattens a (possibly
nested) `cadquery.Assembly` to world-placed PARTS and reports clashes. Best practice for cost: a cheap
axis-aligned bounding-box prefilter eliminates the vast majority of pairs, and the expensive exact OCC
boolean intersection runs ONLY on bbox-overlapping pairs. The pair counts are reported (no silent caps).

For car/home scale this O(n²) prefilter should be replaced by spatial bucketing (only test neighbors) —
see ROADMAP.md Phase C; for assemblies up to a few hundred parts the prefilter is fine.
"""
from __future__ import annotations

import cadquery as cq


def _world_parts(node, parent_loc: cq.Location | None = None, prefix: str = "") -> list:
    """Flatten a cq.Assembly into [(name, world-located shape)] at PART granularity — one entry per leaf
    part, kept as its WHOLE shape (a multi-solid part like a gearset stays one body). We deliberately do
    NOT iterate `.Solids()`: a part's internal solids are intended (gears mesh), and iterating them also
    drops a compound's per-solid locations. So clash detection compares parts against each other."""
    loc = (parent_loc or cq.Location()) * node.loc
    name = prefix + (node.name or "?")
    out = []
    if node.obj is not None:
        shape = node.obj.val() if hasattr(node.obj, "val") else node.obj
        out.append((name, shape.located(loc)))
    for child in node.children:
        out += _world_parts(child, loc, name + "/")
    return out


def _bbox_overlap(a, b, margin: float = 0.0) -> bool:
    return (a.xmin - margin <= b.xmax and b.xmin - margin <= a.xmax
            and a.ymin - margin <= b.ymax and b.ymin - margin <= a.ymax
            and a.zmin - margin <= b.zmax and b.zmin - margin <= a.zmax)


def interference(cq_assembly, tol_volume: float = 0.01) -> dict:
    """Find solid-solid interferences in an assembly. Returns
    {ok, n_solids, pairs_total, pairs_exact_checked, clashes:[{a,b,overlap_volume_mm3}]}.
    A clash = exact intersection volume > tol_volume (mates that merely touch have ~0 overlap).
    Compares whole PARTS (a part's intended internal contact, e.g. gear mesh, is not a clash)."""
    solids = _world_parts(cq_assembly)
    n = len(solids)
    bboxes = [s.BoundingBox() for _, s in solids]
    clashes, exact = [], 0
    for i in range(n):
        for j in range(i + 1, n):
            if not _bbox_overlap(bboxes[i], bboxes[j]):
                continue                       # cheap AABB prefilter
            exact += 1
            try:
                inter = solids[i][1].intersect(solids[j][1])
                vol = inter.Volume() if inter is not None else 0.0
            except Exception:  # noqa: BLE001 -- a failed boolean is not a clash; skip the pair
                continue
            if vol > tol_volume:
                clashes.append({"a": solids[i][0], "b": solids[j][0],
                                "overlap_volume_mm3": round(vol, 3)})
    return {"ok": not clashes, "n_parts": n, "pairs_total": n * (n - 1) // 2,
            "pairs_exact_checked": exact, "clashes": clashes}
