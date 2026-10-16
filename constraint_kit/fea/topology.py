# -*- coding: utf-8 -*-
"""SIMP topology optimisation -- an ORACLE, never a generator (issue #19).

The issue title is the design, and it is the one thing to understand before reading further.
What comes out of this is a **density field**, and a density field is not a manufacturable
design: it is a statement about *where material earns its keep*, which a human then redraws as
parametric geometry. Everything else in this project runs the other way -- parameters in,
verified geometry out, provenance on every number -- and emitting a grey blob as geometry would
invert the whole doctrine. So `Topology` has no `.shape()` and no `.step()`, deliberately, and
`suggestion()` returns prose.

It is also why this is separate from `constraint_kit.optimise`. That searches a **parameter
vector** -- a design somebody drew, with dimensions that mean something, and an answer you can
put on a drawing. This searches a **density field**, which answers a different question (should
there be material here at all) and whose answer is a picture. Keeping them apart is why the
parametric optimizer can report "thickness 17.2, depth 12, stiffness binds" instead of a cloud.

**The groundwork was laid for this on purpose.** `voxel.Model` has accepted `density_floor` and
`penal` since the day it was written, with the comment that they exist "so a topology optimizer
can reuse this assembly unchanged" -- and it does. Nothing in `voxel.py` changed to support this
module.

The method is the standard one, and the standard one is standard for good reasons:

* **SIMP**: stiffness interpolated as `rho^p`, which makes intermediate density structurally
  inefficient and so pushes the field toward 0 or 1 without any combinatorial search.
* **Sensitivity filtering**: the first thing that goes wrong with unfiltered SIMP is
  checkerboarding -- alternating full and empty elements, which is an artefact of the element
  rather than a structure. The filter is not decoration and there is a test that turns it off to
  prove the artefact returns.
* **The optimality criterion** for the update, with the Lagrange multiplier found by bisection
  so the volume constraint holds *exactly* at every iteration rather than approximately. A
  design that halved its compliance by quietly using more material has not optimised anything.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .hex8 import hex8_stiffness
from .voxel import Grid, Model

#: Density below which an element is treated as void. Not zero: a zero-stiffness element makes
#: the matrix singular, and the sensitivity at exactly zero density is undefined.
DENSITY_FLOOR = 1e-3

#: Largest density change one element may make in one iteration. The classic move limit -- it
#: keeps the optimality-criterion update stable, at the cost of needing more iterations.
MOVE_LIMIT = 0.2

#: Filter radius in ELEMENT widths. 1.5 is the conventional minimum that suppresses
#: checkerboarding; larger gives smoother, less efficient, more manufacturable fields.
FILTER_RADIUS = 1.5


@dataclass(frozen=True)
class Topology:
    """A density field, its history, and a clear statement of what it is not."""

    density: np.ndarray
    compliance: float
    history: list
    converged: bool
    volume_fraction: float
    penal: float
    filter_radius: float
    note: str = ""
    _meta: dict = field(default_factory=dict)

    def as_grid(self, pitch, origin=(0.0, 0.0, 0.0)) -> Grid:
        """The field as a `Grid`, so the suggestion can be MEASURED rather than admired.

        The one legitimate mechanical thing to do with it. The compliance reported above is
        computed on a field with intermediate densities, which is not a part -- re-analysing it
        is how you find out what the suggestion is actually worth.
        """
        return Grid(density=self.density, pitch=pitch, origin=origin)

    def suggestion(self) -> str:
        """What to DO with this, in words, because it is not a design."""
        solid = float((self.density > 0.9).mean())
        grey = float(((self.density > 0.1) & (self.density < 0.9)).mean())
        return (
            "This is a SUGGESTION, not a design. It says where material earns its keep under the "
            "load you gave, at %.0f%% of the volume you started with -- %.0f%% of the field came "
            "out essentially solid and %.0f%% is intermediate, and intermediate density is not a "
            "material. Read it as a sketch of a load path and redraw it as parametric geometry: "
            "ribs where the field is solid, a pocket where it is empty, and your own judgement "
            "about the grey. Nothing here can be printed and nothing here has a provenance."
            % (100.0 * self.volume_fraction, 100.0 * solid, 100.0 * grey))

    def report(self) -> str:
        lines = ["SIMP topology, %d iteration(s) at p=%.1f, filter radius %.2f elements"
                 % (len(self.history), self.penal, self.filter_radius),
                 "  volume fraction  %8.4f  (held exactly, every iteration)"
                 % self.volume_fraction,
                 "  compliance       %8.4g  (was %8.4g at the start)"
                 % (self.compliance, self.history[0]["compliance"] if self.history else 0.0),
                 "  solid (>0.9)     %8.1f%%" % (100.0 * (self.density > 0.9).mean()),
                 "  void  (<0.1)     %8.1f%%" % (100.0 * (self.density < 0.1).mean()),
                 "  intermediate     %8.1f%%  <- not a material; this is the part you decide"
                 % (100.0 * ((self.density >= 0.1) & (self.density <= 0.9)).mean())]
        if not self.converged:
            lines.append("  NOT CONVERGED: the iteration budget ran out while the field was "
                         "still moving. Treat it as a direction, not an answer.")
        if self.note:
            lines.append("  " + self.note)
        lines.append("  " + self.suggestion())
        return "\n".join(lines)


def checkerboard_score(density: np.ndarray) -> float:
    """Amplitude of the ALTERNATING component of the field. 0 is clean, 1 is a checkerboard.

    The field is multiplied by the checkerboard parity `(-1)^(i+j+k)` and the result averaged
    over each 2x2x2 block. For a true checkerboard the parity exactly cancels the alternation
    and the block mean is the amplitude; for anything that does not alternate at the element
    scale the parity makes the product alternate instead, and the block mean is near zero.
    Scaled so a perfect 0/1 checkerboard scores 1.

    **The obvious measure does not work, and getting it wrong cost a test.** The first version
    took the mean difference between each element and its six neighbours -- which is large for a
    checkerboard and *also* large for a legitimately sharp solid/void boundary, which is exactly
    what a well-resolved topology is made of. It scored a cleanly filtered field at 0.14 and an
    unfiltered one at 0.25, so the filter looked only mildly effective when the real difference
    is an order of magnitude. A measure that cannot tell a defect from the thing you wanted is
    not a measure of the defect.

    Has its own test, including a smooth ramp -- which must score near zero however much it
    varies overall.
    """
    d = np.asarray(density, dtype=float)
    i, j, k = np.indices(d.shape)
    parity = np.where((i + j + k) % 2 == 0, 1.0, -1.0)
    signed = d * parity

    # Average over 2x2x2 blocks: trim to even dimensions, reshape, and mean the sub-axes. A
    # dimension of 1 has no alternation to measure along it and is left alone.
    trimmed = signed[:d.shape[0] // 2 * 2 or 1,
                     :d.shape[1] // 2 * 2 or 1,
                     :d.shape[2] // 2 * 2 or 1]
    blocks = trimmed
    for axis in range(3):
        if blocks.shape[axis] >= 2:
            shape = list(blocks.shape)
            shape[axis : axis + 1] = [blocks.shape[axis] // 2, 2]
            blocks = blocks.reshape(shape).mean(axis=axis + 1)
    return float(2.0 * np.abs(blocks).mean())


def _filter_weights(shape, radius: float):
    """Cone-shaped neighbourhood weights for the sensitivity filter, as offset/weight pairs.

    Built once and applied by array shifting, which keeps this to numpy -- `scipy.ndimage` is
    not importable inside FreeCAD (see `manufacture.py`) and the physics tier runs in there.
    """
    reach = int(np.floor(radius))
    out = []
    span = range(-reach, reach + 1)
    for i in span:
        for j in span:
            for k in span:
                distance = float(np.sqrt(i * i + j * j + k * k))
                weight = radius - distance
                if weight > 0.0:
                    out.append(((i, j, k), weight))
    return out


def _apply_filter(values: np.ndarray, density: np.ndarray, weights) -> np.ndarray:
    """The classic sensitivity filter: a density-weighted cone average of the neighbourhood.

    `(sum over neighbours of w * rho * dc) / (rho * sum of w)`. The density weighting is what
    makes it a *sensitivity* filter rather than a plain blur, and it is what stops the filter
    pulling material into regions the solve said were useless.
    """
    num = np.zeros_like(values)
    den = np.zeros_like(values)
    nx, ny, nz = values.shape

    def window(n, d):
        if d >= 0:
            return slice(0, n - d), slice(d, n)
        return slice(-d, n), slice(0, n + d)

    for (i, j, k), weight in weights:
        dx, sx = window(nx, i)
        dy, sy = window(ny, j)
        dz, sz = window(nz, k)
        num[dx, dy, dz] += weight * density[sx, sy, sz] * values[sx, sy, sz]
        den[dx, dy, dz] += weight * density[sx, sy, sz]
    safe = np.maximum(den, 1e-30)
    return num / safe


def _element_energies(model: Model, grid: Grid, result, KE: np.ndarray) -> np.ndarray:
    """`u_e' K0 u_e` per element, on the FULL grid. The sensitivity rests on this.

    `K0` is the unit-density element stiffness, so this is the strain energy the element would
    carry at full density -- which is what the SIMP derivative wants, not the penalised value.
    """
    energies = np.zeros(grid.density.size)
    active = result.active_elements
    if active.size:
        nodes = grid.element_nodes(active)
        edof = (nodes[:, :, None] * 3 + np.arange(3)[None, None, :]).reshape(len(nodes), 24)
        u = result.displacement.ravel()[edof]                       # (m, 24)
        energies[active] = np.einsum("ij,jk,ik->i", u, KE, u)
    return energies.reshape(grid.shape)


def _optimality_update(density: np.ndarray, dc: np.ndarray, target: float) -> np.ndarray:
    """One optimality-criterion step, with the multiplier bisected so the volume is EXACT.

    The bracket starts wide and closes on a relative width, so the volume constraint is satisfied
    to floating point rather than to a tolerance somebody picked. That matters more than it
    sounds: a volume that drifts by a percent per iteration makes every compliance comparison in
    the history meaningless.
    """
    n = density.size
    low, high = 1e-9, 1e9
    best = density
    for _ in range(200):
        mid = 0.5 * (low + high)
        step = density * np.sqrt(np.maximum(-dc, 0.0) / mid)
        best = np.clip(np.clip(step, density - MOVE_LIMIT, density + MOVE_LIMIT),
                       DENSITY_FLOOR, 1.0)
        if best.sum() > target * n:
            low = mid
        else:
            high = mid
        if (high - low) / (high + low) < 1e-12:
            break
    return best


def topology(*, grid: Grid, E: float, nu: float, fix, load,
             volume_fraction: float = 0.4, penal: float = 3.0,
             filter_radius: float = FILTER_RADIUS, max_iter: int = 60,
             tol: float = 0.01, method: str = "auto") -> Topology:
    """Minimise compliance at a fixed volume fraction. Returns a field, not a part.

    `fix` and `load` take the same selector form as `voxel.Model`, so the same lambdas that
    analyse a part can define a design domain -- which is the point of having built the two on
    one assembly path.
    """
    if not 0.0 < volume_fraction < 1.0:
        raise ValueError(
            f"volume_fraction must lie strictly between 0 and 1, got {volume_fraction}. At 1 "
            f"there is nothing to optimise and at 0 there is no structure.")
    if penal < 1.0:
        raise ValueError(
            f"penal must be at least 1, got {penal}. Below 1 the interpolation REWARDS "
            f"intermediate density instead of punishing it, so the field never resolves toward "
            f"solid or void and the method does the opposite of its job.")
    if filter_radius < 0.0:
        raise ValueError(f"filter_radius must not be negative, got {filter_radius}")
    if max_iter < 1:
        raise ValueError(f"max_iter must be at least 1, got {max_iter}")

    where_load, force = load
    density = np.full(grid.shape, float(volume_fraction))
    KE = hex8_stiffness(grid.pitch, E, nu)
    weights = _filter_weights(grid.shape, filter_radius) if filter_radius > 0.0 else []

    history: list = []
    converged = False
    compliance = float("inf")

    for _ in range(int(max_iter)):
        working = Grid(density=density, pitch=grid.pitch, origin=grid.origin)
        model = Model(working, E, nu, density_floor=DENSITY_FLOOR, penal=penal)
        model.fix(fix)
        model.load(where_load, force)
        result = model.solve(method=method)
        compliance = float(result.compliance)

        energies = _element_energies(model, working, result, KE)
        # d(compliance)/d(rho) for SIMP: negative, since more material is always stiffer.
        dc = -penal * np.maximum(density, DENSITY_FLOOR) ** (penal - 1.0) * energies
        if weights:
            dc = _apply_filter(dc, np.maximum(density, DENSITY_FLOOR), weights)

        updated = _optimality_update(density, dc, float(volume_fraction))
        change = float(np.abs(updated - density).max())
        density = updated
        history.append({
            "compliance": compliance,
            "volume_fraction": float(density.mean()),
            "max_change": change,
        })
        if change <= tol:
            converged = True
            break

    # One final solve on the field being returned, so the reported compliance is the compliance
    # OF THIS FIELD rather than of the one before the last update. Otherwise `as_grid` and
    # `compliance` would disagree, and a reader could not check one against the other.
    final = Grid(density=density, pitch=grid.pitch, origin=grid.origin)
    model = Model(final, E, nu, density_floor=DENSITY_FLOOR, penal=penal)
    model.fix(fix)
    model.load(where_load, force)
    compliance = float(model.solve(method=method).compliance)

    note = ""
    score = checkerboard_score(density)
    if filter_radius <= 0.0 and score > 0.05:
        note = ("no sensitivity filter was used and the field is checkerboarded (score %.3f). "
                "That alternation is an artefact of the element, not a structure -- do not read "
                "a load path off it." % score)
    return Topology(density=density, compliance=compliance, history=history,
                    converged=converged, volume_fraction=float(volume_fraction),
                    penal=float(penal), filter_radius=float(filter_radius), note=note,
                    _meta={"checkerboard": score})
