"""Mass properties (T4.1) — exact centre-of-gravity + inertia tensor for a built `cq.Assembly`,
DENSITY-WEIGHTED, via OCC `GProp` over the same world-flatten used for interference (`validate._world_parts`).

For each world-placed leaf: OCC volume properties are accumulated into one system with
`GProp_GProps.Add(g, density)` so heavier materials pull the CG correctly. OCC's `Add` applies Huygens
internally, and `MatrixOfInertia()` returns the inertia **about the combined centre of mass** (verified
empirically vs the analytic box/cylinder), so we report it directly + its principal moments (eigenvalues
of the symmetric tensor). No manual parallel-axis shift is needed.

Units: density g/mm³ · volume mm³ → mass g; inertia g·mm². Geometry is truth — validated analytically
against a box and a cylinder in the test suite (NOT the VLM)."""
from __future__ import annotations

from .parts import DENSITY_G_MM3
from .validate import _world_parts


def mass_properties(cq_assembly, density_of=None) -> dict:
    """CG + inertia of a built assembly. `density_of` maps a world-leaf name (as produced by
    `_world_parts`) to its density g/mm³; a dict or a callable. Missing/None → steel. Returns
    {mass_g, cg[3], inertia_about_cg[3][3], principal_moments[3], n_parts}."""
    import numpy as np
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps

    steel = DENSITY_G_MM3["steel"]

    def rho_of(name: str) -> float:
        if density_of is None:
            return steel
        if not isinstance(density_of, dict):
            return density_of(name)
        # world names carry wrapping prefixes (e.g. the root assembly's auto-name); match the density
        # key by stripping leading path segments (longest suffix first) so keys are wrap-position agnostic.
        segs = name.split("/")
        for i in range(len(segs)):
            key = "/".join(segs[i:])
            if key in density_of:
                return density_of[key]
        return steel

    total = GProp_GProps()                       # reference point = origin
    n = 0
    for name, shape in _world_parts(cq_assembly):
        g = GProp_GProps()
        BRepGProp.VolumeProperties_s(shape.wrapped, g)
        total.Add(g, rho_of(name))
        n += 1
    if n == 0:
        return {"mass_g": 0.0, "cg": [0.0, 0.0, 0.0], "inertia_about_cg": None,
                "principal_moments": None, "n_parts": 0}

    m_total = total.Mass()
    c = total.CentreOfMass()
    cg = np.array([c.X(), c.Y(), c.Z()])
    mat = total.MatrixOfInertia()                 # OCC: already about the combined centre of mass
    i_cg = np.array([[mat.Value(i, j) for j in (1, 2, 3)] for i in (1, 2, 3)])
    pm = np.linalg.eigvalsh(i_cg)                 # principal moments (symmetric tensor)
    return {"mass_g": round(float(m_total), 4),
            "cg": [round(float(x), 4) for x in cg],
            "inertia_about_cg": [[round(float(i_cg[i, j]), 3) for j in range(3)] for i in range(3)],
            "principal_moments": [round(float(x), 3) for x in pm], "n_parts": n}
