# -*- coding: utf-8 -*-
"""Checks that return structured verdicts, and know when they have gone stale.

Every check here returns a dict, never a bare bool and never a formatted string: the UI renders
it, an agent reasons over it, and the repair layer attributes causes from it. A check that can
only print cannot be built on.

**Stale is a first-class state.** Each result carries a `fingerprint` of the geometry it was
computed against, so a stored green verdict cannot outlive the edit that invalidated it. This
project has been bitten by exactly that: six renders of a redesigned part sat at the top of a
README for weeks, and nothing in the toolchain could tell they no longer described the model.
Timestamps cannot answer it (git does not preserve mtimes, so a fresh clone is all one age) and
hashing the .FCStd cannot either (saving changes the bytes whether or not anything moved). So the
fingerprint hashes what a result actually depends on: each part's name, its volume to a
thousandth of a mm3, and its placement.

Placement is in there deliberately. Moving a part changes interference without changing any
volume, so a volume-only fingerprint would call a moved assembly unchanged.
"""
from __future__ import annotations

import hashlib
import itertools

BBOX_MARGIN = 1e-6

#: Volumes below this are numerical noise from a boolean on touching faces, not a clash. Two
#: seated faces share a surface; OCC reports a sliver, and calling that an interference would
#: make every correct assembly look broken.
CLASH_TOL_MM3 = 1e-3


def fingerprint(doc) -> str:
    """A short hash of the geometry every check depends on."""
    h = hashlib.sha256()
    for obj in doc.Objects:
        shape = getattr(obj, "Shape", None)
        if shape is None or shape.isNull():
            continue
        pl = obj.Placement
        rot = pl.Rotation
        h.update(obj.Name.encode("utf-8"))
        h.update(f"{shape.Volume:.3f}".encode())
        h.update(f"{pl.Base.x:.6f},{pl.Base.y:.6f},{pl.Base.z:.6f}".encode())
        h.update(f"{rot.Axis.x:.6f},{rot.Axis.y:.6f},{rot.Axis.z:.6f},{rot.Angle:.9f}".encode())
    return h.hexdigest()[:16]


def is_stale(result: dict, doc) -> bool:
    """Has the model moved since this result was computed?

    A result with no fingerprint counts as stale: unknown provenance is not the same as valid,
    and defaulting the other way would let a hand-built dict pose as a fresh check.
    """
    return result.get("fingerprint") != fingerprint(doc)


def _stamp(doc, payload: dict) -> dict:
    payload["fingerprint"] = fingerprint(doc)
    return payload


def _doc_of(objs):
    for obj in objs:
        if getattr(obj, "Document", None) is not None:
            return obj.Document
    raise ValueError("cannot check an empty set of parts")


def _bbox_overlap(a, b, margin: float = BBOX_MARGIN) -> bool:
    return (a.XMin - margin <= b.XMax and b.XMin - margin <= a.XMax
            and a.YMin - margin <= b.YMax and b.YMin - margin <= a.YMax
            and a.ZMin - margin <= b.ZMax and b.ZMin - margin <= a.ZMax)


def interference(objs: list, tol_mm3: float = CLASH_TOL_MM3) -> dict:
    """Exact clash detection, bounding-box prefiltered.

    Returns `{ok, n_parts, pairs_total, pairs_exact_checked, clashes: [...], fingerprint}`.
    `pairs_exact_checked` is reported rather than hidden so the cost of a run is visible and the
    prefilter can be shown to be doing something.

    Boxes are a superset of real overlaps, so prefiltering never misses a clash -- but note the
    boxes come from `Shape.BoundBox`, which bounds a spline's control polygon rather than its
    surface. That direction of error is safe here (it only ever sends an extra pair to the exact
    boolean) and would not be safe for measuring anything.
    """
    objs = [o for o in objs if getattr(o, "Shape", None) is not None and not o.Shape.isNull()]
    doc = _doc_of(objs)
    shapes = [(o.Name, o.Shape) for o in objs]
    boxes = [s.BoundBox for _, s in shapes]

    clashes, exact = [], 0
    for i, j in itertools.combinations(range(len(shapes)), 2):
        if not _bbox_overlap(boxes[i], boxes[j]):
            continue
        exact += 1
        common = shapes[i][1].common(shapes[j][1])
        vol = common.Volume if not common.isNull() else 0.0
        if vol > tol_mm3:
            clashes.append({"a": shapes[i][0], "b": shapes[j][0],
                            "volume_mm3": round(vol, 4)})

    clashes.sort(key=lambda c: -c["volume_mm3"])
    return _stamp(doc, {
        "check": "interference",
        "ok": not clashes,
        "n_parts": len(shapes),
        "pairs_total": len(shapes) * (len(shapes) - 1) // 2,
        "pairs_exact_checked": exact,
        "clashes": clashes,
    })


def seating(objs: list, max_gap: float = 1e-6) -> dict:
    """Shortest distance between every bounding-box-adjacent pair.

    A mate that was solved by the kernel comes out at exactly zero. A pair that should be seated
    and is not shows up here as a gap, which is the symptom of a mate that was never declared.
    """
    objs = [o for o in objs if getattr(o, "Shape", None) is not None and not o.Shape.isNull()]
    doc = _doc_of(objs)
    pairs = []
    for a, b in itertools.combinations(objs, 2):
        if not _bbox_overlap(a.Shape.BoundBox, b.Shape.BoundBox, margin=1.0):
            continue
        gap = float(a.Shape.distToShape(b.Shape)[0])
        pairs.append({"a": a.Name, "b": b.Name, "gap_mm": round(gap, 6),
                      "seated": gap <= max_gap})
    return _stamp(doc, {"check": "seating", "ok": True, "pairs": pairs,
                        "n_seated": sum(1 for p in pairs if p["seated"])})


def mass(objs: list, density_g_cm3: float = 1.27) -> dict:
    """Volume and mass roll-up. Density defaults to PETG, the material these parts get printed in."""
    objs = [o for o in objs if getattr(o, "Shape", None) is not None and not o.Shape.isNull()]
    doc = _doc_of(objs)
    if density_g_cm3 <= 0:
        raise ValueError(f"density must be positive, got {density_g_cm3}")
    per = []
    total_mm3 = 0.0
    for o in objs:
        v = float(o.Shape.Volume)
        total_mm3 += v
        per.append({"name": o.Name, "volume_cm3": round(v / 1000.0, 6),
                    "mass_g": round(v / 1000.0 * density_g_cm3, 4)})
    per.sort(key=lambda e: -e["mass_g"])
    return _stamp(doc, {
        "check": "mass",
        "ok": True,
        "density_g_cm3": density_g_cm3,
        "volume_cm3": round(total_mm3 / 1000.0, 6),
        "mass_g": round(total_mm3 / 1000.0 * density_g_cm3, 4),
        "parts": per,
    })


def solids(objs: list) -> dict:
    """Every GENERATED part must be exactly one valid, closed solid.

    Worth its own check because a boolean that severs a part returns a perfectly valid shape with
    two solids in it rather than an error -- two fragmentation bugs in this project history got
    several steps downstream before anyone noticed.

    Imported geometry is held to validity and closedness but NOT to the one-solid rule: a vendor
    assembly legitimately contains many solids, and reporting that as broken would train people
    to ignore this check. Those are listed under `multi_solid_imports` as information instead.
    """
    from .document import is_imported

    objs = [o for o in objs if getattr(o, "Shape", None) is not None and not o.Shape.isNull()]
    doc = _doc_of(objs)
    bad, multi_imports = [], []
    for o in objs:
        s = o.Shape
        n = len(s.Solids)
        imported = is_imported(o)
        if imported and n > 1:
            multi_imports.append(o.Name)
        broken = (not s.isValid()) or (not s.isClosed()) or n < 1 or (n != 1 and not imported)
        if broken:
            bad.append({"name": o.Name, "solids": n, "imported": imported,
                        "valid": bool(s.isValid()), "closed": bool(s.isClosed())})
    return _stamp(doc, {"check": "solids", "ok": not bad, "n_parts": len(objs), "bad": bad,
                        "multi_solid_imports": multi_imports})


#: Checks a "Run all" invokes, in the order a human wants to read them.
ALL = ("solids", "interference", "seating", "mass")
_BY_NAME = {"solids": solids, "interference": interference, "seating": seating, "mass": mass}


def run_all(objs: list, names: tuple = ALL) -> dict:
    """Run several checks and summarise. Never raises for a failing check; raises for a bad call."""
    results = {}
    for name in names:
        if name not in _BY_NAME:
            raise KeyError(f"no check named {name!r}; have {sorted(_BY_NAME)}")
        results[name] = _BY_NAME[name](objs)
    return {"ok": all(r.get("ok", False) for r in results.values()), "checks": results}
