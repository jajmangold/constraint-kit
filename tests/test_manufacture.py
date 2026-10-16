#!/usr/bin/env python3
"""Manufacturing rules as CONSTRAINTS, not a post-hoc audit (issue #18).

    python3 tests/test_manufacture.py

The point of the issue title. An optimizer told to minimise mass subject to strength will happily
return a 0.9 mm wall at an 80 degree overhang with a 0.6 mm cooling channel, because nothing in
its problem statement said otherwise. Auditing that afterwards and sending it back is a loop with
a human in it; making printability a constraint puts it inside the search, where the optimizer
simply never proposes the unprintable design in the first place.

Every rule here is computed from geometry rather than from a parameter name, which is what makes
it general. A wall is thin because the solid is thin there, not because something called
`wall_thickness` is small -- and a part with a thin region nobody named is exactly the part an
audit misses.

The three rules carry very different confidence, and the module is explicit about which is which:

* **Overhang is EXACT.** It is a question about surface normals, and the tessellation has them to
  whatever accuracy it was asked for. No resolution limit, no approximation.
* **Thin walls and narrow channels are RESOLUTION-LIMITED.** They are morphological openings on
  the voxel grid, so a wall thinner than the pitch is invisible and one near the pitch is
  uncertain. The check says so, and refuses to answer when the rule is finer than the grid.
* **Build volume is trivial** and included because it is the one that wastes a whole print.

The thin-wall test is a morphological **opening**: a region of the solid can contain a ball of
radius r if and only if it survives erosion by r followed by dilation by r. So the solid minus
its own opening is exactly the set of places thinner than 2r, which is a definition rather than a
heuristic, and it is why this finds a thin region anywhere rather than only where a dimension was
named.
"""
from __future__ import annotations

import math
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from constraint_kit.fea import Grid, occupancy_from_triangles  # noqa: E402
from constraint_kit.manufacture import (  # noqa: E402
    Rules, UnresolvableRule, narrow_channels, overhangs, printability, thin_regions,
)

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


# ------------------------------------------------------------------- triangle-soup fixtures

def _cross2(o, a, b) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _inside(p, a, b, c) -> bool:
    """Is `p` strictly inside triangle `abc` (assumed counter-clockwise)?"""
    return (_cross2(a, b, p) > 0.0 and _cross2(b, c, p) > 0.0 and _cross2(c, a, p) > 0.0)


def _ear_clip(prof) -> list:
    """Triangulate a simple counter-clockwise polygon. Correct for CONCAVE profiles.

    The textbook algorithm, here because the obvious shortcut is wrong: a fan from one vertex
    only triangulates a polygon that vertex can see all of, and a concave profile is exactly the
    case where it cannot. An ear is three consecutive vertices turning left with no other vertex
    inside the triangle they span; clipping it is always valid, and a simple polygon always has
    one.
    """
    poly = list(prof)
    out = []
    guard = 0
    while len(poly) > 3:
        guard += 1
        if guard > 10_000:
            raise RuntimeError("ear clipping made no progress; the profile is not simple")
        n = len(poly)
        for i in range(n):
            a, b, c = poly[(i - 1) % n], poly[i], poly[(i + 1) % n]
            if _cross2(a, b, c) <= 0.0:
                continue                                   # reflex or collinear: not an ear
            if any(_inside(p, a, b, c) for j, p in enumerate(poly)
                   if j not in ((i - 1) % n, i, (i + 1) % n)):
                continue                                   # another vertex is in the way
            out.append((a, b, c))
            poly.pop(i)
            break
        else:
            raise RuntimeError("no ear found; the profile is self-intersecting")
    out.append(tuple(poly))
    return out


def _extrude(profile_xz, width: float) -> np.ndarray:
    """A closed triangle soup from a 2-D profile in XZ, extruded along +Y, wound OUTWARD.

    One helper rather than three hand-wound fixtures, because hand-winding is where the bugs
    were. Three of these tests failed against entirely correct code until this existed: an
    inverted side-wall winding makes the TOP face of a part report a downward normal, so the
    overhang check found 360 mm2 of "unsupported" roof on a part whose only real overhang was 240
    mm2 of arm underside. The symptom reads exactly like a broken overhang check.

    So the orientation is derived rather than typed:

    * the profile is reversed if its signed area is negative, so it is always counter-clockwise
    * a side wall on edge `(p0 -> p1)` then has outward normal proportional to `(dz, 0, -dx)`,
      which is checked below against a unit square
    * a counter-clockwise fan in XZ has normal `-Y`, so it is the cap at `y = 0` and the cap at
      `y = width` is its reverse

    The cap is triangulated by EAR CLIPPING rather than by a fan. A fan from the first vertex is
    only valid for a profile that vertex can see all of, and the L fixture is concave -- a fan
    across its reflex corner produced a triangle lying partly outside the shape and wound
    BACKWARDS, 300 mm2 of it. Printed upright that was invisible, because cap faces point along
    Y and are never overhangs in a Z build; printed on its side it showed up as 300 mm2 of
    phantom unsupported area. It would also have broken ray-parity voxelisation, which counts
    crossings and cannot survive a face pointing the wrong way, so the thin-wall numbers on that
    fixture were suspect too.
    """
    prof = [(float(x), float(z)) for x, z in profile_xz]
    if len(prof) < 3:
        raise ValueError("a profile needs at least three points, got %d" % len(prof))
    signed = sum(prof[i][0] * prof[(i + 1) % len(prof)][1]
                 - prof[(i + 1) % len(prof)][0] * prof[i][1]
                 for i in range(len(prof)))
    if signed < 0.0:
        prof = prof[::-1]

    tris = []
    for a, b, c in _ear_clip(prof):
        near = [(a[0], 0.0, a[1]), (b[0], 0.0, b[1]), (c[0], 0.0, c[1])]
        far = [(a[0], width, a[1]), (b[0], width, b[1]), (c[0], width, c[1])]
        tris.append(near)                 # CCW in XZ gives -Y, the outward normal at y = 0
        tris.append(far[::-1])            # and its reverse at y = width

    n = len(prof)
    for i in range(n):
        (x0, z0), (x1, z1) = prof[i], prof[(i + 1) % n]
        a = (x0, 0.0, z0)
        b = (x1, 0.0, z1)
        c = (x1, width, z1)
        d = (x0, width, z0)
        tris.append([a, c, b])            # outward: (dz, 0, -dx)
        tris.append([a, d, c])
    return np.array(tris, dtype=float)


def _box(lo, hi) -> np.ndarray:
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    out = _extrude([(x0, z0), (x1, z0), (x1, z1), (x0, z1)], y1 - y0)
    out[:, :, 1] += y0
    return out


def _wedge(base: float, height: float, width: float) -> np.ndarray:
    """A right triangular prism whose HYPOTENUSE is an underside, so it is a real overhang.

    Profile in XZ: (0,0) - (0,h) - (b,h). The vertical edge is at x=0, the roof is at z=h, and
    the hypotenuse runs from (0,0) up to (b,h) facing down and to the right -- so its outward
    normal has a downward component and the face needs support.

    The angle from the build direction is `atan(base/height)` exactly, which is what makes this
    the fixture for testing a threshold rather than a tendency. `base == height` is exactly 45
    degrees.
    """
    return _extrude([(0.0, 0.0), (0.0, height), (base, height)], width)


def _ell(arm: float, thickness: float, height: float, width: float) -> np.ndarray:
    """An L-profile extruded along Y: a vertical post with a cantilevered arm at the top.

    The arm's UNDERSIDE is a genuine overhang -- downward-facing and well above the lowest point
    of the part -- which a lone floating box is not. A box is dropped onto the build plate by any
    slicer however high it was modelled, so its bottom face is supported and measuring it proves
    nothing. This has a face that is unsupported wherever the part sits.
    """
    a, s, h = arm, thickness, height
    return _extrude([(0.0, 0.0), (s, 0.0), (s, h - s), (a, h - s), (a, h), (0.0, h)], width)


@test
def test_the_fixture_builder_winds_every_face_outward():
    """The fixtures are test infrastructure, so they get a test. They were wrong before.

    A unit cube: six faces, each 1 mm2, with normals exactly along the six axes and summing to
    zero. A single inverted face breaks the sum, and an inverted side wall breaks the specific
    axis -- which is the failure that made the overhang check look broken.
    """
    tri = _box((0, 0, 0), (1, 1, 1))
    cross = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    area = 0.5 * np.linalg.norm(cross, axis=1)
    assert abs(area.sum() - 6.0) < 1e-12, area.sum()
    # the area-weighted normal of any closed surface is exactly zero
    assert np.allclose(cross.sum(axis=0), 0.0, atol=1e-12), cross.sum(axis=0)
    # and exactly 1 mm2 points along each of the six axes
    normal = cross / np.linalg.norm(cross, axis=1)[:, None]
    for axis in range(3):
        for sign in (-1.0, 1.0):
            facing = np.isclose(normal[:, axis], sign, atol=1e-12)
            assert abs(area[facing].sum() - 1.0) < 1e-12, (axis, sign, area[facing].sum())

    # And a CONCAVE profile, which is where this was actually wrong. A fan triangulation across
    # the L's reflex corner emitted a backwards triangle; the area-weighted normal sum catches
    # exactly that, and the cap area must equal the profile area rather than exceeding it.
    ell = _ell(30.0, 10.0, 40.0, 12.0)
    cross = np.cross(ell[:, 1] - ell[:, 0], ell[:, 2] - ell[:, 0])
    assert np.allclose(cross.sum(axis=0), 0.0, atol=1e-9), (
        "the L is not a closed oriented surface: %s" % cross.sum(axis=0))
    area = 0.5 * np.linalg.norm(cross, axis=1)
    normal = cross / np.linalg.norm(cross, axis=1)[:, None]
    profile_area = 10.0 * 40.0 + 20.0 * 10.0        # post plus arm
    for sign in (-1.0, 1.0):
        cap = area[np.isclose(normal[:, 1], sign, atol=1e-12)].sum()
        assert abs(cap - profile_area) < 1e-9, (sign, cap, profile_area)


# ----------------------------------------------------------------------------- overhangs

@test
def test_a_box_has_no_overhang_because_its_only_down_face_is_on_the_plate():
    """The first thing to get right, and the easy thing to get wrong.

    A box's bottom face points straight down, which is the worst possible overhang angle -- and
    it needs no support whatsoever, because it is lying on the build plate. A check that counts
    it reports every part ever printed as unprintable.
    """
    out = overhangs(_box((0, 0, 0), (20, 20, 10)), max_angle_deg=45.0)
    assert out["unsupported_area_mm2"] == 0.0, out
    assert out["plate_area_mm2"] > 390.0, (
        "the 400 mm2 bottom face should be recognised as plate-supported: %s" % out)
    assert out["worst_angle_deg"] == 0.0, out
    assert out["fraction"] == 0.0, out


@test
def test_an_overhanging_arm_is_found_with_its_exact_area():
    """An L: the cantilevered arm's underside is unsupported wherever the part sits.

    A floating box is NOT this test. A box is dropped onto the plate by any slicer, however high
    it was modelled, so its bottom face is supported and measuring it proves nothing. The L has
    a down-facing face above its own lowest point, which is what an overhang is.
    """
    arm, thickness, height, width = 30.0, 10.0, 40.0, 12.0
    out = overhangs(_ell(arm, thickness, height, width), max_angle_deg=45.0)
    expect = (arm - thickness) * width            # the arm underside, 20 x 12
    assert abs(out["unsupported_area_mm2"] - expect) < 1e-6, (out, expect)
    assert abs(out["worst_angle_deg"] - 90.0) < 1e-6, (
        "a flat underside is the full 90 degrees from the build direction: %s" % out)
    assert abs(out["plate_area_mm2"] - thickness * width) < 1e-6, (
        "the post's own footprint is on the plate: %s" % out)


@test
def test_the_overhang_threshold_is_exact_at_forty_five_degrees():
    """A 45 degree slope is the classic limit, and the fixture is exactly 45 degrees.

    `base == height` makes the sloped face's normal sit at exactly 45 degrees, so a 44 degree
    rule must reject it and a 46 degree rule must accept it. An off-by-one in the angle
    convention -- from the vertical rather than from the horizontal -- shows up here as the
    difference between everything passing and everything failing.
    """
    tri = _wedge(base=20.0, height=20.0, width=10.0)
    assert overhangs(tri, max_angle_deg=46.0)["unsupported_area_mm2"] == 0.0
    strict = overhangs(tri, max_angle_deg=44.0)
    expect = math.sqrt(2.0) * 20.0 * 10.0            # the sloped face area
    assert abs(strict["unsupported_area_mm2"] - expect) < 1e-6, (strict, expect)
    assert abs(strict["worst_angle_deg"] - 45.0) < 1e-6, strict


@test
def test_a_shallower_slope_needs_no_support_and_a_steeper_one_does():
    shallow = _wedge(base=40.0, height=20.0, width=10.0)      # 63 degrees from vertical
    steep = _wedge(base=10.0, height=40.0, width=10.0)        # 14 degrees from vertical
    assert overhangs(shallow, max_angle_deg=45.0)["unsupported_area_mm2"] > 0.0
    assert overhangs(steep, max_angle_deg=45.0)["unsupported_area_mm2"] == 0.0


@test
def test_the_build_direction_is_an_argument_so_orientation_can_be_compared():
    """Which way up to print changes the answer, and that is the decision being supported.

    The same L printed upright needs support under its arm; printed on its side, that face is
    vertical and needs none -- while a different face becomes the problem. Being able to ask the
    question per orientation is the point.
    """
    tri = _ell(30.0, 10.0, 40.0, 12.0)
    up = overhangs(tri, max_angle_deg=45.0, build_dir=(0, 0, 1))
    sideways = overhangs(tri, max_angle_deg=45.0, build_dir=(0, 1, 0))
    assert up["unsupported_area_mm2"] > 0.0, up
    assert sideways["unsupported_area_mm2"] != up["unsupported_area_mm2"], (up, sideways)
    # printed along Y the L-profile faces are the caps, which are then flat on the plate
    assert sideways["unsupported_area_mm2"] == 0.0, sideways


@test
def test_overhangs_refuse_a_nonsensical_angle_or_direction():
    tri = _box((0, 0, 0), (10, 10, 10))
    for bad in (-1.0, 0.0, 90.0, 120.0):
        try:
            overhangs(tri, max_angle_deg=bad)
        except ValueError as exc:
            assert "angle" in str(exc).lower(), str(exc)
            continue
        raise AssertionError("max_angle_deg=%s should have been refused" % bad)
    try:
        overhangs(tri, max_angle_deg=45.0, build_dir=(0, 0, 0))
    except ValueError as exc:
        assert "direction" in str(exc).lower() or "zero" in str(exc).lower(), str(exc)
        return
    raise AssertionError("a zero build direction must be refused")


# ----------------------------------------------------------------------- thin walls

def _grid(tri: np.ndarray, pitch: float) -> Grid:
    return occupancy_from_triangles(tri, pitch, pad=2)


@test
def test_a_thick_slab_has_no_thin_region_and_a_thin_one_does():
    """The morphological opening, on geometry whose thickness is known by construction."""
    pitch = 0.5
    thick = thin_regions(_grid(_box((0, 0, 0), (20, 20, 8)), pitch), min_wall_mm=3.0)
    thin = thin_regions(_grid(_box((0, 0, 0), (20, 20, 1.5)), pitch), min_wall_mm=3.0)
    print("      8 mm slab %.3f thin, 1.5 mm slab %.3f thin (3 mm rule)"
          % (thick["thin_fraction"], thin["thin_fraction"]))

    # NOT zero for the thick slab: an opening cannot cover a 90 degree corner, so every convex
    # edge registers. That is geometry, not a bug, and it is why the verdict is a fraction.
    assert thick["thin_fraction"] < 0.10, thick
    assert thin["thin_fraction"] > 0.9, (
        "a 1.5 mm slab against a 3 mm rule is thin everywhere: %s" % thin)
    # the separation is what has to be big, and an order of magnitude is plenty
    assert thin["thin_fraction"] > 8.0 * max(thick["thin_fraction"], 1e-9), (thick, thin)


@test
def test_the_thinnest_dimension_is_what_decides_not_the_largest():
    """A plate is thin because of its thickness, however big its face is."""
    pitch = 0.5
    wide = thin_regions(_grid(_box((0, 0, 0), (60, 60, 1.0)), pitch), min_wall_mm=2.0)
    assert wide["thin_fraction"] > 0.9, wide
    cube = thin_regions(_grid(_box((0, 0, 0), (6, 6, 6)), pitch), min_wall_mm=2.0)
    assert cube["thin_fraction"] < 0.25, cube
    assert wide["thin_fraction"] > 3.0 * cube["thin_fraction"], (wide, cube)


@test
def test_a_rule_finer_than_the_grid_is_refused_rather_than_guessed():
    """A wall thinner than the pitch is invisible, and answering anyway would be a lie.

    This is the honest boundary of a voxel method, and the one place a printability check could
    most easily reassure somebody about a wall it cannot see. So it refuses, and names the pitch
    that would be needed.
    """
    grid = _grid(_box((0, 0, 0), (20, 20, 8)), 1.0)
    try:
        thin_regions(grid, min_wall_mm=0.8)            # finer than the 1.0 mm pitch
    except UnresolvableRule as exc:
        text = str(exc)
        assert "pitch" in text and "0.8" in text, text
        assert "0.2" in text or "0.26" in text or "refine" in text.lower(), (
            "it should say what pitch WOULD resolve it: " + text)
        return
    raise AssertionError("a rule finer than the grid must be refused")


@test
def test_thin_regions_reports_how_much_it_can_be_trusted():
    """The ratio of rule to pitch is the confidence, and it travels with the answer."""
    grid = _grid(_box((0, 0, 0), (20, 20, 8)), 0.5)
    out = thin_regions(grid, min_wall_mm=4.0)
    assert out["voxels_across_the_rule"] >= 8.0, out
    assert "pitch" in out["note"].lower(), out["note"]


# ------------------------------------------------------------------- narrow channels

@test
def test_a_narrow_channel_is_found_and_a_wide_one_is_not():
    """The dual of the thin-wall test: an opening on the EMPTY space instead of the solid.

    A cooling passage or a bolt hole too small for the nozzle to trace is as unprintable as a
    wall too thin to extrude, and it is the same morphological question asked of the complement.
    """
    pitch = 0.5
    # Built explicitly rather than voxelised, because the void has to be ENCLOSED. An earlier
    # version cut a slot right through a slab, which is open to the air on both sides -- so the
    # flood fill correctly discarded it and the test measured nothing. Air around a part is not
    # a channel; only what the solid encloses is.
    def block_with_void(void_voxels: int) -> Grid:
        occ = np.ones((48, 48, 24), dtype=float)
        occ[0, :, :] = occ[-1, :, :] = 0.0          # a shell of empty padding, so the flood
        occ[:, 0, :] = occ[:, -1, :] = 0.0          # fill has somewhere to start
        occ[:, :, 0] = occ[:, :, -1] = 0.0
        mid = 24
        half = max(1, void_voxels // 2)
        occ[8:40, mid - half:mid + half, 8:16] = 0.0
        return Grid(density=occ, pitch=(pitch,) * 3)

    narrow = narrow_channels(block_with_void(2), min_channel_mm=3.0)      # 1 mm void
    wide = narrow_channels(block_with_void(16), min_channel_mm=3.0)       # 8 mm void
    print("      1 mm void %.3f narrow, 8 mm void %.3f narrow (3 mm rule)"
          % (narrow["narrow_fraction"], wide["narrow_fraction"]))
    assert narrow["narrow_fraction"] > 0.9, narrow
    assert wide["narrow_fraction"] < 0.25, wide


# ------------------------------------------------------------------- the whole check

@test
def test_printability_rolls_the_rules_up_and_names_what_failed():
    tri = _ell(30.0, 10.0, 40.0, 12.0)                 # a cantilevered arm: pure overhang
    grid = _grid(tri, 0.5)
    rules = Rules(min_wall_mm=2.0, min_channel_mm=2.0, max_overhang_deg=45.0,
                  build_volume_mm=(220.0, 220.0, 250.0))
    out = printability(tri, grid, rules)

    assert out.ok is False, out.report()
    assert "overhang" in " ".join(out.violations).lower(), out.violations
    assert out.metrics["unsupported_area_mm2"] > 0.0, out.metrics
    assert out.metrics["fits_build_volume"] == 1.0, out.metrics
    text = out.report()
    assert "overhang" in text.lower(), text
    bad = sorted({c for c in text if ord(c) > 127})
    assert not bad, "non-ASCII %r in a console report:\n%s" % (bad, text)


@test
def test_a_part_too_big_for_the_printer_fails_on_that_alone():
    tri = _box((0, 0, 0), (400.0, 20.0, 10.0))
    grid = _grid(tri, 2.0)
    rules = Rules(min_wall_mm=2.0, min_channel_mm=2.0, max_overhang_deg=45.0,
                  build_volume_mm=(220.0, 220.0, 250.0))
    out = printability(tri, grid, rules)
    assert out.ok is False
    assert out.metrics["fits_build_volume"] == 0.0, out.metrics
    assert any("build volume" in v.lower() for v in out.violations), out.violations
    # and it should say by how much, because that decides whether to split or rotate
    assert "400" in out.report() or "220" in out.report(), out.report()


@test
def test_a_printable_part_passes_every_rule_and_says_which_were_checked():
    tri = _box((0, 0, 0), (40, 40, 20))
    grid = _grid(tri, 1.0)
    rules = Rules(min_wall_mm=3.0, min_channel_mm=3.0, max_overhang_deg=45.0,
                  build_volume_mm=(220.0, 220.0, 250.0))
    out = printability(tri, grid, rules)
    assert out.ok is True, out.report()
    assert out.violations == [], out.violations
    for key in ("unsupported_area_mm2", "thin_fraction", "narrow_fraction",
                "fits_build_volume", "printable"):
        assert key in out.metrics, (key, sorted(out.metrics))
    assert out.metrics["printable"] == 1.0, out.metrics


@test
def test_every_metric_is_a_plain_number_so_it_can_be_constrained():
    """The whole point of #18: these go into an optimizer, not into a report a human reads."""
    tri = _box((0, 0, 0), (40, 40, 20))
    grid = _grid(tri, 1.0)
    out = printability(tri, grid, Rules())
    for key, value in out.metrics.items():
        assert isinstance(value, float), (key, type(value).__name__)
        assert not isinstance(value, bool), key
        assert math.isfinite(value) or value == float("inf"), (key, value)


@test
def test_rules_refuse_values_that_cannot_mean_anything():
    for kwargs, expect in (({"min_wall_mm": 0.0}, "min_wall"),
                           ({"min_channel_mm": -1.0}, "min_channel"),
                           ({"max_overhang_deg": 0.0}, "overhang"),
                           ({"max_thin_fraction": 1.0}, "thin"),
                           ({"max_thin_fraction": -0.1}, "thin"),
                           ({"build_volume_mm": (0.0, 10.0, 10.0)}, "build volume")):
        try:
            Rules(**kwargs)
        except ValueError as exc:
            assert expect.split("_")[0] in str(exc).lower(), (kwargs, str(exc))
            continue
        raise AssertionError("%s should have been refused" % kwargs)


def main() -> int:
    failed = []
    for fn in TESTS:
        try:
            fn()
            print("  PASS  %s" % fn.__name__)
        except Exception as exc:  # noqa: BLE001
            failed.append(fn.__name__)
            print("  FAIL  %s: %s" % (fn.__name__, exc))
            traceback.print_exc()
    print("\n%d/%d passed" % (len(TESTS) - len(failed), len(TESTS)))
    if failed:
        print("FAILED: %s" % failed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
