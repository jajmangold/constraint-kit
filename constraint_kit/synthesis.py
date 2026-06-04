"""SMT-backed design SYNTHESIS — the 4th solver class (see ROADMAP.md / the solver taxonomy).

The other solvers CHECK or PLACE a given design (mates → placement, planetary → mobility check, SolveSpace
→ geometric loop). This one **designs one to a spec**: it solves a discrete/mixed-integer constraint
problem with Z3 (SMT) and returns a valid configuration — or an honest, provable **UNSAT** when none exists.

It does NOT touch continuous geometry (SolveSpace's job) or forward param DAGs (`parameters.py`); it fills
the gap of *synthesis / configuration / optimization* over discrete engineering choices (tooth counts,
sizes, catalog selections). Deterministic; provenance = the requirement + the exact constraint set used;
the result includes a ready-to-build `part_spec` so synthesis flows straight into exact CAD.
"""
from __future__ import annotations

import math
from fractions import Fraction


def synthesize_planetary(target_ratio: float, n_planets: int = 3, teeth_min: int = 12,
                         teeth_max: int = 40, module: float = 1.0, width: float = 6.0,
                         ratio_tol: float = 0.0, objective: str = "compact") -> dict:
    """Design a planetary gearset meeting a ring-fixed speed ratio (n_sun/n_carrier = 1 + Zr/Zs), subject
    to the ISO assembly/equal-spacing condition, planet non-interference, and tooth bounds. `objective`:
    'compact' (min ring), 'strength' (max sun teeth), or 'none'. Returns a config + provenance + a ready
    `part_spec` for `planetary_gearset`, or {ok:false, reason} when the spec is infeasible (provable UNSAT)."""
    from z3 import Ints, Optimize, sat

    n = int(n_planets)
    requirement = {"target_ratio": target_ratio, "n_planets": n, "teeth_range": [teeth_min, teeth_max],
                   "ratio_tol": ratio_tol, "objective": objective}
    constraints = ["Zr == Zs + 2*Zp", "ring-fixed ratio (Zs+Zr)/Zs == target",
                   "assembly condition (Zs+Zr) % N == 0", "planet non-interference (tip dia < spacing)",
                   f"teeth in [{teeth_min},{teeth_max}]"]

    Zs, Zp, Zr = Ints("Zs Zp Zr")
    opt = Optimize()
    opt.add(Zr == Zs + 2 * Zp)
    opt.add(Zs >= teeth_min, Zp >= teeth_min, Zs <= teeth_max, Zp <= teeth_max)
    opt.add((Zs + Zr) % n == 0)                                   # equal-spacing / assemblability
    sin_x1000 = int(round(1000 * math.sin(math.pi / n)))          # neighbour-planet chord coefficient
    opt.add(1000 * (Zp + 2) < sin_x1000 * (Zs + Zp))              # tip dia < neighbour centre spacing
    # ratio (Zs+Zr)/Zs vs target, as exact rational (or a tolerance band)
    if ratio_tol > 0:
        lo = Fraction(target_ratio - ratio_tol).limit_denominator(10000)
        hi = Fraction(target_ratio + ratio_tol).limit_denominator(10000)
        opt.add(lo.denominator * (Zs + Zr) >= lo.numerator * Zs)
        opt.add(hi.denominator * (Zs + Zr) <= hi.numerator * Zs)
    else:
        r = Fraction(target_ratio).limit_denominator(10000)
        opt.add(r.denominator * (Zs + Zr) == r.numerator * Zs)
    if objective == "compact":
        opt.minimize(Zr)
    elif objective == "strength":
        opt.maximize(Zs)

    if opt.check() != sat:
        return {"ok": False, "reason": "no planetary set satisfies the spec (UNSAT)",
                "requirement": requirement, "constraints": constraints, "solver": "z3 SMT"}
    m = opt.model()
    zs, zp, zr = m[Zs].as_long(), m[Zp].as_long(), m[Zr].as_long()
    return {
        "ok": True,
        "config": {"module": module, "sun_teeth": zs, "planet_teeth": zp, "ring_teeth": zr,
                   "n_planets": n},
        "achieved_ratio": round(1 + zr / zs, 6),
        "requirement": requirement, "constraints": constraints,
        "part_spec": {"id": "planetary", "type": "planetary_gearset", "material": "steel",
                      "params": {"module": module, "sun_teeth": zs, "planet_teeth": zp,
                                 "n_planets": n, "width": width}},
        "solver": "z3 SMT (optimize)",
    }
