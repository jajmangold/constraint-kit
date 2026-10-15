# -*- coding: utf-8 -*-
"""Linear (eigenvalue) buckling on the voxel grid (issue #1).

**Why this matters more now than it used to: the optimizer will find it.** "The lightest design
that passes stress and deflection" pushes straight at thin walls and slender columns, and a
linear static solve is perfectly content with a strut that would fold the instant it was loaded.
A stress check cannot see buckling at all -- the member sits nowhere near its allowable right up
until it collapses -- so without this the optimizer is free to return a design that fails in a
mode nothing was watching.

The method: solve the static problem, build the geometric stiffness from the resulting stress
state, and find the smallest `lambda` at which `K + lambda*Kg` stops being positive definite.
Because `Kg` is linear in the stress, that `lambda` is a load **factor**: 1.0 means the applied
load is exactly critical, 3.0 means three times it.

**What this is not, stated because the difference gets people hurt:**

* **Linear buckling is an UPPER bound.** It assumes a perfect member, perfectly loaded, made of
  a perfectly linear material. A real printed column with a bowed wall and a slightly eccentric
  load fails below it, often well below. `report()` says this every time rather than leaving it
  to be remembered.
* **It is not post-buckling.** It says when stability is lost, not what happens after.
* **A localised mode is usually an artefact.** A staircased boundary turns individual surface
  voxels into tiny cantilevers that buckle at absurd factors while the structure is fine. So
  localisation is measured and a suspect mode is flagged, because the alternative is quietly
  reporting the buckling load of one voxel as the buckling load of the machine.

The eigensolver is **subspace iteration in pure numpy**, which is both the method conventional
buckling solvers have used for forty years and the only one that runs everywhere this has to.
ARPACK was tried twice and failed twice, for two unrelated environmental reasons that are
recorded at `_largest_eigenpairs` so nobody repeats the afternoon: scipy's generalised symmetric
mode recurses on a supplied `Minv` LinearOperator, and inside FreeCAD `eigs` dies before its
first matvec because the bundled numpy has a recursive `dtype.__repr__`. One code path that
works in both interpreters beats a fast path that works in one.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla

from .hex8 import geometric_stiffness
from .voxel import ASSEMBLY_CHUNK, Model, SolveFailure

# A mode whose energy is this concentrated in its busiest 5% of nodes is reported as suspect.
# A clean global column mode measures well under 0.2; a single-voxel artefact approaches 1.0.
LOCALISATION_SUSPECT = 0.5

# Subspace iteration: sweeps allowed, and the relative movement of the wanted eigenvalues below
# which they count as settled. 1e-8 is far tighter than any engineering need and cheap to reach
# once the subspace has found the right modes; the sweep cap is what makes a stuck problem fail
# rather than hang.
SUBSPACE_MAX_SWEEPS = 300
SUBSPACE_TOL = 1e-8

# Factors above this are reported as "not a buckling concern" rather than as a number. At a
# thousand times the design load the member yields, melts or is hit by something long before
# stability is the question, and quoting four significant figures of it invites false precision.
IRRELEVANT_FACTOR = 1.0e3


class BucklingFailure(RuntimeError):
    """No critical load factor could be found, or the one found is not a result.

    Raised rather than returned, because every alternative is worse. A structure in net tension
    has no positive factor at all and "it does not buckle" is the honest answer; a negative
    factor means it would buckle under the REVERSED load, which is a different question than the
    one asked.
    """


@dataclass(frozen=True)
class Buckling:
    """Critical load factors and their mode shapes, with the doubt attached."""

    factors: list                  # ascending, positive, multiples of the APPLIED load
    modes: np.ndarray              # (n_modes, n_nodes, 3)
    localisation: list             # per mode: energy fraction in the busiest 5% of nodes
    suspect: list                  # indices of modes that look like meshing artefacts
    n_dof: int
    applied_force: tuple

    @property
    def critical_factor(self) -> float:
        return float(self.factors[0]) if self.factors else float("inf")

    def report(self) -> str:
        lines = ["linear buckling on %d DOF, load applied %s N"
                 % (self.n_dof, tuple(round(v, 3) for v in self.applied_force)),
                 "  %-6s %14s %14s" % ("mode", "load factor", "localisation")]
        for i, factor in enumerate(self.factors):
            flag = "  <- SUSPECT, looks like a meshing artefact" if i in self.suspect else ""
            shown = ">%.0f" % IRRELEVANT_FACTOR if factor > IRRELEVANT_FACTOR else "%.3f" % factor
            lines.append("  %-6d %14s %14.3f%s"
                         % (i + 1, shown, self.localisation[i], flag))
        first = self.critical_factor
        if first > IRRELEVANT_FACTOR:
            lines.append("  Not a buckling concern: the critical load is more than %.0f times "
                         "the applied one, and the member will fail some other way first."
                         % IRRELEVANT_FACTOR)
        elif first < 1.0:
            lines.append("  FAILS: the applied load is ABOVE critical by a factor of %.2f. This "
                         "member is unstable as loaded." % (1.0 / first))
        else:
            lines.append("  the applied load is %.2fx below critical" % first)
        if self.suspect:
            lines.append("  Mode(s) %s are localised in a few nodes. A staircased boundary turns "
                         "surface voxels into tiny cantilevers that buckle early while the "
                         "structure is fine -- refine the pitch and see whether the factor moves "
                         "before believing it."
                         % ", ".join(str(i + 1) for i in self.suspect))
        lines.append("  LINEAR buckling is an UPPER BOUND. It assumes a perfect member,")
        lines.append("  perfectly loaded. A real part has imperfections -- a bowed wall, an")
        lines.append("  eccentric load, a weak layer line -- and fails below this, often well")
        lines.append("  below. Keep a factor of two or more on anything slender.")
        return "\n".join(lines)


def _assemble_geometric(model: Model, active: np.ndarray, stress: np.ndarray,
                        reduce_map: np.ndarray, n_red: int):
    """Assemble the reduced geometric stiffness on the SAME DOF map as `K`.

    The same map matters: the eigenproblem pairs the two matrices row for row, and a geometric
    stiffness built on its own numbering would produce eigenvalues that look entirely plausible
    and mean nothing.
    """
    grid = model.grid
    nodes_all = grid.element_nodes(active)
    rho = grid.density.ravel()[active]
    stiff = np.maximum(rho, model.density_floor) ** model.penal

    rows, cols, vals = [], [], []
    for start in range(0, len(active), ASSEMBLY_CHUNK):
        stop = start + min(ASSEMBLY_CHUNK, len(active) - start)
        nodes = nodes_all[start:stop]
        edof = (nodes[:, :, None] * 3 + np.arange(3)[None, None, :]).reshape(len(nodes), 24)
        red = reduce_map[edof]
        for local, element in enumerate(range(start, stop)):
            KG = geometric_stiffness(grid.pitch, stress[element]) * stiff[element]
            r = np.repeat(red[local], 24)
            c = np.tile(red[local], 24)
            v = KG.ravel()
            ok = (r >= 0) & (c >= 0)
            rows.append(r[ok].astype(np.int32))
            cols.append(c[ok].astype(np.int32))
            vals.append(v[ok])

    return sp.coo_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))),
                         shape=(n_red, n_red)).tocsr()


def _inverse_applier(K: sp.spmatrix, n_red: int):
    """`K^-1` applied to a BLOCK of right-hand sides, by whatever route works here.

    A block rather than a vector because subspace iteration always wants several at once, and a
    direct factorisation solves them together for very little more than one. The iterative path
    has to loop, which is the real cost of running inside FreeCAD -- but it runs, which the
    compiled path does not (the bundled SuperLU is built against a different numpy ABI).

    The CG tolerance is tight on purpose: it is inside an eigenvalue iteration, where a sloppy
    inner solve does not merely slow convergence, it changes the operator being analysed.
    """
    from .voxel import _preconditioner, direct_solver_available

    if direct_solver_available():
        try:
            lu = spla.splu(K.tocsc())
            return (lambda rhs: np.asarray(lu.solve(np.asfortranarray(rhs)))), "direct"
        except Exception:                                        # noqa: BLE001
            pass

    M = _preconditioner(K)

    def solve_many(rhs):
        rhs = np.atleast_2d(rhs)
        out = np.empty_like(rhs, dtype=float)
        for j in range(rhs.shape[1]):
            u, info = spla.cg(K, rhs[:, j], rtol=1e-11, maxiter=20_000, M=M)
            if info != 0:
                raise SolveFailure(
                    "the inner CG solve inside the eigenvalue iteration did not converge "
                    "(info=%d) on column %d; the system is probably near-singular" % (info, j))
            out[:, j] = u
        return out

    return solve_many, "cg"


def _largest_eigenpairs(B, K, n_red: int, want: int, seed, tol: float,
                        quotient: float) -> tuple:
    """Largest `mu` of `B phi = mu K phi`, by SUBSPACE ITERATION in pure numpy.

    **Why not ARPACK.** Two attempts failed for two unrelated reasons, both environmental, and
    both worth recording so nobody spends the afternoon again.

    `eigsh(B, M=K, Minv=...)` -- scipy's generalised symmetric mode re-wraps a supplied `Minv`
    and, with a LinearOperator in that slot, recursed until the interpreter gave up:
    `maximum recursion depth exceeded`, from an eigensolver, on a correctly posed problem.

    `eigs` on the collapsed operator `x -> K^-1 B x` -- which is the right reformulation, since
    its eigenvalues ARE the pencil's -- dies inside FreeCAD before a single matvec.
    FreeCAD 1.0 bundles a numpy whose `numpy.core._dtype.__repr__` recurses through
    `issubdtype`, and scipy's ARPACK wrapper formats a dtype on the way in. Nothing to do with
    this code, and nothing this code can fix.

    So: **subspace iteration**, which needs nothing but numpy, a sparse matvec, and a linear
    solve. It is also the method conventional buckling solvers have used for forty years, and it
    suits this problem exactly. A block of vectors is pushed through `K^-1 B`, re-orthogonalised,
    and the small projected pencil is solved exactly by Rayleigh-Ritz; the block converges to the
    dominant invariant subspace. **Near-degenerate modes are handled naturally**, which matters
    here because a square column has two almost identical first modes and single-vector power
    iteration would thrash between them.
    """
    solve_many, how = _inverse_applier(K, n_red)
    block = min(n_red - 1, max(want + 4, 2 * want))

    rng = np.random.default_rng(0)                    # fixed: a design verdict is reproducible
    X = rng.normal(size=(n_red, block))
    if seed is not None and np.any(seed):
        X[:, 0] = seed

    previous = None
    for sweep in range(SUBSPACE_MAX_SWEEPS):
        Y = solve_many(B @ X)
        if not np.all(np.isfinite(Y)):
            raise BucklingFailure(
                "the subspace iteration produced non-finite values on %d DOF via %s; the "
                "stiffness matrix is probably singular, which means the restraints do not "
                "remove all rigid-body motion" % (n_red, how))
        # Plain QR first, purely to keep the projected matrices well conditioned. It is not the
        # K-orthogonalisation -- Rayleigh-Ritz below does that exactly, in the small space.
        Q, _ = np.linalg.qr(Y)
        As = Q.T @ (B @ Q)
        Bs = Q.T @ (K @ Q)
        As = 0.5 * (As + As.T)
        Bs = 0.5 * (Bs + Bs.T)
        try:
            L = np.linalg.cholesky(Bs)
        except np.linalg.LinAlgError as exc:
            raise BucklingFailure(
                "the projected stiffness lost positive-definiteness after %d sweep(s), so the "
                "iteration subspace has collapsed. Ask for fewer modes." % sweep) from exc
        Linv = np.linalg.inv(L)
        C = Linv @ As @ Linv.T
        values, vectors = np.linalg.eigh(0.5 * (C + C.T))
        order = np.argsort(-values)
        values, vectors = values[order], vectors[:, order]
        X = Q @ (Linv.T @ vectors)

        top = values[:want]
        if previous is not None:
            moved = np.abs(top - previous) / np.maximum(np.abs(top), 1e-30)
            if float(moved.max()) <= max(tol, SUBSPACE_TOL):
                return top, X[:, :want], "%s, %d sweeps" % (how, sweep + 1)
        previous = top

    raise BucklingFailure(
        "subspace iteration did not settle in %d sweeps on %d DOF via %s. A positive factor does "
        "exist -- a probe found a Rayleigh quotient of %.3e, so a critical factor near %.3g -- "
        "but the modes are still moving. Coarsen the grid or ask for fewer modes."
        % (SUBSPACE_MAX_SWEEPS, n_red, how, quotient, 1.0 / quotient))


def _best_rayleigh(B, K, keep_flat: np.ndarray, static, n_red: int) -> tuple:
    """The largest `(v'Bv)/(v'Kv)` over a few probe vectors, and the vector that achieved it.

    A positive value PROVES a positive critical factor exists, because the pencil's largest
    eigenvalue is the maximum of this quotient over all v. A non-positive value over every probe
    does not prove the opposite, but on a real structure it is decisive: the static displacement
    field is the first probe, and a member that is going to buckle buckles roughly the way it
    already deflects, so if even that direction is stabilising then the stress state is
    stabilising.
    """
    probes = [static.displacement.ravel()[keep_flat]]
    rng = np.random.default_rng(0)                   # fixed, so the verdict is reproducible
    for _ in range(4):
        probes.append(rng.normal(size=n_red))

    best, best_v = -np.inf, None
    for v in probes:
        norm = float(v @ (K @ v))
        if norm <= 0.0:
            continue
        q = float(v @ (B @ v)) / norm
        if q > best:
            best, best_v = q, v
    return (best if np.isfinite(best) else -np.inf), best_v


def _localisation(mode: np.ndarray) -> float:
    """Fraction of the mode energy carried by the busiest 5% of nodes.

    Energy rather than amplitude, so a node moving twice as far counts four times -- which is
    the right weighting for "is this mode really here, or is it one voxel wobbling".
    """
    energy = np.sum(mode ** 2, axis=1)
    total = float(energy.sum())
    if total <= 0.0:
        return 0.0
    k = max(1, int(round(0.05 * len(energy))))
    top = np.sort(energy)[-k:].sum()
    return float(top / total)


def buckling(model: Model, *, n_modes: int = 1, method: str = "auto",
             tol: float = 0.0) -> Buckling:
    """Critical load factors for the model as restrained and loaded.

    The returned factors multiply the APPLIED load: 1.0 means it is exactly critical. Solve the
    static problem first (this does it internally), because the geometric stiffness is built from
    that stress state and means nothing without it.
    """
    if n_modes < 1:
        raise ValueError(f"n_modes must be at least 1, got {n_modes}")

    static = model.solve(method=method)
    active = model._active()                                     # noqa: SLF001 - same package
    K, reduce_map, n_red, keep_flat = model._assemble(active)    # noqa: SLF001
    if n_modes >= n_red:
        raise BucklingFailure(
            "asked for %d modes from a system with %d degrees of freedom; a generalised "
            "eigensolver needs strictly fewer modes than DOF" % (n_modes, n_red))

    KG = _assemble_geometric(model, active, static.stress, reduce_map, n_red)

    # Posed as `(-Kg) phi = mu K phi` with `mu = 1/lambda`, so the SPD matrix is the one in the
    # mass slot where the solver wants it, and the largest `mu` is the smallest `lambda`. The
    # other way round needs shift-invert on an indefinite matrix, which is both slower and much
    # fussier about a near-singular shift.
    B = (-KG).tocsr()

    # Does a positive factor exist AT ALL? Asked before the eigensolve, not after.
    #
    # The largest eigenvalue of the pencil is the maximum of the Rayleigh quotient
    # `(v'Bv)/(v'Kv)`, so any vector with a positive quotient proves a positive factor exists.
    # Probing a handful -- the static displacement field first, since a structure tends to buckle
    # roughly the way it already deflects -- answers the question cheaply, and the best probe
    # then seeds ARPACK, which converges markedly faster from a good start.
    #
    # It also stops the tension case being reported as an ARPACK failure. A member in net tension
    # has no positive factor, and ARPACK hunting for one that does not exist runs to its
    # iteration limit and raises something about convergence -- which reads as "the solver broke"
    # when the truth is "it does not buckle", an entirely different and much better answer.
    quotient, seed = _best_rayleigh(B, K, keep_flat, static, n_red)
    if quotient <= 0.0:
        raise BucklingFailure(
            "no positive critical load factor exists for this model. The structure does not "
            "buckle under this load -- a net applied force of %s N puts it in tension, and a "
            "member in tension is stiffened by its own stress rather than destabilised by it. "
            "Reverse the load to ask the other question."
            % (tuple(round(v, 3) for v in static.applied_force),))

    mu, vec, how = _largest_eigenpairs(B, K, n_red, min(n_modes + 4, n_red - 1), seed, tol,
                                       quotient)
    order = np.argsort(-mu)                                      # largest mu first
    mu, vec = mu[order], vec[:, order]

    factors, modes = [], []
    for value, column in zip(mu, vec.T):
        if value <= 0.0:
            continue                                             # lambda <= 0: reversed load
        factors.append(1.0 / float(value))
        full = np.zeros(model.grid.n_nodes * 3)
        full[keep_flat] = column
        modes.append(full.reshape(-1, 3))
        if len(factors) == n_modes:
            break

    if not factors:
        total = static.applied_force
        raise BucklingFailure(
            "no positive critical load factor exists for this model. The structure does not "
            "buckle under this load -- a net applied force of %s N puts it in tension, and a "
            "member in tension is stiffened by its own stress rather than destabilised by it. "
            "Reverse the load to ask the other question."
            % (tuple(round(v, 3) for v in total),))

    localisation = [_localisation(m) for m in modes]
    suspect = [i for i, value in enumerate(localisation) if value >= LOCALISATION_SUSPECT]
    return Buckling(factors=factors, modes=np.array(modes), localisation=localisation,
                    suspect=suspect, n_dof=n_red, applied_force=static.applied_force)
