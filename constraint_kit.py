"""constraint-kit — geometry-only assembly/placement mate solver (API SKETCH, not implemented).

Extracted in spirit from ../placement/scripts/constraint_place.py. The goal is a backend-agnostic kernel
that solves the world transform of an object B placed onto target A subject to a list of mates — no Blender,
no AI, no optimization. Same logic that seats hats/glasses on a rig, generalized to any A/B.

This file is a STUB capturing the intended interface + the mate vocabulary. The working implementations of
each mate already exist (Blender-coupled) in constraint_place.py; porting them here = the "later" work.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum

# Geometry is passed as plain arrays (Nx3 verts + 4x4 transforms) so any backend (Blender, trimesh, OCC,
# numpy) can drive it. No imports of bpy/mathutils here on purpose.


class Mate(str, Enum):
    COINCIDENT = "coincident"          # put B's ref frame/point onto A's anchor       (crown/eye centering)
    ALIGN_AXIS = "align_axis"          # orient a B axis to an A axis / world up        (asset facing)
    SCALE_TO_FEATURE = "scale_to_feature"  # match B dimension to a measured A feature  (head/face/body width)
    CONTACT = "contact"                # slide B along an axis until surfaces touch     (crown/floor/nose seat)
    INTERFERENCE_REMOVAL = "interference_removal"  # trim A-geometry penetrating B      (clip under hat)


@dataclass
class Anchor:
    """A semantic frame on the target A to mate against (the analog of a CAD datum / mate face).

    In avatars these come from the rig (eye bones, head axis, foot plane); in CAD from datums/faces;
    in scenes from the depth-derived floor plane. Backend supplies the resolved frame.
    """
    name: str                      # e.g. "eye_line", "crown", "floor", "datum_A1"
    origin: tuple                  # (x, y, z) world point
    axes: tuple = ((1, 0, 0), (0, 1, 0), (0, 0, 1))  # local frame (right, fwd, up)


@dataclass
class MateSpec:
    mate: Mate
    # mate-specific params, e.g. {"feature": "head_width", "factor": 1.15} for SCALE_TO_FEATURE,
    # {"axis": "up", "from_subset": "central_column"} for CONTACT.
    params: dict = field(default_factory=dict)


@dataclass
class PlacementResult:
    transform: list               # 4x4 world transform for B (the solved answer)
    notes: dict = field(default_factory=dict)  # e.g. {"contact_gap": 0.0, "clipped": 1867}


def place(b_verts, a_anchor: Anchor, mates: list[MateSpec], a_surface=None) -> PlacementResult:
    """Solve B's world transform so all mates are satisfied, in order. DETERMINISTIC.

    b_verts:   Nx3 array of B's vertices in its own space.
    a_anchor:  the target frame to mate onto.
    mates:     ordered mates (scale → align → coincident → contact → interference), like constraint_place.
    a_surface: optional A geometry (verts/BVH) needed by CONTACT / INTERFERENCE_REMOVAL.

    NOT IMPLEMENTED — port the per-mate math from ../placement/scripts/constraint_place.py:
      SCALE_TO_FEATURE  <- the `acc.scale *= target_w / bbox_w` blocks
      COINCIDENT/ALIGN  <- the `acc.location += (anchor - center)` + rotation_euler blocks
      CONTACT           <- crown central-column seat / floor seat / nose-bridge depth lock
      INTERFERENCE_REMOVAL <- clip_protrusions_under() (ray-toward-axis BVH test)
    """
    raise NotImplementedError("seed stub — see README.md and ../placement/scripts/constraint_place.py")
