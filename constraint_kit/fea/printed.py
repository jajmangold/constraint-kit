# -*- coding: utf-8 -*-
"""Printed parts are anisotropic, and von Mises does not know that (issue #2).

An FDM wall is a stack of welded beads. Along a bead the polymer is continuous; across the weld
it is a diffusion bond formed in under a second against an already-cooling surface. Published
data puts across-layer tensile strength at roughly half the in-plane value for PETG and ABS, and
interlayer shear lower again.

So **the von Mises stress is the wrong number to compare against an allowable for a part that
was printed.** Von Mises presumes a material with no preferred direction; a printed part has a
very strong one. What decides whether such a part survives is the traction on the *layer plane*,
split into the part trying to peel the layers apart and the part trying to slide them over one
another. Neither is recoverable from an invariant, which is why both solver tiers keep the full
stress tensor.

This matters in practice rather than in principle. The knee-exo gantry plate, under its
documented 764 N belt load, comes out at:

| layer plane normal | peel SF | shear SF | governing |
|---|---|---|---|
| Z — printed flat | 1.87 | 1.70 | **1.70** |
| X — printed on its side | 1.41 | 1.54 | **1.41** |
| Y — printed on its end | **0.74** | 1.43 | **0.74** |

Printed flat, the in-plane von Mises verdict (SF 1.06) governs and the welds are not the problem.
Printed on its end, the belt tension pulls straight across every weld and the part delaminates
at three quarters of its design load. **Print orientation is a load-bearing design decision**,
and it is one a von Mises number cannot even express.

Two things this module is careful not to pretend:

* **The de-rate factors are an assumption, not a measurement.** They are literature-typical for
  the polymer — not a property of a particular filament, nozzle temperature, layer height, or
  printer. So `InterlayerCheck` carries the factors it used, and `report()` says they are
  assumed. A safety factor computed from assumed properties that travels without them will be
  quoted as though it were measured.
* **Compression across layers is not peeling.** A weld in compression is as strong as the bulk,
  so only *tensile* normal traction counts. Treating compression as a peel load would condemn
  parts that are entirely fine.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# Across-layer strength as a fraction of in-plane, for a typical unannealed FDM part in PETG or
# ABS at normal print temperatures. Literature-typical rather than measured, deliberately on the
# conservative side of the published spread (0.45-0.60 tensile), and stated everywhere they are
# used. A well-characterised or annealed process does better; pass your own `factors`.
LAYER_FACTORS = {"tensile": 0.50, "shear": 0.45}

# Same reason as everywhere else in this package: a peak at one node is a mesh artefact as much
# as a structural fact, and it moves with refinement while a high percentile does not.
PERCENTILE = 99.0

# The three orientations a part can be printed in without a fixture, as layer-plane normals.
ORIENTATIONS = {"X": (1.0, 0.0, 0.0), "Y": (0.0, 1.0, 0.0), "Z": (0.0, 0.0, 1.0)}


def traction_on_plane(stress: np.ndarray, normal) -> tuple:
    """Resolve a stress tensor onto a plane: `(direct, shear_magnitude)` per row.

    `stress` is (n, 6) as `(sxx, syy, szz, sxy, syz, szx)` — the layout both solver tiers use.
    `normal` is the plane normal and need not be a unit vector. `direct` is signed, positive in
    tension; `shear` is a magnitude and so never negative.
    """
    s = np.asarray(stress, dtype=float)
    if s.ndim != 2 or s.shape[1] != 6:
        raise ValueError(f"stress must be (n, 6) as (sxx, syy, szz, sxy, syz, szx), got {s.shape}")
    n = np.asarray(normal, dtype=float)
    if n.shape != (3,):
        raise ValueError(f"normal must be a 3-vector, got shape {n.shape}")
    length = float(np.linalg.norm(n))
    if length == 0.0:
        raise ValueError("the zero vector is not a plane normal")
    n = n / length

    sxx, syy, szz, sxy, syz, szx = s.T
    # t = sigma . n, written out rather than assembled as n separate 3x3 tensors: this runs over
    # a few hundred thousand rows and the explicit form never materialises them.
    tx = sxx * n[0] + sxy * n[1] + szx * n[2]
    ty = sxy * n[0] + syy * n[1] + syz * n[2]
    tz = szx * n[0] + syz * n[1] + szz * n[2]

    direct = tx * n[0] + ty * n[1] + tz * n[2]
    # Clamped at zero: |t|^2 - direct^2 is non-negative in exact arithmetic and can land a few
    # ulps below it in floating point, and a negative shear magnitude is nonsense downstream.
    shear = np.sqrt(np.maximum(tx ** 2 + ty ** 2 + tz ** 2 - direct ** 2, 0.0))
    return direct, shear


@dataclass(frozen=True)
class InterlayerCheck:
    """Whether the WELDS survive, as distinct from whether the material does."""

    normal: tuple
    peel: float                 # tensile traction across the layers, at `percentile`
    shear: float                # shear traction in the layer plane, at `percentile`
    peak_peel: float            # reported for context, never used for the verdict
    peak_shear: float
    allow_peel: float
    allow_shear: float
    in_plane_allowable: float
    percentile: float
    factors: dict = field(default_factory=lambda: dict(LAYER_FACTORS))

    @staticmethod
    def _sf(allow: float, demand: float) -> float:
        return float("inf") if demand <= 0.0 else allow / demand

    @property
    def sf_peel(self) -> float:
        return self._sf(self.allow_peel, self.peel)

    @property
    def sf_shear(self) -> float:
        return self._sf(self.allow_shear, self.shear)

    @property
    def governing(self) -> float:
        return min(self.sf_peel, self.sf_shear)

    @property
    def governing_mode(self) -> str:
        if self.sf_peel == self.sf_shear:
            return "neither" if self.governing == float("inf") else "both"
        return "peel" if self.sf_peel < self.sf_shear else "shear"

    @property
    def passes(self) -> bool:
        return self.governing >= 1.0

    def report(self) -> str:
        lines = [
            "interlayer check, layer plane normal (%.3g, %.3g, %.3g)" % self.normal,
            "  peel  %7.2f MPa vs %6.2f allowable   SF %s"
            % (self.peel, self.allow_peel, _fmt(self.sf_peel)),
            "  shear %7.2f MPa vs %6.2f allowable   SF %s"
            % (self.shear, self.allow_shear, _fmt(self.sf_shear)),
        ]
        if self.peel <= 0.0:
            lines.append("  the layers are in COMPRESSION on this plane, which does not peel "
                         "a weld; a bond in compression is as strong as the bulk material.")
        if self.governing == float("inf"):
            lines.append("  VERDICT no interlayer demand on this plane at all.")
        elif self.passes:
            lines.append("  VERDICT passes; %s governs at SF %.2f."
                         % (self.governing_mode, self.governing))
        else:
            lines.append("  VERDICT FAILS on %s at SF %.2f. The welds give out before the "
                         "material does, so the in-plane allowable is not the binding "
                         "constraint here -- the print orientation is."
                         % (self.governing_mode, self.governing))
        lines.append("  from an in-plane allowable of %.1f MPa and ASSUMED across-layer factors "
                     "(tensile %.2f, shear %.2f). Literature-typical for unannealed FDM, NOT "
                     "measured for this filament or printer; measure them if the margin is "
                     "thin." % (self.in_plane_allowable, self.factors["tensile"],
                                self.factors["shear"]))
        lines.append("  peak peel %.2f / shear %.2f MPa, reported for context: a peak at one "
                     "node is a mesh artefact as much as a load, which is why the verdict uses "
                     "the %.0fth percentile." % (self.peak_peel, self.peak_shear,
                                                 self.percentile))
        return "\n".join(lines)


def _fmt(sf: float) -> str:
    return "  n/a" if sf == float("inf") else "%5.2f" % sf


def check(stress: np.ndarray, normal, *, in_plane_allowable: float,
          factors: dict | None = None, percentile: float = PERCENTILE) -> InterlayerCheck:
    """Can the WELDS take it, given where the layers lie?

    `normal` is the layer-plane normal: for a part printed flat on the bed that is the build
    direction. `in_plane_allowable` is the ordinary material allowable — the one a von Mises
    check uses — and the across-layer pair is derived from it by `factors`.
    """
    use = dict(LAYER_FACTORS if factors is None else factors)
    for key in ("tensile", "shear"):
        if key not in use:
            raise ValueError(
                f"factors must give both 'tensile' and 'shear'; {key!r} is missing. An "
                f"across-layer check with one of the two assumed silently is not a check.")
        value = float(use[key])
        if not 0.0 < value <= 1.0:
            raise ValueError(
                f"factors[{key!r}] must lie in (0, 1], got {value}. Above 1.0 would claim a weld "
                f"stronger than the bulk polymer, which no FDM process achieves.")
    if in_plane_allowable <= 0.0:
        raise ValueError(f"in_plane_allowable must be positive, got {in_plane_allowable}")
    if not 0.0 < percentile <= 100.0:
        raise ValueError(f"percentile must lie in (0, 100], got {percentile}")

    direct, shear = traction_on_plane(stress, normal)
    # Only TENSION peels a weld. Clamping at zero rather than taking an absolute value, because
    # a compressive normal traction is carried by the bond as well as by the bulk.
    peeling = np.maximum(direct, 0.0)
    n = np.asarray(normal, dtype=float)
    n = n / float(np.linalg.norm(n))

    return InterlayerCheck(
        normal=tuple(float(v) for v in n),
        peel=float(np.percentile(peeling, percentile)),
        shear=float(np.percentile(shear, percentile)),
        peak_peel=float(peeling.max()) if peeling.size else 0.0,
        peak_shear=float(shear.max()) if shear.size else 0.0,
        allow_peel=in_plane_allowable * float(use["tensile"]),
        allow_shear=in_plane_allowable * float(use["shear"]),
        in_plane_allowable=float(in_plane_allowable),
        percentile=float(percentile),
        factors=use,
    )


def best_orientation(stress: np.ndarray, *, in_plane_allowable: float,
                     factors: dict | None = None,
                     percentile: float = PERCENTILE) -> list:
    """Rank the three printable orientations, best first, as `[(name, InterlayerCheck), ...]`.

    This is the practical output: which way up to print the part. A part loaded in one direction
    wants its layers *parallel* to that load, so the load runs along the beads rather than across
    the welds — and that is a conclusion worth reading off the stress field rather than guessing
    at, because on a real part with a real load path the answer is not always the obvious axis.
    """
    ranked = [(name, check(stress, n, in_plane_allowable=in_plane_allowable,
                           factors=factors, percentile=percentile))
              for name, n in ORIENTATIONS.items()]
    ranked.sort(key=lambda pair: pair[1].governing, reverse=True)
    return ranked
