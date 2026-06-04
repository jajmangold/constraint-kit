"""Geometric closed-loop DOF solver (planar mechanisms) via python-solvespace.

This completes the constraint-kit taxonomy of loops:
  - tree assemblies   -> deterministic mate kernel (builder.py)
  - GEAR loops        -> analytical constraint solver (planetary.py — Willis mobility)
  - GEOMETRIC loops   -> a real constraint solver (THIS module — SolveSpace)

A 4-bar linkage / slider-crank closes a loop through *geometric* constraints (link lengths + joints),
not gear ratios, so the analytical method doesn't apply and the deterministic tree solver can't close
it — SolveSpace does. We report mechanism mobility two independent ways that must agree: SolveSpace's
solved DOF and the analytical Gruebler/Kutzbach count.

python-solvespace is imported lazily (compiled extension, container-only). Its `create_2d_base()`
workplane floats with a fixed base DOF (measured once via `_base_dof`); mechanism mobility =
solved_dof − base_dof.
"""
from __future__ import annotations

import math

_BASE_DOF: int | None = None


def gruebler(n_links: int, lower_pairs: int, higher_pairs: int = 0) -> int:
    """Planar mobility (Kutzbach): M = 3(L−1) − 2·j_lower − j_higher."""
    return 3 * (n_links - 1) - 2 * lower_pairs - higher_pairs


def _base_dof() -> int:
    global _BASE_DOF
    if _BASE_DOF is None:
        from python_solvespace import SolverSystem
        s = SolverSystem(); s.create_2d_base(); s.solve()
        _BASE_DOF = s.dof()
    return _BASE_DOF


def _grashof(*lengths: float) -> bool:
    """Grashof condition: shortest + longest <= sum of the other two -> a link can fully rotate."""
    s = sorted(lengths)
    return s[0] + s[-1] <= sum(s[1:-1])


def solve_four_bar(ground: float = 100.0, crank: float = 30.0, coupler: float = 90.0,
                   rocker: float = 60.0, input_angle_deg: float | None = None) -> dict:
    """Solve a planar 4-bar (ground pivots A@origin, D@(ground,0); crank A→B, coupler B→C, rocker D→C).
    With no input the mechanism mobility is 1; fixing the crank angle determines the position."""
    from python_solvespace import ResultFlag, SolverSystem
    base = _base_dof()
    s = SolverSystem(); wp = s.create_2d_base()
    A = s.add_point_2d(0, 0, wp); s.dragged(A, wp)
    D = s.add_point_2d(ground, 0, wp); s.dragged(D, wp)
    B = s.add_point_2d(crank * math.cos(math.radians(45)), crank * math.sin(math.radians(45)), wp)
    C = s.add_point_2d(ground - rocker * 0.7, rocker * 0.7, wp)
    s.distance(A, B, crank, wp); s.distance(B, C, coupler, wp); s.distance(D, C, rocker, wp)
    if input_angle_deg is not None:
        s.angle(s.add_line_2d(A, B, wp), s.add_line_2d(A, D, wp), float(input_angle_deg), wp)
    ok = s.solve() == ResultFlag.OKAY
    pts, resid = {}, {}
    if ok:
        def xy(p):
            u, v = s.params(p.params); return (round(u, 4), round(v, 4))
        pts = {"A": xy(A), "B": xy(B), "C": xy(C), "D": xy(D)}
        resid = {"crank": abs(math.dist(pts["A"], pts["B"]) - crank),
                 "coupler": abs(math.dist(pts["B"], pts["C"]) - coupler),
                 "rocker": abs(math.dist(pts["D"], pts["C"]) - rocker)}
    return {
        "mechanism": "four_bar", "ok": ok, "mechanism_dof": s.dof() - base,
        "gruebler_dof": gruebler(4, 4), "positions": pts, "link_residuals": resid,
        "grashof": _grashof(ground, crank, coupler, rocker),
        "input_fixed": input_angle_deg is not None,
        "solver": "python-solvespace (geometric constraint solver)",
    }


def solve_slider_crank(crank: float = 25.0, rod: float = 80.0, offset: float = 0.0,
                       input_angle_deg: float | None = None) -> dict:
    """Solve a planar slider-crank (crank pivot A@origin, slider S on the line y=offset, rod B→S)."""
    from python_solvespace import ResultFlag, SolverSystem
    base = _base_dof()
    s = SolverSystem(); wp = s.create_2d_base()
    A = s.add_point_2d(0, 0, wp); s.dragged(A, wp)
    G = s.add_point_2d(crank + rod + 20, offset, wp); s.dragged(G, wp)   # fixed pt on slider axis
    B = s.add_point_2d(0, crank, wp)
    S = s.add_point_2d(crank + rod, offset, wp)
    s.distance(A, B, crank, wp); s.distance(B, S, rod, wp)
    s.horizontal(s.add_line_2d(S, G, wp), wp)                            # slider travels on y=offset
    if input_angle_deg is not None:
        s.angle(s.add_line_2d(A, B, wp), s.add_line_2d(A, G, wp), float(input_angle_deg), wp)
    ok = s.solve() == ResultFlag.OKAY
    pts, resid = {}, {}
    if ok:
        def xy(p):
            u, v = s.params(p.params); return (round(u, 4), round(v, 4))
        pts = {"A": xy(A), "B": xy(B), "S": xy(S)}
        resid = {"crank": abs(math.dist(pts["A"], pts["B"]) - crank),
                 "rod": abs(math.dist(pts["B"], pts["S"]) - rod),
                 "slider_on_axis": abs(pts["S"][1] - offset)}
    return {
        "mechanism": "slider_crank", "ok": ok, "mechanism_dof": s.dof() - base,
        "gruebler_dof": gruebler(4, 4), "positions": pts, "link_residuals": resid,
        "input_fixed": input_angle_deg is not None,
        "solver": "python-solvespace (geometric constraint solver)",
    }
