"""ISO 286 limits & fits — REAL tolerances computed from the standard formulas, driven by nominal size.

Standard tolerance factor (3 < D <= 500 mm):  i = 0.45·∛D + 0.001·D  (µm), D = geometric mean of the
size step. IT grade value = factor·i. Fundamental deviations from the published ISO 286-1 formulas
(validated against ISO 286-2 tables at Ø20: H7=+21/0, g6=−7/−20, k6=+15/+2, n6=+28/+15, f7=−20/−41).

Honest scope: hole-basis (H) holes + shaft letters f, g, h, k, n have clean validated power-law formulas
and yield exact deviations + clearances. Interference letters p, r, s, … do NOT reduce to a single
formula in ISO 286, so they are reported class-only (no fabricated micron values). 3 < D <= 500 mm.
All deviations in micrometres (µm).
"""
from __future__ import annotations

import math

STEPS = [(3, 6), (6, 10), (10, 18), (18, 30), (30, 50), (50, 80), (80, 120), (120, 180),
         (180, 250), (250, 315), (315, 400), (400, 500)]
GRADE_FACTOR = {5: 7, 6: 10, 7: 16, 8: 25, 9: 40, 10: 64, 11: 100, 12: 160, 13: 250,
                14: 400, 15: 640, 16: 1000}
# shaft letters with a clean, ISO-table-validated closed-form fundamental deviation. Interference letters
# p..zc are TABULATED in ISO 286 (no closed form) -> deliberately NOT here (we don't fabricate them).
EXACT_SHAFT_LETTERS = {"f", "g", "h", "js", "k", "m", "n"}


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
    """Return (es, ei) shaft deviations µm for supported letters (f,g,h,k,n), else None."""
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
    return None  # p, r, s, ... : tabulated in ISO 286 (no clean formula) -> class only, never faked


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
