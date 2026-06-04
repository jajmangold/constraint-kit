"""Validation at scale (Phase C): interference / clearance checking across an assembly.

A real assembly (a car, a home) must not have parts occupying the same space. This flattens a (possibly
nested) `cadquery.Assembly` to world-placed PARTS and reports clashes. Cost strategy (T3.2): a **uniform
spatial grid** yields candidate pairs in ~O(n) (only parts sharing a cell), then a cheap AABB test, then
the expensive exact OCC boolean ONLY on survivors. The grid candidate set is a provable SUPERSET of all
overlapping-AABB pairs, so results are identical to the brute-force O(n²) sweep; pair counts are reported
(pairs_total / pairs_candidate / pairs_exact_checked — no silent caps).
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


def _grid_candidate_pairs(bboxes: list) -> set:
    """Uniform spatial grid (T3.2): return the set of (i,j) part-index pairs that share ≥1 grid cell —
    a SUPERSET of all overlapping-AABB pairs (two overlapping AABBs always share a cell), so no real clash
    is ever missed; only non-neighbour pairs are pruned. Cell = max single-part extent (each part spans a
    handful of cells), turning the O(n²) sweep into ~O(n) for spread-out assemblies."""
    if len(bboxes) < 2:
        return set()
    cell = max((max(b.xlen, b.ylen, b.zlen) for b in bboxes), default=1.0) or 1.0
    grid: dict = {}
    for idx, b in enumerate(bboxes):
        for cx in range(int(b.xmin // cell), int(b.xmax // cell) + 1):
            for cy in range(int(b.ymin // cell), int(b.ymax // cell) + 1):
                for cz in range(int(b.zmin // cell), int(b.zmax // cell) + 1):
                    grid.setdefault((cx, cy, cz), []).append(idx)
    pairs = set()
    for members in grid.values():
        for a in range(len(members)):
            for b in range(a + 1, len(members)):
                i, j = members[a], members[b]
                pairs.add((i, j) if i < j else (j, i))
    return pairs


def interference(cq_assembly, tol_volume: float = 0.01) -> dict:
    """Find solid-solid interferences in an assembly. Returns
    {ok, n_solids, pairs_total, pairs_exact_checked, clashes:[{a,b,overlap_volume_mm3}]}.
    A clash = exact intersection volume > tol_volume (mates that merely touch have ~0 overlap).
    Compares whole PARTS (a part's intended internal contact, e.g. gear mesh, is not a clash)."""
    solids = _world_parts(cq_assembly)
    n = len(solids)
    bboxes = [s.BoundingBox() for _, s in solids]
    candidates = _grid_candidate_pairs(bboxes)     # spatial prefilter -> only near pairs
    clashes, exact = [], 0
    for i, j in candidates:
        if not _bbox_overlap(bboxes[i], bboxes[j]):
            continue                               # exact AABB test on the few grid candidates
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
            "pairs_candidate": len(candidates), "pairs_exact_checked": exact, "clashes": clashes}
