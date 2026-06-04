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


# T4.3 — material strength: representative handbook yield strength (MPa) + Young's modulus E (GPa). These
# vary by alloy/temper/grade — they are engineering defaults for a first-pass check, NOT a certified spec
# (the spec compiler resolves provenance-backed values when an exact grade is named).
MATERIAL_STRENGTH = {
    "steel": {"yield_mpa": 235, "E_gpa": 210}, "structural steel": {"yield_mpa": 235, "E_gpa": 210},
    "carbon steel": {"yield_mpa": 350, "E_gpa": 210}, "stainless": {"yield_mpa": 215, "E_gpa": 193},
    "304 stainless": {"yield_mpa": 215, "E_gpa": 193}, "aluminum": {"yield_mpa": 276, "E_gpa": 69},
    "aluminium": {"yield_mpa": 276, "E_gpa": 69}, "6061": {"yield_mpa": 276, "E_gpa": 69},
    "titanium": {"yield_mpa": 880, "E_gpa": 114}, "ti-6al-4v": {"yield_mpa": 880, "E_gpa": 114},
    "brass": {"yield_mpa": 200, "E_gpa": 100}, "bronze": {"yield_mpa": 140, "E_gpa": 110},
    "cast iron": {"yield_mpa": 130, "E_gpa": 110}, "nylon": {"yield_mpa": 50, "E_gpa": 2.5},
    "abs": {"yield_mpa": 40, "E_gpa": 2.3}, "pla": {"yield_mpa": 50, "E_gpa": 3.5},
    "pom": {"yield_mpa": 65, "E_gpa": 3.0}, "delrin": {"yield_mpa": 65, "E_gpa": 3.0},
    "polycarbonate": {"yield_mpa": 62, "E_gpa": 2.4},
}

# ISO 898-1 proof strength Sp (MPa) by bolt property class, and ISO metric coarse tensile stress area (mm^2)
_BOLT_PROOF_MPA = {"4.6": 225, "4.8": 310, "5.8": 380, "8.8": 600, "9.8": 650, "10.9": 830, "12.9": 970}
_TENSILE_AREA_MM2 = {"M3": 5.03, "M4": 8.78, "M5": 14.2, "M6": 20.1, "M8": 36.6, "M10": 58.0,
                     "M12": 84.3, "M16": 157.0, "M20": 245.0}


def beam_bending(material: str, length_mm: float, width_mm: float, height_mm: float, load_n: float,
                 safety_factor: float = 2.0) -> dict:
    """T4.3: cantilever beam (rectangular section) with an end point load — closed-form max bending stress
    and tip deflection, checked against the material yield over a safety factor. σ_max = 6PL/(b·h²) at the
    fixed end; δ = PL³/(3EI), I = b·h³/12. Honest `ok:None` if the material's strength isn't known."""
    mat = MATERIAL_STRENGTH.get(material.lower())
    if mat is None:
        return {"ok": None, "material": material,
                "reason": f"unknown material {material!r}; known: {sorted(MATERIAL_STRENGTH)}"}
    if min(width_mm, height_mm, length_mm) <= 0:
        return {"ok": None, "reason": "length/width/height must be positive"}
    I = width_mm * height_mm ** 3 / 12.0                      # mm^4
    sigma = 6.0 * load_n * length_mm / (width_mm * height_mm ** 2)   # N/mm^2 = MPa
    E_mpa = mat["E_gpa"] * 1000.0
    deflection = load_n * length_mm ** 3 / (3.0 * E_mpa * I)   # mm
    allowable = mat["yield_mpa"] / safety_factor
    ok = sigma <= allowable
    return {"ok": ok, "material": material, "max_stress_mpa": round(sigma, 3),
            "yield_mpa": mat["yield_mpa"], "allowable_mpa": round(allowable, 3),
            "safety_factor": safety_factor, "actual_safety_factor": round(mat["yield_mpa"] / sigma, 2)
            if sigma else None, "deflection_mm": round(deflection, 4),
            "reason": f"bending stress {round(sigma, 1)}MPa {'<=' if ok else '>'} allowable "
                      f"{round(allowable, 1)}MPa (yield {mat['yield_mpa']}/SF {safety_factor})"}


def bolt_preload(size: str, prop_class: str = "8.8", applied_load_n: float = 0.0,
                 preload_fraction: float = 0.75) -> dict:
    """T4.3: bolt proof load and recommended preload from ISO 898-1 proof strength × tensile stress area.
    proof_load = Sp·As; recommended preload = fraction·proof_load (0.75 typical for reusable joints). If an
    applied tensile load is given, `ok` = it stays below the proof load (the bolt won't yield). Honest
    `ok:None` for an unknown size/class."""
    size_n = size.upper().split("-")[0].split("X")[0].strip()   # "M8-1.25"/"M8x1.25" -> "M8"
    As = _TENSILE_AREA_MM2.get(size_n)
    Sp = _BOLT_PROOF_MPA.get(prop_class)
    if As is None or Sp is None:
        return {"ok": None, "size": size, "prop_class": prop_class,
                "reason": f"unknown size/class (sizes {sorted(_TENSILE_AREA_MM2)}, "
                          f"classes {sorted(_BOLT_PROOF_MPA)})"}
    proof_load = Sp * As                                       # N
    preload = preload_fraction * proof_load
    ok = applied_load_n < proof_load if applied_load_n else True
    return {"ok": ok, "size": size_n, "prop_class": prop_class, "tensile_area_mm2": As,
            "proof_strength_mpa": Sp, "proof_load_n": round(proof_load, 1),
            "recommended_preload_n": round(preload, 1), "applied_load_n": applied_load_n,
            "reason": (f"proof load {round(proof_load)}N (Sp {Sp}MPa × As {As}mm²); "
                       f"recommended preload {round(preload)}N at {preload_fraction:g}×"
                       + (f"; applied {applied_load_n}N {'<' if ok else '>='} proof" if applied_load_n
                          else ""))}


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
