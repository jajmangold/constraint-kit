"""Tolerance stack-up (T4.2) — chain toleranced dimensions to a resultant (e.g. a clearance = hole −
shaft) and report worst-case AND statistical (RSS) bounds. Each dimension's tolerance is either explicit
(upper/lower deviations in mm) or sourced from ISO 286 by fit (`hole:"H7"` / `shaft:"g6"`), so a stack
cross-checks against `iso286.fit` exactly. Worst-case = guaranteed bounds; RSS = √Σtol² (statistical,
narrower) for production assemblies. Engineering depth: 'will it fit across the whole chain', not one pair."""
from __future__ import annotations

import math

from . import iso286


def _devs(dim: dict) -> tuple:
    """(upper, lower) signed deviations in mm for a dimension — from an ISO 286 fit or explicit values."""
    code = dim.get("hole") or dim.get("shaft")
    if code:
        letter = "".join(c for c in code if c.isalpha())
        grade = int("".join(c for c in code if c.isdigit()))
        fn = iso286.hole_deviation if "hole" in dim else iso286.shaft_deviation
        up_um, lo_um = fn(letter, grade, dim["nominal"])
        return up_um / 1000.0, lo_um / 1000.0
    return float(dim.get("upper", 0.0)), float(dim.get("lower", 0.0))


def stackup(dims: list, as_clearance: bool = False) -> dict:
    """Resultant of Σ dir·dim. `dir` (+1 add, −1 subtract) defaults to +1. Returns nominal + worst-case
    and RSS min/max; with as_clearance, ok flags (resultant min ≥ 0 → no interference)."""
    nom = wc_hi = wc_lo = mean = var = 0.0
    rows = []
    for d in dims:
        direction = d.get("dir", 1)
        n = float(d["nominal"])
        up, lo = _devs(d)
        nom += direction * n
        wc_hi += up if direction > 0 else -lo
        wc_lo += lo if direction > 0 else -up
        mid, tol = (up + lo) / 2.0, (up - lo) / 2.0
        mean += direction * (n + mid)
        var += tol * tol
        rows.append({"name": d.get("name"), "nominal": n, "dir": direction,
                     "upper": round(up, 4), "lower": round(lo, 4)})
    rss = math.sqrt(var)
    out = {"nominal": round(nom, 4),
           "worst_case": {"min": round(nom + wc_lo, 4), "max": round(nom + wc_hi, 4)},
           "rss": {"min": round(mean - rss, 4), "max": round(mean + rss, 4), "mean": round(mean, 4)},
           "dims": rows}
    if as_clearance:
        out["ok_worstcase"] = (nom + wc_lo) >= 0
        out["ok_rss"] = (mean - rss) >= 0
    return out
