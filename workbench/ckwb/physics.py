# -*- coding: utf-8 -*-
"""Bridge from a FreeCAD part to the verified `constraint_kit.fea` tier.

This runs the structural solver *inside FreeCAD*, in-process, which is only possible because the
physics tier was deliberately built on numpy and scipy alone — both of which FreeCAD 1.0 already
ships (numpy 1.26.4, scipy 1.16.0). Nothing is installed, no service is started, and no geometry
is marshalled anywhere.

The bridge itself is small: tessellate the part's shape, hand the triangles to the voxeliser, run
the solve, report. All the verification lives in the tier's own suite — element symmetry, six
rigid-body modes, `PL/AE` and `P/A` exact to 1e-9, bending convergent to Timoshenko — so there is
nothing to re-prove here beyond the plumbing.

Mesh deflection is tessellated at a quarter of the voxel pitch: meshing finer than the sampling
grid is wasted work, and meshing coarser puts facet error into the geometry the solver sees.
"""
from __future__ import annotations

import os
import sys

import FreeCAD

#: Material table, matching drivers/fea_part.py so a number means the same thing in both places.
#: E (MPa), nu, density (g/cm3), allowable (MPa), note.
MATERIALS = {
    "petg": (2000.0, 0.40, 1.27, 15.0, "printed, in-plane; creep-aware allowable"),
    "pla": (3300.0, 0.36, 1.24, 20.0, "printed, in-plane; poor above 50 C"),
    "abs": (2000.0, 0.35, 1.04, 15.0, "printed, in-plane"),
    "al6061": (68900.0, 0.33, 2.70, 165.0, "T6 wrought, yield 276 MPa at SF 1.67"),
    "steel": (200000.0, 0.30, 7.85, 150.0, "mild steel, yield 250 MPa at SF 1.67"),
}


def _import_fea():
    """Import the physics tier, adding the repo root to sys.path if it is not already importable.

    The workbench normally lives at `<constraint-kit>/workbench`, so the tier is one directory up.
    `CK_REPO` overrides that for an installed copy (a symlink into FreeCAD's Mod directory, say),
    and the error names the fix rather than leaving an ImportError to interpret.
    """
    try:
        from constraint_kit import fea
        return fea
    except ImportError:
        pass
    # realpath so a linked install resolves to the real checkout rather than to FreeCAD's
    # Mod directory, which has no constraint_kit beside it.
    here = os.path.realpath(__file__)
    candidates = [os.environ.get("CK_REPO"),
                  os.path.dirname(os.path.dirname(os.path.dirname(here)))]
    for root in candidates:
        if root and os.path.isdir(os.path.join(root, "constraint_kit")):
            if root not in sys.path:
                sys.path.insert(0, root)
            try:
                from constraint_kit import fea
                return fea
            except ImportError:
                continue
    raise ImportError(
        "cannot import constraint_kit.fea. It needs only numpy and scipy, both of which FreeCAD "
        "ships, so this is a path problem: set CK_REPO to the constraint-kit checkout.")


_DIRECT_OK: bool | None = None


def direct_solver_available() -> bool:
    """Is scipy's sparse DIRECT solver usable in this interpreter? Probed once, then cached.

    It is not, inside FreeCAD 1.0.2. The bundle pairs numpy 1.26.4 with scipy 1.16.0, and
    scipy's compiled SuperLU extension was built against a different numpy ABI, so `spsolve`
    and `factorized` both fail with the memorable `argument 3 must be numpy.ndarray, not
    numpy.ndarray` — two distinct ndarray types in one process. Pure-Python iterative solvers
    (`cg`, `minres`) and sparse matvec are unaffected, because they never cross that boundary.

    This is probed rather than hardcoded on "are we in FreeCAD", so a future bundle that ships a
    matched pair gets the faster direct path automatically with no code change.

    It also means the physics tier works here *only* because its CG path uses a Jacobi
    preconditioner. The ILU alternative that was removed earlier (as mathematically invalid for
    CG) would have failed here too, for this entirely separate reason: `spilu` is the same
    SuperLU extension.
    """
    global _DIRECT_OK
    if _DIRECT_OK is None:
        try:
            import numpy as np
            import scipy.sparse as sp
            import scipy.sparse.linalg as spla
            spla.spsolve(sp.csc_matrix(np.eye(3)), np.ones(3))
            _DIRECT_OK = True
        except Exception:  # noqa: BLE001 - any failure means "do not use it"
            _DIRECT_OK = False
    return _DIRECT_OK


def triangles_of(shape, deflection: float):
    """Tessellate a shape into an (n, 3, 3) array of triangle vertices, in world coordinates."""
    import numpy as np

    verts, facets = shape.tessellate(deflection)
    if not facets:
        raise ValueError("the shape tessellated to no triangles; is it an empty or non-solid shape?")
    pts = np.array([[v.x, v.y, v.z] for v in verts], dtype=float)
    idx = np.array(facets, dtype=int)
    return pts[idx]


def _selector(expr: str):
    """Reuse the driver's parsed (never eval'd) selector language: `x<=-59 & z>=0`."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    if root not in sys.path:
        sys.path.insert(0, root)
    from drivers.fea_part import parse_fix
    return parse_fix(expr)


def stiffness(obj, material: str = "petg", pitch: float = 2.0, *,
              fix: str, load: str, force=(0.0, 0.0, -100.0)) -> dict:
    """Solve one part for deflection and stress. Boundary conditions are arguments, never guessed.

    `fix` and `load` are selector expressions over node coordinates in millimetres, in the part's
    own placed position — e.g. `x<=-59`, or `x>=24@xz` to restrain only two axes, which is what a
    roller or a V-wheel actually does.

    Returns a structured verdict with the mesh size, so a number is never quoted without the
    resolution it came from. For a converged answer run several pitches and extrapolate with
    `constraint_kit.fea.richardson`, which declines rather than inventing a limit.
    """
    fea = _import_fea()
    key = str(material).lower()
    if key not in MATERIALS:
        raise ValueError(f"unknown material {material!r}; have {', '.join(sorted(MATERIALS))}")
    E, nu, density, allowable, _note = MATERIALS[key]

    shape = getattr(obj, "Shape", None)
    if shape is None or shape.isNull():
        raise ValueError(f"{getattr(obj, 'Name', obj)} has no shape to analyse")

    tri = triangles_of(shape, max(pitch / 4.0, 1e-3))
    grid = fea.occupancy_from_triangles(tri, float(pitch), pad=1)

    model = fea.Model(grid, E, nu)
    for expr in (fix,) if isinstance(fix, str) else fix:
        sel, axes = _selector(expr)
        model.fix(sel, axes=axes)
    load_sel, _axes = _selector(load)
    model.load(load_sel, tuple(float(v) for v in force))

    # Inside FreeCAD the direct solver is unusable (see direct_solver_available); CG with a
    # Jacobi preconditioner is pure numpy and works. "auto" would pick direct below 80k DOF.
    res = model.solve(method="auto" if direct_solver_available() else "cg")
    volume_cm3 = grid.material_volume / 1000.0
    peak = res.von_mises_percentile(99.0)
    return {
        "part": getattr(obj, "Name", ""),
        "material": key,
        "pitch_mm": float(pitch),
        "elements": int(len(res.active_elements)),
        "dof": int(res.n_dof),
        "method": res.method,
        "residual": res.residual,
        "volume_cm3": round(volume_cm3, 4),
        "mass_g": round(volume_cm3 * density, 3),
        "max_disp_mm": round(res.max_displacement, 6),
        "stress_p99_mpa": round(peak, 4),
        "stress_peak_mpa": round(res.peak_von_mises, 4),
        "allowable_mpa": allowable,
        "safety_factor": round(allowable / peak, 3) if peak > 0 else None,
        "compliance": res.compliance,
        "direct_solver": direct_solver_available(),
        "note": "one mesh is not a converged answer; run a ladder and extrapolate",
    }
