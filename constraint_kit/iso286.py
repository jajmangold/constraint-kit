"""ISO 286 limits & fits — REAL tolerances computed from the standard formulas, driven by nominal size.

Standard tolerance factor (3 < D <= 500 mm):  i = 0.45·∛D + 0.001·D  (µm), D = geometric mean of the
size step. IT grade value = factor·i. Fundamental deviations from the published ISO 286-1 formulas
(validated against ISO 286-2 tables at Ø20: H7=+21/0, g6=−7/−20, k6=+15/+2, n6=+28/+15, f7=−20/−41).

Honest scope: hole-basis (H) holes + shaft letters f, g, h, js, k, m, n have clean validated power-law
formulas (exact deviations + clearances). Interference letters p, r, s do NOT reduce to a formula, so their
fundamental deviations are TABULATED (INTERFERENCE_EI) — extracted deterministically from ISO 286-2:2010
and cross-validated against the closed-form columns + known fit anchors (T5.1), never fabricated. Letters
t, u, v, … remain class-only until extracted. 3 < D <= 500 mm. All deviations in micrometres (µm).
"""
from __future__ import annotations

import math

STEPS = [(3, 6), (6, 10), (10, 18), (18, 30), (30, 50), (50, 80), (80, 120), (120, 180),
         (180, 250), (250, 315), (315, 400), (400, 500)]
GRADE_FACTOR = {5: 7, 6: 10, 7: 16, 8: 25, 9: 40, 10: 64, 11: 100, 12: 160, 13: 250,
                14: 400, 15: 640, 16: 1000}
# shaft letters with a clean, ISO-table-validated closed-form fundamental deviation. Interference letters
# p..zc have NO closed form -> their fundamental deviation (ei) is TABULATED below from ISO 286-2.
EXACT_SHAFT_LETTERS = {"f", "g", "h", "js", "k", "m", "n"}

# Interference/transition shaft fundamental deviations ei (µm), keyed by (size_lo, size_hi] mm. These are
# NOT closed-form — they were extracted DETERMINISTICALLY from the authoritative ISO 286-2:2010 table via
# pdf_oxide bbox reconstruction (T5.1), then cross-validated: (a) the closed-form columns m/n on the same
# tables reproduce iso286's formula, and (b) every value here matches known ISO fit-table anchors
# (p6@20=+22, r6@20=+28, s6@20=+35, s6@60=+53). The fundamental deviation ei is grade-independent; the
# upper deviation is es = ei + IT(grade). Interference fits subdivide sizes >50 mm finer than the IT steps.
ISO286_2_PROVENANCE = {
    "standard": "ISO 286-2:2010", "extractor": "pdf_oxide bbox reconstruction",
    "validation": "closed-form m/n columns + known fit-table anchors (p6/r6/s6)",
    "scope": "shaft letters p, r, s; hole-basis; class-only fallback outside these letters/ranges",
}
INTERFERENCE_EI = {
    "p": {(6, 10): 15, (10, 18): 18, (18, 30): 22, (30, 50): 26, (50, 80): 32, (80, 120): 37,
          (120, 180): 43, (180, 250): 50, (250, 315): 56, (315, 400): 62, (400, 500): 68},
    "r": {(3, 6): 15, (6, 10): 19, (10, 18): 23, (18, 30): 28, (30, 50): 34, (50, 65): 41, (65, 80): 43,
          (80, 100): 51, (100, 120): 54, (120, 140): 63, (140, 160): 65, (160, 180): 68, (180, 200): 77,
          (200, 225): 80, (225, 250): 84, (250, 280): 94, (280, 315): 98, (315, 355): 108, (355, 400): 114,
          (400, 450): 126, (450, 500): 132},
    "s": {(3, 6): 19, (6, 10): 23, (10, 18): 28, (18, 30): 35, (30, 50): 43, (50, 65): 53, (65, 80): 59,
          (80, 100): 71, (100, 120): 79, (120, 140): 92, (140, 160): 100, (160, 180): 108, (180, 200): 122,
          (200, 225): 130, (225, 250): 140, (250, 280): 158, (280, 315): 170, (315, 355): 190,
          (355, 400): 208, (400, 450): 232, (450, 500): 252},
}


def interference_ei(letter: str, nominal: float):
    """Tabulated shaft fundamental deviation ei (µm) for p/r/s at `nominal`, or None if unsupported
    letter/size (caller falls back to class-only — never fabricated)."""
    tbl = INTERFERENCE_EI.get(letter.lower())
    if not tbl:
        return None
    for (lo, hi), ei in tbl.items():
        if lo < nominal <= hi:
            return ei
    return None


def geom_mean(nominal: float) -> float | None:
    """Geometric mean of the ISO size step containing `nominal` (None if out of 3..500 mm range)."""
    for lo, hi in STEPS:
        if lo < nominal <= hi:
            return math.sqrt(lo * hi)
    return None


def tol_factor(d_mean: float) -> float:
    return 0.45 * d_mean ** (1 / 3) + 0.001 * d_mean  # µm


def it_grade(grade: int, nominal: float) -> int | None:
    d = geom_mean(nominal)
    if d is None or grade not in GRADE_FACTOR:
        return None
    return round(GRADE_FACTOR[grade] * tol_factor(d))


def shaft_deviation(letter: str, grade: int, nominal: float):
    """Return (es, ei) shaft deviations µm: closed-form for f/g/h/js/k/m/n, ISO 286-2-tabulated for the
    interference letters p/r/s (T5.1); None otherwise (caller falls back to class-only)."""
    d = geom_mean(nominal)
    it = it_grade(grade, nominal)
    if d is None or it is None:
        return None
    lo = letter.lower()
    if lo == "h":
        return (0.0, -it)
    if lo == "js":                                   # symmetric about the zero line
        return (it / 2.0, -it / 2.0)
    if lo == "g":
        es = round(-2.5 * d ** 0.34); return (es, es - it)
    if lo == "f":
        es = round(-5.5 * d ** 0.41); return (es, es - it)
    if lo == "k":
        ei = round(0.6 * d ** (1 / 3)); return (ei + it, ei)
    if lo == "m":                                    # ei = IT7 - IT6 (validated at Ø4/Ø20/Ø63)
        ei = (it_grade(7, nominal) or 0) - (it_grade(6, nominal) or 0); return (ei + it, ei)
    if lo == "n":
        ei = round(5 * d ** 0.34); return (ei + it, ei)
    ei_t = interference_ei(lo, nominal)         # p/r/s: tabulated ei from ISO 286-2 (T5.1), es = ei + IT
    if ei_t is not None:
        return (ei_t + it, ei_t)
    return None  # t, u, v, ... still tabulated-only (not yet extracted) -> class only, never faked


def hole_deviation(letter: str, grade: int, nominal: float):
    """Return (ES, EI) hole deviations µm. Hole-basis only: H -> (IT, 0)."""
    it = it_grade(grade, nominal)
    if it is None or letter.upper() != "H":
        return None
    return (it, 0.0)


def fit(hole_letter: str, hole_grade: int, shaft_letter: str, shaft_grade: int,
        nominal: float) -> dict | None:
    """Exact ISO 286 fit at `nominal`: hole/shaft deviations + min/max clearance (µm) + class.
    Returns None if the letters/size are outside the exactly-supported scope (caller falls back to
    class-only). Negative clearance == interference."""
    h = hole_deviation(hole_letter, hole_grade, nominal)
    s = shaft_deviation(shaft_letter, shaft_grade, nominal)
    if h is None or s is None:
        return None
    es_hole, ei_hole = h   # ES, EI
    es_shaft, ei_shaft = s
    max_clearance = es_hole - ei_shaft   # ES_hole - ei_shaft
    min_clearance = ei_hole - es_shaft   # EI_hole - es_shaft
    if min_clearance >= 0:
        fit_class = "clearance"
    elif max_clearance <= 0:
        fit_class = "interference"
    else:
        fit_class = "transition"
    return {
        "nominal_mm": nominal,
        "hole_upper_dev_um": es_hole, "hole_lower_dev_um": ei_hole,
        "shaft_upper_dev_um": es_shaft, "shaft_lower_dev_um": ei_shaft,
        "hole_IT_um": it_grade(hole_grade, nominal), "shaft_IT_um": it_grade(shaft_grade, nominal),
        "min_clearance_um": min_clearance, "max_clearance_um": max_clearance,
        "fit_class": fit_class,
    }
