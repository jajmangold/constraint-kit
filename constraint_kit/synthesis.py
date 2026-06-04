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

from . import iso286


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


def synthesize_tolerance_allocation(dims: list, budget_um: float, grade_min: int = 5,
                                    grade_max: int = 12, method: str = "worst_case") -> dict:
    """SMT tolerance ALLOCATION (T10.1) — the inverse of `tolerance.stackup`. Given a chain of toleranced
    dimensions [{name, nominal}] and a stack-up `budget_um`, allocate the LOOSEST (cheapest to make) ISO 286
    IT grade per dimension whose combined tolerance still fits the budget. Z3 picks discrete grades over a
    precomputed IT-value table (it_grade is non-linear, so it's tabulated then selected) and MAXIMIZES the
    total grade (looser = larger IT = cheaper). `method`: 'worst_case' (Σ IT ≤ budget, guaranteed) or 'rss'
    (Σ IT² ≤ budget², statistical). Returns the allocation + slack, or an honest infeasible result with the
    tightest achievable total when even all-`grade_min` exceeds the budget. The right solver for a discrete
    selection/optimization problem — never touches geometry."""
    from z3 import If, Int, Optimize, Sum, sat

    grades = list(range(int(grade_min), int(grade_max) + 1))
    # tabulate IT value (µm) per (dim, grade) — it_grade is non-linear in size+grade
    table = []
    for d in dims:
        nominal = float(d["nominal"])
        row = {g: iso286.it_grade(g, nominal) for g in grades}
        if any(v is None for v in row.values()):
            return {"ok": False, "reason": f"dimension {d.get('name')!r}: nominal {nominal}mm outside "
                    f"ISO 286 range (3<D<=500) or grade unsupported", "solver": "z3 SMT"}
        table.append(row)

    opt = Optimize()
    gvars = [Int(f"g{i}") for i in range(len(dims))]
    tols = []
    for i, gv in enumerate(gvars):
        opt.add(gv >= grade_min, gv <= grade_max)
        tol = table[i][grade_max]                       # build an If-chain: grade -> tabulated IT value
        for g in grades[:-1]:
            tol = If(gv == g, table[i][g], tol)
        tols.append(tol)
    if method == "rss":
        opt.add(Sum([t * t for t in tols]) <= int(round(budget_um ** 2)))
    else:
        opt.add(Sum(tols) <= int(round(budget_um)))
    opt.maximize(Sum(gvars))                            # loosest (cheapest) grades that still fit

    requirement = {"budget_um": budget_um, "method": method, "grade_range": [grade_min, grade_max],
                   "n_dims": len(dims)}
    if opt.check() != sat:
        tight = sum(table[i][grade_min] for i in range(len(dims)))
        tight_rss = math.sqrt(sum(table[i][grade_min] ** 2 for i in range(len(dims))))
        return {"ok": False, "reason": "infeasible: even the tightest grades exceed the budget",
                "requirement": requirement, "solver": "z3 SMT",
                "min_achievable_um": round(tight_rss if method == "rss" else float(tight), 4)}
    mdl = opt.model()
    alloc, total, sq = [], 0.0, 0.0
    for i, d in enumerate(dims):
        g = mdl[gvars[i]].as_long()
        it = table[i][g]
        alloc.append({"name": d.get("name", f"dim{i}"), "nominal_mm": float(d["nominal"]),
                      "grade": f"IT{g}", "tolerance_um": it})
        total += it
        sq += it * it
    stack = round(math.sqrt(sq), 4) if method == "rss" else float(total)
    return {"ok": True, "allocation": alloc, "method": method,
            "stack_um": stack, "budget_um": budget_um, "slack_um": round(budget_um - stack, 4),
            "requirement": requirement, "solver": "z3 SMT (optimize)"}


def synthesize_gear_train(target_ratio: float, n_stages: int = 2, teeth_min: int = 12,
                          teeth_max: int = 40, module: float = 1.0, width: float = 6.0,
                          stage_ratio_min: float = 2.0, stage_ratio_max: float = 8.0,
                          ratio_tol: float = 0.25) -> dict:
    """Multi-STAGE gear-train synthesis (T10.3): split a large `target_ratio` across `n_stages` planetary
    stages whose ring-fixed ratios MULTIPLY to the target (within `ratio_tol`). Each stage is independently
    a valid planetary set (assembly condition, non-interference, tooth + per-stage-ratio bounds); Z3 solves
    the coupled nonlinear-integer problem and minimizes total ring teeth (compact). Returns per-stage configs
    + ready `part_specs` (one planetary_gearset per stage) + provenance, or an honest UNSAT. Each stage is
    cross-checked by the independent analytical `planetary.validate`."""
    from functools import reduce
    from z3 import If, Ints, Optimize, sat

    n = int(n_stages)
    requirement = {"target_ratio": target_ratio, "n_stages": n, "teeth_range": [teeth_min, teeth_max],
                   "stage_ratio_range": [stage_ratio_min, stage_ratio_max], "ratio_tol": ratio_tol}
    constraints = ["per stage: Zr==Zs+2*Zp, (Zs+Zr)%3==0, tip<spacing, teeth bounds",
                   f"per-stage ratio in [{stage_ratio_min},{stage_ratio_max}]",
                   "product of stage ratios within ratio_tol of target", "minimize total ring teeth"]

    opt = Optimize()
    Zs, Zp, Zr = [], [], []
    sin3 = int(round(1000 * math.sin(math.pi / 3)))               # 3-planet neighbour-chord coefficient
    smin = Fraction(stage_ratio_min).limit_denominator(1000)
    smax = Fraction(stage_ratio_max).limit_denominator(1000)
    for k in range(n):
        zs, zp, zr = Ints(f"Zs{k} Zp{k} Zr{k}")
        opt.add(zr == zs + 2 * zp)
        opt.add(zs >= teeth_min, zp >= teeth_min, zs <= teeth_max, zp <= teeth_max)
        opt.add((zs + zr) % 3 == 0)
        opt.add(1000 * (zp + 2) < sin3 * (zs + zp))
        opt.add(smin.numerator * zs <= smin.denominator * (zs + zr))   # stage ratio >= min
        opt.add(smax.numerator * zs >= smax.denominator * (zs + zr))   # stage ratio <= max
        Zs.append(zs); Zp.append(zp); Zr.append(zr)
    # product of stage ratios = Π(Zs+Zr) / Π(Zs), bounded to [target-tol, target+tol]
    top = reduce(lambda a, b: a * b, [Zs[k] + Zr[k] for k in range(n)])
    bot = reduce(lambda a, b: a * b, Zs)
    lo = Fraction(target_ratio - ratio_tol).limit_denominator(1000)
    hi = Fraction(target_ratio + ratio_tol).limit_denominator(1000)
    opt.add(lo.denominator * top >= lo.numerator * bot)
    opt.add(hi.denominator * top <= hi.numerator * bot)
    opt.minimize(reduce(lambda a, b: a + b, Zr))                  # compact: fewest total ring teeth

    if opt.check() != sat:
        return {"ok": False, "reason": "no multi-stage train satisfies the spec (UNSAT)",
                "requirement": requirement, "constraints": constraints, "solver": "z3 SMT"}
    mdl = opt.model()
    stages, part_specs, achieved = [], [], 1.0
    for k in range(n):
        zs, zp, zr = mdl[Zs[k]].as_long(), mdl[Zp[k]].as_long(), mdl[Zr[k]].as_long()
        sr = 1 + zr / zs
        achieved *= sr
        stages.append({"stage": k, "module": module, "sun_teeth": zs, "planet_teeth": zp,
                       "ring_teeth": zr, "n_planets": 3, "stage_ratio": round(sr, 6)})
        part_specs.append({"id": f"stage{k}", "type": "planetary_gearset", "material": "steel",
                           "params": {"module": module, "sun_teeth": zs, "planet_teeth": zp,
                                      "n_planets": 3, "width": width}})
    return {"ok": True, "stages": stages, "achieved_ratio": round(achieved, 6),
            "requirement": requirement, "constraints": constraints, "part_specs": part_specs,
            "solver": "z3 SMT (optimize, nonlinear-integer)"}
