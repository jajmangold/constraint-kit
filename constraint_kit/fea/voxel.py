# -*- coding: utf-8 -*-
"""Linear elastostatics on a voxel occupancy grid.

Why voxels rather than a body-fitted tetrahedral mesh: this solver is the inner loop of an
optimizer, so it has to run hundreds of times without a meshing step that can fail. A structured
grid cannot fail to mesh, every element shares one stiffness matrix, and the occupancy array is
exactly what a topology optimizer wants to work on anyway. The cost is a stair-stepped boundary,
which is why `von_mises` returns element-centre values and why surface stress peaks off a voxel
model should be read as "this region is hot", never as a number to quote.

Scope, stated so nobody builds on sand: linear static, small strain, isotropic, homogeneous,
homogeneous Dirichlet (u = 0) restraints, nodal loads. No contact, no plasticity, no buckling,
no prescribed non-zero displacement. Those are real gaps, not oversights -- buckling in
particular matters for the gear-cutter's Z column and is the obvious next addition.

Units are whatever you feed it, consistently. mm / N / MPa throughout the machine projects.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla

from .hex8 import NODE_OFFSET, elasticity_matrix, hex8_b_centre, hex8_stiffness

# Above this many reduced DOF, a sparse direct factorisation of a 3D elasticity operator starts
# costing more memory than it is worth; switch to preconditioned CG. Measured on the knee-exo
# gantry plate (bulky, not slender -- the worst case for fill-in): direct beat Jacobi-CG at every
# size it could handle, 1.0 s vs 4.2 s at 25k DOF and 7.8 s vs 16.8 s at 72k, and both agreed to
# six significant figures. Note this threshold cannot be right for all shapes: a slender beam
# factorises almost banded and stays cheap far past here, while a cube is the opposite.
DIRECT_DOF_LIMIT = 80_000

# Cap on CG iterations. Generous but finite, and emphatically not a multiple of the DOF count:
# at 100k DOF a "20 * n" default means two million iterations, which is indistinguishable from a
# hang when anything upstream is wrong.
CG_MAX_ITER = 30_000
ASSEMBLY_CHUNK = 16_384  # elements per triplet batch; 16k -> ~150 MB transient


_DIRECT_OK: bool | None = None


def direct_solver_available() -> bool:
    """Is scipy's sparse DIRECT solver usable in THIS interpreter? Probed once, then cached.

    It is not, inside FreeCAD 1.0.2. That bundle pairs numpy 1.26.4 with a scipy whose compiled
    SuperLU extension was built against a different numpy ABI, so `spsolve`, `factorized` and
    `splu` all fail with the memorable `argument 3 must be numpy.ndarray, not numpy.ndarray` --
    two distinct ndarray types in one process. Pure-Python iterative solvers and sparse matvec
    are unaffected, because they never cross that boundary.

    This lives HERE rather than only in the workbench bridge because `method="auto"` has to
    respect it. The bridge knew and passed `method="cg"` explicitly, so the bridge worked -- and
    every other caller of `auto` inside FreeCAD was broken, which is exactly how the buckling
    solver failed the first time it ran in there. A capability check that only one caller knows
    about is a trap for the next one.

    Probed rather than hardcoded on "are we in FreeCAD", so a future bundle shipping a matched
    pair gets the faster path with no code change.
    """
    global _DIRECT_OK
    if _DIRECT_OK is None:
        try:
            spla.spsolve(sp.csc_matrix(np.eye(3)), np.ones(3))
            _DIRECT_OK = True
        except Exception:  # noqa: BLE001 - any failure at all means do not use it
            _DIRECT_OK = False
    return _DIRECT_OK


class SolveFailure(RuntimeError):
    """The linear solve did not converge, or converged to something that is not a solution.

    Raised rather than returning the iterate, because a half-converged displacement field looks
    entirely plausible when plotted and will quietly poison an optimizer for hours.
    """


@dataclass(frozen=True)
class Grid:
    """Occupancy on a uniform rectangular lattice.

    `density` is float in [0, 1] with shape (nx, ny, nz). Binary occupancy is the common case;
    intermediate values exist so a SIMP topology optimizer can share this assembly path.
    """

    density: np.ndarray
    pitch: tuple[float, float, float]
    origin: tuple[float, float, float] = (0.0, 0.0, 0.0)

    def __post_init__(self) -> None:
        d = np.asarray(self.density, dtype=float)
        if d.ndim != 3:
            raise ValueError(f"density must be a 3-D array, got shape {d.shape}")
        if d.size == 0:
            raise ValueError("density grid is empty")
        if np.any(d < 0.0) or np.any(d > 1.0):
            raise ValueError("density must lie in [0, 1]")
        if min(self.pitch) <= 0.0:
            raise ValueError(f"pitch must be positive in every axis, got {self.pitch}")
        object.__setattr__(self, "density", d)

    @property
    def shape(self) -> tuple[int, int, int]:
        return self.density.shape  # type: ignore[return-value]

    @property
    def node_shape(self) -> tuple[int, int, int]:
        nx, ny, nz = self.shape
        return nx + 1, ny + 1, nz + 1

    @property
    def n_nodes(self) -> int:
        return int(np.prod(self.node_shape))

    @property
    def element_volume(self) -> float:
        return float(np.prod(self.pitch))

    @property
    def material_volume(self) -> float:
        """Volume of material, counting partial densities. Same units cubed as `pitch`."""
        return float(self.density.sum()) * self.element_volume

    def node_coords(self) -> np.ndarray:
        """(n_nodes, 3) physical coordinates, in the same C-order as node ids."""
        nx, ny, nz = self.node_shape
        hx, hy, hz = self.pitch
        ox, oy, oz = self.origin
        i, j, k = np.meshgrid(np.arange(nx), np.arange(ny), np.arange(nz), indexing="ij")
        return np.stack([ox + i.ravel() * hx, oy + j.ravel() * hy, oz + k.ravel() * hz], axis=1)

    def element_centres(self) -> np.ndarray:
        """(n_elements, 3) centroids in the same C-order as `density.ravel()`."""
        nx, ny, nz = self.shape
        hx, hy, hz = self.pitch
        ox, oy, oz = self.origin
        i, j, k = np.meshgrid(np.arange(nx), np.arange(ny), np.arange(nz), indexing="ij")
        return np.stack([ox + (i.ravel() + 0.5) * hx,
                         oy + (j.ravel() + 0.5) * hy,
                         oz + (k.ravel() + 0.5) * hz], axis=1)

    def element_nodes(self, elements: np.ndarray) -> np.ndarray:
        """(m, 8) node ids for the given flat element indices."""
        nx, ny, nz = self.shape
        ei, ej, ek = np.unravel_index(np.asarray(elements), (nx, ny, nz))
        nodes = np.empty((len(ei), 8), dtype=np.int64)
        for n, (ox, oy, oz) in enumerate(NODE_OFFSET):
            nodes[:, n] = np.ravel_multi_index(
                (ei + ox, ej + oy, ek + oz), self.node_shape)
        return nodes


@dataclass
class Result:
    """Outcome of one solve. Every field is in the input unit system."""

    displacement: np.ndarray        # (n_nodes, 3)
    compliance: float               # f . u  -- twice the strain energy
    active_elements: np.ndarray     # flat indices of elements that carried stiffness
    von_mises: np.ndarray           # (n_active,) element-centre equivalent stress
    n_dof: int                      # size of the system actually solved
    residual: float                 # ||K u - f|| / ||f||, reported not assumed
    method: str
    # The force actually written into the reduced system, summed over every node. Not the force
    # the caller asked for -- the two differ whenever a selector catches a node the solve then
    # discards, which is exactly how a cantilever once read a third of its true deflection with
    # a residual of 9e-10. Reporting it is how a cross-check can tell a solver disagreement
    # apart from two solvers being handed different problems.
    applied_force: tuple = (0.0, 0.0, 0.0)
    # The full element-centre tensor, (n_active, 6) as (sxx, syy, szz, sxy, syz, szx). von Mises
    # is derived from it rather than the other way round, so the two cannot drift apart.
    stress: np.ndarray = None    # type: ignore[assignment]

    @property
    def max_displacement(self) -> float:
        """Largest nodal displacement magnitude."""
        if self.displacement.size == 0:
            return 0.0
        return float(np.linalg.norm(self.displacement, axis=1).max())

    @property
    def peak_von_mises(self) -> float:
        return float(self.von_mises.max()) if self.von_mises.size else 0.0

    def von_mises_percentile(self, q: float = 99.0) -> float:
        """A percentile of element stress, which is what to quote off a voxel model.

        The absolute peak sits on a stair-step corner and is a meshing artefact as much as a
        structural fact; the 99th percentile moves with the structure instead.
        """
        if self.von_mises.size == 0:
            return 0.0
        return float(np.percentile(self.von_mises, q))


@dataclass(frozen=True)
class Envelope:
    """Several load cases solved on one matrix, and the field that bounds all of them.

    The envelope is an **elementwise** maximum over the cases, and that is the whole reason this
    class exists rather than a loop over `solve()`. A real part is governed in one region by one
    load and in another region by a different one, so the design field exists in none of the
    individual cases. `max(case.peak for case in cases)` would answer a different and easier
    question, and `governing_case_per_element` is here so the distinction stays visible.

    Displacement is reported per case rather than enveloped, deliberately: an envelope of
    displacement magnitudes is not a displacement field of anything, and quoting it as one would
    invite reading a shape off it that no load produces.
    """

    cases: dict                  # name -> Result, in declaration order
    von_mises: np.ndarray        # (n_active,) elementwise max over every case
    active_elements: np.ndarray
    n_dof: int
    method: str

    def _stacked(self) -> np.ndarray:
        return np.stack([r.von_mises for r in self.cases.values()])

    def governing_case_per_element(self) -> np.ndarray:
        """Which case drives each element. An array of names, so it can be read directly."""
        names = np.array(list(self.cases), dtype=object)
        return names[self._stacked().argmax(axis=0)]

    @property
    def peak_von_mises(self) -> float:
        return float(self.von_mises.max()) if self.von_mises.size else 0.0

    def von_mises_percentile(self, q: float = 99.0) -> float:
        """A percentile of the ENVELOPE, for the same reason as on a single result."""
        if self.von_mises.size == 0:
            return 0.0
        return float(np.percentile(self.von_mises, q))

    @property
    def max_displacement(self) -> float:
        return max((r.max_displacement for r in self.cases.values()), default=0.0)

    @property
    def worst_case_for_displacement(self) -> str:
        return max(self.cases, key=lambda n: self.cases[n].max_displacement)

    @property
    def worst_case_for_stress(self) -> str:
        return max(self.cases, key=lambda n: self.cases[n].von_mises_percentile(99.0))

    def governing(self, *, allowable: float, q: float = 99.0) -> tuple:
        """`(case name, safety factor)` from the ENVELOPE, not from any single case.

        The safety factor is the envelope's, so it is never better than the worst case — a part
        is safe only if it is safe everywhere under every load it will actually see. The name is
        the case that contributes most of it, which is what to go and look at.
        """
        if allowable <= 0.0:
            raise ValueError(f"allowable must be positive, got {allowable}")
        demand = self.von_mises_percentile(q)
        sf = float("inf") if demand <= 0.0 else allowable / demand
        return self.worst_case_for_stress, sf

    def report(self, *, allowable: float | None = None, q: float = 99.0) -> str:
        """The whole envelope as ASCII text, naming each case and what it governs."""
        lines = ["%d load case(s) on one %d-DOF matrix (%s)"
                 % (len(self.cases), self.n_dof, self.method),
                 "  %-22s %10s %10s %10s" % ("case", "defl mm", "p%.0f MPa" % q, "peak MPa")]
        for name, res in self.cases.items():
            lines.append("  %-22s %10.4f %10.2f %10.2f"
                         % (name[:22], res.max_displacement,
                            res.von_mises_percentile(q), res.peak_von_mises))
        lines.append("  %-22s %10s %10.2f %10.2f"
                     % ("ENVELOPE", "-", self.von_mises_percentile(q), self.peak_von_mises))

        governing = self.governing_case_per_element()
        counts = {name: int((governing == name).sum()) for name in self.cases}
        total = max(sum(counts.values()), 1)
        share = ", ".join("%s %.0f%%" % (n, 100.0 * c / total)
                          for n, c in sorted(counts.items(), key=lambda kv: -kv[1]) if c)
        lines.append("  governs by element: " + share)
        if len({n for n, c in counts.items() if c}) > 1:
            lines.append("  More than one case governs, so no single case is the design case and "
                         "the envelope is the only field that bounds the part.")
        if allowable is not None:
            name, sf = self.governing(allowable=allowable, q=q)
            lines.append("  envelope SF %.2f against %.0f MPa allowable; %s contributes the peak"
                         % (sf, allowable, name))
            if sf < 1.0:
                lines.append("  FAILS: the envelope exceeds the allowable.")
        return "\n".join(lines)


class Model:
    """A grid plus material, restraints and loads."""

    def __init__(self, grid: Grid, E: float, nu: float, *,
                 density_floor: float = 1e-3, penal: float = 3.0):
        """`density_floor` and `penal` implement the SIMP interpolation E_e = (rho^p) * E.

        For a binary occupancy grid both are inert: rho is 0 or 1, and elements below the floor
        are dropped from the system entirely rather than given a token stiffness, which keeps the
        solve small. They exist so a topology optimizer can reuse this assembly unchanged.
        """
        self.grid = grid
        self.E = float(E)
        self.nu = float(nu)
        self.density_floor = float(density_floor)
        self.penal = float(penal)
        self._KE = hex8_stiffness(grid.pitch, self.E, self.nu)
        self._D = elasticity_matrix(self.E, self.nu)
        self._Bc = hex8_b_centre(grid.pitch)
        n = grid.n_nodes
        self._fixed = np.zeros((n, 3), dtype=bool)
        self._force = np.zeros((n, 3), dtype=float)
        # Named load cases, in declaration order. The unnamed `_force` above is NOT one of these:
        # mixing the two is ambiguous (is the unnamed load in every case, or a case of its own?)
        # and `solve_cases` refuses it rather than picking a reading.
        self._cases: dict = {}

    # ---- restraints and loads -------------------------------------------------------------

    def _select_nodes(self, where) -> np.ndarray:
        """Resolve a node selector to an index array.

        Accepts a boolean mask over nodes, an integer index array, or a callable taking the
        (n_nodes, 3) coordinate array and returning a boolean mask -- the callable form is the
        one worth using, because `lambda p: p[:, 0] <= 0.0` says what it means.
        """
        if callable(where):
            mask = np.asarray(where(self.grid.node_coords()))
            if mask.shape != (self.grid.n_nodes,) or mask.dtype != bool:
                raise ValueError(
                    "node selector must return a boolean mask of shape (n_nodes,), got "
                    f"shape {mask.shape} dtype {mask.dtype}")
            idx = np.flatnonzero(mask)
        else:
            arr = np.asarray(where)
            idx = np.flatnonzero(arr) if arr.dtype == bool else arr.astype(np.int64)
        if idx.size == 0:
            raise ValueError("node selector matched no nodes -- check the coordinate bounds")
        return idx

    def fix(self, where, axes: str = "xyz") -> "Model":
        """Restrain selected nodes to zero displacement on the named axes."""
        idx = self._select_nodes(where)
        for a in axes:
            if a not in "xyz":
                raise ValueError(f"axes must be drawn from 'xyz', got {axes!r}")
            self._fixed[idx, "xyz".index(a)] = True
        return self

    def _material_nodes(self) -> np.ndarray:
        """Boolean mask of nodes belonging to at least one element above the density floor.

        Not cached: `Grid.density` is a live array (a topology optimizer mutates it in place),
        and a stale mask here would put load on nodes that no longer carry material.
        """
        active = self._active()
        mask = np.zeros(self.grid.n_nodes, dtype=bool)
        if active.size:
            mask[self.grid.element_nodes(active).ravel()] = True
        return mask

    def case(self, name: str) -> np.ndarray:
        """Declare a load case and return its force vector, creating it if new.

        Declaring one and never loading it is a mistake rather than an empty case, so
        `solve_cases` refuses it by name -- a case that is silently zero would report a safety
        factor of infinity for a load someone meant to apply.
        """
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"a load case name must be a non-empty string, got {name!r}")
        return self._cases.setdefault(name, np.zeros((self.grid.n_nodes, 3), dtype=float))

    def load(self, where, force: tuple[float, float, float], *,
             case: str | None = None) -> "Model":
        """Spread a TOTAL force vector equally over the selected nodes THAT CARRY MATERIAL.

        Equal division is a uniform-traction approximation. It is right for a pad or a flange
        face and wrong for a Hertzian contact; for the machine load cases here -- a cutter
        pushing on a spindle mount, a strap pulling on a cuff -- it is the honest model.

        Nodes with no material around them are excluded before the force is divided, and this is
        the whole point rather than a detail. A grid is padded with empty voxels, so a selector
        like `x >= 59` legitimately catches a layer of empty nodes beyond the part. Those nodes
        are dropped from the reduced system at solve time, so any share of the load given to them
        simply vanishes -- and the solve still converges, cleanly, on a fraction of the force
        that was asked for. Measured when this was wrong: a cantilever reported 0.288 mm against
        0.968 mm of beam theory because 200 N had arrived as 60.5 N. Converged, plausible, and
        wrong by a factor of three is the worst failure this code can produce, so the division is
        over material nodes only and the full requested force always reaches the structure.
        """
        idx = self._select_nodes(where)
        material = self._material_nodes()
        kept = idx[material[idx]]
        if kept.size == 0:
            raise ValueError(
                f"the load selector matched {len(idx)} node(s) but none of them touch material "
                "at this density threshold, so there is nothing to push on -- check the "
                "coordinate bounds against the part's actual extent")
        target = self._force if case is None else self.case(case)
        target[kept] += np.asarray(force, dtype=float) / len(kept)
        return self

    def load_face(self, axis: str, side: str, force: tuple[float, float, float], *,
                  case: str | None = None) -> "Model":
        """Spread a TOTAL force over one outer face of the grid with consistent nodal weights.

        Unlike `load`, this is exact rather than approximate. Uniform pressure on a bilinear
        quad face does not lump into equal nodal forces -- each element face contributes a
        quarter of its area to each of its four nodes, so edge nodes receive half and corner
        nodes a quarter of an interior node's share. Summing that over only the element faces
        that are actually solid handles ragged boundaries for free.

        This matters beyond tidiness: with equal division the exact uniform-strain solution is no
        longer a solution, so a verification test against `PL/AE` could only ever be approximate
        and would hide real formulation errors inside its tolerance.
        """
        if axis not in "xyz":
            raise ValueError(f"axis must be 'x', 'y' or 'z', got {axis!r}")
        if side not in ("min", "max"):
            raise ValueError(f"side must be 'min' or 'max', got {side!r}")
        ax = "xyz".index(axis)
        grid = self.grid
        n_el = grid.shape[ax]
        layer = 0 if side == "min" else n_el - 1

        sel = [slice(None)] * 3
        sel[ax] = slice(layer, layer + 1)
        dens = grid.density[tuple(sel)]
        local = np.flatnonzero(dens.ravel() > self.density_floor)
        if local.size == 0:
            raise ValueError(
                f"no material on the {side} {axis} face -- nothing to load there")
        shape = [grid.shape[0], grid.shape[1], grid.shape[2]]
        shape[ax] = 1
        idx = list(np.unravel_index(local, tuple(shape)))
        idx[ax] = idx[ax] + layer
        elements = np.ravel_multi_index(tuple(idx), grid.shape)

        # Of each solid element's eight nodes, the four on the loaded outer surface take the
        # load; which four is decided by the node offset along `ax`.
        want = 0 if side == "min" else 1
        picks = [n for n, off in enumerate(NODE_OFFSET) if off[ax] == want]
        nodes = grid.element_nodes(elements)[:, picks]

        weight = np.zeros(grid.n_nodes)
        np.add.at(weight, nodes.ravel(), 1.0)
        total = weight.sum()
        target = self._force if case is None else self.case(case)
        target += np.outer(weight / total, np.asarray(force, dtype=float))
        return self

    # ---- assembly -------------------------------------------------------------------------

    def _active(self) -> np.ndarray:
        return np.flatnonzero(self.grid.density.ravel() > self.density_floor)

    def _assemble(self, active: np.ndarray):
        """Build the reduced stiffness matrix and the DOF map in one pass.

        Rows and columns belonging to restrained DOF, or to nodes no active element touches, are
        never emitted -- that is equivalent to eliminating them (valid because restraints are
        homogeneous) and it keeps both the triplet arrays and the factorisation small.
        """
        grid = self.grid
        n_dof_full = grid.n_nodes * 3
        nodes_all = grid.element_nodes(active)

        touched = np.zeros(grid.n_nodes, dtype=bool)
        touched[nodes_all.ravel()] = True
        keep = np.repeat(touched, 3).reshape(-1, 3) & ~self._fixed
        keep_flat = keep.ravel()

        reduce_map = np.full(n_dof_full, -1, dtype=np.int64)
        n_red = int(keep_flat.sum())
        if n_red == 0:
            raise SolveFailure("every degree of freedom is restrained or unused")
        reduce_map[keep_flat] = np.arange(n_red)

        rho = grid.density.ravel()[active]
        stiff = np.maximum(rho, self.density_floor) ** self.penal
        KE_flat = self._KE.ravel()

        rows_acc, cols_acc, vals_acc = [], [], []
        for start in range(0, len(active), ASSEMBLY_CHUNK):
            nodes = nodes_all[start:start + ASSEMBLY_CHUNK]
            edof = (nodes[:, :, None] * 3 + np.arange(3)[None, None, :]).reshape(len(nodes), 24)
            red = reduce_map[edof]                                  # (m, 24)
            r = np.repeat(red, 24, axis=1).reshape(-1)              # row index per KE entry
            c = np.tile(red, (1, 24)).reshape(-1)                   # col index per KE entry
            v = (KE_flat[None, :] * stiff[start:start + len(nodes), None]).reshape(-1)
            ok = (r >= 0) & (c >= 0)
            rows_acc.append(r[ok].astype(np.int32))
            cols_acc.append(c[ok].astype(np.int32))
            vals_acc.append(v[ok])

        K = sp.coo_matrix(
            (np.concatenate(vals_acc),
             (np.concatenate(rows_acc), np.concatenate(cols_acc))),
            shape=(n_red, n_red)).tocsr()
        return K, reduce_map, n_red, keep_flat

    # ---- solve ----------------------------------------------------------------------------

    def solve(self, *, method: str = "auto", tol: float = 1e-9,
              residual_tol: float = 1e-6, maxiter: int | None = None) -> Result:
        """Solve K u = f and report, never assume, the residual."""
        active = self._active()
        self._guard(active)
        if self._cases:
            raise SolveFailure(
                "this model has named load cases (%s); call solve_cases() for them. solve() "
                "would silently analyse none of them." % ", ".join(sorted(self._cases)))
        if not np.any(self._force):
            raise SolveFailure("no loads applied: the solution is trivially zero")

        K, _reduce_map, n_red, keep_flat = self._assemble(active)
        f = self._force.ravel()[keep_flat]

        if np.allclose(f, 0.0):
            raise SolveFailure(
                "every loaded node is restrained or outside the material -- "
                "the reduced load vector is zero")

        chosen = _choose(method, n_red)

        if chosen == "direct":
            u_red = spla.spsolve(K.tocsc(), f)
        elif chosen == "cg":
            M = _preconditioner(K)
            u_red, info = spla.cg(K, f, rtol=tol, maxiter=maxiter or CG_MAX_ITER, M=M)
            if info != 0:
                raise SolveFailure(
                    f"conjugate gradient stopped after {maxiter or CG_MAX_ITER} iterations "
                    f"without reaching rtol={tol:.0e} on {n_red} DOF (info={info}); coarsen the "
                    "grid, loosen tol, or install pyamg for a multigrid preconditioner")
        else:
            raise ValueError(f"method must be 'auto', 'direct' or 'cg', got {method!r}")

        return self._finish(active, keep_flat, K, f, u_red, chosen, residual_tol, self._force)

    def solve_cases(self, *, method: str = "auto", tol: float = 1e-9,
                    residual_tol: float = 1e-6, maxiter: int | None = None) -> "Envelope":
        """Solve every named load case on ONE assembled matrix, and envelope the results.

        A real machine part does not have *a* load. The gantry plate carries belt tension when
        the drive pulls and a reaction when it pushes; the gear cutter sees cutting force one way
        and clamping force another. Analysing one of those and quoting its safety factor is a
        guess dressed as an answer.

        Two things make this more than a loop over `solve()`. **The matrix is assembled and
        factorised once** -- neither depends on the load, and assembly is the expensive part on a
        big grid, so four cases cost barely more than one. And **the envelope is elementwise**,
        which is the engineering point: one region can be governed by one case and another region
        by a different one, so the design field exists in none of the individual cases.
        """
        grid = self.grid
        if self._cases and np.any(self._force):
            raise SolveFailure(
                "this model has named load cases AND an unnamed load. That is ambiguous -- the "
                "unnamed load could belong to every case or be a case of its own, and the two "
                "readings give different answers. Put it in each case explicitly, which also "
                "makes it say so.")

        cases = dict(self._cases)
        if not cases:
            if not np.any(self._force):
                raise SolveFailure("no loads applied in any case: there is nothing to solve")
            cases = {"default": self._force}

        empty = [name for name, f in cases.items() if not np.any(f)]
        if empty:
            raise SolveFailure(
                "load case(s) %s were declared and never loaded. A case that is silently zero "
                "reports a safety factor of infinity for a load somebody meant to apply, so it "
                "is refused rather than solved." % ", ".join(repr(n) for n in empty))

        active = self._active()
        self._guard(active)
        K, _reduce_map, n_red, keep_flat = self._assemble(active)

        chosen = _choose(method, n_red)

        # One factorisation (or one preconditioner) shared by every right-hand side.
        if chosen == "direct":
            lu = spla.splu(K.tocsc())
            back = lu.solve
        elif chosen == "cg":
            M = _preconditioner(K)

            def back(rhs):
                u, info = spla.cg(K, rhs, rtol=tol, maxiter=maxiter or CG_MAX_ITER, M=M)
                if info != 0:
                    raise SolveFailure(
                        f"conjugate gradient stopped after {maxiter or CG_MAX_ITER} iterations "
                        f"without reaching rtol={tol:.0e} on {n_red} DOF (info={info})")
                return u
        else:
            raise ValueError(f"method must be 'auto', 'direct' or 'cg', got {method!r}")

        results = {}
        for name, force in cases.items():
            f = force.ravel()[keep_flat]
            if np.allclose(f, 0.0):
                raise SolveFailure(
                    f"every loaded node in case {name!r} is restrained or outside the material, "
                    "so its reduced load vector is zero")
            u_red = back(f)
            results[name] = self._finish(
                active, keep_flat, K, f, u_red, chosen, residual_tol, force)

        stacked = np.stack([r.von_mises for r in results.values()])
        return Envelope(cases=results, von_mises=stacked.max(axis=0),
                        active_elements=active, n_dof=n_red, method=chosen)

    def _guard(self, active: np.ndarray) -> None:
        """The preconditions shared by `solve` and `solve_cases`, in one place."""
        if active.size == 0:
            raise SolveFailure(
                "no element exceeds the density floor -- the grid is empty at this threshold")
        if not self._fixed.any():
            raise SolveFailure(
                "no restraints applied: the system has six rigid-body modes and is singular")
        if not (self._fixed.any(axis=1) & self._material_nodes()).any():
            raise SolveFailure(
                "every restrained node sits in empty space, so nothing is actually held: the "
                "restraint selector matched only padding outside the part")

    def _finish(self, active, keep_flat, K, f, u_red, chosen, residual_tol, force) -> Result:
        """Turn a solved reduced vector into a Result, residual checked rather than assumed."""
        if not np.all(np.isfinite(u_red)):
            raise SolveFailure(
                "the solve produced non-finite displacements, which means the reduced system is "
                "singular -- the restraints do not remove all rigid-body motion")
        nf = float(np.linalg.norm(f))
        residual = float(np.linalg.norm(K @ u_red - f) / nf) if nf else 0.0
        if residual > residual_tol:
            raise SolveFailure(
                f"solve returned a relative residual of {residual:.3e}, above the "
                f"{residual_tol:.0e} limit -- treat the displacement field as unusable")

        u_full = np.zeros(self.grid.n_nodes * 3)
        u_full[keep_flat] = u_red
        stress = self._stress(active, u_full)
        return Result(
            displacement=u_full.reshape(-1, 3),
            compliance=float(f @ u_red),
            active_elements=active,
            von_mises=self._von_mises_of(stress),
            n_dof=int(len(f)),
            residual=residual,
            method=chosen,
            applied_force=tuple(float(v) for v in force.sum(axis=0)),
            stress=stress,
        )

    def _stress(self, active: np.ndarray, u_full: np.ndarray) -> np.ndarray:
        """Element-centre stress TENSOR for the active elements, as (m, 6).

        Kept rather than collapsed straight to von Mises, because von Mises presumes a material
        with no preferred direction and a printed part is exactly the counter-example that
        matters here: an FDM wall is a stack of welded beads and the weld is the weak direction.
        Whether such a part survives is decided by the traction on its layer plane, which no
        invariant can recover. See `tet.traction_on_plane`.
        """
        nodes = self.grid.element_nodes(active)
        edof = (nodes[:, :, None] * 3 + np.arange(3)[None, None, :]).reshape(len(nodes), 24)
        ue = u_full[edof]                                     # (m, 24)
        eps = ue @ self._Bc.T                                 # (m, 6)
        sig = eps @ self._D.T                                 # (m, 6)
        # SIMP consistency: an element at density rho carries rho^p of the stiffness, so the
        # stress it actually sees is scaled the same way. Inert for binary occupancy.
        rho = self.grid.density.ravel()[active]
        sig *= (np.maximum(rho, self.density_floor) ** self.penal)[:, None]
        return sig

    @staticmethod
    def _von_mises_of(sig: np.ndarray) -> np.ndarray:
        sx, sy, sz, txy, tyz, tzx = sig.T
        return np.sqrt(0.5 * ((sx - sy) ** 2 + (sy - sz) ** 2 + (sz - sx) ** 2)
                       + 3.0 * (txy ** 2 + tyz ** 2 + tzx ** 2))


def _choose(method: str, n_red: int) -> str:
    """Resolve `method` for this system in this interpreter.

    `auto` wants the direct solver while the system is small enough for it to be the faster
    choice -- and only if the direct solver actually works here, which inside FreeCAD it does
    not.
    """
    if method != "auto":
        if method not in ("direct", "cg"):
            raise ValueError(f"method must be 'auto', 'direct' or 'cg', got {method!r}")
        return method
    if n_red > DIRECT_DOF_LIMIT:
        return "cg"
    return "direct" if direct_solver_available() else "cg"


def _preconditioner(K: sp.spmatrix):
    """Jacobi (diagonal) preconditioner.

    Deliberately NOT incomplete-LU, which is the obvious thing to reach for and is wrong here:
    conjugate gradients requires a SYMMETRIC positive-definite preconditioner, and an ILU gives
    M = (LU)^-1, which is not symmetric. Supplying one breaks the Krylov orthogonality that CG
    depends on, and it fails the way broken preconditioners do -- not with an error, but by
    stagnating. Measured on the knee-exo gantry plate at 14k DOF: ILU-CG ran 5000 iterations in
    42 s and finished at a relative residual of 8e-3, while Jacobi-CG converged to 1e-9 in 1.6 s.

    Jacobi is a weak preconditioner and the iteration count does grow with slenderness, but it
    is valid, it costs one divide per DOF, and it never allocates. A genuinely better symmetric
    option is algebraic multigrid (pyamg), which is not a dependency here; incomplete Cholesky
    would also serve but scipy does not ship one.
    """
    d = K.diagonal().copy()
    d[d == 0.0] = 1.0
    return spla.LinearOperator(K.shape, matvec=lambda x: x / d)
