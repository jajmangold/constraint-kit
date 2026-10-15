# -*- coding: utf-8 -*-
"""An INDEPENDENT reference solver: Gmsh quadratic tetrahedra through CalculiX.

This module is not a second implementation of our physics. It is a wrapper around two mature
external solvers whose only job is to disagree with us when we are wrong. Everything in
`voxel.py` is ours — our element, our assembly, our conjugate gradients, our stress recovery —
and a thing tested only against itself and against the handful of shapes that have a closed
form has not been tested on the shapes we care about.

**Why these two.** CalculiX 2.22 and Gmsh 4.13 ship inside FreeCAD 1.0, at
`<FreeCAD>/bin/{ccx,gmsh}.exe`, so this costs no new dependency on a machine that already has
FreeCAD. And they are independent in every way that matters: a different element family
(C3D10 quadratic tets against our trilinear hexes), a different mesh topology (conforming,
boundary-fitted tets against a staircased lattice), a different assembly, and a different linear
solver (SPOOLES against our CG or SuperLU). They share no line of code with us and no author.

**Why external processes rather than a library.** Both are invoked as executables over files.
That keeps this module to numpy and the standard library, so `fea/` stays importable on a bare
host and inside FreeCAD alike, and it means a crash in a 500k-DOF factorisation takes down a
subprocess rather than the interpreter that asked for it.

Two hard-won details are worth reading before touching this:

* **Gmsh native `.msh` node order for a 10-node tet is NOT CalculiX order.** The two mid-edge
  nodes on the fourth vertex are swapped. Feeding a `.msh` straight through produces
  `*ERROR in e_c3d: nonpositive jacobian determinant`, not a wrong answer — which is the good
  outcome, but only if nobody swallows the exit code. Rather than hand-permute, this asks Gmsh
  for its own Abaqus (`.inp`) output and reads the element block from that: the conversion is
  then Gmsh problem, maintained by people who care about it.
* **A load spread over a node set must still sum to the load.** `voxel.Model.load` once lost 70%
  of a force to padding nodes, and the result converged beautifully to the wrong number. So
  `TetResult.applied_force` reports what was actually written into the deck, and the suite
  asserts on it.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

import numpy as np

# Where FreeCAD puts them on each platform. Checked in order, and only as a fallback after the
# environment variables, so a user with their own build is never second-guessed.
_FREECAD_HINTS = (
    r"C:\Program Files\FreeCAD 1.0\bin",
    r"C:\Program Files\FreeCAD 1.1\bin",
    "/usr/lib/freecad/bin",
    "/Applications/FreeCAD.app/Contents/Resources/bin",
)

# CalculiX writes its mesh-quality complaints to stdout and keeps going to a nonzero exit; these
# are the ones that mean the answer is meaningless rather than merely imperfect.
_FATAL = ("nonpositive jacobian", "*error")


class ToolchainMissing(RuntimeError):
    """Gmsh or CalculiX could not be found.

    Raised rather than skipped. A cross-check that quietly does not run is worse than no
    cross-check at all, because a suite that reports green reads as agreement.
    """


class SolverFailed(RuntimeError):
    """Gmsh or CalculiX ran and failed. Carries what it said, because it usually explains why."""


def _find(env: str, exe: str) -> str | None:
    override = os.environ.get(env)
    if override:
        return override if os.path.isfile(override) else None
    found = shutil.which(exe)
    if found:
        return found
    for directory in _FREECAD_HINTS:
        candidate = os.path.join(directory, exe + (".exe" if os.name == "nt" else ""))
        if os.path.isfile(candidate):
            return candidate
    return None


def tools() -> tuple[str, str]:
    """`(gmsh, ccx)` absolute paths, or `ToolchainMissing` naming how to point at them."""
    gmsh_path = _find("CK_GMSH", "gmsh")
    ccx_path = _find("CK_CCX", "ccx")
    if gmsh_path and ccx_path:
        return gmsh_path, ccx_path
    missing = [n for n, p in (("gmsh", gmsh_path), ("CalculiX (ccx)", ccx_path)) if not p]
    raise ToolchainMissing(
        f"the reference solver needs {' and '.join(missing)}, which were not found. Both ship "
        f"inside FreeCAD at <FreeCAD>/bin — on Windows usually "
        f"{_FREECAD_HINTS[0]}. Set CK_GMSH and CK_CCX to the executables, or put them on PATH. "
        f"Until then the voxel tier has no independent check and its numbers rest on closed-form "
        f"cases alone.")


def versions() -> dict:
    """What is actually installed, for a report that has to be believable later."""
    gmsh_path, ccx_path = tools()
    out = {}
    try:
        # gmsh prints its version to stderr on some builds and stdout on others; take whichever
        # answered rather than picking one and reporting "?" on the other half of the world.
        proc = subprocess.run([gmsh_path, "--version"], capture_output=True, text=True,
                              timeout=30)
        out["gmsh"] = (proc.stderr.strip() or proc.stdout.strip() or "?").splitlines()[0]
    except Exception:  # noqa: BLE001
        out["gmsh"] = "?"
    try:
        text = subprocess.run([ccx_path, "-v"], capture_output=True, text=True,
                              timeout=30).stdout
        match = re.search(r"Version\s+([\d.]+)", text)
        out["ccx"] = match.group(1) if match else "?"
    except Exception:  # noqa: BLE001
        out["ccx"] = "?"
    return out


# ----------------------------------------------------------------------------------- the mesh

@dataclass(frozen=True)
class TetMesh:
    """Second-order tetrahedra. `tets` is (m, 10) indices into `nodes`, in CalculiX order."""

    nodes: np.ndarray            # (n, 3) float
    tets: np.ndarray             # (m, 10) int

    def __post_init__(self) -> None:
        nodes = np.asarray(self.nodes, dtype=float)
        tets = np.asarray(self.tets, dtype=np.int64)
        if nodes.ndim != 2 or nodes.shape[1] != 3:
            raise ValueError(f"nodes must be (n, 3), got {nodes.shape}")
        if tets.ndim != 2 or tets.shape[1] != 10:
            raise ValueError(f"tets must be (m, 10) second-order elements, got {tets.shape}")
        if len(tets) and (tets.min() < 0 or tets.max() >= len(nodes)):
            raise ValueError("an element references a node outside the node array")
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "tets", tets)

    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        return self.nodes.min(axis=0), self.nodes.max(axis=0)

    def volume(self) -> float:
        """Total volume from the CORNER nodes only.

        Exact for a straight-sided tet, which these are: Gmsh places mid-edge nodes on the
        geometry, so a curved boundary makes this a slight underestimate of the meshed volume —
        stated rather than hidden, and irrelevant on the boxes this is checked against.
        """
        p = self.nodes[self.tets[:, :4]]
        a = p[:, 1] - p[:, 0]
        b = p[:, 2] - p[:, 0]
        c = p[:, 3] - p[:, 0]
        return float(np.abs(np.einsum("ij,ij->i", np.cross(a, b), c)).sum() / 6.0)


def _run(cmd: list, cwd: str, what: str, timeout: float = 1800.0):
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise SolverFailed(f"{what} did not finish within {timeout:.0f}s") from exc
    except OSError as exc:
        raise SolverFailed(f"{what} could not be started: {exc}") from exc
    return proc


def _parse_inp(path: str) -> TetMesh:
    """Read nodes and the C3D10 element block out of Gmsh own Abaqus output.

    Gmsh writes the 1-D and 2-D entities as T3D3/CPS6 blocks too; only the volume elements are
    wanted, and only the nodes they use — the rest are duplicates of surface nodes that would
    otherwise show up as unattached degrees of freedom and make the stiffness matrix singular.
    """
    coords: dict = {}
    elements: list = []
    block = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("**"):
                continue
            if line.startswith("*"):
                squashed = line.upper().replace(" ", "")
                block = "N" if squashed.startswith("*NODE") else (
                    "E" if "TYPE=C3D10" in squashed else None)
                continue
            if block == "N":
                parts = line.split(",")
                coords[int(parts[0])] = (float(parts[1]), float(parts[2]), float(parts[3]))
            elif block == "E":
                ids = [int(x) for x in line.split(",") if x.strip()]
                elements.append(ids[1:11])

    if not elements:
        raise SolverFailed(
            "gmsh produced no C3D10 volume elements. The geometry may be a surface rather than "
            "a solid, or too coarse a pitch left no room for a single element.")

    used = sorted({n for element in elements for n in element})
    index = {gmsh_id: i for i, gmsh_id in enumerate(used)}
    nodes = np.array([coords[i] for i in used], dtype=float)
    tets = np.array([[index[n] for n in element] for element in elements], dtype=np.int64)
    return TetMesh(nodes=nodes, tets=tets)


def _mesh(geo_body: str, pitch: float, workdir: str | None) -> TetMesh:
    gmsh_path, _ = tools()
    if pitch <= 0.0:
        raise ValueError(f"pitch must be positive, got {pitch}")
    own = workdir is None
    directory = workdir or tempfile.mkdtemp(prefix="ck-tet-")
    try:
        geo = os.path.join(directory, "model.geo")
        inp = os.path.join(directory, "model.inp")
        with open(geo, "w", encoding="utf-8") as fh:
            fh.write(geo_body)
            # Second order, complete (10 nodes, not 8): CalculiX C3D10 wants all of them, and a
            # trilinear-vs-quadratic comparison is the whole point of the exercise.
            fh.write(f"\nMesh.CharacteristicLengthMax = {pitch!r};\n"
                     f"Mesh.CharacteristicLengthMin = {pitch!r};\n"
                     "Mesh.ElementOrder = 2;\n"
                     "Mesh.SecondOrderIncomplete = 0;\n"
                     "Mesh.Optimize = 1;\n")
        proc = _run([gmsh_path, geo, "-3", "-format", "inp", "-o", inp, "-v", "1"],
                    directory, "gmsh")
        if proc.returncode != 0 or not os.path.isfile(inp):
            raise SolverFailed(
                f"gmsh failed (exit {proc.returncode}): "
                f"{(proc.stderr or proc.stdout or '').strip()[:500]}")
        return _parse_inp(inp)
    finally:
        if own:
            shutil.rmtree(directory, ignore_errors=True)


def mesh_box(size, pitch: float, workdir: str | None = None) -> TetMesh:
    """A rectangular block from the origin to `size`, meshed at roughly `pitch`."""
    sx, sy, sz = (float(v) for v in size)
    if min(sx, sy, sz) <= 0.0:
        raise ValueError(f"box size must be positive in every axis, got {size}")
    return _mesh('SetFactory("OpenCASCADE");\n'
                 f"Box(1) = {{0, 0, 0, {sx!r}, {sy!r}, {sz!r}}};\n", pitch, workdir)


def mesh_cad(path: str, pitch: float, workdir: str | None = None) -> TetMesh:
    """Mesh a STEP, BREP or IGES solid. Gmsh carries its own OpenCASCADE, so this is exact.

    This is the path that matters: the voxel tier samples the same file onto a lattice, so the
    two solvers are reading one geometry rather than two descriptions of it.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    literal = os.path.abspath(path).replace("\\", "/")
    return _mesh('SetFactory("OpenCASCADE");\n'
                 f'Merge "{literal}";\n', pitch, workdir)


# --------------------------------------------------------------------------------- the solve

@dataclass(frozen=True)
class TetResult:
    """One CalculiX run. Nodal displacements and nodal (extrapolated) von Mises stress."""

    displacement: np.ndarray     # (n, 3)
    von_mises: np.ndarray        # (n,)
    applied_force: tuple         # what was actually written into the deck, summed
    n_nodes: int
    n_elements: int

    @property
    def max_displacement(self) -> float:
        if self.displacement.size == 0:
            return 0.0
        return float(np.linalg.norm(self.displacement, axis=1).max())

    @property
    def peak_von_mises(self) -> float:
        return float(self.von_mises.max()) if self.von_mises.size else 0.0

    def von_mises_percentile(self, q: float = 99.0) -> float:
        """A percentile, quoted for the same reason as on the voxel side.

        CalculiX extrapolates integration-point stress to the nodes, which overshoots at a
        re-entrant corner exactly where our staircase does. Comparing peaks would compare two
        different artefacts; comparing percentiles compares the structure.
        """
        if self.von_mises.size == 0:
            return 0.0
        return float(np.percentile(self.von_mises, q))


def _nodes_where(mesh: TetMesh, where) -> np.ndarray:
    """Resolve a selector exactly as `voxel.Model._select_nodes` does, so a comparison is fair."""
    if callable(where):
        mask = np.asarray(where(mesh.nodes))
        if mask.shape != (len(mesh.nodes),) or mask.dtype != bool:
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


def _write_set(fh, name: str, ids) -> None:
    fh.write(f"*NSET, NSET={name}\n")
    for start in range(0, len(ids), 8):
        fh.write(", ".join(str(int(i) + 1) for i in ids[start:start + 8]) + ",\n")


def _read_frd(path: str, n_nodes: int) -> tuple:
    """Parse displacement and stress out of a CalculiX .frd.

    Fixed-width, and the widths are the thing: 3 characters of record tag, 10 of node id, then
    12 per component. Splitting on whitespace works until a six-figure node id meets a negative
    exponent and two fields run together.
    """
    disp = np.zeros((n_nodes, 3))
    stress = np.zeros((n_nodes, 6))
    seen_disp = seen_stress = 0
    block = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            tag = line[:3].strip()
            if tag == "-4":
                block = line[3:].split()[0] if line[3:].split() else None
                continue
            if tag in ("-5", "1P", "2C", "3C"):
                continue
            if tag == "-3":
                block = None
                continue
            if tag != "-1" or block not in ("DISP", "STRESS"):
                continue
            node = int(line[3:13]) - 1
            body = line[13:].rstrip("\n").rstrip()
            values = [float(body[12 * k:12 * (k + 1)]) for k in range(len(body) // 12)]
            if not 0 <= node < n_nodes:
                continue
            if block == "DISP":
                disp[node] = values[:3]
                seen_disp += 1
            else:
                stress[node] = values[:6]
                seen_stress += 1

    if seen_disp == 0:
        raise SolverFailed(f"no displacement block in {os.path.basename(path)}")
    sxx, syy, szz, sxy, syz, szx = stress.T
    von_mises = np.sqrt(0.5 * ((sxx - syy) ** 2 + (syy - szz) ** 2 + (szz - sxx) ** 2)
                        + 3.0 * (sxy ** 2 + syz ** 2 + szx ** 2))
    return disp, von_mises


def solve(mesh: TetMesh, *, E: float, nu: float, fix, load,
          axes: str = "xyz", workdir: str | None = None) -> TetResult:
    """Run one linear static analysis.

    `fix` and `load` take the same selector form as `voxel.Model` — a callable over an
    (n_nodes, 3) coordinate array — so the two solvers can be handed literally the same lambda
    and there is no chance of comparing two slightly different problems.

    `load` is `(selector, (fx, fy, fz))`, the total force divided over the selected nodes. Unlike
    the voxel path there are no empty nodes to lose it to, since a tet mesh has no padding.
    """
    _, ccx_path = tools()
    if E <= 0.0:
        raise ValueError(f"E must be positive, got {E}")
    if not -1.0 < nu < 0.5:
        raise ValueError(f"nu must lie in (-1, 0.5), got {nu}")
    for axis in axes:
        if axis not in "xyz":
            raise ValueError(f"axes must be drawn from 'xyz', got {axes!r}")

    where_load, force = load
    fixed = _nodes_where(mesh, fix)
    loaded = _nodes_where(mesh, where_load)
    share = np.asarray(force, dtype=float) / len(loaded)

    own = workdir is None
    directory = workdir or tempfile.mkdtemp(prefix="ck-ccx-")
    try:
        deck = os.path.join(directory, "job.inp")
        with open(deck, "w", encoding="utf-8") as fh:
            fh.write("*NODE, NSET=Nall\n")
            for i, (x, y, z) in enumerate(mesh.nodes, start=1):
                fh.write("%d, %.10g, %.10g, %.10g\n" % (i, x, y, z))
            fh.write("*ELEMENT, TYPE=C3D10, ELSET=Eall\n")
            for e, element in enumerate(mesh.tets, start=1):
                fh.write("%d, %s\n" % (e, ", ".join(str(int(n) + 1) for n in element)))
            _write_set(fh, "FIXED", fixed)
            _write_set(fh, "LOADED", loaded)
            fh.write(f"*MATERIAL, NAME=MAT\n*ELASTIC\n{E!r}, {nu!r}\n")
            fh.write("*SOLID SECTION, ELSET=Eall, MATERIAL=MAT\n")
            fh.write("*STEP\n*STATIC\n*BOUNDARY\n")
            for axis in axes:
                dof = "xyz".index(axis) + 1
                fh.write(f"FIXED, {dof}, {dof}, 0.0\n")
            fh.write("*CLOAD\n")
            for dof, value in enumerate(share, start=1):
                if value:
                    fh.write("LOADED, %d, %.12g\n" % (dof, value))
            fh.write("*NODE FILE\nU\n*EL FILE\nS\n*END STEP\n")

        proc = _run([ccx_path, "-i", "job"], directory, "CalculiX")
        text = (proc.stdout or "") + (proc.stderr or "")
        lowered = text.lower()
        frd = os.path.join(directory, "job.frd")
        if proc.returncode != 0 or any(marker in lowered for marker in _FATAL):
            detail = "\n".join(
                line for line in text.splitlines() if "error" in line.lower()
            )[:600] or text.strip()[-600:]
            raise SolverFailed(
                f"CalculiX failed (exit {proc.returncode}) on {len(mesh.tets)} elements: "
                f"{detail}")
        if not os.path.isfile(frd):
            raise SolverFailed(
                f"CalculiX exited 0 but wrote no .frd. Output: {text.strip()[-400:]}")

        disp, von_mises = _read_frd(frd, len(mesh.nodes))
        return TetResult(displacement=disp, von_mises=von_mises,
                         applied_force=tuple(share * len(loaded)),
                         n_nodes=len(mesh.nodes), n_elements=len(mesh.tets))
    finally:
        if own:
            shutil.rmtree(directory, ignore_errors=True)
