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


def joints_from_b123d(part) -> dict:
    """Extract a build123d part's RigidJoints as {label: cq.Location} (oriented frames).
    These ARE build123d Joint frames — first-class, authored on the part definition."""
    out = {}
    for label, j in part.joints.items():
        out[label] = cq.Location(j.location.wrapped)
    return out
