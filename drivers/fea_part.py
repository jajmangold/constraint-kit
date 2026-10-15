#!/usr/bin/env python3
"""Stiffness and stress of one part, from an STL or a STEP, with a mesh-convergence bracket.

    python drivers/fea_part.py PART.stl --material petg \
        --fix  "y<=125" \
        --load "y>=188 : 0,764,0" \
        --converge

Boundary conditions are arguments, never inferred. Choosing where a part is held and where it is
pushed IS the engineering; a tool that guesses produces a number that looks like analysis and is
not. The selector language is deliberately tiny -- conjunctions of `axis op value` in millimetres,
e.g. `x>=-38 & y>=144 & y<=186` -- and is parsed, not eval'd.

`--converge` solves a geometric ladder of three pitches and Richardson-extrapolates, so the
output is a limit with an error bar rather than a number. A single voxel-FEA value is not a
result: a trilinear hex locks in bending and a stair-stepped boundary concentrates stress, so
what is trustworthy is where the sequence is heading and how far the finest mesh still is from
it. If the ladder is not converging, `constraint_kit.fea.richardson` says so and declines to
extrapolate rather than producing a confident number out of noise.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from constraint_kit.fea import (  # noqa: E402
    ConvergenceError, Grid, Model, load_stl, occupancy_from_triangles, richardson,
)

# E (MPa), nu, density (g/cm3), allowable stress (MPa), note.
#
# The PETG allowable is NOT a datasheet tensile strength. It is the 15 MPa working figure already
# used throughout knee-exo (its "PETG hub 29.4 MPa, SF 0.51" is 29.4 x 0.51 = 15.0), which is a
# creep-aware allowable for a printed part under sustained load rather than a short-term ultimate.
# Using a 50 MPa datasheet number here would silently triple every safety factor.
MATERIALS = {
    "petg":   (2000.0, 0.40, 1.27, 15.0, "printed, in-plane; creep-aware allowable per knee-exo"),
    "pla":    (3300.0, 0.36, 1.24, 20.0, "printed, in-plane; brittle, poor above 50 C"),
    "abs":    (2000.0, 0.35, 1.04, 15.0, "printed, in-plane"),
    "al6061": (68900.0, 0.33, 2.70, 165.0, "T6 wrought, yield 276 MPa, allowable at SF 1.67"),
    "steel":  (200000.0, 0.30, 7.85, 150.0, "mild steel, yield 250 MPa, allowable at SF 1.67"),
}

_OPS = {">=": np.greater_equal, "<=": np.less_equal, ">": np.greater, "<": np.less}


def parse_fix(spec: str) -> tuple:
    """`x>=24@xz` -> (selector, "xz"). A bare selector restrains all three axes.

    A V-wheel on a rail, a roller, a slide or a bearing journal reacts in some directions and
    not others, and clamping all three where the hardware only reacts two invents stiffness the
    machine does not have.

    Worth knowing how this behaves when it does NOT matter, because that is the more common
    case and it is informative. On the knee-exo gantry plate, releasing Y at the V-wheel arms
    (`x>=24@xz` instead of `x>=24`) freed 1,980 DOF and changed the deflection by nothing at all
    -- 1.0048 mm either way, to five figures. That is not the restraint being irrelevant; it is
    proof of where the load goes. The belt groove wall already reacts the whole 764 N, so the
    V-wheel arms were carrying no part of it. A restraint that can be released without changing
    the answer is a restraint that was carrying no load, which is a load-path finding worth
    having rather than a null result.
    """
    body, sep, axes = spec.rpartition("@")
    if not sep:
        return parse_selector(spec), "xyz"
    axes = axes.strip().lower()
    if not axes or set(axes) - set("xyz"):
        raise ValueError(f"restraint axes must be drawn from 'xyz', got {axes!r} in {spec!r}")
    return parse_selector(body), axes


def parse_selector(expr: str):
    """`x>=-38 & y<=186` -> callable(coords) -> bool mask. Parsed, not eval'd."""
    clauses = []
    for raw in expr.split("&"):
        term = raw.strip()
        for sym in (">=", "<=", ">", "<"):          # two-char first, or ">=" parses as ">"
            if sym in term:
                axis, _, value = term.partition(sym)
                axis = axis.strip().lower()
                if axis not in ("x", "y", "z"):
                    raise ValueError(f"selector axis must be x, y or z, got {axis!r} in {term!r}")
                clauses.append(("xyz".index(axis), _OPS[sym], float(value)))
                break
        else:
            raise ValueError(
                f"cannot parse selector clause {term!r}; expected e.g. 'y>=144' "
                "joined by '&'")
    if not clauses:
        raise ValueError(f"empty selector: {expr!r}")

    def sel(p: np.ndarray) -> np.ndarray:
        mask = np.ones(len(p), dtype=bool)
        for ax, op, val in clauses:
            mask &= op(p[:, ax], val)
        return mask

    sel.__doc__ = expr
    return sel


def parse_load(spec: str):
    """`y>=188 : 0,764,0` -> (selector, (fx, fy, fz)) in newtons."""
    where, _, vec = spec.partition(":")
    if not vec.strip():
        raise ValueError(f"load needs a force vector after ':', got {spec!r}")
    parts = [float(v) for v in vec.split(",")]
    if len(parts) != 3:
        raise ValueError(f"load force must be 'fx,fy,fz', got {vec!r}")
    return parse_selector(where), tuple(parts)


def build_grid(path: str, pitch: float, pad: int = 1) -> Grid:
    if path.lower().endswith((".stl",)):
        return occupancy_from_triangles(load_stl(path), pitch, pad=pad)
    from constraint_kit.fea import occupancy_from_shape
    import cadquery as cq
    return occupancy_from_shape(cq.importers.importStep(path), pitch, pad=pad)


def analyse(path, pitch, mat, fixes, loads, method="auto"):
    E, nu, rho, allow, _ = mat
    t0 = time.perf_counter()
    grid = build_grid(path, pitch)
    m = Model(grid, E, nu)
    for sel, axes in fixes:
        m.fix(sel, axes=axes)
    for where, force in loads:
        m.load(where, force)
    res = m.solve(method=method)
    vol = grid.material_volume / 1000.0                      # cm3
    peak = res.von_mises_percentile(99.0)
    return {
        "pitch": pitch,
        "elements": int(len(res.active_elements)),
        "dof": res.n_dof,
        "volume_cm3": vol,
        "mass_g": vol * rho,
        "max_disp_mm": res.max_displacement,
        "stress_p99": peak,
        "stress_peak": res.peak_von_mises,
        "safety_factor": allow / peak if peak > 0 else math.inf,
        "compliance": res.compliance,
        "seconds": time.perf_counter() - t0,
        "method": res.method,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("part", help="STL or STEP file")
    ap.add_argument("--material", default="petg", choices=sorted(MATERIALS))
    ap.add_argument("--pitch", type=float, default=2.0, help="voxel pitch in mm")
    ap.add_argument("--fix", action="append", required=True, metavar="SELECTOR",
                    help="restrain nodes, e.g. 'y<=125' or 'x>=24@xz' for "
                         "selected axes only; repeatable")
    ap.add_argument("--load", action="append", required=True, metavar="SELECTOR:fx,fy,fz",
                    help="total force in N over the selected nodes; repeatable")
    ap.add_argument("--converge", action="store_true",
                    help="also solve one level coarser and one finer, then Richardson-extrapolate")
    ap.add_argument("--ratio", type=float, default=1.4, metavar="R",
                    help="refinement ratio for --converge (default 1.4)")
    ap.add_argument("--tol", type=float, default=0.05, metavar="F",
                    help="remaining mesh error below which a result counts as converged")
    ap.add_argument("--method", default="auto", choices=("auto", "direct", "cg"))
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)  # progress must be visible when piped

    mat = MATERIALS[args.material]
    fixes = [parse_fix(s) for s in args.fix]
    loads = [parse_load(s) for s in args.load]

    pitches = [args.pitch]
    if args.converge:
        # Geometric, and deliberately NOT rounded. Richardson puts the refinement ratio in an
        # exponent, so the ladder must satisfy h1/h2 == h2/h3 exactly. This originally used 1.4x
        # and 0.7x -- but 0.7 is not 1/1.4, and that 2% ratio error lands straight in the order
        # estimate. `richardson` now refuses such a ladder instead of absorbing it.
        if args.ratio <= 1.0:
            ap.error(f"--ratio must exceed 1.0, got {args.ratio}")
        pitches = [args.pitch * args.ratio, args.pitch, args.pitch / args.ratio]

    print(f"{os.path.basename(args.part)}  material={args.material} "
          f"E={mat[0]:.0f} MPa  nu={mat[1]}  allowable={mat[3]:.0f} MPa")
    print(f"  ({mat[4]})")
    for s in args.fix:
        print(f"  fix   {s}")
    for s in args.load:
        print(f"  load  {s}  N")
    print()
    hdr = (f"{'pitch':>6s} {'elems':>8s} {'dof':>8s} {'mass g':>8s} {'disp mm':>9s} "
           f"{'s99 MPa':>8s} {'peak':>8s} {'SF':>6s} {'solve':>7s}")
    print(hdr)
    print("-" * len(hdr))

    rows = []
    for p in pitches:
        try:
            r = analyse(args.part, p, mat, fixes, loads, args.method)
        except Exception as exc:                                     # noqa: BLE001
            print(f"{p:6.2f}  FAILED: {exc}")
            continue
        rows.append(r)
        print(f"{r['pitch']:6.2f} {r['elements']:8d} {r['dof']:8d} {r['mass_g']:8.1f} "
              f"{r['max_disp_mm']:9.4f} {r['stress_p99']:8.2f} {r['stress_peak']:8.2f} "
              f"{r['safety_factor']:6.2f} {r['seconds']:6.1f}s")

    if not rows:
        return 1
    if len(rows) >= 3:
        hs = [r["pitch"] for r in rows]
        print()
        for label, key, unit in (("deflection", "max_disp_mm", "mm"),
                                 ("p99 stress", "stress_p99", "MPa")):
            try:
                c = richardson(hs, [r[key] for r in rows], tol=args.tol)
            except ConvergenceError as exc:
                print(f"  {label:11s} not extrapolable: {exc}")
                continue
            print(f"  {label:11s} {unit:4s} {c}")
        print("\nA voxel boundary concentrates stress, so the peak column is an upper bound "
              "and p99 is the structural signal.")
    fin = rows[-1]
    if fin["safety_factor"] < 1.0:
        print(f"\nFAIL: p99 stress {fin['stress_p99']:.1f} MPa exceeds the {mat[3]:.0f} MPa "
              f"allowable (SF {fin['safety_factor']:.2f}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
