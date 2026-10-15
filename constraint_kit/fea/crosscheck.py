# -*- coding: utf-8 -*-
"""Compare the voxel tier against the tetrahedral reference, and say what the gap means.

The job here is narrow and worth stating, because it is easy to build the wrong thing. This
module does NOT decide whether a part is safe, and it does not assert that two solvers agree. It
measures the gap between them and attributes it, because when two solvers disagree the useful
output is the size and the direction of the disagreement, never an exception.

Attribution is possible rather than hand-waving, because the two methods fail in known and
opposite ways:

* **A trilinear hexahedron locks in bending.** With few elements through a thickness the voxel
  model is too stiff and reads LOW on deflection. This is the dominant error on a plate and it
  has a known cure: refine, and watch the ratio climb toward one.
* **A staircased boundary cannot represent a fillet or a round hole.** It both removes material
  the part has and adds re-entrant corners the part does not, so it reads HIGH on peak stress
  and is unreliable near a curved edge at any affordable pitch.
* **A conforming quadratic tet mesh has neither problem** and is the reference for that reason —
  but it is not truth either, which is why `exact_displacement` can be supplied when a closed
  form exists and both are then measured against it.

So a ratio below one on deflection is expected and quantifiable; a ratio above one is not, and
is the interesting case, because the voxel model has no mechanism for being too soft. That
asymmetry is in the verdict text, because the number alone invites the wrong reading.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# How far apart two solvers may land before the gap is worth acting on. 15% is not a precision
# claim -- it is the point past which the difference exceeds what mesh choice alone explains on
# the cases checked here, and so stops being a discretisation detail and starts being a question.
TOLERANCE = 0.15

# A stress percentile rather than a peak, on both sides. Comparing peaks compares two different
# artefacts: CalculiX extrapolation overshoot at a node against our staircase corner.
STRESS_PERCENTILE = 99.0


class NotComparable(ValueError):
    """The two results are not of the same problem, so their difference means nothing.

    Raised rather than reported, because this is a mistake in the caller rather than a finding
    about the solvers. A comparison of two different load cases would produce a number, and that
    number would be worse than useless: it would look like a solver disagreement.
    """


@dataclass(frozen=True)
class Agreement:
    """The measured gap between the two solvers, with the reason for it where one is known."""

    displacement_voxel: float
    displacement_reference: float
    stress_voxel: float
    stress_reference: float
    exact_displacement: float | None
    n_dof_voxel: int
    n_nodes_reference: int
    n_elements_reference: int
    tolerance: float = TOLERANCE

    @property
    def displacement_ratio(self) -> float:
        """Voxel over reference. Below one means the voxel model is the stiffer of the two."""
        if self.displacement_reference == 0.0:
            return float("nan")
        return self.displacement_voxel / self.displacement_reference

    @property
    def stress_ratio(self) -> float:
        if self.stress_reference == 0.0:
            return float("nan")
        return self.stress_voxel / self.stress_reference

    @property
    def agrees(self) -> bool:
        """Within tolerance on displacement.

        Displacement alone, deliberately. It is a global, integral quantity and the one a
        compliance or a safety factor rests on; a stress percentile is a local quantity that a
        staircase is entitled to get wrong near a curve. Stress is reported and not gated.
        """
        ratio = self.displacement_ratio
        return bool(np.isfinite(ratio) and abs(ratio - 1.0) <= self.tolerance)

    @property
    def verdict(self) -> str:
        ratio = self.displacement_ratio
        if not np.isfinite(ratio):
            return "no reference deflection to compare against"
        if abs(ratio - 1.0) <= self.tolerance:
            return "agree"
        if ratio < 1.0:
            return "voxel too stiff"
        return "voxel too soft -- unexplained"

    def report(self) -> str:
        """The whole comparison as text, including what the gap is attributable to."""
        lines = [
            "voxel vs tetrahedral reference (%d DOF vs %d nodes / %d C3D10)"
            % (self.n_dof_voxel, self.n_nodes_reference, self.n_elements_reference),
            "  displacement  %.6g vs %.6g mm   ratio %.4f"
            % (self.displacement_voxel, self.displacement_reference, self.displacement_ratio),
            "  stress p%.0f     %.4g vs %.4g MPa  ratio %.4f"
            % (STRESS_PERCENTILE, self.stress_voxel, self.stress_reference, self.stress_ratio),
        ]
        if self.exact_displacement:
            lines.append(
                "  closed form   %.6g mm   voxel x%.4f, reference x%.4f"
                % (self.exact_displacement,
                   self.displacement_voxel / self.exact_displacement,
                   self.displacement_reference / self.exact_displacement))

        ratio = self.displacement_ratio
        if self.agrees:
            lines.append("  VERDICT agree, within %.0f%%" % (100.0 * self.tolerance))
        elif ratio < 1.0:
            lines.append(
                "  VERDICT the voxel model is TOO STIFF -- it reads %.0f%% of the reference "
                "deflection." % (100.0 * ratio))
            lines.append(
                "          Expected direction: a trilinear hexahedron locks in bending, so a "
                "coarse grid under-predicts deflection. Refine the pitch, especially through "
                "the thinnest dimension, and this ratio should climb toward one.")
        else:
            lines.append(
                "  VERDICT the voxel model is TOO SOFT -- %.0f%% of the reference. This "
                "direction has no discretisation explanation: a stiffness-locking element does "
                "not become compliant on a coarse grid. Suspect the boundary conditions, the "
                "load selector, or lost material in the voxelisation before suspecting the "
                "mesh." % (100.0 * ratio))
        return "\n".join(lines)


def compare(voxel_result, tet_result, *, exact_displacement: float | None = None,
            tolerance: float = TOLERANCE, check_load: bool = True) -> Agreement:
    """Measure the gap between a `voxel.Result` and a `tet.TetResult` of the same problem.

    `check_load` compares the force the two solvers actually applied, and refuses the comparison
    if they differ. That check exists because of a real defect: `voxel.Model.load` once divided
    a force over padding nodes outside the part and silently dropped their share, so 200 N
    arrived as 60.5 N. The solve converged to a residual of 9e-10 and was wrong by a factor of
    three. A cross-check that did not compare the loads would have reported a solver
    disagreement and sent the next person looking in the wrong place entirely.
    """
    from . import voxel as _voxel  # local: keeps the module import-light

    if not isinstance(voxel_result, _voxel.Result):
        raise TypeError(f"expected a voxel.Result, got {type(voxel_result).__name__}")

    if check_load:
        theirs = np.asarray(tet_result.applied_force, dtype=float)
        ours = getattr(voxel_result, "applied_force", None)
        if ours is None:
            raise NotComparable(
                "this voxel.Result does not report the force it applied, so the two runs cannot "
                "be confirmed to be the same load case. Pass check_load=False to compare anyway, "
                "accepting that a load mismatch will read as a solver disagreement.")
        ours = np.asarray(ours, dtype=float)
        scale = max(float(np.abs(theirs).max()), float(np.abs(ours).max()), 1e-30)
        if float(np.abs(theirs - ours).max()) > 1e-6 * scale:
            raise NotComparable(
                f"the two runs applied different loads -- voxel {tuple(ours)} against reference "
                f"{tuple(theirs)}. Their difference would not be a solver disagreement.")

    return Agreement(
        displacement_voxel=voxel_result.max_displacement,
        displacement_reference=tet_result.max_displacement,
        stress_voxel=voxel_result.von_mises_percentile(STRESS_PERCENTILE),
        stress_reference=tet_result.von_mises_percentile(STRESS_PERCENTILE),
        exact_displacement=exact_displacement,
        n_dof_voxel=voxel_result.n_dof,
        n_nodes_reference=tet_result.n_nodes,
        n_elements_reference=tet_result.n_elements,
        tolerance=tolerance,
    )
