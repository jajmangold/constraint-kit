# -*- coding: utf-8 -*-
"""Physics tier: linear elastostatics on voxel grids, numpy/scipy only.

This is PLAN.md Phase 0. It exists to be the reward signal for the optimizer, which sets every
design constraint: no CAD dependency (so it runs inside FreeCAD's Python as well as on the host),
no solver install (so the loop has no setup that can rot), no meshing step (so an optimizer
iteration cannot fail to mesh).

    from constraint_kit.fea import Grid, Model, load_stl, occupancy_from_triangles

    tri = load_stl("stl/P22_DriveShell.stl")
    grid = occupancy_from_triangles(tri, pitch=1.5)
    res = (Model(grid, E=2400.0, nu=0.36)            # PETG, MPa
           .fix(lambda p: p[:, 2] <= grid.origin[2] + 1e-9)
           .load_face("z", "max", (0.0, 0.0, -150.0))
           .solve())
    print(res.max_displacement, res.von_mises_percentile(99))

Graduate to Gmsh + CalculiX for a final number on a shipping part, and to JAX-FEM if gradients
are ever needed; see PLAN.md section 4 for why neither is the inner loop.
"""
from .convergence import Convergence, ConvergenceError, richardson
from .hex8 import elasticity_matrix, hex8_b_centre, hex8_stiffness
from .sample import load_stl, occupancy_from_shape, occupancy_from_triangles
from .voxel import Grid, Model, Result, SolveFailure

__all__ = [
    "Convergence",
    "ConvergenceError",
    "Grid",
    "Model",
    "Result",
    "SolveFailure",
    "elasticity_matrix",
    "hex8_stiffness",
    "hex8_b_centre",
    "load_stl",
    "occupancy_from_triangles",
    "occupancy_from_shape",
    "richardson",
]
