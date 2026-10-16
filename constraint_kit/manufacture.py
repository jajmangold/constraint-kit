# -*- coding: utf-8 -*-
"""Manufacturing rules as CONSTRAINTS, not a post-hoc audit (issue #18).

The point of the issue title. An optimizer told to minimise mass subject to strength will happily
return a 0.9 mm wall at an 80 degree overhang with a 0.6 mm cooling channel, because nothing in
its problem statement said otherwise. Auditing that afterwards and sending it back is a loop with
a human in it; making printability a constraint puts it *inside* the search, where the optimizer
never proposes the unprintable design in the first place.

Every rule here is computed from **geometry**, never from a parameter name, which is what makes
it general. A wall is thin because the solid is thin there, not because something called
`wall_thickness` is small -- and a part with a thin region nobody named is exactly the part an
audit misses.

**The three rules carry very different confidence, and that is stated rather than averaged away:**

* **Overhang is EXACT.** It is a question about surface normals and the tessellation has them.
  No resolution limit, no approximation, and it answers the question that actually decides print
  orientation.
* **Thin walls and narrow channels are RESOLUTION-LIMITED.** They are morphological openings on
  the voxel grid, so a wall thinner than the pitch is invisible. The check refuses rather than
  reassuring anybody about a wall it cannot see.
* **Build volume is trivial**, and here because it is the rule that wastes a whole print.

The thin-wall test is a morphological **opening**, which is a definition rather than a heuristic:
a region of the solid can contain a ball of radius `r` if and only if it survives erosion by `r`
followed by dilation by `r`. So the solid minus its own opening is *exactly* the set of places
thinner than `2r`. That is why this finds a thin region anywhere, including the ones that arose
from an interaction between two dimensions and have no name of their own.

**numpy only, and that is not an aesthetic choice.** The first version used
`scipy.ndimage.binary_erosion`, which is the obvious tool -- and importing it inside FreeCAD 1.0
raises `ValueError: All ufuncs must have type numpy.ufunc` from `scipy.special._multiufuncs`,
because `ndimage` imports `special` and the bundled scipy and numpy disagree about what a ufunc
is. That is the third distinct failure the same ABI mismatch has caused in this project, after
the SuperLU direct solver and the recursive `dtype.__repr__` that ruled out ARPACK. So the
morphology is implemented here, in about forty lines of array shifting, and works everywhere.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# Default rules, for a 0.4 mm nozzle on a well-behaved printer. Conservative rather than
# aspirational: these are values a part comes out of at the first attempt, not the thinnest wall
# anybody has ever coaxed out of a machine.
DEFAULT_MIN_WALL_MM = 1.2            # three perimeters at 0.4 mm
DEFAULT_MIN_CHANNEL_MM = 2.0         # a hole smaller than this closes up or needs reaming
DEFAULT_MAX_OVERHANG_DEG = 45.0      # from the build direction; the classic limit
DEFAULT_BUILD_VOLUME_MM = (220.0, 220.0, 250.0)

# A morphological opening INEVITABLY flags convex edges and corners, and this is geometry rather
# than a bug: a ball of radius r centred inside a solid cannot cover the material right at a 90
# degree corner, so by the ball-fitting definition that corner IS thinner than 2r. Measured on a
# 20 x 20 x 8 mm slab against a 3 mm rule at 0.5 mm pitch: 6.7% of the volume flagged, all of it
# within one ball radius of an edge, and the slab is 8 mm thick everywhere that matters.
#
# So the verdict is a FRACTION rather than a count. A few percent is the edge signature of a
# perfectly printable part; a genuinely thin wall measures in the tens of percent (the same slab
# at 1.5 mm thick flags over 90%). Separating those two is what the threshold is for, and the
# alternative -- fudging the measure to hide the edge effect -- would also hide a real thin
# region that happened to sit near an edge.
DEFAULT_MAX_THIN_FRACTION = 0.15

# A rule this many voxels wide or less cannot be resolved. Two is the floor: one voxel of wall
# cannot be distinguished from a staircase artefact, and the erosion that implements the test has
# nothing to work with.
MIN_VOXELS_ACROSS_RULE = 2.0


class UnresolvableRule(ValueError):
    """The grid is too coarse to answer this rule.

    Raised rather than answered, because this is the one place a printability check could most
    easily reassure somebody about a wall it physically cannot see. The message names the pitch
    that *would* resolve it, so the fix is immediate.
    """


@dataclass(frozen=True)
class Rules:
    """What the process can make. Every field is a real machine limit, not a preference."""

    min_wall_mm: float = DEFAULT_MIN_WALL_MM
    min_channel_mm: float = DEFAULT_MIN_CHANNEL_MM
    max_overhang_deg: float = DEFAULT_MAX_OVERHANG_DEG
    build_volume_mm: tuple = DEFAULT_BUILD_VOLUME_MM
    build_dir: tuple = (0.0, 0.0, 1.0)
    # How much of the solid may register as thin before the part fails. Not zero: see
    # DEFAULT_MAX_THIN_FRACTION -- every convex edge registers, and a part made only of edges
    # would be rejected by a zero threshold while being entirely printable.
    max_thin_fraction: float = DEFAULT_MAX_THIN_FRACTION

    def __post_init__(self) -> None:
        if self.min_wall_mm <= 0.0:
            raise ValueError(f"min_wall_mm must be positive, got {self.min_wall_mm}")
        if self.min_channel_mm <= 0.0:
            raise ValueError(f"min_channel_mm must be positive, got {self.min_channel_mm}")
        if not 0.0 < self.max_overhang_deg < 90.0:
            raise ValueError(
                f"max_overhang_deg must lie strictly between 0 and 90, got "
                f"{self.max_overhang_deg}. It is measured FROM THE BUILD DIRECTION, so 0 would "
                f"forbid every sloped face and 90 would permit a flat ceiling.")
        if len(self.build_volume_mm) != 3 or min(self.build_volume_mm) <= 0.0:
            raise ValueError(
                f"build volume must be three positive dimensions, got {self.build_volume_mm}")
        if not 0.0 <= self.max_thin_fraction < 1.0:
            raise ValueError(
                f"max_thin_fraction must lie in [0, 1), got {self.max_thin_fraction}. Zero is "
                f"allowed and almost never right: a convex edge registers as thin by the "
                f"ball-fitting definition, so zero rejects printable parts.")


# ---------------------------------------------------------------------------------- overhangs

def overhangs(triangles: np.ndarray, *, max_angle_deg: float = DEFAULT_MAX_OVERHANG_DEG,
              build_dir=(0.0, 0.0, 1.0), plate_tol_mm: float = 1e-6) -> dict:
    """Area of downward-facing surface too steep to print without support. EXACT.

    The angle is measured **from the build direction**, which is the convention slicers use: a
    vertical wall is 0 degrees and a flat ceiling is 90. A face needs support when it faces
    downward and that angle exceeds the limit.

    **The plate exclusion is the thing to get right.** A box's bottom face points straight down,
    the worst possible angle, and needs no support at all because it is lying on the build plate.
    A check that counts it reports every part ever printed as unprintable, so a face all of
    whose vertices sit within `plate_tol_mm` of the part's lowest vertex is measured separately
    and reported as `plate_area_mm2`.
    """
    if not 0.0 < max_angle_deg < 90.0:
        raise ValueError(
            f"max_angle_deg must lie strictly between 0 and 90, got {max_angle_deg}; it is "
            f"measured from the build direction")
    tri = np.asarray(triangles, dtype=float)
    if tri.ndim != 3 or tri.shape[1:] != (3, 3):
        raise ValueError(f"triangles must be (n, 3, 3), got {tri.shape}")
    up = np.asarray(build_dir, dtype=float)
    if up.shape != (3,) or not np.any(up):
        raise ValueError(f"build direction must be a non-zero 3-vector, got {build_dir}")
    up = up / np.linalg.norm(up)

    cross = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    area = 0.5 * np.linalg.norm(cross, axis=1)
    keep = area > 0.0
    if not np.any(keep):
        raise ValueError("every triangle is degenerate; there is no surface to measure")
    cross, area, tri = cross[keep], area[keep], tri[keep]
    normal = cross / np.linalg.norm(cross, axis=1)[:, None]

    # The outward normal's component along the build direction. Negative means the face looks
    # downward, which is the only kind that can need support.
    along = normal @ up
    # A triangle rests on the plate when ALL THREE of its vertices do, measured against the
    # lowest VERTEX in the part.
    #
    # Not the lowest centroid, which is what this did first and which is wrong in a way that
    # only shows up on a shape whose lowest point is an edge rather than a face. A 45 degree
    # wedge touches the plate along one line, so no face centroid sits at the true minimum --
    # the lowest centroid belongs to a sloped face a third of the way up, and taking it as the
    # plate height excluded half of the very overhang being measured. The wedge reported exactly
    # half its hypotenuse as unsupported, which is the kind of wrong that looks like a rounding
    # problem and is not.
    vertex_height = tri @ up
    floor = float(vertex_height.min())
    on_plate = np.all(vertex_height <= floor + plate_tol_mm, axis=1)

    downward = (along < 0.0) & ~on_plate
    # Angle from the build direction: a vertical wall has along == 0 (90 deg between normal and
    # up, so 0 from the build direction), a flat underside has along == -1 (90 deg).
    angle = np.degrees(np.arcsin(np.clip(-along, 0.0, 1.0)))
    unsupported = downward & (angle > max_angle_deg + 1e-9)

    total = float(area.sum())
    return {
        "unsupported_area_mm2": float(area[unsupported].sum()),
        "total_area_mm2": total,
        "plate_area_mm2": float(area[on_plate].sum()),
        "worst_angle_deg": float(angle[downward].max()) if np.any(downward) else 0.0,
        "fraction": float(area[unsupported].sum() / total) if total else 0.0,
        "max_angle_deg": float(max_angle_deg),
    }


# --------------------------------------------------------------- thin walls, narrow channels

def _ball_offsets(radius: int) -> list:
    """Integer offsets inside a sphere of `radius` voxels, as the structuring element.

    Spherical rather than cubic, because a cube measures a different thickness along a diagonal
    than along an axis and the answer would then depend on how the part happened to be oriented
    on the grid.
    """
    r = int(radius)
    span = range(-r, r + 1)
    return [(i, j, k) for i in span for j in span for k in span
            if i * i + j * j + k * k <= r * r + 1e-9]


def _shift_and(acc: np.ndarray, src: np.ndarray, off, fill: bool) -> None:
    """`acc &= shift(src, off)` in place, with `fill` outside the array. No scipy, no copies."""
    i, j, k = off
    nx, ny, nz = src.shape

    def win(n, d):
        # the destination slice and the matching source slice for a shift of d
        if d >= 0:
            return slice(0, n - d), slice(d, n)
        return slice(-d, n), slice(0, n + d)

    dx, sx = win(nx, i)
    dy, sy = win(ny, j)
    dz, sz = win(nz, k)
    if not fill:
        # the region the shift leaves undefined takes the fill value, so clear it first
        edge = np.zeros_like(acc)
        edge[dx, dy, dz] = True
        acc &= edge
    acc[dx, dy, dz] &= src[sx, sy, sz]


def _erode(mask: np.ndarray, radius: int) -> np.ndarray:
    """Erosion by a ball: a voxel survives when every ball offset around it is also solid.

    `border_value=0`: outside the array counts as empty, so a solid that runs off the edge of
    the grid is correctly treated as ending there rather than continuing forever.
    """
    if radius <= 0:
        return mask.copy()
    out = mask.copy()
    for off in _ball_offsets(radius):
        if off == (0, 0, 0):
            continue
        _shift_and(out, mask, off, fill=False)
        if not out.any():
            break
    return out


def _dilate(mask: np.ndarray, radius: int) -> np.ndarray:
    """Dilation by a ball: the union of the mask over every ball offset."""
    if radius <= 0:
        return mask.copy()
    out = mask.copy()
    nx, ny, nz = mask.shape
    for i, j, k in _ball_offsets(radius):
        if (i, j, k) == (0, 0, 0):
            continue

        def win(n, d):
            if d >= 0:
                return slice(0, n - d), slice(d, n)
            return slice(-d, n), slice(0, n + d)

        dx, sx = win(nx, i)
        dy, sy = win(ny, j)
        dz, sz = win(nz, k)
        out[dx, dy, dz] |= mask[sx, sy, sz]
    return out


def _connected_to_border(empty: np.ndarray, max_steps: int | None = None) -> np.ndarray:
    """Which empty voxels reach the grid border. Iterative 6-neighbour propagation.

    Used to tell outside air from an enclosed void: the air around a part is not a channel, and
    counting it as one would report every part as riddled with narrow passages.
    """
    reached = np.zeros_like(empty)
    reached[0, :, :] = reached[-1, :, :] = True
    reached[:, 0, :] = reached[:, -1, :] = True
    reached[:, :, 0] = reached[:, :, -1] = True
    reached &= empty

    cap = max_steps if max_steps is not None else int(sum(empty.shape)) + 8
    for _ in range(cap):
        grown = reached.copy()
        grown[1:, :, :] |= reached[:-1, :, :]
        grown[:-1, :, :] |= reached[1:, :, :]
        grown[:, 1:, :] |= reached[:, :-1, :]
        grown[:, :-1, :] |= reached[:, 1:, :]
        grown[:, :, 1:] |= reached[:, :, :-1]
        grown[:, :, :-1] |= reached[:, :, 1:]
        grown &= empty
        if np.array_equal(grown, reached):
            return reached
        reached = grown
    # A labyrinth longer than the sum of the dimensions cannot occur on a rectangular grid with
    # 6-connectivity, so reaching here means something is wrong rather than merely slow.
    raise RuntimeError(
        "the flood fill did not settle in %d steps on a %s grid, which should be impossible"
        % (cap, empty.shape))


def _opening_shortfall(mask: np.ndarray, pitch, rule_mm: float, what: str) -> dict:
    """Voxels of `mask` that its own morphological opening by `rule_mm/2` does not recover.

    Exactly the set of places where `mask` is thinner than `rule_mm`, by the definition of an
    opening. Spherical structuring element, because a cubic one would measure a different
    thickness along a diagonal than along an axis and the answer would depend on how the part
    happened to be oriented on the grid.
    """
    h = min(float(v) for v in pitch)
    across = rule_mm / h
    if across <= MIN_VOXELS_ACROSS_RULE:
        raise UnresolvableRule(
            "a %s rule of %g mm is only %.2f voxels across at a %g mm pitch, which cannot be "
            "resolved -- an erosion has nothing to work with and a feature that thin is "
            "indistinguishable from a staircase artefact. Refine the pitch to %.3g mm or "
            "coarser-rule the check."
            % (what, rule_mm, across, h, rule_mm / (MIN_VOXELS_ACROSS_RULE * 2.0)))

    radius = max(1, int(round(0.5 * across)))
    solid = np.ascontiguousarray(mask, dtype=bool)
    opened = _dilate(_erode(solid, radius), radius)
    shortfall = solid & ~opened
    n_solid = int(solid.sum())
    return {
        "voxels": int(shortfall.sum()),
        "fraction": float(shortfall.sum() / n_solid) if n_solid else 0.0,
        "voxels_across_the_rule": float(across),
        "note": ("a morphological opening at a %g mm pitch: the rule is %.2f voxels across, so "
                 "a feature within a voxel or two of it is uncertain and anything thinner is "
                 "invisible" % (h, across)),
    }


def thin_regions(grid, min_wall_mm: float = DEFAULT_MIN_WALL_MM) -> dict:
    """Where the solid is thinner than `min_wall_mm`. Resolution-limited; refuses when it must."""
    out = _opening_shortfall(np.asarray(grid.density) > 0.0, grid.pitch, min_wall_mm,
                             "minimum wall")
    return {"thin_voxels": out["voxels"], "thin_fraction": out["fraction"],
            "voxels_across_the_rule": out["voxels_across_the_rule"], "note": out["note"],
            "min_wall_mm": float(min_wall_mm)}


def narrow_channels(grid, min_channel_mm: float = DEFAULT_MIN_CHANNEL_MM) -> dict:
    """Where the EMPTY space is narrower than `min_channel_mm`.

    The dual of the thin-wall test, and the same morphological question asked of the complement.
    A cooling passage or a bolt hole too small for the nozzle to trace is as unprintable as a
    wall too thin to extrude.

    Empty space outside the part is excluded, because the air around a part is not a channel:
    only voids the solid encloses or indents are measured, found by flood-filling from the grid
    boundary and discarding whatever it reaches.
    """
    solid = np.ascontiguousarray(np.asarray(grid.density) > 0.0)
    empty = ~solid
    # Flood from the border: everything reached is outside air, not a channel.
    void = empty & ~_connected_to_border(empty)

    if not void.any():
        h = min(float(v) for v in grid.pitch)
        across = min_channel_mm / h
        if across <= MIN_VOXELS_ACROSS_RULE:
            raise UnresolvableRule(
                "a minimum channel rule of %g mm is only %.2f voxels across at a %g mm pitch, "
                "which cannot be resolved. Refine the pitch to %.3g mm."
                % (min_channel_mm, across, h, min_channel_mm / (MIN_VOXELS_ACROSS_RULE * 2.0)))
        return {"narrow_voxels": 0, "narrow_fraction": 0.0,
                "voxels_across_the_rule": float(across),
                "note": "the part encloses no internal void, so there is no channel to measure",
                "min_channel_mm": float(min_channel_mm)}

    out = _opening_shortfall(void, grid.pitch, min_channel_mm, "minimum channel")
    return {"narrow_voxels": out["voxels"], "narrow_fraction": out["fraction"],
            "voxels_across_the_rule": out["voxels_across_the_rule"], "note": out["note"],
            "min_channel_mm": float(min_channel_mm)}


# ------------------------------------------------------------------------------- the roll-up

@dataclass(frozen=True)
class Printability:
    """Whether the process can make this, with every number an optimizer can constrain."""

    ok: bool
    metrics: dict
    violations: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    rules: Rules = field(default_factory=Rules)

    def report(self) -> str:
        lines = ["printability against: wall >= %g mm, channel >= %g mm, overhang <= %g deg, "
                 "build volume %g x %g x %g mm"
                 % (self.rules.min_wall_mm, self.rules.min_channel_mm,
                    self.rules.max_overhang_deg, *self.rules.build_volume_mm)]
        for key in sorted(self.metrics):
            lines.append("  %-26s %12.4g" % (key, self.metrics[key]))
        if self.ok:
            lines.append("  PASSES every rule that could be checked.")
        else:
            lines.append("  FAILS:")
            for text in self.violations:
                lines.append("    - " + text)
        for note in self.notes:
            lines.append("  note: " + note)
        lines.append("  Overhang is EXACT (it is a question about surface normals). Wall and "
                     "channel are RESOLUTION-LIMITED morphological openings on the voxel grid, "
                     "so a feature near the pitch is uncertain and one below it is invisible --")
        lines.append("  and a few percent of any part registers as thin at its own convex "
                     "edges, which is why the wall verdict is a fraction rather than a count.")
        return "\n".join(lines)


def printability(triangles: np.ndarray, grid, rules: Rules | None = None) -> Printability:
    """Every rule at once, as numbers an optimizer can constrain and a sentence a human can read.

    A rule the grid cannot resolve is reported as a NOTE and left out of the verdict rather than
    failing the part -- "I could not check this" and "this is unprintable" are different answers
    and must not be collapsed, or a coarse search would reject every design.
    """
    rules = rules or Rules()
    metrics: dict = {}
    violations: list = []
    notes: list = []

    over = overhangs(triangles, max_angle_deg=rules.max_overhang_deg,
                     build_dir=rules.build_dir)
    metrics["unsupported_area_mm2"] = over["unsupported_area_mm2"]
    metrics["unsupported_fraction"] = over["fraction"]
    metrics["worst_overhang_deg"] = over["worst_angle_deg"]
    if over["unsupported_area_mm2"] > 0.0:
        violations.append(
            "overhang: %.1f mm2 of downward surface is steeper than %g deg (worst %.1f deg), so "
            "it needs support. Rotating the part is usually cheaper than printing supports."
            % (over["unsupported_area_mm2"], rules.max_overhang_deg, over["worst_angle_deg"]))

    try:
        thin = thin_regions(grid, rules.min_wall_mm)
        metrics["thin_fraction"] = thin["thin_fraction"]
        metrics["thin_voxels"] = float(thin["thin_voxels"])
        if thin["thin_fraction"] > rules.max_thin_fraction:
            violations.append(
                "thin wall: %.1f%% of the solid is thinner than %g mm, above the %.0f%% that a "
                "part's own convex edges account for"
                % (100.0 * thin["thin_fraction"], rules.min_wall_mm,
                   100.0 * rules.max_thin_fraction))
        elif thin["thin_voxels"] > 0:
            notes.append(
                "%.1f%% of the solid registers as thinner than %g mm, which is within the %.0f%% "
                "expected from convex edges alone -- an opening cannot cover a 90 degree corner. "
                "Treat it as clean unless the flagged region is somewhere surprising."
                % (100.0 * thin["thin_fraction"], rules.min_wall_mm,
                   100.0 * rules.max_thin_fraction))
    except UnresolvableRule as exc:
        notes.append(str(exc))

    try:
        narrow = narrow_channels(grid, rules.min_channel_mm)
        metrics["narrow_fraction"] = narrow["narrow_fraction"]
        metrics["narrow_voxels"] = float(narrow["narrow_voxels"])
        if narrow["narrow_fraction"] > rules.max_thin_fraction:
            violations.append(
                "narrow channel: %.1f%% of the enclosed void is narrower than %g mm"
                % (100.0 * narrow["narrow_fraction"], rules.min_channel_mm))
    except UnresolvableRule as exc:
        notes.append(str(exc))

    tri = np.asarray(triangles, dtype=float).reshape(-1, 3)
    extent = tri.max(axis=0) - tri.min(axis=0)
    # Sorted against sorted, so a part is judged on whether it fits in SOME orientation rather
    # than in the one it happens to be modelled in.
    fits = bool(np.all(np.sort(extent) <= np.sort(np.asarray(rules.build_volume_mm)) + 1e-9))
    metrics["fits_build_volume"] = 1.0 if fits else 0.0
    metrics["largest_dimension_mm"] = float(extent.max())
    if not fits:
        violations.append(
            "build volume: the part is %.1f x %.1f x %.1f mm and the machine is %g x %g x %g, "
            "so it will not fit in any orientation. Split it or use a bigger machine."
            % (*extent, *rules.build_volume_mm))

    ok = not violations
    metrics["printable"] = 1.0 if ok else 0.0
    return Printability(ok=ok, metrics=metrics, violations=violations, notes=notes, rules=rules)
