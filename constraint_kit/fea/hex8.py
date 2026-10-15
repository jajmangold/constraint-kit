# -*- coding: utf-8 -*-
"""Trilinear 8-node hexahedron for linear elastostatics.

Deliberately numpy-only: this module is the reward signal for the optimizer, so it must run
without a CAD stack, without a solver install, and inside FreeCAD's bundled Python.

The element stiffness is built by 2x2x2 Gauss quadrature rather than pasted from a closed-form
24x24 table. Quadrature is longer but it is checkable -- `hex8_stiffness` is verified in the test
suite by three properties that a wrong formulation cannot satisfy by accident: exact symmetry,
exactly six zero eigenvalues (the rigid-body modes), and exact reproduction of a uniform strain
field. A hardcoded table is verifiable only against the source it was copied from.

Known limitation, stated because it decides mesh sizing: full integration makes this element
stiff in bending (parasitic shear / shear locking). A single element through the thickness of a
beam under-predicts deflection badly. Use >= 6 elements through any bending section, and prefer
the convergence trend over any single absolute number -- `tests/test_fea.py` demonstrates the
convergence rather than asserting a tolerance the element cannot meet.

Conventions, fixed here and relied on everywhere downstream:
  * Node order is the standard VTK/Abaqus hex ordering, bottom face then top face, CCW seen from
    +Z:  (-,-,-) (+,-,-) (+,+,-) (-,+,-) (-,-,+) (+,-,+) (+,+,+) (-,+,+)
  * DOF order within an element is node-major: [ux0 uy0 uz0 ux1 uy1 uz1 ...], length 24.
  * Strain order is [exx, eyy, ezz, gxy, gyz, gzx] with ENGINEERING shear strains (gamma = 2*eps),
    which is why the shear block of D carries mu and not 2*mu.
  * Units are not enforced. Use a consistent set; mm / N / MPa is what the machines are drawn in,
    and gives stress in MPa directly.
"""
from __future__ import annotations

import numpy as np

# Natural coordinates of the eight nodes, in the fixed order documented above.
NODE_NAT = np.array([
    [-1.0, -1.0, -1.0],
    [+1.0, -1.0, -1.0],
    [+1.0, +1.0, -1.0],
    [-1.0, +1.0, -1.0],
    [-1.0, -1.0, +1.0],
    [+1.0, -1.0, +1.0],
    [+1.0, +1.0, +1.0],
    [-1.0, +1.0, +1.0],
])

# Integer grid offsets of the eight nodes, same order. Used by the voxel grid to build
# element->node connectivity; kept beside NODE_NAT so the two can never drift apart.
NODE_OFFSET = ((NODE_NAT + 1.0) / 2.0).astype(np.int64)

_GP = 1.0 / np.sqrt(3.0)  # 2-point Gauss in each direction, weights all 1.0


def elasticity_matrix(E: float, nu: float) -> np.ndarray:
    """Isotropic 6x6 constitutive matrix for the engineering-strain ordering above.

    Raises rather than returning a silently wrong matrix: nu -> 0.5 is incompressible (lambda
    blows up) and nu <= -1 is thermodynamically impossible, and both would otherwise produce a
    plausible-looking solve.
    """
    E = float(E)
    nu = float(nu)
    if E <= 0.0:
        raise ValueError(f"Young's modulus must be positive, got {E}")
    if not (-1.0 < nu < 0.5):
        raise ValueError(f"Poisson's ratio must lie in (-1, 0.5), got {nu}")
    lam = E * nu / ((1.0 + nu) * (1.0 - 2.0 * nu))
    mu = E / (2.0 * (1.0 + nu))
    D = np.zeros((6, 6))
    D[:3, :3] = lam
    D[0, 0] = D[1, 1] = D[2, 2] = lam + 2.0 * mu
    D[3, 3] = D[4, 4] = D[5, 5] = mu
    return D


def shape_gradients(nat: np.ndarray) -> np.ndarray:
    """dN/d(xi,eta,zeta) at one natural point. Returns (8, 3).

    N_i = 1/8 (1 + xi*xi_i)(1 + eta*eta_i)(1 + zeta*zeta_i)
    """
    xi, eta, zeta = nat
    s = NODE_NAT
    g = np.empty((8, 3))
    g[:, 0] = 0.125 * s[:, 0] * (1.0 + eta * s[:, 1]) * (1.0 + zeta * s[:, 2])
    g[:, 1] = 0.125 * s[:, 1] * (1.0 + xi * s[:, 0]) * (1.0 + zeta * s[:, 2])
    g[:, 2] = 0.125 * s[:, 2] * (1.0 + xi * s[:, 0]) * (1.0 + eta * s[:, 1])
    return g


def strain_displacement(dN_dx: np.ndarray) -> np.ndarray:
    """Assemble the 6x24 B matrix from physical shape-function gradients (8, 3)."""
    B = np.zeros((6, 24))
    for i in range(8):
        dx, dy, dz = dN_dx[i]
        c = 3 * i
        B[0, c + 0] = dx
        B[1, c + 1] = dy
        B[2, c + 2] = dz
        B[3, c + 0] = dy
        B[3, c + 1] = dx
        B[4, c + 1] = dz
        B[4, c + 2] = dy
        B[5, c + 0] = dz
        B[5, c + 2] = dx
    return B


def hex8_stiffness(h: tuple[float, float, float], E: float, nu: float) -> np.ndarray:
    """24x24 stiffness of one axis-aligned rectangular hex of side lengths h = (hx, hy, hz).

    For a rectangular element the Jacobian is constant and diagonal, J = diag(hx, hy, hz) / 2,
    so the physical gradients are just the natural ones scaled by 2/h. Keeping the quadrature
    loop anyway costs microseconds once per analysis and keeps the code honest about what it is.
    """
    hx, hy, hz = (float(v) for v in h)
    if min(hx, hy, hz) <= 0.0:
        raise ValueError(f"element sides must be positive, got {h}")
    D = elasticity_matrix(E, nu)
    scale = np.array([2.0 / hx, 2.0 / hy, 2.0 / hz])
    detJ = hx * hy * hz / 8.0

    KE = np.zeros((24, 24))
    for a in (-_GP, _GP):
        for b in (-_GP, _GP):
            for c in (-_GP, _GP):
                B = strain_displacement(shape_gradients(np.array([a, b, c])) * scale)
                KE += B.T @ D @ B * detJ  # weight is 1.0 for 2-point Gauss
    # Quadrature is exact here, so the only asymmetry is float round-off; symmetrise so that
    # downstream eigenvalue and Cholesky-style checks see an exactly symmetric operator.
    return 0.5 * (KE + KE.T)


def hex8_b_centre(h: tuple[float, float, float]) -> np.ndarray:
    """B matrix at the element centroid, for recovering a single stress per element.

    Element-centre stress is used rather than nodal extrapolation: on a voxel mesh the centroid
    is the superconvergent point, and corner values on a stair-stepped boundary are dominated by
    the staircase rather than by the structure. See `voxel.von_mises`.
    """
    hx, hy, hz = (float(v) for v in h)
    scale = np.array([2.0 / hx, 2.0 / hy, 2.0 / hz])
    return strain_displacement(shape_gradients(np.zeros(3)) * scale)


def _stress_block(sigma: np.ndarray) -> np.ndarray:
    """The 9x9 block-diagonal of the 3x3 Cauchy tensor, for the geometric stiffness.

    `sigma` is in the (sxx, syy, szz, sxy, syz, szx) order used throughout this package.
    """
    sxx, syy, szz, sxy, syz, szx = (float(v) for v in sigma)
    s3 = np.array([[sxx, sxy, szx],
                   [sxy, syy, syz],
                   [szx, syz, szz]])
    S = np.zeros((9, 9))
    for c in range(3):
        S[3 * c:3 * c + 3, 3 * c:3 * c + 3] = s3
    return S


def displacement_gradient(dN_dx: np.ndarray) -> np.ndarray:
    """The 9x24 G matrix: every first derivative of every displacement component.

    Rows run (du/dx, du/dy, du/dz, dv/dx, ... dw/dz); columns are the 24 element DOF. This is
    the operator the geometric stiffness needs, and it is a different object from the 6x24 `B`
    of `strain_displacement`: `B` throws away the antisymmetric part of the gradient because
    strain does not depend on local rotation, while buckling depends on exactly that part -- a
    member rotates as it buckles, and the work the existing stress does through that rotation is
    the whole mechanism.
    """
    G = np.zeros((9, 24))
    for i in range(8):
        for c in range(3):          # displacement component
            for d in range(3):      # derivative direction
                G[3 * c + d, 3 * i + c] = dN_dx[i, d]
    return G


def geometric_stiffness(h: tuple[float, float, float], sigma: np.ndarray) -> np.ndarray:
    """24x24 geometric (initial-stress) stiffness of one rectangular hex under `sigma`.

    `K + lambda*Kg` loses positive-definiteness at the critical load factor `lambda`, which is
    what makes linear buckling an eigenvalue problem rather than an iteration.

    LINEAR in `sigma` by construction, and that is the property the whole method rests on: it is
    why the eigenvalue comes out as a load *factor*. Double the load, double the stress, double
    `Kg`, halve the factor -- and the critical load is unchanged, which is the real invariant.

    Sign follows the physics and is worth stating because an error in it inverts every answer:
    with the stress passed as computed (compression negative), a member in compression gives a
    negative quadratic form -- it is easier to buckle -- and one in tension gives a positive one,
    which is a guitar string being harder to deflect the tighter it is wound.
    """
    hx, hy, hz = (float(v) for v in h)
    if min(hx, hy, hz) <= 0.0:
        raise ValueError(f"element sides must be positive, got {h}")
    sigma = np.asarray(sigma, dtype=float)
    if sigma.shape != (6,):
        raise ValueError(f"sigma must be 6 components (sxx, syy, szz, sxy, syz, szx), got "
                         f"{sigma.shape}")
    S = _stress_block(sigma)
    scale = np.array([2.0 / hx, 2.0 / hy, 2.0 / hz])
    detJ = hx * hy * hz / 8.0

    KG = np.zeros((24, 24))
    for a in (-_GP, _GP):
        for b in (-_GP, _GP):
            for c in (-_GP, _GP):
                G = displacement_gradient(shape_gradients(np.array([a, b, c])) * scale)
                KG += G.T @ S @ G * detJ      # weight is 1.0 for 2-point Gauss
    return 0.5 * (KG + KG.T)
