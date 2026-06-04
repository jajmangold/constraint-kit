"""Design-rule checks (E9) — automatable engineering rules fed by geometry + ISO 286 / the spec compiler.
These answer not just "does it fit" (interference) but "is it RIGHT": enough thread engagement, an
appropriate fit class for the application, adequate running clearance. Each returns {ok, reason, ...};
`ok` is None when the rule can't judge (unknown application) — honest, never a fake pass."""
from __future__ import annotations

# T9.1 — minimum thread engagement as a multiple of nominal diameter, by the mating (tapped) material
# (machinery rule of thumb: steel ~1xD, cast iron ~1.25, brass ~1.5, aluminium ~2x, plastics ~2.5).
_ENGAGE_FACTOR = {"steel": 1.0, "cast_iron": 1.25, "brass": 1.5, "bronze": 1.5,
                  "aluminum": 2.0, "aluminium": 2.0, "plastic": 2.5, "nylon": 2.5}


def fastener_engagement(nominal_d: float, engagement_len: float, mating_material: str = "steel") -> dict:
    f = _ENGAGE_FACTOR.get(mating_material.lower(), 1.0)
    req = f * nominal_d
    ok = engagement_len >= req
    return {"ok": ok, "nominal_d": nominal_d, "engagement_len": engagement_len,
            "required_min": round(req, 3), "ratio": round(engagement_len / nominal_d, 2),
            "material": mating_material,
            "reason": f"engagement {engagement_len}mm {'>=' if ok else '<'} {round(req, 2)}mm "
                      f"(= {f}xD for {mating_material})"}


# T9.2 — recommended ISO 286 fits per application (hole-basis unless a bare shaft tolerance is given).
_RECOMMENDED_FITS = {
    "running_clearance": {"H7/g6", "H8/f7", "H9/d9", "H7/f7"},
    "locational_clearance": {"H7/h6", "H7/h7", "H8/h7"},
    "locational_transition": {"H7/k6", "H7/js6", "H7/n6"},
    "press_interference": {"H7/p6", "H7/s6", "H7/r6", "H7/u6"},
    "bearing_on_shaft": {"j5", "j6", "k5", "k6", "m5", "m6", "js6"},   # shaft tol for a bearing inner race
    "bearing_in_housing": {"H6", "H7", "J7", "K7", "M7"},               # housing bore tol for outer race
}


def fit_appropriateness(application: str, fit_class: str) -> dict:
    rec = _RECOMMENDED_FITS.get(application)
    if rec is None:
        return {"ok": None, "application": application,
                "reason": f"unknown application {application!r}; known: {sorted(_RECOMMENDED_FITS)}"}
    norm = fit_class.replace(" ", "")
    ok = norm in rec
    return {"ok": ok, "application": application, "fit": fit_class, "recommended": sorted(rec),
            "reason": f"{fit_class} {'is' if ok else 'is NOT'} a recommended fit for {application}"}


def min_clearance(cq_assembly, required: float) -> dict:
    """Exact minimum gap between part pairs (OCC BRepExtrema), bbox-margin prefiltered; flag pairs closer
    than `required` mm. NB intended-contact pairs (gear mesh, seated parts) read as gap 0 — apply this to
    parts that should NOT touch. O(n^2) margin prefilter (fine at this scale; spatial bucketing later)."""
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape

    from .validate import _bbox_overlap, _world_parts
    parts = _world_parts(cq_assembly)
    bb = [s.BoundingBox() for _, s in parts]
    violations: list[dict] = []
    gmin = float("inf")
    for i in range(len(parts)):
        for j in range(i + 1, len(parts)):
            if not _bbox_overlap(bb[i], bb[j], margin=required):
                continue                                  # only pairs that could be within `required`
            dist = BRepExtrema_DistShapeShape(parts[i][1].wrapped, parts[j][1].wrapped)
            gap = dist.Value() if dist.IsDone() else 0.0
            gmin = min(gmin, gap)
            if gap < required:
                violations.append({"a": parts[i][0], "b": parts[j][0], "gap_mm": round(gap, 3)})
    return {"ok": not violations, "required_mm": required, "n_parts": len(parts),
            "min_gap_mm": (round(gmin, 3) if gmin != float("inf") else None), "violations": violations}
