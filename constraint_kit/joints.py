"""Joints — first-class, oriented, authored mate frames (the durable grounding for mate intent).

A joint is a NAMED frame on a part: an origin + an orientation whose **+Z axis is the mate axis**
(e.g. a bore's axis, a screw's shaft, a face normal). Unlike selector-derived references
(`.faces(">Z")`) joints are authored from the part's parameters, so the NAME survives any parameter
change — `block.joints["bore"]` always exists and is recomputed, never "the 3rd face that happened to
face +Z". This is the fix for the grounding problem (see ../README.md).

Frames are cadquery Locations (same OCP kernel as build123d), so the existing coincident solve
(`a_world * b_local.inverse`) aligns BOTH position and axis — verified: a frame whose +Z is the bore
axis, mated to another joint, rotates the moving part so the axes coincide.

build123d parts author joints with real `RigidJoint`; we extract their frames with
`joints_from_b123d`. cadquery-native parts author the same frames with `frame()`. Identical downstream.
"""
from __future__ import annotations

import cadquery as cq


def frame(origin=(0, 0, 0), z_axis=(0, 0, 1), x_axis=None) -> cq.Location:
    """An oriented joint frame as a cq.Location: +Z = `z_axis` (the mate axis), at `origin`.
    `x_axis` optionally pins the roll; if omitted cadquery picks a perpendicular."""
    origin = tuple(float(v) for v in origin)
    z_axis = tuple(float(v) for v in z_axis)
    if x_axis is None:
        plane = cq.Plane(origin=origin, normal=z_axis)
    else:
        plane = cq.Plane(origin=origin, xDir=tuple(float(v) for v in x_axis), normal=z_axis)
    return cq.Location(plane)


def _cylinder_axis(face):
    """(axis_dir, radius) of a cylindrical face via OCC, or None if the face isn't a cylinder."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Cylinder
    ad = BRepAdaptor_Surface(face.wrapped)
    if ad.GetType() != GeomAbs_Cylinder:
        return None
    cyl = ad.Cylinder()
    d = cyl.Axis().Direction()
    return (d.X(), d.Y(), d.Z()), cyl.Radius()


def derive_ports(wp) -> dict:
    """Derive named oriented port frames from a BUILT solid by geometric query (T2.1) — so interfaces can
    come from geometry, not hand-authoring. Returns {name: cq.Location}, each query fail-soft (skipped if
    absent): 'top'/'bottom' (extreme planar faces, +Z/−Z), 'center' (bbox centre), 'bore_axis' (the
    smallest-radius cylindrical face = the bore, frame on its true OCC axis).

    NOTE: these are SELECTOR-derived from the current solid — a snapshot. Making the *names* stable across
    parameter changes (so a derived port survives regen like an authored joint does) is the deeper problem,
    tracked as research R2.a; this gives the snapshot capability that intent-mating (T2.2) builds on."""
    ports: dict = {}
    try:
        c = wp.faces(">Z").val().Center(); ports["top"] = frame((c.x, c.y, c.z), (0, 0, 1))
    except Exception:  # noqa: BLE001
        pass
    try:
        c = wp.faces("<Z").val().Center(); ports["bottom"] = frame((c.x, c.y, c.z), (0, 0, -1))
    except Exception:  # noqa: BLE001
        pass
    try:
        bb = wp.val().BoundingBox()
        ports["center"] = frame(((bb.xmin + bb.xmax) / 2, (bb.ymin + bb.ymax) / 2,
                                 (bb.zmin + bb.zmax) / 2))
    except Exception:  # noqa: BLE001
        pass
    try:
        cyls = [(ax[1], f, ax[0]) for f in wp.faces("%Cylinder").vals()
                if (ax := _cylinder_axis(f)) is not None]
        if cyls:
            _, f, axis = min(cyls, key=lambda x: x[0])    # smallest radius = the bore
            c = f.Center(); ports["bore_axis"] = frame((c.x, c.y, c.z), axis)
    except Exception:  # noqa: BLE001
        pass
    return ports


def joints_from_b123d(part) -> dict:
    """Extract a build123d part's RigidJoints as {label: cq.Location} (oriented frames).
    These ARE build123d Joint frames — first-class, authored on the part definition."""
    out = {}
    for label, j in part.joints.items():
        out[label] = cq.Location(j.location.wrapped)
    return out
