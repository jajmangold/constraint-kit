# -*- coding: utf-8 -*-
"""Richardson extrapolation over a mesh-refinement ladder.

Phase 0 reports a column of numbers at decreasing voxel pitch and leaves a human to read the
trend. An optimizer cannot read a trend. Before a parameter sweep is allowed to call design B
stiffer than design A it has to know whether the gap it measured is structural or is mesh error,
and that requires three things rather than one: the extrapolated limit, the observed order of
convergence, and an error bar on the finest value.

The method is standard (Richardson / Roache). For a discretisation whose error behaves as
C*h^p on a ladder with a CONSTANT refinement ratio r = h1/h2 = h2/h3:

    (f1 - f2) / (f2 - f3) = r^p          ->  p   = ln((f1-f2)/(f2-f3)) / ln(r)
    f_exact               ~ f3 + (f3 - f2) / (r^p - 1)

Two design rules here, both learned from this solver's own output:

* **Refuse rather than smooth.** A sequence that turns around is not converging, and Richardson
  applied to it yields a confident number derived from noise. The real gantry-plate ladder did
  exactly this at coarse pitch (1.31, 1.10, 0.84, 0.93 mm) because thin walls were falling
  through the grid, so the geometry -- not just its discretisation -- was changing between
  meshes. Reporting `extrapolated=None` with a reason is the only honest answer.
* **The constant-ratio assumption is checked, not assumed.** `r` sits in an exponent, so a
  sloppy ladder corrupts the order estimate directly. `drivers/fea_part.py` originally built its
  ladder with 1.4x and 0.7x, and 0.7 is not 1/1.4 -- a 2% ratio error, silently absorbed into p.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# A sequence whose successive differences are this small relative to its magnitude has stopped
# moving; the order of convergence is then undefined (0/0) rather than zero.
_STALL_RTOL = 1e-14


class ConvergenceError(ValueError):
    """The ladder cannot support an extrapolation at all (bad shape, spacing or ordering).

    Distinct from a ladder that is merely not converged: that is a legitimate finding and comes
    back as a `Convergence` with `converged=False`, not an exception.
    """


@dataclass(frozen=True)
class Convergence:
    """A convergence estimate, carrying the inputs it came from so a logged number is auditable."""

    pitches: tuple[float, ...]        # the three used, coarsest first
    values: tuple[float, ...]         # every value supplied
    order: float | None               # observed p; None if undefined or unobtainable
    extrapolated: float | None        # Richardson limit; None if the ladder does not support one
    rel_error: float                  # |finest - limit| / |limit|; inf when unknowable
    monotone: bool
    converged: bool
    note: str

    @property
    def finest(self) -> float:
        return self.values[-1]

    def __str__(self) -> str:
        if self.extrapolated is None:
            return (f"not converging: {self.note} "
                    f"(finest {self.finest:.6g} at pitch {self.pitches[-1]:g})")
        order = "undefined" if self.order is None else f"{self.order:.2f}"
        return (f"{self.extrapolated:.6g} extrapolated from {self.finest:.6g} "
                f"(order {order}, {self.rel_error:.1%} remaining, "
                f"{'converged' if self.converged else 'NOT converged'})")


def richardson(pitches, values, *, tol: float = 0.05,
               ratio_rtol: float = 1e-3) -> Convergence:
    """Estimate the mesh-independent limit of `values` sampled at `pitches`.

    `pitches` must be positive and strictly descending (coarse to fine) and the finest three must
    share a constant refinement ratio to within `ratio_rtol`. `tol` is the remaining relative
    error below which the result is called converged.

    Raises `ConvergenceError` if the ladder cannot support an estimate. Returns a `Convergence`
    with `extrapolated=None` if it can, but the sequence is not converging.
    """
    p = [float(v) for v in pitches]
    f = [float(v) for v in values]
    if len(p) != len(f):
        raise ConvergenceError(
            f"got {len(p)} pitches and {len(f)} values; they must correspond one to one")
    if len(p) < 3:
        raise ConvergenceError(
            f"need at least 3 refinement levels to estimate an order, got {len(p)}")
    if any(v <= 0.0 for v in p):
        raise ConvergenceError(f"pitches must all be positive, got {p}")
    if any(a <= b for a, b in zip(p, p[1:])):
        raise ConvergenceError(
            f"pitches must descend strictly from coarse to fine, got {p}; sorting them here "
            "would hide the caller pairing values with the wrong meshes")

    # Only the finest three carry information about the limit; coarser levels are history.
    h1, h2, h3 = p[-3:]
    f1, f2, f3 = f[-3:]

    r1, r2 = h1 / h2, h2 / h3
    if abs(r1 - r2) > ratio_rtol * max(r1, r2):
        raise ConvergenceError(
            f"refinement ratio is not constant: {h1:g}/{h2:g} = {r1:.6g} but "
            f"{h2:g}/{h3:g} = {r2:.6g}. Richardson puts this ratio in an exponent, so build the "
            f"ladder geometrically (h*r, h, h/r) rather than with a rounded factor")
    r = 0.5 * (r1 + r2)

    d1, d2 = f2 - f1, f3 - f2
    scale = max(abs(f1), abs(f2), abs(f3), 1e-300)
    common = dict(pitches=(h1, h2, h3), values=tuple(f))

    # Already flat: converged, but with no order to report (0/0, not 0).
    if max(abs(d1), abs(d2)) <= _STALL_RTOL * scale:
        return Convergence(order=None, extrapolated=f3, rel_error=0.0, monotone=True,
                           converged=True,
                           note="values identical across the ladder; already mesh-independent",
                           **common)

    if d1 == 0.0 or d2 == 0.0 or (d1 > 0.0) != (d2 > 0.0):
        return Convergence(
            order=None, extrapolated=None, rel_error=math.inf, monotone=False, converged=False,
            note=(f"sequence is not monotone ({f1:.6g} -> {f2:.6g} -> {f3:.6g}); refinement is "
                  "changing the geometry, not just resolving it -- usually a load-carrying wall "
                  "thinner than the pitch"),
            **common)

    rp = d1 / d2                      # equals r**p exactly, so use it directly
    if rp <= 1.0 + 1e-12:
        return Convergence(
            order=None, extrapolated=None, rel_error=math.inf, monotone=True, converged=False,
            note=(f"successive changes are growing, not shrinking ({d1:.6g} then {d2:.6g}); "
                  "refinement is still finding structure rather than resolving it. On a voxel "
                  "grid this means a load-carrying wall is only a few elements thick, so whether "
                  "it lands on element centres changes the stiffness more than the refinement "
                  "does -- a body-fitted mesh, not a finer grid, is the way out"),
            **common)

    order = math.log(rp) / math.log(r)
    extrapolated = f3 + d2 / (rp - 1.0)
    denom = abs(extrapolated) if abs(extrapolated) > 1e-300 else scale
    rel_error = abs(f3 - extrapolated) / denom
    converged = rel_error <= tol

    return Convergence(
        order=order, extrapolated=extrapolated, rel_error=rel_error, monotone=True,
        converged=converged,
        note=("within tolerance" if converged else
              f"{rel_error:.1%} of the answer is still mesh error, above the {tol:.0%} limit; "
              f"refine to about {h3 * (tol / rel_error) ** (1.0 / order):.3g} mm to reach it"),
        **common)
