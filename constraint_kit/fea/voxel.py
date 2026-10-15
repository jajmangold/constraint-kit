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

    def load(self, where, force: tuple[float, float, float]) -> "Model":
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
        self._force[kept] += np.asarray(force, dtype=float) / len(kept)
        return self

    def load_face(self, axis: str, side: str, force: tuple[float, float, float]) -> "Model":
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
        self._force += np.outer(weight / total, np.asarray(force, dtype=float))
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
        grid = self.grid
        active = self._active()
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
        if not np.any(self._force):
            raise SolveFailure("no loads applied: the solution is trivially zero")

        K, reduce_map, n_red, keep_flat = self._assemble(active)
        f = self._force.ravel()[keep_flat]

        if np.allclose(f, 0.0):
            raise SolveFailure(
                "every loaded node is restrained or outside the material -- "
                "the reduced load vector is zero")

        chosen = method
        if chosen == "auto":
            chosen = "direct" if n_red <= DIRECT_DOF_LIMIT else "cg"

        if chosen == "direct":
            u_red = spla.spsolve(K.tocsc(), f)
            if not np.all(np.isfinite(u_red)):
                raise SolveFailure(
                    "direct solve produced non-finite displacements, which means the reduced "
                    "system is singular -- the restraints do not remove all rigid-body motion")
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

        nf = float(np.linalg.norm(f))
        residual = float(np.linalg.norm(K @ u_red - f) / nf) if nf else 0.0
        if residual > residual_tol:
            raise SolveFailure(
                f"solve returned a relative residual of {residual:.3e}, above the "
                f"{residual_tol:.0e} limit -- treat the displacement field as unusable")

        u_full = np.zeros(grid.n_nodes * 3)
        u_full[keep_flat] = u_red
        displacement = u_full.reshape(-1, 3)
        compliance = float(f @ u_red)
        stress = self._stress(active, u_full)

        return Result(
            displacement=displacement,
            compliance=compliance,
            active_elements=active,
            von_mises=self._von_mises_of(stress),
            n_dof=n_red,
            residual=residual,
            method=chosen,
            applied_force=tuple(float(v) for v in self._force.sum(axis=0)),
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
