"""Planetary (epicyclic) gear-train math + validation — the kinematics the geometry must honor.

A planetary set is the canonical CLOSED-LOOP assembly: each planet meshes BOTH the sun (external) and
the ring (internal), so the planet carries two simultaneous mesh constraints. The deterministic TREE
solver in builder.py places each part by ONE mate from its parent and therefore cannot, on its own,
close that loop — see `builder.find_loops`. This module supplies the closed-form relations + the
consistency conditions a DOF solver validates (see `dof/` service).

Conventions: Zs sun teeth, Zp planet teeth, N planets, module m. Ring teeth Zr = Zs + 2·Zp (standard,
equal module). The sun-planet (external) and ring-planet (internal) centre distances are BOTH
m·(Zs+Zp)/2 — that equality is the geometric loop closure.
"""
from __future__ import annotations

import math


def compute(module: float, sun_teeth: int, planet_teeth: int, n_planets: int = 3) -> dict:
    Zs, Zp, N = int(sun_teeth), int(planet_teeth), int(n_planets)
    Zr = Zs + 2 * Zp
    c = module * (Zs + Zp) / 2.0  # carrier PCD = sun-planet centre distance = ring-planet centre distance
    return {
        "module": module, "sun_teeth": Zs, "planet_teeth": Zp, "ring_teeth": Zr, "n_planets": N,
        "carrier_pcd": c,
        "center_distance_sun_planet": module * (Zs + Zp) / 2.0,
        "center_distance_ring_planet": module * (Zr - Zp) / 2.0,  # internal mesh
        "planet_angles_deg": [round(i * 360.0 / N, 6) for i in range(N)],
        # speed ratios: ring fixed, sun in, carrier out -> n_sun/n_carrier = 1 + Zr/Zs
        "ratio_sun_to_carrier_ring_fixed": 1.0 + Zr / Zs,
        "dof_ring_fixed": 1,   # 1-DOF mechanism when the ring is grounded
        "dof_free": 2,         # 2-DOF when no member grounded (sun & ring independent)
    }


def mobility(module: float, sun_teeth: int, planet_teeth: int, n_planets: int = 3,
             grounded: str = "ring") -> dict:
    """Closed-loop DOF / mobility analysis — what the tree solver cannot do. A planetary train is a
    2-DOF gear system (sun and ring independent); grounding one member (ring/sun/carrier) leaves a
    1-DOF mechanism. Also reports whether the closed loop is geometrically CONSISTENT (the per-planet
    sun+ring mesh constraints close), which is the precondition for any solver to succeed."""
    info = compute(module, sun_teeth, planet_teeth, n_planets)
    errs = validate(module, sun_teeth, planet_teeth, n_planets)
    free_dof = 2
    grounds = {"ring", "sun", "carrier"}
    dof = free_dof - (1 if grounded in grounds else 0)
    return {
        **info,
        "loop_consistent": not errs,
        "violations": errs,
        "grounded": grounded if grounded in grounds else None,
        "gear_dof": dof,                       # 1 with a member grounded, 2 free
        "is_mechanism": dof >= 1 and not errs,
        # each planet meshes BOTH sun and ring -> N loop-closing constraints the tree solver skips
        "loop_closing_constraints": int(n_planets),
        "method": "epicyclic mobility (Willis) + geometric loop-closure check",
    }


def validate(module: float, sun_teeth: int, planet_teeth: int, n_planets: int = 3) -> list[str]:
    """Return a list of constraint-violation messages ([] = a valid, assemblable planetary set).
    These are exactly the closed-loop consistency conditions the tree solver cannot check itself."""
    Zs, Zp, N = int(sun_teeth), int(planet_teeth), int(n_planets)
    Zr = Zs + 2 * Zp
    errs: list[str] = []
    if N < 2:
        errs.append(f"n_planets must be >= 2 (got {N})")
    # equal-spacing / assembly condition: (Zs + Zr) divisible by N
    if N >= 1 and (Zs + Zr) % N != 0:
        errs.append(f"assembly condition fails: (Zs+Zr)={Zs + Zr} not divisible by n_planets={N} "
                    f"(planets cannot be equally spaced and mesh both members)")
    # geometric loop closure: sun-planet and ring-planet centre distances must be equal
    if (Zs + Zp) != (Zr - Zp):
        errs.append("centre-distance mismatch: sun-planet != ring-planet (loop does not close)")
    # neighbouring-planet non-interference: planet tip dia < chord between adjacent planet centres
    if N >= 2:
        tip_d = module * (Zp + 2)
        chord = 2.0 * (module * (Zs + Zp) / 2.0) * math.sin(math.pi / N)
        if tip_d >= chord:
            errs.append(f"adjacent planets interfere: tip dia {tip_d:.3f} >= centre spacing {chord:.3f}")
    return errs
