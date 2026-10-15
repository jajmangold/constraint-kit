# -*- coding: utf-8 -*-
"""Triangle soup -> voxel occupancy, by ray parity.

This is the same technique as the `Mesh.foraminate` trick already used in knee-exo, and for the
same reason: point-in-solid classification on a perforated B-rep solid runs ~50 ms a query, so a
grid of any useful size is an hour's work, while a ray gives the whole column of a grid at once
and costs microseconds. Here it is reimplemented on plain numpy so the physics tier carries no
CAD dependency and the 15 printed parts already exported to `stl/` can be analysed as they are.

The algorithm: project every triangle onto XY, find which voxel columns its projection covers,
intersect the column's vertical ray with the triangle's plane, and record the crossing height.
A voxel is inside iff an odd number of crossings lie below its centre -- which `np.searchsorted`
answers for a whole column in one call.

Two honest caveats, both reported rather than hidden:

* **Parity needs a watertight mesh.** A column with an odd crossing count is proof of a leak (or
  of a ray threading an edge exactly). `occupancy_from_triangles` counts those columns and warns
  with the fraction, because a quietly leaky mesh produces a plausible-looking solid with a
  hollow stripe through it, and that would be read as a structural finding.
* **Rays are jittered by a sub-micron deterministic offset.** CAD geometry is full of
  axis-aligned faces that land exactly on voxel-centre planes, where an un-jittered ray threads
  a shared edge and gets counted twice or not at all. The offset is ~1e-3 of a pitch, which is
  far below any printable feature, and it is deterministic so results are reproducible.
"""
from __future__ import annotations

import struct
import warnings

import numpy as np

from .voxel import Grid

# Fraction of a pitch by which sample rays are displaced off the exact voxel-centre lattice, to
# avoid degenerate edge-threading hits. Irrational-looking on purpose: round fractions are what
# land on CAD features.
_JITTER = (0.00137, 0.00219)


def load_stl(path: str) -> np.ndarray:
    """Read a binary or ASCII STL into an (n_triangles, 3, 3) float array.

    Written out rather than taking a dependency on trimeshor meshio: it is thirty lines, it is
    the only mesh format the toolchain actually emits, and the physics tier is meant to import
    with nothing but numpy and scipy present.
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    if len(raw) < 84:
        raise ValueError(f"{path}: too short to be an STL ({len(raw)} bytes)")

    # An ASCII STL starts with "solid", but so do some binary files written by careless tools;
    # the reliable discriminator is whether the binary triangle count matches the file length.
    n_claimed = struct.unpack_from("<I", raw, 80)[0]
    if 84 + n_claimed * 50 == len(raw):
        data = np.frombuffer(raw, dtype=np.uint8, count=n_claimed * 50, offset=84)
        rec = data.reshape(n_claimed, 50)
        verts = rec[:, 12:48].copy().view("<f4").reshape(n_claimed, 3, 3)
        return verts.astype(np.float64)

    text = raw.decode("ascii", errors="replace")
    if "facet" not in text:
        raise ValueError(
            f"{path}: not a usable STL -- binary triangle count {n_claimed} disagrees with the "
            f"file length {len(raw)}, and no ASCII 'facet' keyword was found")
    vals = [float(tok) for line in text.splitlines()
            if line.strip().startswith("vertex") for tok in line.split()[1:4]]
    arr = np.asarray(vals, dtype=np.float64)
    if arr.size % 9:
        raise ValueError(f"{path}: ASCII STL has {arr.size // 3} vertices, not a multiple of 3")
    return arr.reshape(-1, 3, 3)


def occupancy_from_triangles(tri: np.ndarray, pitch: float | tuple[float, float, float], *,
                             pad: int = 1, origin: tuple[float, float, float] | None = None,
                             shape: tuple[int, int, int] | None = None,
                             leak_warn: float = 0.001) -> Grid:
    """Voxelise a closed triangle soup.

    `pad` adds a margin of empty voxels on every side, which keeps the structure off the grid
    boundary so that face selectors and gradient-style neighbourhood operations have somewhere
    to stand. `origin`/`shape` override the derived bounds when several parts must share one
    lattice (an assembly-level analysis, or a before/after comparison).
    """
    tri = np.asarray(tri, dtype=np.float64)
    if tri.ndim != 3 or tri.shape[1:] != (3, 3):
        raise ValueError(f"triangles must have shape (n, 3, 3), got {tri.shape}")
    if len(tri) == 0:
        raise ValueError("no triangles to voxelise")

    h = (float(pitch),) * 3 if np.isscalar(pitch) else tuple(float(v) for v in pitch)  # type: ignore[arg-type]
    if min(h) <= 0.0:
        raise ValueError(f"pitch must be positive, got {pitch}")

    lo = tri.reshape(-1, 3).min(axis=0)
    hi = tri.reshape(-1, 3).max(axis=0)
    if origin is None:
        origin = tuple(lo[a] - pad * h[a] for a in range(3))
    if shape is None:
        # Voxel count, not node count: the material spans ceil(extent / pitch) voxels and the pad
        # adds that many empty ones at each end. An off-by-one here leaves a dead layer at the
        # top of every axis, which then reads as "no material on the max face" when loading.
        shape = tuple(int(np.ceil((hi[a] - lo[a]) / h[a])) + 2 * pad for a in range(3))
    nx, ny, nz = (int(v) for v in shape)
    ox, oy, oz = (float(v) for v in origin)

    # Column sample points, jittered off the exact lattice (see module docstring).
    xs = ox + (np.arange(nx) + 0.5 + _JITTER[0]) * h[0]
    ys = oy + (np.arange(ny) + 0.5 + _JITTER[1]) * h[1]
    zc = oz + (np.arange(nz) + 0.5) * h[2]

    a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
    normal = np.cross(b - a, c - a)
    # A triangle whose plane is parallel to the ray cannot be crossed by it. These are the
    # vertical walls of the part, and they are the majority on an extruded shape.
    crossable = np.abs(normal[:, 2]) > 1e-14

    tmin = tri.min(axis=1)
    tmax = tri.max(axis=1)
    i0 = np.searchsorted(xs, tmin[:, 0], "left")
    i1 = np.searchsorted(xs, tmax[:, 0], "right")
    j0 = np.searchsorted(ys, tmin[:, 1], "left")
    j1 = np.searchsorted(ys, tmax[:, 1], "right")

    col_parts: list[np.ndarray] = []
    z_parts: list[np.ndarray] = []
    for t in np.flatnonzero(crossable & (i1 > i0) & (j1 > j0)):
        gx, gy = np.meshgrid(xs[i0[t]:i1[t]], ys[j0[t]:j1[t]], indexing="ij")
        px, py = gx.ravel(), gy.ravel()
        ax, ay = a[t, 0], a[t, 1]
        bx, by = b[t, 0], b[t, 1]
        cx, cy = c[t, 0], c[t, 1]
        # Signed areas against each edge; a point is interior iff all three agree in sign.
        w0 = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
        w1 = (cx - bx) * (py - by) - (cy - by) * (px - bx)
        w2 = (ax - cx) * (py - cy) - (ay - cy) * (px - cx)
        hit = ((w0 > 0) & (w1 > 0) & (w2 > 0)) | ((w0 < 0) & (w1 < 0) & (w2 < 0))
        if not hit.any():
            continue
        n = normal[t]
        z = a[t, 2] - (n[0] * (px[hit] - ax) + n[1] * (py[hit] - ay)) / n[2]
        ii = (i0[t] + np.arange(i1[t] - i0[t]))[:, None].repeat(j1[t] - j0[t], axis=1).ravel()
        jj = (j0[t] + np.arange(j1[t] - j0[t]))[None, :].repeat(i1[t] - i0[t], axis=0).ravel()
        col_parts.append(ii[hit] * ny + jj[hit])
        z_parts.append(z)

    if not col_parts:
        raise ValueError(
            "no ray crossed any triangle -- the pitch is probably coarser than the part, or the "
            "mesh is degenerate")

    cols = np.concatenate(col_parts)
    zz = np.concatenate(z_parts)
    order = np.lexsort((zz, cols))
    cols, zz = cols[order], zz[order]

    n_col = nx * ny
    starts = np.searchsorted(cols, np.arange(n_col), "left")
    ends = np.searchsorted(cols, np.arange(n_col), "right")
    counts = ends - starts

    density = np.zeros((nx, ny, nz), dtype=float)
    odd = 0
    for col in np.flatnonzero(counts):
        seg = zz[starts[col]:ends[col]]
        if len(seg) % 2:
            odd += 1
            continue  # a leak: trust nothing in this column rather than inventing material
        inside = (np.searchsorted(seg, zc, "right") % 2) == 1
        if inside.any():
            density[col // ny, col % ny, :] = inside

    n_hit = int(np.count_nonzero(counts))
    if odd and odd / max(n_hit, 1) > leak_warn:
        warnings.warn(
            f"{odd} of {n_hit} sampled columns ({odd / n_hit:.1%}) had an odd crossing count, "
            "which means the mesh is not watertight; those columns were left empty. Treat any "
            "stiffness computed from this grid as a lower bound.",
            RuntimeWarning, stacklevel=2)

    if not density.any():
        raise ValueError(
            "voxelisation produced no material -- try a finer pitch; thin walls fall through a "
            "grid coarser than their thickness")

    return Grid(density=density, pitch=h, origin=(ox, oy, oz))


def occupancy_from_shape(shape, pitch: float | tuple[float, float, float], *,
                         linear_deflection: float | None = None, **kwargs) -> Grid:
    """Voxelise a live OCC/cadquery shape by tessellating it first.

    Tessellation deflection defaults to a quarter of the voxel pitch: meshing finer than the
    sampling grid is wasted work, and meshing coarser than it puts facet error into the geometry
    the solver sees. Accepts a cadquery Workplane/Shape or a raw TopoDS_Shape.
    """
    from OCP.BRep import BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS

    for attr in ("wrapped", "val"):          # cadquery Shape / Workplane
        if hasattr(shape, attr):
            shape = getattr(shape, attr)
            shape = shape() if callable(shape) else shape
    shape = getattr(shape, "wrapped", shape)

    h = (float(pitch),) * 3 if np.isscalar(pitch) else tuple(float(v) for v in pitch)  # type: ignore[arg-type]
    defl = linear_deflection if linear_deflection is not None else min(h) / 4.0
    BRepMesh_IncrementalMesh(shape, defl, False, 0.5, True)

    tris: list[np.ndarray] = []
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        face = TopoDS.Face_s(exp.Current())
        loc = TopLoc_Location()
        poly = BRep_Tool.Triangulation_s(face, loc)
        if poly is not None:
            trsf = loc.Transformation()
            nodes = np.array([[(p := poly.Node(i).Transformed(trsf)).X(), p.Y(), p.Z()]
                              for i in range(1, poly.NbNodes() + 1)])
            idx = np.array([list(poly.Triangle(i).Get())
                            for i in range(1, poly.NbTriangles() + 1)]) - 1
            if len(idx):
                tris.append(nodes[idx])
        exp.Next()

    if not tris:
        raise ValueError("shape tessellated to no triangles -- is it an empty or non-solid shape?")
    return occupancy_from_triangles(np.concatenate(tris), h, **kwargs)
