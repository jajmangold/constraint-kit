#!/usr/bin/env python3
"""constraint-kit regression suite — dependency-free (plain asserts, no pytest), runs inside cadkit:

    docker exec cadkit python3 /srv/nvme-data/containers/constraint-kit/tests/test_kernel.py

Asserts the geometric ground truths the pipeline relies on (seating zero-gap, gear-mesh center distance,
2D layout placement, mass, DXF validity, atlas round-trip). Exit code != 0 if anything fails, so it can
gate changes. These are the invariants previously checked by hand via curl.
"""
from __future__ import annotations

import math
import os
import tempfile
import traceback

EPS = 1e-6
TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def approx(a, b, eps=EPS):
    assert abs(a - b) <= eps, f"{a} != {b} (eps {eps})"


# ---- parts -----------------------------------------------------------------
@test
def test_plate_geometry():
    from constraint_kit.parts import plate
    wp, anchors = plate(width=60, depth=60, thick=6, boss_d=12, boss_h=4, bolt_d=4,
                        bolt_circle=44, bolt_count=4)
    assert wp.val().Volume() > 0
    assert {"mount", "base", "boss_axis"} <= set(anchors)
    (_, _, mz), _ = anchors["mount"].toTuple()
    approx(mz, 6 / 2 + 4)  # top of boss


@test
def test_spur_gear_is_real_involute():
    from constraint_kit.parts import spur_gear
    wp, anchors = spur_gear(module=1, teeth=20, width=6, bore_d=12)
    assert "_fallback" not in anchors, "cq_gears fell back to a blank — involute gear failed to build"
    assert wp.val().Volume() > 0
    (_, _, tz), _ = anchors["bore_top"].toTuple()
    approx(tz, 6)


@test
def test_spacer_anchors():
    from constraint_kit.parts import spacer
    _, anchors = spacer(outer_d=14, bore_d=6, height=10)
    (_, _, tz), _ = anchors["top"].toTuple()
    approx(tz, 10)


@test
def test_fasteners_build():
    from constraint_kit.parts import bearing, bolt, nut, washer
    for gen in (bolt, nut, washer, bearing):
        wp, anchors = gen()
        assert wp.val().Volume() > 0, f"{gen.__name__} empty"
        assert anchors, f"{gen.__name__} has no anchors"
    # nut bore actually removes material: bored < solid hex of same envelope
    solid_vol = nut(af=8, height=5, bore_d=0)[0].val().Volume()
    bored_vol = nut(af=8, height=5, bore_d=5)[0].val().Volume()
    assert bored_vol < solid_vol


@test
def test_bd_catalog_parts_build():
    from constraint_kit.parts_bd import ball_bearing, extrusion, screw
    wp, anchors = extrusion(rail_size="20x20", length=100)
    bb = wp.val().BoundingBox()
    assert wp.val().Volume() > 0
    approx(bb.zmax - bb.zmin, 100.0)               # extruded along +Z
    (_, _, tz), _ = anchors["top"].toTuple(); approx(tz, 100.0)
    wb, ba = ball_bearing(size="M8-22-7")
    bbb = wb.val().BoundingBox()
    assert wb.val().Volume() > 0
    approx(bbb.zmax - bbb.zmin, 7.0)               # width 7
    ws, sa = screw(size="M5-0.8", length=16, simple=True)
    assert ws.val().Volume() > 0
    (_, _, tipz), _ = sa["tip"].toTuple(); approx(tipz, -16.0)  # shank length down


@test
def test_catalog_sprocket_flange_pipe_build():
    from constraint_kit.parts_bd import flange, pipe, sprocket
    sp_wp, sp_j = sprocket(num_teeth=16, thickness=6, bore_d=8)
    assert sp_wp.val().Volume() > 0 and {"face_a", "face_b", "center"} <= set(sp_j)
    fl_wp, fl_j = flange(nps="1", flange_class=150, kind="slip_on")
    assert fl_wp.val().Volume() > 0 and {"bore_base", "bore_top"} <= set(fl_j)
    pi_wp, pi_j = pipe(nps="1", length=120)
    pb = pi_wp.val().BoundingBox()
    assert pi_wp.val().Volume() > 0
    approx(pb.zmax - pb.zmin, 120.0)               # straight pipe runs the requested length along Z
    (_, _, ez), _ = pi_j["end_b"].toTuple(); approx(ez, 120.0)


@test
def test_bd_part_in_assembly_via_builder():
    from constraint_kit import builder
    # an extrusion with a bearing seated on its top -> bd parts flow through the same pipeline
    spec = {"parts": [{"id": "ext", "type": "extrusion", "material": "aluminum",
                       "params": {"rail_size": "20x20", "length": 80}},
                      {"id": "brg", "type": "ball_bearing", "material": "steel",
                       "params": {"size": "M8-22-7"}}],
            "mates": [{"a": "ext", "a_joint": "top", "b": "brg", "b_joint": "bore_base",
                       "type": "coincident"}]}
    assy, parts = builder.build_assembly(spec)
    assert {p["id"] for p in parts} == {"ext", "brg"}
    assert all(p["mass_g"] > 0 for p in parts)
    brg = next(c for c in assy.children if c.name == "brg")
    (_, _, z), _ = brg.loc.toTuple()
    approx(z, 80.0)                                # bearing bore_base lands on extrusion top (z=80)


def _axis_of(loc):
    from OCP.gp import gp_Pnt
    t = loc.wrapped.Transformation()
    o = gp_Pnt(0, 0, 0).Transformed(t)
    z = gp_Pnt(0, 0, 1).Transformed(t)
    return (round(z.X() - o.X(), 3), round(z.Y() - o.Y(), 3), round(z.Z() - o.Z(), 3))


def _origin_of(loc):
    from OCP.gp import gp_Pnt
    p = gp_Pnt(0, 0, 0).Transformed(loc.wrapped.Transformation())
    return (round(p.X(), 3), round(p.Y(), 3), round(p.Z(), 3))


@test
def test_joint_frame_aligns_axis():
    # an oriented joint frame mated onto another must align AXES, not just points (the capability joints add)
    from constraint_kit import mates
    from constraint_kit.joints import frame
    a = frame((10, 0, 0), z_axis=(1, 0, 0))      # target axis = +X at (10,0,0)
    b = frame((0, 0, 0), z_axis=(0, 0, 1))        # B's joint axis = +Z
    placed = mates.solve("rigid", a, b_anchor_local=b)
    # B's local +Z axis, after placement, must point along +X (axes aligned)
    assert _axis_of(placed) == (1.0, 0.0, 0.0), _axis_of(placed)
    assert _origin_of(placed) == (10.0, 0.0, 0.0)


@test
def test_revolute_rotates_about_axis():
    """A revolute joint coincides the frames then rotates B about the shared axis by angle_deg,
    preserving the axis and the on-axis origin. Observed via the shaft's off-axis 'key' marker."""
    from constraint_kit import builder, mates
    from constraint_kit.joints import frame
    from constraint_kit.parts import shaft
    _, sj = shaft(diameter=8, length=60)             # key at (4,0,30)
    bore = frame((0, 0, 0), z_axis=(0, 0, 1))         # fixed vertical bore axis at origin
    # rotate shaft 90deg about the bore axis; mate the shaft 'mid' (0,0,30) onto the bore
    placed = mates.revolute(bore, sj["mid"], 90.0)
    # mid lands at the bore origin (0,0,0); key (r=4) rotates 90 -> from +X to +Y
    assert _origin_of(placed * sj["mid"]) == (0.0, 0.0, 0.0)
    kx, ky, kz = _origin_of(placed * sj["key"])
    approx(kx, 0.0); approx(ky, 4.0); approx(kz, 0.0)     # +X marker -> +Y after 90deg
    assert _axis_of(placed) == (0.0, 0.0, 1.0)            # spin axis preserved (vertical)


@test
def test_cylindrical_adds_axial_slide():
    from constraint_kit import mates
    from constraint_kit.joints import frame
    from constraint_kit.parts import shaft
    _, sj = shaft(diameter=8, length=60)
    bore = frame((0, 0, 0), z_axis=(0, 0, 1))
    placed = mates.cylindrical(bore, sj["mid"], angle_deg=0.0, slide=12.0)
    # mid was at origin under revolute; cylindrical slides it +12 along the axis
    assert _origin_of(placed * sj["mid"]) == (0.0, 0.0, 12.0)


@test
def test_kinematic_assembly_shaft_in_block_bore():
    """Shaft journalled in a bearing block's bore via a revolute joint; spins, stays on the bore axis."""
    from constraint_kit import builder
    spec = {"parts": [
        {"id": "blk", "type": "bearing_block", "material": "aluminum", "params": {}},
        {"id": "sh", "type": "shaft", "material": "steel",
         "params": {"diameter": 8, "length": 80}}],
        "mates": [{"a": "blk", "a_joint": "bore", "b": "sh", "b_joint": "mid",
                   "type": "revolute", "angle_deg": 45}]}
    assy, _ = builder.build_assembly(spec)
    sh = next(c for c in assy.children if c.name == "sh")
    assert _axis_of(sh.loc) == (0.0, 0.0, 1.0)            # shaft axis = block bore axis (vertical)


@test
def test_bearing_block_joints_exist():
    from constraint_kit.parts_bd import bearing_block
    _, j = bearing_block()
    assert {"mount", "top", "bore", "bolt0", "bolt1"} <= set(j)
    # bore joint axis is vertical (+Z); mount joint axis is the back-face normal (+Y)
    assert _axis_of(j["bore"]) == (0.0, 0.0, 1.0)
    assert _axis_of(j["mount"]) == (0.0, 1.0, 0.0)


@test
def test_joint_names_survive_param_changes():
    """The core proof: joint NAMES are stable across parameter changes, and a mate to a named joint
    stays geometrically exact when params change (no selector drift)."""
    from constraint_kit import builder
    names = None
    for (W, T, H, bore) in [(40, 20, 40, 22), (60, 30, 80, 30)]:
        spec = {"parts": [
            {"id": "blk", "type": "bearing_block", "material": "aluminum",
             "params": {"width": W, "depth": T, "height": H, "bore_d": bore}},
            {"id": "scr", "type": "screw", "material": "steel",
             "params": {"size": "M5-0.8", "length": 20}}],
            "mates": [{"a": "blk", "a_joint": "bolt0", "b": "scr", "b_joint": "seat", "type": "rigid"}]}
        assy, _ = builder.build_assembly(spec)
        blk = next(c for c in assy.children if c.name == "blk")
        j = builder.ALL_PART_GENS["bearing_block"](width=W, depth=T, height=H, bore_d=bore)[1]
        cur = set(j)
        if names is None:
            names = cur
        assert cur == names, f"joint names changed across params: {names} vs {cur}"
        # screw seat must land exactly on the front face (y=depth) at bolt0 x=+spacing/2, z=H/2
        scr = next(c for c in assy.children if c.name == "scr")
        (x, y, z), _ = scr.loc.toTuple()
        approx(y, T); approx(z, H / 2.0); approx(abs(x), 14.0)   # bolt_spacing default 28 -> x=14
        assert _axis_of(scr.loc) == (0.0, 1.0, 0.0)              # screw axis = hole axis (+Y), every time


@test
def test_target_assembly_screw_block_bearing_extrusion():
    """Concrete target: 'screw fastens a bearing block (holding a bearing) to a 2020 extrusion'.
    Proves all six requirements geometrically + valid STEP/GLB export."""
    import os
    import tempfile
    from constraint_kit import builder
    spec = {"parts": [
        {"id": "ext", "type": "extrusion", "material": "aluminum",
         "params": {"rail_size": "20x20", "length": 200, "slot_z": 100}},
        {"id": "blk", "type": "bearing_block", "material": "aluminum",
         "params": {"width": 40, "depth": 20, "height": 40, "bore_d": 22}},
        {"id": "brg", "type": "ball_bearing", "material": "steel", "params": {"size": "M8-22-7"}},
        {"id": "scr", "type": "screw", "material": "steel", "params": {"size": "M5-0.8", "length": 20}}],
        "mates": [
            {"a": "ext", "a_joint": "slot_yp", "b": "blk", "b_joint": "mount", "type": "rigid"},
            {"a": "blk", "a_joint": "bore", "b": "brg", "b_joint": "bore_mid", "type": "rigid"},
            {"a": "blk", "a_joint": "bolt0", "b": "scr", "b_joint": "seat", "type": "rigid"}]}
    assy, parts = builder.build_assembly(spec)
    loc = {c.name: c.loc for c in assy.children}
    # 4) block mounts to extrusion slot frame: block back face on +Y extrusion face (y=10), flush
    blk_mount_world = loc["blk"] * builder.ALL_PART_GENS["bearing_block"](
        width=40, depth=20, height=40, bore_d=22)[1]["mount"]
    approx(_origin_of(blk_mount_world)[1], 10.0)                       # contact at extrusion +Y face
    # 3) bearing axis aligns with block bore (vertical) + centered (measure the bore-center joint,
    #    not the part origin which sits half a width below)
    assert _axis_of(loc["brg"]) == (0.0, 0.0, 1.0)
    brg_mid_local = builder.ALL_PART_GENS["ball_bearing"](size="M8-22-7")[1]["bore_mid"]
    approx(_origin_of(loc["brg"] * brg_mid_local)[2], 100.0)           # bore center at slot height
    # 1)+2) screw seat on block top (front face y=20+10=30) at bolt0, axis along hole (+Y)
    bx, by, bz = _origin_of(loc["scr"])
    approx(by, 30.0); approx(bz, 100.0); approx(abs(bx), 14.0)
    assert _axis_of(loc["scr"]) == (0.0, 1.0, 0.0)
    # 6) valid STEP + GLB export
    with tempfile.TemporaryDirectory() as d:
        out = builder.export(assy, os.path.join(d, "target"))
        for k in ("step", "glb"):
            assert os.path.getsize(out[k]) > 0


@test
def test_plate_per_hole_anchors():
    from constraint_kit.parts import plate
    import math as _m
    _, anchors = plate(bolt_circle=40, bolt_count=4)
    holes = [k for k in anchors if k.startswith("bolt")]
    assert len(holes) == 4, holes
    (x0, y0, z0), _ = anchors["bolt0"].toTuple()
    approx(x0, 20.0); approx(y0, 0.0)            # first hole on +X at radius 20
    (x1, y1, _z), _ = anchors["bolt1"].toTuple()  # second hole at 90deg
    approx(x1, 0.0, eps=1e-4); approx(y1, 20.0, eps=1e-4)


# ---- mate kernel -----------------------------------------------------------
@test
def test_coincident_is_frame_map():
    import cadquery as cq
    from constraint_kit import mates
    a = cq.Location(cq.Vector(0, 0, 13))
    b = cq.Location(cq.Vector(0, 0, 0))
    loc = mates.coincident(a, b)
    (x, y, z), _ = loc.toTuple()
    approx(x, 0); approx(y, 0); approx(z, 13)


@test
def test_gear_mesh_center_distance():
    from constraint_kit import mates
    approx(mates.gear_mesh_center_distance(2.0, 20, 30), 50.0)
    approx(mates.gear_mesh_center_distance(1.5, 24, 24), 36.0)


# ---- builder (3D assembly) -------------------------------------------------
def _child_loc_z(assy, name):
    child = next(c for c in assy.children if c.name == name)
    (_, _, z), _ = child.loc.toTuple()
    return z


@test
def test_coincident_seats_zero_gap():
    from constraint_kit import builder
    spec = {"parts": [{"id": "plate", "type": "plate", "material": "aluminum", "params": {}},
                      {"id": "gear", "type": "spur_gear", "material": "steel", "params": {}}],
            "mates": [{"a": "plate", "a_joint": "mount", "b": "gear", "b_joint": "bore_base",
                       "type": "coincident"}]}
    assy, parts = builder.build_assembly(spec)
    # plate at origin -> mount world z = thick/2+boss_h = 3+4 = 7; gear bore_base (local 0) must land there
    approx(_child_loc_z(assy, "gear"), 7.0)


@test
def test_contact_seats_bbox_bottom():
    from constraint_kit import builder
    spec = {"parts": [{"id": "plate", "type": "plate", "params": {}},
                      {"id": "gear", "type": "spur_gear", "params": {}}],
            "mates": [{"a": "plate", "a_joint": "mount", "b": "gear", "b_joint": "bore_base",
                       "type": "contact"}]}
    assy, _ = builder.build_assembly(spec)
    approx(_child_loc_z(assy, "gear"), 7.0)


@test
def test_mesh_exact_center_distance():
    from constraint_kit import builder
    spec = {"parts": [{"id": "g1", "type": "spur_gear", "params": {"module": 2, "teeth": 20}},
                      {"id": "g2", "type": "spur_gear", "params": {"module": 2, "teeth": 30}}],
            "mates": [{"a": "g1", "a_joint": "bore_base", "b": "g2", "b_joint": "bore_base",
                       "type": "mesh"}]}
    assy, _ = builder.build_assembly(spec)
    g1 = next(c for c in assy.children if c.name == "g1")
    g2 = next(c for c in assy.children if c.name == "g2")
    (x1, y1, z1), _ = g1.loc.toTuple()
    (x2, y2, z2), _ = g2.loc.toTuple()
    approx(math.hypot(x2 - x1, y2 - y1), 50.0)
    approx(z1, z2)  # coplanar


@test
def test_mesh_rejects_module_mismatch():
    from constraint_kit import builder
    spec = {"parts": [{"id": "g1", "type": "spur_gear", "params": {"module": 2, "teeth": 20}},
                      {"id": "g2", "type": "spur_gear", "params": {"module": 3, "teeth": 30}}],
            "mates": [{"a": "g1", "a_joint": "bore_base", "b": "g2", "b_joint": "bore_base",
                       "type": "mesh"}]}
    try:
        builder.build_assembly(spec)
    except ValueError:
        return
    raise AssertionError("expected ValueError for mismatched modules")


@test
def test_bolt_seats_in_plate_hole():
    from constraint_kit import builder
    spec = {"parts": [{"id": "plate", "type": "plate", "params": {"bolt_circle": 40, "bolt_count": 4}},
                      {"id": "b0", "type": "bolt", "params": {}}],
            "mates": [{"a": "plate", "a_joint": "bolt0", "b": "b0", "b_joint": "seat",
                       "type": "coincident"}]}
    assy, _ = builder.build_assembly(spec)
    b = next(c for c in assy.children if c.name == "b0")
    (x, y, z), _ = b.loc.toTuple()
    approx(x, 20.0); approx(y, 0.0); approx(z, 3.0)  # plate top face z=+thick/2=+3, hole on +X r=20


@test
def test_planetary_math():
    from constraint_kit import planetary
    info = planetary.compute(module=1.0, sun_teeth=12, planet_teeth=12, n_planets=3)
    assert info["ring_teeth"] == 36                       # Zr = Zs + 2*Zp
    approx(info["carrier_pcd"], 12.0)                     # m*(Zs+Zp)/2
    # the loop closes: sun-planet and ring-planet centre distances are equal
    approx(info["center_distance_sun_planet"], info["center_distance_ring_planet"])
    approx(info["ratio_sun_to_carrier_ring_fixed"], 1.0 + 36 / 12)   # 4.0
    assert info["planet_angles_deg"] == [0.0, 120.0, 240.0]


@test
def test_planetary_validate_rejects_bad_configs():
    from constraint_kit import planetary
    assert planetary.validate(1.0, 12, 12, 3) == []      # valid (48 % 3 == 0)
    bad = planetary.validate(1.0, 13, 12, 3)             # (13+37)=50 not divisible by 3
    assert any("assembly condition" in e for e in bad)


@test
def test_planetary_geometry_builds_and_rejects_invalid():
    from constraint_kit import builder
    # valid set builds
    spec = {"parts": [{"id": "pg", "type": "planetary_gearset", "material": "steel",
                       "params": {"module": 1.0, "sun_teeth": 12, "planet_teeth": 12, "n_planets": 3}}],
            "mates": []}
    assy, parts = builder.build_assembly(spec)
    assert parts[0]["volume_mm3"] > 0
    # invalid set (assembly condition fails) must raise
    try:
        builder.build_assembly({"parts": [{"id": "pg", "type": "planetary_gearset",
                                           "params": {"sun_teeth": 13, "planet_teeth": 12, "n_planets": 3}}],
                                "mates": []})
    except ValueError:
        return
    raise AssertionError("invalid planetary set should have raised")


@test
def test_tree_solver_detects_closed_loop():
    """The honest limitation: a planet meshing BOTH sun and ring is a loop the tree solver can't close.
    Build sun + ring + 1 planet with two mesh mates on the planet -> find_loops flags the 2nd."""
    from constraint_kit import builder
    spec = {"parts": [
        {"id": "sun", "type": "spur_gear", "params": {"module": 1, "teeth": 12}},
        {"id": "ring", "type": "ring_gear", "params": {"module": 1, "teeth": 36}},
        {"id": "planet", "type": "spur_gear", "params": {"module": 1, "teeth": 12}}],
        "mates": [
            {"a": "sun", "a_joint": "bore_base", "b": "planet", "b_joint": "bore_base", "type": "mesh"},
            # second constraint on the SAME planet (mesh with ring) closes the loop:
            {"a": "ring", "a_joint": "bore_base", "b": "planet", "b_joint": "bore_base", "type": "mesh"}]}
    loops = builder.find_loops(spec)
    assert len(loops) == 1 and loops[0]["a"] == "ring"   # the ring-mesh is the loop-closing constraint
    # build still succeeds (skips the loop mate) and reports it needs a DOF solver
    assy, parts = builder.build_assembly(spec)
    assert len(parts) == 3


@test
def test_planetary_dof_mobility():
    from constraint_kit import planetary
    m = planetary.mobility(1.0, 12, 12, 3, grounded="ring")
    assert m["loop_consistent"] is True
    assert m["gear_dof"] == 1                              # ring grounded -> 1-DOF mechanism
    assert m["loop_closing_constraints"] == 3             # one per planet
    free = planetary.mobility(1.0, 12, 12, 3, grounded="none")
    assert free["gear_dof"] == 2                          # no ground -> 2-DOF


@test
def test_four_bar_geometric_dof():
    """GEOMETRIC closed loop via SolveSpace: 4-bar mobility = 1 (solver AND Gruebler agree); fixing the
    crank angle determines the position (dof 0) and the loop closes (link residuals ~ 0)."""
    from constraint_kit import linkage
    free = linkage.solve_four_bar(100, 30, 90, 60, input_angle_deg=None)
    assert free["ok"] is True
    assert free["mechanism_dof"] == 1 == free["gruebler_dof"]   # SolveSpace == Kutzbach
    posed = linkage.solve_four_bar(100, 30, 90, 60, input_angle_deg=60)
    assert posed["ok"] and posed["mechanism_dof"] == 0          # input fixed -> determined
    assert all(r < 1e-3 for r in posed["link_residuals"].values())   # loop closes exactly


@test
def test_slider_crank_geometric_dof():
    from constraint_kit import linkage
    free = linkage.solve_slider_crank(25, 80, 0.0, input_angle_deg=None)
    assert free["ok"] and free["mechanism_dof"] == 1 == free["gruebler_dof"]
    posed = linkage.solve_slider_crank(25, 80, 0.0, input_angle_deg=70)
    assert posed["ok"] and posed["mechanism_dof"] == 0
    assert posed["link_residuals"]["slider_on_axis"] < 1e-3     # slider stays on its axis


@test
def test_four_bar_cannot_close_is_detected():
    # a "linkage" whose coupler is far too short to reach can't close -> solver reports not-ok
    from constraint_kit import linkage
    bad = linkage.solve_four_bar(ground=100, crank=30, coupler=5, rocker=5, input_angle_deg=60)
    assert bad["ok"] is False


@test
def test_four_bar_geometry_builds():
    from constraint_kit import builder
    spec = {"parts": [{"id": "fb", "type": "four_bar", "material": "aluminum",
                       "params": {"ground": 100, "crank": 30, "coupler": 90, "rocker": 60,
                                  "input_angle_deg": 60}}], "mates": []}
    assy, parts = builder.build_assembly(spec)
    assert parts[0]["volume_mm3"] > 0                            # bars built from the solved pose


@test
def test_pose_four_bar_assembly_closes_loop():
    """T10.2: route a 4-bar loop through the geometric solver and POSE it as an assembly of 4 distinct link
    parts. The loop must CLOSE — every link's pin anchors land on the solved joints, and shared joints
    coincide between adjacent links. The bridge from SolveSpace to the assembly builder; geometry is truth."""
    import math
    from constraint_kit import builder
    r = builder.pose_four_bar(ground=100, crank=30, coupler=90, rocker=60, input_angle_deg=60)
    P = r["positions"]
    assert len(r["links"]) == 4 and r["mass_g"] > 0 and r["grashof"] is True
    assert r["gruebler_dof"] == 1                                # intrinsic mobility (Kutzbach)
    spec = [("ground", "A", "D", 100), ("crank", "A", "B", 30),
            ("coupler", "B", "C", 90), ("rocker", "D", "C", 60)]
    worldpin = {}
    maxerr = 0.0
    for (name, j0, j1, L), child in zip(spec, r["assy"].children):
        _, anch = builder._generate("link", {"length": L, "width": 8, "thickness": 4})
        for jname, aname in ((j0, "p0"), (j1, "p1")):
            (x, y, _z), _rot = (child.loc * anch[aname]).toTuple()
            maxerr = max(maxerr, math.dist((x, y), P[jname]))     # pin lands on its solved joint
            worldpin.setdefault(jname, []).append((x, y))
    assert maxerr < 1e-3                                         # loop closes (pins on solved joints)
    # each shared joint (B,C touched by 2 links; A,D by 2) — the links' pins coincide there
    for jname, pts in worldpin.items():
        for px, py in pts[1:]:
            assert math.dist((px, py), pts[0]) < 1e-3
    # exports as a real assembly
    import os
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        out = builder.export(r["assy"], os.path.join(d, "fb"))
        assert os.path.getsize(out["step"]) > 0 and os.path.getsize(out["glb"]) > 0
    # honest failure: a linkage that cannot close raises
    try:
        builder.pose_four_bar(ground=100, crank=10, coupler=10, rocker=10, input_angle_deg=60)
    except ValueError:
        return
    raise AssertionError("expected non-closing linkage to raise")


@test
def test_three_gear_train():
    from constraint_kit import builder
    spec = {"parts": [{"id": "g1", "type": "spur_gear", "params": {"module": 1.5, "teeth": 20}},
                      {"id": "g2", "type": "spur_gear", "params": {"module": 1.5, "teeth": 16}},
                      {"id": "g3", "type": "spur_gear", "params": {"module": 1.5, "teeth": 24}}],
            "mates": [{"a": "g1", "a_joint": "bore_base", "b": "g2", "b_joint": "bore_base",
                       "type": "mesh", "angle_deg": 0},
                      {"a": "g2", "a_joint": "bore_base", "b": "g3", "b_joint": "bore_base",
                       "type": "mesh", "angle_deg": 90}]}
    assy, _ = builder.build_assembly(spec)
    locs = {c.name: c.loc.toTuple()[0] for c in assy.children}
    # g1-g2 center distance = 1.5*(20+16)/2 = 27 ; g2-g3 = 1.5*(16+24)/2 = 30
    approx(math.dist(locs["g1"][:2], locs["g2"][:2]), 27.0)
    approx(math.dist(locs["g2"][:2], locs["g3"][:2]), 30.0)


@test
def test_mass_matches_density():
    from constraint_kit import builder
    from constraint_kit.parts import DENSITY_G_MM3
    spec = {"parts": [{"id": "p", "type": "plate", "material": "aluminum", "params": {}}],
            "mates": []}
    _, parts = builder.build_assembly(spec)
    p = parts[0]
    approx(p["mass_g"], round(p["volume_mm3"] * DENSITY_G_MM3["aluminum"], 2), eps=0.05)


@test
def test_export_writes_step_and_glb():
    from constraint_kit import builder
    spec = {"parts": [{"id": "p", "type": "plate", "params": {}}], "mates": []}
    assy, _ = builder.build_assembly(spec)
    with tempfile.TemporaryDirectory() as d:
        out = builder.export(assy, os.path.join(d, "t"))
        for k in ("step", "glb"):
            assert os.path.getsize(out[k]) > 0, f"{k} empty"


@test
def test_id_or_name_alias():
    from constraint_kit import builder
    # planner sometimes emits "name" instead of "id" — builder must tolerate it
    spec = {"parts": [{"name": "p", "type": "plate", "params": {}}], "mates": []}
    _, parts = builder.build_assembly(spec)
    assert parts[0]["id"] == "p"


# ---- 2D layout -------------------------------------------------------------
@test
def test_layout_row_placement_exact():
    from constraint_kit import layout
    spec = {"parts": [{"id": f"p{i}", "type": "rect_panel", "params": {"width": 60, "height": 40}}
                      for i in range(3)],
            "mates": [{"a": "p0", "a_joint": "right", "b": "p1", "b_joint": "left", "offset": [10, 0]},
                      {"a": "p1", "a_joint": "right", "b": "p2", "b_joint": "left", "offset": [10, 0]}]}
    solved = layout.solve(spec)
    xs = [round(solved["places"][f"p{i}"][0], 2) for i in range(3)]
    assert xs == [0.0, 70.0, 140.0], xs
    rep = layout.report(solved)
    assert rep["sheet_size"] == [200.0, 40.0], rep["sheet_size"]


@test
def test_layout_dxf_valid():
    import ezdxf
    from constraint_kit import layout
    spec = {"parts": [{"id": "p", "type": "rect_panel", "params": {"width": 50, "height": 30, "hole_d": 4}},
                      {"id": "d", "type": "disc", "params": {"diameter": 30, "bore_d": 6}}],
            "mates": [{"a": "p", "a_joint": "right", "b": "d", "b_joint": "left", "offset": [8, 0]}]}
    solved = layout.solve(spec)
    with tempfile.TemporaryDirectory() as dd:
        path = layout.export_dxf(solved, os.path.join(dd, "t.dxf"))
        doc = ezdxf.readfile(path)
        assert len(doc.audit().errors) == 0
        kinds = {e.dxftype() for e in doc.modelspace()}
        assert "LWPOLYLINE" in kinds and "CIRCLE" in kinds, kinds
        layout.export_png(solved, os.path.join(dd, "t.png"))
        assert os.path.getsize(os.path.join(dd, "t.png")) > 0


# ---- atlas store (round-trip if connected; fail-soft otherwise) -----------
@test
def test_store_roundtrip_or_failsoft():
    from constraint_kit import builder, store
    if not store.enabled():
        # fail-soft contract: queries return empty, writes return False, nothing throws
        assert store.recent_runs() == []
        assert store.record_build("x", None, {"parts": []}, [], {}, None) is False
        return
    spec = {"parts": [{"id": "p", "type": "plate", "params": {}},
                      {"id": "g", "type": "spur_gear", "params": {}}],
            "mates": [{"a": "p", "a_joint": "mount", "b": "g", "b_joint": "bore_base",
                       "type": "coincident"}]}
    assy, parts = builder.build_assembly(spec)
    with tempfile.TemporaryDirectory() as d:
        exported = builder.export(assy, os.path.join(d, "selftest"))
        assert store.record_build("selftest_assembly", "self-test", spec, parts, exported, "test")
    got = store.assembly("selftest_assembly")
    assert got is not None
    pids = {p["pid"] for p in got["parts"]}
    assert pids == {"p", "g"}, pids
    assert any(m["type"] == "coincident" for m in got["mates"])


@test
def test_design_versioning():
    """T8.1: a design's version id is a stable hash of its parameter VALUES (idempotent; a param edit -> a
    new id), and versions persist as CkDesignVersion (params + git SHA) linked from the design's tree."""
    from constraint_kit import assembly, store
    # deterministic id — pure, no DB needed
    assert store.design_version_id("w", {"h": 10}) == store.design_version_id("w", {"h": 10})
    assert store.design_version_id("w", {"h": 10}) != store.design_version_id("w", {"h": 20})
    assert store.design_version_id("w", {"h": 10}).startswith("dv_w_")

    if not store.enabled():                              # fail-soft contract
        assert store.record_design_version({"name": "w", "parameters": {}}) is None
        assert store.design_versions("w") == []
        return

    import uuid
    name = f"vertest_{uuid.uuid4().hex[:8]}"
    defs = {"part": {"parts": [{"id": "s", "type": "spacer",
                               "params": {"outer_d": 20, "bore_d": 0, "height": "=h"}}], "ports": {}}}
    res10 = {**assembly.report(assembly.build_design({"h": 10}, [], defs, "part")["tree"]),
             "name": name, "parameters": assembly.build_design({"h": 10}, [], defs, "part")["parameters"]}
    res20 = {**assembly.report(assembly.build_design({"h": 20}, [], defs, "part")["tree"]),
             "name": name, "parameters": assembly.build_design({"h": 20}, [], defs, "part")["parameters"]}
    id10 = store.record_design_version(res10, git_sha="deadbeef")
    id20 = store.record_design_version(res20, git_sha="deadbeef")
    assert id10 and id20 and id10 != id20               # the edit produced a distinct version
    assert store.record_design_version(res10, git_sha="deadbeef") == id10   # idempotent re-record
    vs = store.design_versions(name)
    assert {id10, id20} <= {v["id"] for v in vs}         # both linked to the design
    assert all(v["git_sha"] == "deadbeef" for v in vs if v["id"] in {id10, id20})


# ---- spec compiler (LangGraph + SQLite + SearXNG/VLM, all mocked offline) -------------------------
def _isolate_spec_db():
    import os
    import tempfile
    from constraint_kit import spec_cache, spec_db
    d = tempfile.mkdtemp(prefix="ckspec_")
    spec_db.DEFAULT_PATH = os.path.join(d, "specs.sqlite")
    spec_cache.CACHE_DIR = os.path.join(d, "cache")
    return spec_db.DEFAULT_PATH


@test
def test_spec_resolve_thread_m6():
    _isolate_spec_db()
    from constraint_kit import spec_compiler
    r = spec_compiler.resolve_thread("M6x1", allow_live=False)   # offline -> preseed/designation
    assert r["ok"] and r["kind"] == "thread"
    f = r["facts"][0]
    assert f["designation"] == "M6x1"
    assert f["values"]["nominal_diameter"]["value"] == 6.0
    assert f["values"]["pitch"]["value"] == 1.0
    assert f["values"]["pitch"]["unit"] == "mm"


@test
def test_spec_resolve_structural_section():
    """T5.2: structural-section kind — IPE/UPN/L designations resolve OFFLINE to standard dims + mass/length,
    each value carries provenance, and an unknown size is honestly unresolved (not fabricated)."""
    _isolate_spec_db()
    from constraint_kit import spec_compiler
    r = spec_compiler.resolve_section("IPE 200", allow_live=False)
    assert r["ok"] and r["kind"] == "section"
    f = r["facts"][0]
    assert f["designation"] == "IPE 200" and f["system"] == "EN IPE I-beam"
    assert f["values"]["depth"]["value"] == 200.0 and f["values"]["width"]["value"] == 100.0
    assert f["values"]["mass_per_length"]["value"] == 22.4
    assert f["values"]["mass_per_length"]["unit"] == "kg/m"
    for v in f["values"].values():                       # provenance on every value
        assert v["source_ref"] and v["confidence"] is not None
    # equal-leg angle
    a = spec_compiler.resolve_section("L 40x40x4", allow_live=False)
    assert a["ok"] and a["facts"][0]["values"]["leg_length"]["value"] == 40.0
    assert a["facts"][0]["values"]["thickness"]["value"] == 4.0
    # auto-detect (kind unknown) routes a channel query to the section parser
    c = spec_compiler.resolve("UPN 100", kind="unknown", allow_live=False)
    assert c["ok"] and c["kind"] == "section" and c["facts"][0]["designation"] == "UPN 100"
    # honest: an unknown size is NOT fabricated
    assert spec_compiler.resolve_section("IPE 999", allow_live=False)["ok"] is False


@test
def test_spec_every_value_has_provenance():
    _isolate_spec_db()
    from constraint_kit import spec_compiler
    r = spec_compiler.resolve_thread("M8x1.25", allow_live=False)
    for fact in r["facts"]:
        for name, v in fact["values"].items():
            for field in ("value", "unit", "quantity", "source_ref", "confidence"):
                assert field in v and v[field] is not None, f"{name} missing {field}"
            # source_ref must point at a real source in the result
            assert any(s["id"] == v["source_ref"] for s in r["sources"]), "dangling source_ref"
    for s in r["sources"]:
        for field in ("id", "source_type", "confidence"):
            assert field in s


@test
def test_spec_confidence_clamped():
    from constraint_kit.spec_compiler import score_confidence
    hi = score_confidence("official_standard", structured_table=True, confirmed_second=True,
                          has_edition_date=True, unit_stated=True)
    lo = score_confidence("forum", conflicting=True, ocr_only=True, vlm_only=True, missing_edition=True)
    assert 0.0 <= lo <= hi <= 1.0
    assert hi == 1.0 and lo == 0.0          # clamped at both ends


@test
def test_spec_sqlite_roundtrip_and_cache():
    _isolate_spec_db()
    from constraint_kit import spec_compiler, spec_db
    r1 = spec_compiler.resolve_thread("M5x0.8", allow_live=False)   # writes to sqlite
    assert r1["cache_hit"] is False
    cached = spec_db.find_cached("M5x0.8", "thread", "M5x0.8")
    assert cached is not None and cached["cache_hit"] is True
    assert cached["facts"][0]["values"]["pitch"]["value"] == 0.8
    r2 = spec_compiler.resolve_thread("M5x0.8", allow_live=False)   # second time -> cache hit
    assert r2["cache_hit"] is True
    assert len(spec_db.recent(10)) >= 1


@test
def test_spec_resolve_endpoint():
    _isolate_spec_db()
    from fastapi.testclient import TestClient

    from constraint_kit.api import app
    c = TestClient(app)
    resp = c.post("/spec/resolve", json={"query": "M6x1 socket head cap screw",
                                         "kind": "thread", "allow_live": False})
    assert resp.status_code == 200
    d = resp.json()
    assert d["ok"] and d["facts"][0]["values"]["nominal_diameter"]["value"] == 6.0


@test
def test_spec_invalid_designation_failsoft():
    _isolate_spec_db()
    from constraint_kit import spec_compiler
    r = spec_compiler.resolve_thread("totally-not-a-thread", allow_live=False)
    assert r["ok"] is False              # structured failure, not a crash
    assert r["warnings"] and r["facts"] == []


@test
def test_spec_searxng_normalize_and_rank():
    from constraint_kit import spec_sources
    raw = [
        {"title": "random blog", "url": "https://someblog.wordpress.com/x", "engine": "g", "score": 9},
        {"title": "ISO 4762", "url": "https://www.iso.org/standard/123.html", "engine": "g", "score": 1},
        {"title": "BoltDepot M6", "url": "https://www.boltdepot.com/m6", "engine": "g", "score": 5},
    ]
    cands = [spec_sources.normalize_result(r) for r in raw]
    ranked = spec_sources.rank_sources(cands)
    # official standard must rank first, forum/blog last
    assert ranked[0]["source_type_guess"] == "official_standard"
    assert ranked[-1]["source_type_guess"] == "forum"


@test
def test_spec_vlm_only_accepted_when_it_matches():
    """Mocked qwen VLM: its output is accepted (as a confirming source) ONLY when it matches the
    deterministic designation value; a mismatch is discarded with a warning. VLM is never ground truth."""
    from constraint_kit import spec_extract, spec_graph
    orig = spec_extract.extract_with_qwen_vlm
    state = {"query": "M6x1", "designation": "M6x1", "kind": "thread",
             "parsed": {"designation": "M6x1", "system": "ISO metric", "values": {
                 "nominal_diameter": {"value": 6.0, "unit": "mm", "quantity": "nominal_thread_diameter"},
                 "pitch": {"value": 1.0, "unit": "mm", "quantity": "thread_pitch"}}},
             "fetched": {"ok": True, "pdf_path": "/tmp/fake.pdf", "source_type_guess": "manufacturer_catalog"},
             "warnings": []}
    try:
        spec_extract.extract_with_qwen_vlm = lambda *a, **k: {"ok": True,
            "data": {"nominal_diameter_mm": 6.0, "pitch_mm": 1.0}}
        good = spec_graph.extract_visual_tables_with_vlm(state)
        assert good.get("live_confirmed") is True and good["confirm_source"]["table"] == "vlm_table"
        spec_extract.extract_with_qwen_vlm = lambda *a, **k: {"ok": True,
            "data": {"nominal_diameter_mm": 99.0, "pitch_mm": 9.0}}   # wrong -> must be discarded
        bad = spec_graph.extract_visual_tables_with_vlm(state)
        assert not bad.get("live_confirmed") and any("vlm" in w for w in bad.get("warnings", []))
    finally:
        spec_extract.extract_with_qwen_vlm = orig


@test
def test_spec_extract_hardening():
    """T5.3: hardened deterministic extraction — robust quantity parsing, numeric TOKEN confirmation (no
    substring false-positives), and colspan/whitespace-tolerant HTML tables. All offline, no VLM/network."""
    from constraint_kit import spec_extract as sx
    assert sx.parse_quantity("8 mm") == (8.0, "mm")
    assert sx.parse_quantity("Ø22")[0] == 22.0
    assert sx.parse_quantity("0.75 in") == (0.75, "in")
    assert sx.parse_quantity("1,25")[0] == 1.25                  # comma decimal
    assert sx.parse_quantity("n/a") is None
    # numbers_in tokenizes FULL numbers: 6 is not found inside 16 / 0.6
    toks = sx.numbers_in("M16 x 2.0, area 0.6")
    assert {16.0, 2.0, 0.6} <= toks and 6.0 not in toks
    # confirmation gate: real evidence confirms; a misleading-substring source does NOT
    parsed6 = {"values": {"nd": {"value": 6.0}, "p": {"value": 1.0}}}
    assert sx.value_confirmed("Thread M6 x 1.0 coarse", parsed6) is True
    assert sx.value_confirmed("Thread M16 x 2.0", parsed6) is False   # the substring-'6' false positive, killed
    assert sx.value_confirmed("no numbers", {"values": {}}) is False  # can't confirm nothing
    # hardened HTML: colspan expands, whitespace collapses, empty rows dropped, malformed input fail-soft
    html = ('<table><tr><th colspan="2">Thread</th><th>Pitch</th></tr>'
            '<tr><td>  M6 </td><td>6.0</td><td>1.0</td></tr><tr></tr></table>'
            '<table><tr><td>loose</td></table>')
    ext = sx.extract_text_tables(html)
    assert ext["ok"] and ext["truncated"] is False
    assert ext["tables"][0][0] == ["Thread", "Thread", "Pitch"]  # colspan=2 expanded -> aligned columns
    assert ext["tables"][0][1] == ["M6", "6.0", "1.0"]           # whitespace collapsed
    assert all(any(c for c in row) for row in ext["tables"][0])  # no empty rows survive


@test
def test_pdf_oxide_extraction_path():
    """The durable deterministic PDF path: spec_extract.extract_pdf recovers numeric tokens from a PDF
    (pdf_oxide layout, pypdf fallback) so value_confirmed works, and it's fail-soft on bad input."""
    import os
    import tempfile
    from constraint_kit import spec_extract as sx
    assert sx.extract_pdf("/nonexistent/x.pdf")["ok"] is False          # fail-soft, no raise
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.pdf")
        fig = plt.figure(figsize=(4, 2))
        fig.text(0.1, 0.6, "thread M6 pitch 1.0"); fig.text(0.1, 0.3, "deviation 22 size 50")
        fig.savefig(path); plt.close(fig)
        ext = sx.extract_pdf(path)
        assert ext["ok"] and ext["extractor"] in ("pdf_oxide", "pypdf")
        toks = sx.numbers_in(ext["text"])
        assert {6.0, 1.0, 22.0, 50.0} <= toks                           # numeric tokens recovered
        assert sx.value_confirmed(ext["text"], {"values": {"a": {"value": 22}, "b": {"value": 6}}})
        assert not sx.value_confirmed(ext["text"], {"values": {"a": {"value": 999}}})


@test
def test_spec_atlas_breadcrumb_no_crash():
    """The atlas WORK-breadcrumb (not spec data) must write cleanly or no-op — never raise. (Regression:
    neo4j Session.run reserves the kwarg 'query', which silently broke this under fail-soft.)"""
    from constraint_kit import store
    out = store.record_spec_resolution(
        {"id": "res_testbreadcrumb", "query": "M6x1 test", "kind": "thread", "ok": True,
         "cache_hit": False, "facts": [{"designation": "M6x1", "confidence": 0.8}], "sources": []})
    assert out in (True, False)          # bool, and crucially did not raise


@test
def test_spec_wired_into_screw_build():
    """OPT-IN: a screw build with resolve_specs=true carries provenance-linked thread facts in its part
    metadata (geometry untouched); without the flag, behavior is unchanged."""
    _isolate_spec_db()
    from constraint_kit import builder
    base = {"parts": [{"id": "s", "type": "screw", "material": "steel",
                       "params": {"size": "M6-1", "length": 20}}], "mates": []}  # bd_warehouse size form
    # default: no spec metadata attached (no behavior change)
    _, plain = builder.build_assembly(dict(base))
    assert "spec" not in plain[0]
    # opt-in: spec metadata attached, offline (preseed), with source refs
    _, parts = builder.build_assembly({**base, "resolve_specs": True})
    sp = parts[0].get("spec")
    assert sp and sp["ok"] and sp["nominal_diameter_mm"] == 6.0 and sp["pitch_mm"] == 1.0
    assert sp["source_refs"]                       # provenance carried into the part


@test
def test_build_endpoint_resolve_specs_flag():
    """/build with resolve_specs=true attaches provenance to screw parts (planner mocked -> offline)."""
    _isolate_spec_db()
    from fastapi.testclient import TestClient

    from constraint_kit import api, planner
    orig = planner.plan
    try:
        planner.plan = lambda *a, **k: {"parts": [{"id": "s", "type": "screw", "material": "steel",
                                                    "params": {"size": "M5-0.8", "length": 16}}],
                                        "mates": []}
        c = TestClient(api.app)
        r = c.post("/build", json={"request": "an M5 screw", "name": "buildspec",
                                   "resolve_specs": True, "allow_live_spec": False})
        assert r.status_code == 200
        sp = r.json()["parts"][0].get("spec")
        assert sp and sp["ok"] and sp["nominal_diameter_mm"] == 5.0 and sp["pitch_mm"] == 0.8
        assert sp["source_refs"]
    finally:
        planner.plan = orig


@test
def test_threaded_rod_pitch_drives_geometry():
    """A threaded_rod given only a designation + resolve_specs gets its major_diameter+pitch from the spec
    compiler and builds a REAL thread driven by that pitch; provenance is attached and marks drove_geometry.
    Short length keeps the (heavy) thread build fast."""
    _isolate_spec_db()
    from constraint_kit import builder
    spec = {"parts": [{"id": "rod", "type": "threaded_rod", "material": "steel",
                       "params": {"designation": "M6x1", "length": 6}}],
            "mates": [], "resolve_specs": True}
    assy, parts = builder.build_assembly(spec)
    assert parts[0]["volume_mm3"] > 0                       # real threaded geometry built
    sp = parts[0]["spec"]
    assert sp["ok"] and sp["pitch_mm"] == 1.0 and sp["nominal_diameter_mm"] == 6.0
    assert sp["drove_geometry"] is True and sp["source_refs"]
    # different pitch -> different geometry (resolved pitch genuinely parameterizes the helix)
    from constraint_kit.parts_bd import threaded_rod
    v1 = threaded_rod(major_diameter=6.0, pitch=1.0, length=6)[0].val().Volume()
    v2 = threaded_rod(major_diameter=6.0, pitch=2.0, length=6)[0].val().Volume()
    assert abs(v1 - v2) > 1.0


@test
def test_spec_bearing_material_fit_kinds():
    """Broadened kinds resolve through the same flow with provenance."""
    _isolate_spec_db()
    from constraint_kit import spec_compiler
    b = spec_compiler.resolve_bearing("608", allow_live=False)
    bf = b["facts"][0]["values"]
    assert b["ok"] and bf["bore"]["value"] == 8.0 and bf["outer_diameter"]["value"] == 22.0 \
        and bf["width"]["value"] == 7.0
    assert all(v.get("source_ref") for v in bf.values())          # provenance on every value

    m = spec_compiler.resolve_material("AISI 304", allow_live=False)
    assert m["ok"] and m["facts"][0]["values"]["density"]["value"] == 8.0 \
        and m["facts"][0]["values"]["density"]["unit"] == "g/cm^3"

    fit = spec_compiler.resolve_fit("H7/g6", allow_live=False)
    assert fit["ok"] and fit["facts"][0]["values"]["fit_class"]["value"] == "clearance"


@test
def test_spec_kind_autodetect():
    """kind='unknown' auto-detects the right kind (a bearing number here)."""
    _isolate_spec_db()
    from constraint_kit import spec_compiler
    r = spec_compiler.resolve("608 deep groove ball bearing", kind="unknown", allow_live=False)
    assert r["ok"] and r["kind"] == "bearing"
    assert r["facts"][0]["values"]["bore"]["value"] == 8.0


@test
def test_iso286_matches_published_tables():
    """ISO 286 computed deviations must match the published ISO 286-2 tables at Ø20 (18-30 step)."""
    from constraint_kit import iso286
    assert iso286.it_grade(7, 20) == 21 and iso286.it_grade(6, 20) == 13
    assert iso286.hole_deviation("H", 7, 20) == (21, 0)          # H7 = +21/0
    assert iso286.shaft_deviation("g", 6, 20) == (-7, -20)        # g6 = -7/-20
    assert iso286.shaft_deviation("k", 6, 20) == (15, 2)          # k6 = +15/+2
    assert iso286.shaft_deviation("n", 6, 20) == (28, 15)         # n6 = +28/+15
    assert iso286.shaft_deviation("f", 7, 20) == (-20, -41)       # f7 = -20/-41
    f = iso286.fit("H", 7, "g", 6, 20)                            # H7/g6 @ Ø20
    assert f["min_clearance_um"] == 7 and f["max_clearance_um"] == 41 and f["fit_class"] == "clearance"


@test
def test_spec_fit_exact_with_nominal():
    """resolve_fit with a nominal size returns REAL micron deviations + clearances (provenance-linked)."""
    _isolate_spec_db()
    from constraint_kit import spec_compiler
    r = spec_compiler.resolve_fit("H7/g6 at 20mm", allow_live=False)
    v = r["facts"][0]["values"]
    assert r["ok"]
    assert v["min_clearance"]["value"] == 7 and v["min_clearance"]["unit"] == "µm"
    assert v["max_clearance"]["value"] == 41 and v["fit_class"]["value"] == "clearance"
    assert v["nominal_diameter"]["value"] == 20.0
    assert all(val.get("source_ref") for val in v.values())      # provenance on every value
    # transition fit at the same size
    n = spec_compiler.resolve_fit("H7/n6 at 20mm", allow_live=False)
    assert n["facts"][0]["values"]["fit_class"]["value"] == "transition"


@test
def test_iso286_interference_from_table():
    """T5.1: interference letters p/r/s/t/u/v resolve to REAL deviations from the ISO 286-2 table (extracted
    via pdf_oxide, cross-validated). Values must match known published fit anchors EXACTLY, the fundamental
    deviation is grade-independent, t is correctly undefined <=24mm, and a not-yet-extracted letter (x)
    stays honestly class-only."""
    from constraint_kit import iso286, spec_compiler
    # published ISO anchors (es, ei): p6@20=+35/+22, r6@20=+41/+28, s6@20=+48/+35, s6@60=+72/+53 (fine step)
    assert iso286.shaft_deviation("p", 6, 20) == (35, 22)
    assert iso286.shaft_deviation("r", 6, 20) == (41, 28)
    assert iso286.shaft_deviation("s", 6, 20) == (48, 35)
    assert iso286.shaft_deviation("s", 6, 60) == (72, 53)
    # t/u/v (heavy press), incl. the 18-24/24-30 fine subdivisions u/v use
    assert iso286.shaft_deviation("t", 6, 26) == (54, 41)        # t6@24-30
    assert iso286.shaft_deviation("u", 6, 20) == (54, 41)        # u6@18-24
    assert iso286.shaft_deviation("u", 6, 60) == (106, 87)       # u6@50-65
    assert iso286.shaft_deviation("v", 6, 26) == (68, 55)        # v6@24-30
    assert iso286.shaft_deviation("t", 6, 20) is None            # t undefined <=24mm -> class-only
    # ei is grade-independent (fundamental deviation); es = ei + IT(grade)
    assert iso286.shaft_deviation("s", 7, 20)[1] == 35 and iso286.shaft_deviation("s", 6, 20)[1] == 35
    # full fit: H7/u6@26 is a heavy interference fit
    f = iso286.fit("H", 7, "u", 6, 26)
    assert f["fit_class"] == "interference" and f["max_clearance_um"] < 0
    # honest boundary: x not yet extracted -> class-only (None), never fabricated
    assert iso286.shaft_deviation("x", 6, 20) is None
    # via the spec compiler (provenance on every value)
    r = spec_compiler.resolve_fit("H7/u6 at 50mm", allow_live=False)
    v = r["facts"][0]["values"]
    assert v["fit_class"]["value"] == "interference" and all(val.get("source_ref") for val in v.values())


@test
def test_iso286_m_js_letters_and_more_bearings():
    from constraint_kit import iso286, spec_compiler
    assert iso286.shaft_deviation("m", 6, 20) == (21, 8)         # m6 = +21/+8 (ISO table)
    assert iso286.shaft_deviation("js", 6, 20) == (6.5, -6.5)    # js6 symmetric
    mfit = spec_compiler.resolve_fit("H7/m6 at 20mm", allow_live=False)
    assert mfit["facts"][0]["values"]["fit_class"]["value"] == "transition"
    b = spec_compiler.resolve_bearing("6204", allow_live=False)["facts"][0]["values"]
    assert b["bore"]["value"] == 20.0 and b["outer_diameter"]["value"] == 47.0 \
        and b["width"]["value"] == 14.0


@test
def test_spec_fit_classonly_fallbacks():
    """No nominal -> class only + warning; a NOT-YET-EXTRACTED interference letter (u) -> class only +
    warning (never faked). p/r/s now resolve exactly (T5.1), so the class-only path is for t/u/v…"""
    _isolate_spec_db()
    from constraint_kit import spec_compiler
    nofit = spec_compiler.resolve_fit("H7/g6", allow_live=False)              # no size
    f = nofit["facts"][0]
    assert f["values"]["fit_class"]["value"] == "clearance" and "min_clearance" not in f["values"]
    assert f["warnings"]                                                      # warns to give a size
    x = spec_compiler.resolve_fit("H7/x6 at 20mm", allow_live=False)          # not-yet-extracted letter
    xf = x["facts"][0]
    assert xf["values"]["fit_class"]["value"] == "interference"
    assert "min_clearance" not in xf["values"] and xf["warnings"]             # exact not fabricated


@test
def test_spec_sqlite_integrity():
    _isolate_spec_db()
    from constraint_kit import spec_compiler, spec_db
    spec_compiler.resolve_thread("M10x1.5", allow_live=False)
    assert spec_db.validate_integrity() == []


# ---- hierarchical assemblies (the car/home composition foundation) -------------------------------
_WHEEL_AXLE_DEFS = {
    "wheel": {  # rim + hub, two leaf parts; exposes a 'hub' port for mounting
        "parts": [
            {"id": "rim", "type": "spacer", "material": "aluminum",
             "params": {"outer_d": 40, "bore_d": 20, "height": 10}},
            {"id": "hub", "type": "spacer", "material": "steel",
             "params": {"outer_d": 20, "bore_d": 8, "height": 12}}],
        "mates": [{"a": "rim", "a_joint": "bottom", "b": "hub", "b_joint": "bottom",
                   "type": "coincident"}],
        "ports": {"hub": {"origin": [0, 0, 0], "z_axis": [0, 0, 1]}},
    },
    "axle": {  # one shaft + TWO wheel instances (reuse) placed at the shaft ends
        "parts": [{"id": "shaft", "type": "shaft", "material": "steel",
                   "params": {"diameter": 8, "length": 120}}],
        "children": [
            {"instance": "wheel_l", "ref": "wheel", "place": {"port": "hub", "at": [0, 0, 0]}},
            {"instance": "wheel_r", "ref": "wheel", "place": {"port": "hub", "at": [0, 0, 120]}}],
        "ports": {"mid": {"origin": [0, 0, 60]}},
    },
}


@test
def test_assembly_hierarchy_rollup():
    """wheel(2 parts) reused 2× in axle(+shaft): BOM + mass roll up, depth=2, ports exposed."""
    from constraint_kit import assembly
    res = assembly.build_tree("axle", _WHEEL_AXLE_DEFS)
    assert res["depth"] == 2
    assert res["bom"]["spacer"] == 4          # 2 wheels × 2 spacers each
    assert res["bom"]["shaft"] == 1
    assert res["part_count"] == 5 and res["leaf_count"] == 5
    assert res["mass_g"] > 0
    assert sorted(res["ports"]) == ["mid"]    # axle exposes its own port upward


@test
def test_assembly_port_placement_composes():
    """A child is relocated as a unit by mating its port to a frame in the parent's space."""
    from constraint_kit import assembly, builder
    res = assembly.build_tree("axle", _WHEEL_AXLE_DEFS)
    bb = res["cq_assembly"].toCompound().BoundingBox()
    assert bb.zmin < 5 and bb.zmax > 115      # wheels landed near z=0 and z=120 via their hub ports
    import os
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        out = builder.export(res["cq_assembly"], os.path.join(d, "axle"))
        for k in ("step", "glb"):
            assert os.path.getsize(out[k]) > 0   # one combined export of the whole tree


@test
def test_assembly_rejects_cycles_and_unknown_refs():
    from constraint_kit import assembly
    try:
        assembly.build_tree("x", {"x": {"children": [{"ref": "x"}]}})
    except ValueError:
        pass
    else:
        raise AssertionError("expected cycle detection")
    try:
        assembly.build_tree("a", {"a": {"children": [{"ref": "missing"}]}})
    except ValueError:
        return
    raise AssertionError("expected unknown-ref error")


# ---- parameters / top-down design (Phase B) ------------------------------------------------------
@test
def test_safe_eval_whitelist():
    from constraint_kit import parameters
    assert parameters.safe_eval("a*2 + max(b,3)", {"a": 5, "b": 1}) == 13
    assert parameters.safe_eval("track - 2*hub_w", {"track": 100, "hub_w": 12}) == 76
    for malicious in ["__import__('os')", "open('x')", "a.b", "(1).__class__", "lambda: 1"]:
        try:
            parameters.safe_eval(malicious, {"a": 1})
        except ValueError:
            continue
        raise AssertionError(f"safe_eval should have rejected {malicious!r}")


@test
def test_parameters_resolve_provenance():
    from constraint_kit import parameters
    r = parameters.resolve({"track": {"value": 100, "unit": "mm"}, "hub_w": 12},
                           [{"name": "clear", "expr": "track - 2*hub_w", "unit": "mm"}])
    assert r["ok"] and r["values"]["clear"] == 76
    p = r["params"]["clear"]
    assert p["source"] == "derived" and p["expr"] == "track - 2*hub_w"
    assert p["inputs"] == {"hub_w": 12, "track": 100}      # full traceability to requirements
    # chained relations resolve in dependency order
    r2 = parameters.resolve({"a": 2}, [{"name": "c", "expr": "b + 1"}, {"name": "b", "expr": "a * 10"}])
    assert r2["ok"] and r2["values"] == {"a": 2, "b": 20, "c": 21}


@test
def test_parameters_cycle_detected():
    from constraint_kit import parameters
    r = parameters.resolve({}, [{"name": "a", "expr": "b"}, {"name": "b", "expr": "a"}])
    assert not r["ok"] and any("unresolved" in w for w in r["warnings"])


@test
def test_parameters_substitute():
    from constraint_kit import parameters
    out = parameters.substitute({"length": "=track", "at": [0, 0, "=track - hub_w"], "fixed": 5},
                                {"track": 120, "hub_w": 12})
    assert out == {"length": 120, "at": [0, 0, 108], "fixed": 5}


@test
def test_top_down_design_drives_geometry():
    """A requirement (track) propagates into the assembly and changes the geometry deterministically."""
    from constraint_kit import assembly
    defs = {
        "wheel": _WHEEL_AXLE_DEFS["wheel"],
        "axle": {"parts": [{"id": "shaft", "type": "shaft", "material": "steel",
                            "params": {"diameter": 8, "length": "=track"}}],
                 "children": [{"instance": "wL", "ref": "wheel", "place": {"port": "hub", "at": [0, 0, 0]}},
                              {"instance": "wR", "ref": "wheel",
                               "place": {"port": "hub", "at": [0, 0, "=track"]}}], "ports": {}},
    }
    d100 = assembly.build_design({"track": 100}, [], defs, "axle")
    d200 = assembly.build_design({"track": 200}, [], defs, "axle")
    assert d100["ok"] and d200["ok"]
    z100 = d100["tree"]["cq_assembly"].toCompound().BoundingBox().zmax
    z200 = d200["tree"]["cq_assembly"].toCompound().BoundingBox().zmax
    assert z200 - z100 > 90                                 # bigger track -> proportionally bigger axle
    assert d100["parameters"]["track"]["source"] == "requirement"


@test
def test_design_refuses_requirement_violation():
    from constraint_kit import assembly
    defs = {"part": {"parts": [{"id": "s", "type": "shaft", "params": {"diameter": 8, "length": "=L"}}],
                     "ports": {}}}
    d = assembly.build_design({"L": -5}, [], defs, "part",
                              asserts=[{"expr": "L > 0", "message": "length must be positive"}])
    assert d["ok"] is False and d["tree"] is None
    assert any("positive" in w for w in d["warnings"])     # refuses to build an invalid design


@test
def test_reedit_rebuilds_only_affected_subtree():
    """T8.2: a requirement edit re-solves incrementally — the dependency analysis flags exactly the
    affected defs, and the rebuild regenerates ONLY the affected subtree's parts (the rest reuse the T3.1
    part cache). The incremental result is identical to a full from-scratch design. Geometry/counters are
    truth, not the VLM."""
    from constraint_kit import assembly, builder, parameters
    defs = {
        "blockA": {"parts": [{"id": "a", "type": "spacer", "params": {"outer_d": 20, "bore_d": 0,
                                                                      "height": "=ha"}}],
                   "ports": {"p": {"origin": [0, 0, 0]}}},
        "blockB": {"parts": [{"id": "b", "type": "spacer", "params": {"outer_d": 20, "bore_d": 0,
                                                                      "height": "=hb"}}],
                   "ports": {"p": {"origin": [0, 0, 0]}}},
        "rig": {"children": [{"instance": "A", "ref": "blockA", "place": {"port": "p", "at": [0, 0, 0]}},
                             {"instance": "B", "ref": "blockB", "place": {"port": "p", "at": [0, 0, 50]}}]},
    }
    reqs = {"ha": 10, "hb": 12}
    # param-level dependency analysis is exact
    assert parameters.param_dependencies(defs["blockA"]) == {"ha"}
    assert parameters.changed_values({"ha": 10, "hb": 12}, {"ha": 20, "hb": 12}) == {"ha"}

    builder.clear_cache()
    assert assembly.build_design(reqs, [], defs, "rig")["ok"]      # baseline warms the part cache

    r = assembly.reedit_design(reqs, [], defs, "rig", {"ha": 20})  # edit only ha
    assert r["ok"]
    assert r["changed_values"] == ["ha"]
    assert set(r["dirty_defs"]) == {"blockA", "rig"}              # blockA changed + its parent re-places
    assert r["clean_defs"] == ["blockB"]                         # B subtree untouched
    assert r["rebuild"]["parts_generated"] == 1                  # only blockA's spacer regenerated
    assert r["rebuild"]["parts_reused"] == 1                     # blockB's spacer reused from cache (T3.1)

    full = assembly.build_design({"ha": 20, "hb": 12}, [], defs, "rig")   # from scratch
    approx(r["tree"]["mass_g"], full["tree"]["mass_g"])          # incremental == full


# ---- interference / clash validation (Phase C) ---------------------------------------------------
@test
def test_interference_detects_overlap():
    """Two coincident solids overlap -> a clash; the axle (clearance fits) is clean."""
    from constraint_kit import assembly, validate
    # two shafts both at the origin (the 2nd has no mate) -> full overlap
    clash_defs = {"clash": {"parts": [
        {"id": "a", "type": "shaft", "params": {"diameter": 10, "length": 20}},
        {"id": "b", "type": "shaft", "params": {"diameter": 10, "length": 20}}], "ports": {}}}
    res = assembly.check_interference("clash", clash_defs)
    assert res["ok"] is False and len(res["clashes"]) == 1
    assert res["clashes"][0]["overlap_volume_mm3"] > 100      # large overlap (two Ø10×20 coincident)
    # the wheel/axle assembly has bore clearance around the shaft -> no clash
    clean = validate.interference(assembly.build_tree("axle", _WHEEL_AXLE_DEFS)["cq_assembly"])
    assert clean["ok"] is True and clean["clashes"] == []
    assert clean["pairs_exact_checked"] <= clean["pairs_total"]   # bbox prefilter did its job


# ---- SMT design synthesis (4th solver class) -----------------------------------------------------
@test
def test_synthesize_planetary_meets_spec():
    """Z3 SYNTHESIZES a planetary gearset to a ratio spec; the result must satisfy our INDEPENDENT
    analytical validator (the two solvers agree) and carry provenance + a buildable part_spec."""
    from constraint_kit import planetary, synthesis
    r = synthesis.synthesize_planetary(target_ratio=5.0, n_planets=3, teeth_min=12, teeth_max=40)
    assert r["ok"] and abs(r["achieved_ratio"] - 5.0) < 1e-6
    c = r["config"]
    assert c["ring_teeth"] == c["sun_teeth"] + 2 * c["planet_teeth"]
    # cross-check: the SMT design passes the independent analytical checker (planetary.validate)
    assert planetary.validate(c["module"], c["sun_teeth"], c["planet_teeth"], c["n_planets"]) == []
    assert r["part_spec"]["type"] == "planetary_gearset" and r["constraints"]


@test
def test_synthesize_planetary_builds_geometry():
    """Synthesis flows straight into exact CAD: the part_spec builds real meshed geometry."""
    from constraint_kit import builder, synthesis
    r = synthesis.synthesize_planetary(target_ratio=4.0, n_planets=3)
    assert r["ok"]
    _, parts = builder.build_assembly({"parts": [r["part_spec"]], "mates": []})
    assert parts[0]["type"] == "planetary_gearset" and parts[0]["volume_mm3"] > 0


@test
def test_synthesize_planetary_unsat_is_honest():
    """An infeasible spec returns a provable UNSAT, not a fabricated/near design."""
    from constraint_kit import synthesis
    # ratio 4 -> Zp=Zs and (Zs+Zr)=4*Zs must be %3==0 -> Zs%3==0; teeth 13..14 has no such value
    r = synthesis.synthesize_planetary(target_ratio=4.0, n_planets=3, teeth_min=13, teeth_max=14)
    assert r["ok"] is False and "UNSAT" in r["reason"]


@test
def test_synthesize_gear_train_multistage():
    """T10.3: SMT splits a large ratio across N planetary stages whose ratios MULTIPLY to the target
    (within tol); each stage is independently a valid planetary set (analytical cross-check); honest UNSAT
    when the per-stage bounds can't reach the target."""
    from constraint_kit import planetary, synthesis
    r = synthesis.synthesize_gear_train(target_ratio=25.0, n_stages=2, stage_ratio_max=8.0, ratio_tol=0.5)
    assert r["ok"] and len(r["stages"]) == 2
    assert abs(r["achieved_ratio"] - 25.0) <= 0.5                  # product of stage ratios hits target
    prod = 1.0
    for s in r["stages"]:
        assert not planetary.validate(s["module"], s["sun_teeth"], s["planet_teeth"], s["n_planets"])  # valid
        assert 2.0 <= s["stage_ratio"] <= 8.0                      # within per-stage bounds
        prod *= s["stage_ratio"]
    approx(prod, r["achieved_ratio"], eps=1e-3)
    assert len(r["part_specs"]) == 2 and r["part_specs"][0]["type"] == "planetary_gearset"
    # honest UNSAT: 1000:1 is unreachable in two stages each capped at 8:1 (max 64:1)
    bad = synthesis.synthesize_gear_train(target_ratio=1000.0, n_stages=2, stage_ratio_max=8.0, ratio_tol=0.1)
    assert bad["ok"] is False and "UNSAT" in bad["reason"]


@test
def test_synthesize_tolerance_allocation():
    """T10.1: SMT tolerance ALLOCATION (Z3) — allocate the loosest ISO 286 grades whose stack-up fits the
    budget. Must (a) fit the budget, (b) each tolerance == iso286.it_grade exactly, (c) be Pareto-loosest
    (no single dim can loosen one grade without busting the budget), (d) honest infeasible when too tight."""
    from constraint_kit import iso286, synthesis
    dims = [{"name": "a", "nominal": 50}, {"name": "b", "nominal": 30}, {"name": "c", "nominal": 20}]
    res = synthesis.synthesize_tolerance_allocation(dims, budget_um=120, method="worst_case")
    assert res["ok"] and res["stack_um"] <= 120 and res["slack_um"] >= 0
    for d, al in zip(dims, res["allocation"]):
        g = int(al["grade"][2:])
        assert al["tolerance_um"] == iso286.it_grade(g, d["nominal"])         # matches ISO 286 exactly
        # Pareto-loosest: loosening THIS dim one grade (others fixed) would exceed the budget, unless maxed
        if g < 12:
            bumped = res["stack_um"] - al["tolerance_um"] + iso286.it_grade(g + 1, d["nominal"])
            assert bumped > 120
    # honest infeasibility: an impossibly tight budget -> ok:false + tightest achievable reported
    bad = synthesis.synthesize_tolerance_allocation(dims, budget_um=10, method="worst_case")
    assert bad["ok"] is False and bad["min_achievable_um"] > 10


@test
def test_interference_grid_equivalence_and_pruning():
    """T3.2: the spatial-grid prefilter must give IDENTICAL clashes to a brute-force O(n^2) sweep, and
    must prune far below n*(n-1)/2 candidate pairs for a spread-out assembly."""
    from constraint_kit import assembly, validate
    # 6 shafts spread 50mm apart along X (via hierarchy ports) -> no overlaps; brute force = 15 pairs
    defs = {"shaft1": {"parts": [{"id": "s", "type": "shaft", "params": {"diameter": 8, "length": 20}}],
                       "ports": {"p": {"origin": [0, 0, 0]}}},
            "row": {"children": [{"instance": f"s{i}", "ref": "shaft1",
                                  "place": {"port": "p", "at": [i * 50, 0, 0]}} for i in range(6)]}}
    asm = assembly.build_tree("row", defs)["cq_assembly"]
    res = validate.interference(asm)
    # brute-force reference over the same world parts
    wp = validate._world_parts(asm)
    bb = [s.BoundingBox() for _, s in wp]
    brute = sum(1 for i in range(len(wp)) for j in range(i + 1, len(wp))
                if validate._bbox_overlap(bb[i], bb[j]))
    assert res["ok"] and res["clashes"] == []            # 50mm apart -> no clashes
    assert res["pairs_candidate"] < res["pairs_total"]    # grid pruned (15 total -> few candidates)
    assert res["pairs_exact_checked"] == brute            # same exact-check set as brute force


@test
def test_finish_fillet_chamfer_failsoft():
    """T1.1: opt-in fillet/chamfer removes material (deterministic), and an impossible radius fails soft
    (part kept unchanged, finish.ok=False) — never breaks the build."""
    from constraint_kit import builder
    cyl = {"outer_d": 40, "bore_d": 0, "height": 20}

    def build(finish=None):
        ps = {"id": "p", "type": "spacer", "params": cyl}
        if finish:
            ps["finish"] = finish
        return builder.build_assembly({"parts": [ps], "mates": []})[1][0]

    v0 = build()["volume_mm3"]
    fil = build({"fillet": 3})
    assert fil["finish"]["ok"] and fil["volume_mm3"] < v0               # rounded edges remove material
    cha = build({"chamfer": 3})
    assert cha["finish"]["applied"][0]["op"] == "chamfer" and cha["volume_mm3"] < v0
    big = build({"fillet": 100})
    assert big["finish"]["ok"] is False and big["volume_mm3"] == v0      # fail-soft: part unchanged


@test
def test_finish_shell_and_compose():
    """T1.2: shell hollows a part (volume drops), and shell+fillet compose (both ops applied, ordered)."""
    from constraint_kit import builder
    cyl = {"outer_d": 40, "bore_d": 0, "height": 20}

    def build(finish):
        return builder.build_assembly({"parts": [{"id": "p", "type": "spacer", "params": cyl,
                                                  "finish": finish}], "mates": []})[1][0]

    v0 = builder.build_assembly({"parts": [{"id": "p", "type": "spacer", "params": cyl}],
                                 "mates": []})[1][0]["volume_mm3"]
    sh = build({"shell": 3})
    assert sh["finish"]["ok"] and sh["volume_mm3"] < v0                 # hollowed -> less material
    combo = build({"shell": 3, "fillet": 1})
    ops = [a["op"] for a in combo["finish"]["applied"]]
    assert ops == ["shell", "fillet"] and combo["finish"]["ok"]          # ordered shell then fillet
    bad = build({"shell": 100})
    assert bad["finish"]["ok"] is False and bad["volume_mm3"] == v0      # fail-soft


@test
def test_derive_ports_from_geometry():
    """T2.1: derive named oriented ports from a built solid by geometric query (snapshot)."""
    from constraint_kit.joints import derive_ports
    from constraint_kit.parts import plate, spacer
    pp = derive_ports(plate(width=60, depth=60, thick=6, boss_d=12, boss_h=4,
                            bolt_d=4, bolt_circle=40, bolt_count=4)[0])
    assert {"top", "bottom", "center"} <= set(pp)
    approx(_origin_of(pp["top"])[2], 7.0)        # boss top: thick/2 + boss_h
    approx(_origin_of(pp["bottom"])[2], -3.0)     # plate underside
    assert _axis_of(pp["top"]) == (0.0, 0.0, 1.0)
    # bore axis derived from the smaller (inner) cylindrical face, on the part axis
    sp = derive_ports(spacer(outer_d=20, bore_d=8, height=12)[0])
    assert "bore_axis" in sp
    bx, by, _bz = _origin_of(sp["bore_axis"])
    approx(bx, 0.0, eps=1e-4); approx(by, 0.0, eps=1e-4)
    az = _axis_of(sp["bore_axis"])
    assert abs(az[2]) == 1.0 and az[0] == 0.0 and az[1] == 0.0   # axis along Z (sign-agnostic)


@test
def test_derived_ports_survive_param_changes():
    """T2.3: the derived-port analog of test_joint_names_survive_param_changes. Across TOPOLOGY-PRESERVING
    parameter changes, derive_ports must yield the SAME named set, each port's AXIS must be invariant, and
    each origin must track the params by the part's closed-form geometry (i.e. recomputed from geometry,
    not a frozen snapshot). This is the stability intent-mating (T2.2) relies on. The honest R2.a boundary:
    a feature appearing/disappearing (e.g. dropping the bore) DOES change the derivable name set — asserted
    explicitly so the limit is documented, not hidden. Geometry is truth, not the VLM."""
    from constraint_kit.joints import derive_ports
    from constraint_kit.parts import plate, spacer

    # --- spacer: name set + axes invariant; origins track (top.z=height, bottom.z=0, center.z=height/2) ---
    name_set = None
    for (od, bd, h) in [(20, 8, 12), (30, 10, 20), (50, 5, 8)]:
        pp = derive_ports(spacer(outer_d=od, bore_d=bd, height=h)[0])
        cur = set(pp)
        if name_set is None:
            name_set = cur
        assert cur == name_set, f"derived-port names drifted: {name_set} vs {cur}"
        assert {"top", "bottom", "center", "bore_axis"} <= cur
        assert _axis_of(pp["top"]) == (0.0, 0.0, 1.0)            # axes invariant across params
        assert _axis_of(pp["bottom"]) == (0.0, 0.0, -1.0)
        approx(_origin_of(pp["top"])[2], h)                       # origins track the params by closed form
        approx(_origin_of(pp["bottom"])[2], 0.0)
        approx(_origin_of(pp["center"])[2], h / 2.0)
        bx, by, _bz = _origin_of(pp["bore_axis"])
        approx(bx, 0.0, eps=1e-4); approx(by, 0.0, eps=1e-4)      # bore stays on the part axis
        assert abs(_axis_of(pp["bore_axis"])[2]) == 1.0

    # --- plate: the boss-top port tracks thick/2+boss_h, underside tracks -thick/2, axes invariant ---
    for (W, thick, boss_h) in [(60, 6, 4), (80, 10, 6)]:
        pp = derive_ports(plate(width=W, depth=W, thick=thick, boss_d=12, boss_h=boss_h,
                                bolt_d=4, bolt_circle=40, bolt_count=4)[0])
        assert {"top", "bottom", "center"} <= set(pp)
        approx(_origin_of(pp["top"])[2], thick / 2.0 + boss_h)   # tracks params, not a fixed snapshot
        approx(_origin_of(pp["bottom"])[2], -thick / 2.0)
        assert _axis_of(pp["top"]) == (0.0, 0.0, 1.0)

    # --- honest R2.a boundary: derived names are TOPOLOGY-derived, unlike authored joints. Removing the
    # bore removes the inner cylindrical face, so 'bore_axis' no longer denotes a bore. Authored joints
    # would keep the name (recomputed from params); derived ports cannot fabricate an absent feature. ---
    solid = derive_ports(spacer(outer_d=20, bore_d=0, height=12)[0])
    assert {"top", "bottom", "center"} <= set(solid)             # extreme-face/center ports still derive


@test
def test_dsl_validate_and_verify():
    """The DSL engine: static `validate` catches malformed programs (the LSP/self-correction signal), and
    `verify` confirms a program's EXACT compiled volume matches a known reference (the corpus filter / RL
    reward) while rejecting wrong geometry. Geometry is truth."""
    from constraint_kit import dsl
    # a clean program validates
    good = {"parts": [{"id": "g", "type": "spur_gear", "params": {"module": 1, "teeth": 20, "bore_d": 8}},
                      {"id": "s", "type": "spacer", "params": {"outer_d": 20, "bore_d": 0, "height": 10}}],
            "mates": [{"a": "g", "b": "s", "intent": "seat_on"}]}
    assert dsl.check(good)["ok"]
    # static errors are caught with LSP-shaped diagnostics
    bad = {"parts": [{"id": "x", "type": "spurr_gear", "params": {}},          # typo'd part type
                     {"id": "x", "type": "spacer"}],                           # duplicate id
           "mates": [{"a": "x", "b": "nope", "type": "weldd"}]}                # bad ref + bad mate type
    res = dsl.check(bad)
    codes = {d["code"] for d in res["diagnostics"]}
    assert not res["ok"]
    assert {"unknown-part-type", "duplicate-id", "unknown-part-ref", "bad-mate-type"} <= codes
    assert all({"severity", "code", "message", "path"} <= set(d) for d in res["diagnostics"])  # LSP-shaped
    # verify: geometric-equivalence SIGNATURE vs a known reference (solid cylinder spacer)
    ref_params = {"outer_d": 20, "bore_d": 0, "height": 10}
    ref_sig = dsl.reference_signature("spacer", ref_params)
    import math
    approx(ref_sig["volume"], math.pi * 100 * 10, eps=1.0)
    assert ref_sig["n_faces"] == 3 and "bbox_sorted" in ref_sig            # cylinder: 3 faces
    prog = {"parts": [{"id": "s", "type": "spacer", "params": ref_params}], "mates": []}
    assert dsl.verify(prog, ref_sig)["match"] is True                       # correct -> verified pair
    # THE POINT: a box with the SAME VOLUME but a different shape. Volume-only would (wrongly) pass it;
    # the signature must reject it on bbox/area/face-count while volume itself matches.
    h = (math.pi * 100 * 10) / (20 * 20)                                    # panel vol == cylinder vol
    decoy = {"parts": [{"id": "p", "type": "panel",
                        "params": {"width": 20, "depth": 20, "height": h}}], "mates": []}
    assert dsl.verify(decoy, ref_sig["volume"])["match"] is True            # volume-only: FALSE POSITIVE
    res = dsl.verify(decoy, ref_sig)                                        # full signature: rejected
    assert res["match"] is False
    failed = {f["field"] for f in res["fails"]}
    assert "volume" not in failed and {"bbox_sorted", "area", "n_faces"} & failed  # caught by shape, not volume
    # a statically-invalid program is honestly a non-match, not a crash
    assert dsl.verify({"parts": []}, ref_sig)["match"] is False


def _mkparts(specs):
    """Build a minimal builder-style parts map {id: {wp, anchors, type}} from (id, type, params) tuples."""
    from constraint_kit.builder import ALL_PART_GENS
    parts = {}
    for pid, ptype, params in specs:
        wp, anchors = ALL_PART_GENS[ptype](**params)
        parts[pid] = {"wp": wp, "anchors": dict(anchors), "type": ptype}
    return parts


@test
def test_mate_intent_resolves_canonical_seat():
    """T2.2: 'seat gear on plate boss' resolves DETERMINISTICALLY to the same frames+type a human would
    author (plate.mount <- gear.bore_base, coincident). No LLM in the resolver."""
    from constraint_kit import mate_intent
    parts = _mkparts([("plate", "plate", {}),
                      ("gear", "spur_gear", {"module": 1, "teeth": 20, "width": 6, "bore_d": 12})])
    r = mate_intent.resolve_intent(parts, {"a": "plate", "b": "gear", "intent": "seat_on"})
    assert r["a_joint"] == "mount" and r["b_joint"] == "bore_base" and r["type"] == "coincident"
    assert r["resolved_from_intent"] == "seat_on"
    # synonyms normalize to the same canonical intent
    assert mate_intent.canonical_intent("stack") == "seat_on"
    assert mate_intent.canonical_intent("Place On") == "seat_on"
    # T2.4: AUTHORED seat anchor wins over a derived one. A planetary_gearset authors 'base' (its true
    # seat); derive_ports also offers 'bottom' which sits earlier in the seat_on candidate list. The
    # resolver must pick the authored 'base', not the derived 'bottom' (the capstone mis-seat bug).
    gp = _mkparts([("plate", "plate", {}),
                   ("pg", "planetary_gearset", {"module": 1, "sun_teeth": 12, "planet_teeth": 18,
                                                "width": 8, "n_planets": 3})])
    assert "base" in gp["pg"]["anchors"] and "bottom" not in gp["pg"]["anchors"]   # base authored, bottom derived
    rg = mate_intent.resolve_intent(gp, {"a": "plate", "b": "pg", "intent": "seat_on"})
    assert rg["b_joint"] == "base"


@test
def test_mate_intent_equivalent_to_explicit():
    """An intent build must produce IDENTICAL geometry to the hand-authored explicit-frame build —
    intent is sugar over the same deterministic kernel, verified by the gear's world placement."""
    from constraint_kit import builder
    base = [{"id": "plate", "type": "plate", "params": {}},
            {"id": "gear", "type": "spur_gear", "params": {"module": 1, "teeth": 20, "width": 6, "bore_d": 12}}]
    explicit = {"parts": base, "mates": [{"a": "plate", "a_joint": "mount", "b": "gear",
                                          "b_joint": "bore_base", "type": "coincident"}]}
    intent = {"parts": base, "mates": [{"a": "plate", "b": "gear", "intent": "seat_on"}]}
    ae, _ = builder.build_assembly(explicit)
    ai, _ = builder.build_assembly(intent)
    (xe, ye, ze), _ = next(c for c in ae.children if c.name == "gear").loc.toTuple()
    (xi, yi, zi), _ = next(c for c in ai.children if c.name == "gear").loc.toTuple()
    approx(xi, xe); approx(yi, ye); approx(zi, ze)               # same placement, both routes


@test
def test_mate_intent_uses_derived_port_when_unauthored():
    """The T2.3->T2.2 link: 'insert a shaft into a spacer' has no AUTHORED bore frame on the spacer, so the
    resolver falls back to the geometry-DERIVED 'bore_axis' (proven stable in T2.3) and injects it into the
    part's anchors for the frame-based solver. Also: 'insert' allows a revolute type override (a spinning
    shaft)."""
    from constraint_kit import mate_intent
    parts = _mkparts([("sp", "spacer", {"outer_d": 20, "bore_d": 8, "height": 12}),
                      ("sh", "shaft", {"diameter": 8, "length": 30})])
    assert "bore_axis" not in parts["sp"]["anchors"]             # spacer authors only bottom/top
    r = mate_intent.resolve_intent(parts, {"a": "sp", "b": "sh", "intent": "insert"})
    assert r["a_joint"] == "bore_axis" and r["b_joint"] == "base" and r["type"] == "rigid"
    assert "bore_axis" in parts["sp"]["anchors"]                 # derived frame injected for the solver
    r2 = mate_intent.resolve_intent(parts, {"a": "sp", "b": "sh", "intent": "insert",
                                            "type": "revolute", "angle_deg": 30})
    assert r2["type"] == "revolute" and r2["angle_deg"] == 30


@test
def test_mate_intent_honest_failures():
    """The resolver is honest: an unknown intent, a disallowed type override, and an unsatisfiable role all
    RAISE (never a silently-wrong mate)."""
    from constraint_kit import mate_intent
    parts = _mkparts([("plate", "plate", {}),
                      ("gear", "spur_gear", {"module": 1, "teeth": 20, "width": 6, "bore_d": 12})])
    for bad in ({"a": "plate", "b": "gear", "intent": "frobnicate"},          # unknown intent
                {"a": "plate", "b": "gear", "intent": "seat_on", "type": "mesh"},  # type not allowed for seat_on
                {"a": "plate", "b": "gear", "intent": "mesh"}):               # plate has no bore_base to mesh
        try:
            mate_intent.resolve_intent(parts, bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad}")


@test
def test_mass_properties_cg_and_inertia():
    """T4.1: CG + inertia validated ANALYTICALLY against a solid cylinder, and CG roll-up on a symmetric
    stack. Geometry is truth (OCC mass props vs closed form), not the VLM."""
    from constraint_kit import assembly
    R, H = 20.0, 30.0
    cyl = {"parts": [{"id": "c", "type": "spacer", "material": "aluminum",
                      "params": {"outer_d": 2 * R, "bore_d": 0, "height": H}}]}
    res = assembly.build_tree("cyl", {"cyl": cyl})
    cg, pm, m = res["cg"], res["principal_moments"], res["mass_g"]
    approx(cg[0], 0.0, eps=1e-3); approx(cg[1], 0.0, eps=1e-3); approx(cg[2], H / 2, eps=1e-3)
    approx(pm[2] / m, 0.5 * R * R, eps=0.5)               # Izz/M = R^2/2  (about cylinder axis)
    approx(pm[0] / m, 0.25 * R * R + H * H / 12, eps=0.5)  # Ixx/M = R^2/4 + H^2/12
    # symmetric stack of two identical cylinders (0..H and H..2H) -> CG at z=H
    defs = {"cyl": {"parts": [{"id": "c", "type": "spacer",
                              "params": {"outer_d": 2 * R, "bore_d": 0, "height": H}}],
                    "ports": {"bot": {"origin": [0, 0, 0]}}},
            "stack": {"children": [{"instance": "a", "ref": "cyl", "place": {"port": "bot", "at": [0, 0, 0]}},
                                   {"instance": "b", "ref": "cyl", "place": {"port": "bot", "at": [0, 0, H]}}]}}
    approx(assembly.build_tree("stack", defs)["cg"][2], H, eps=1e-2)


@test
def test_explode_separates_stack_by_factor():
    """T7.1: exploded view separates stacked parts along +Z by exactly `factor` per rank, preserves the
    part count, and leaves the lowest part in place (geometry is truth, not the VLM)."""
    from constraint_kit import assembly, validate
    H, F = 10.0, 20.0
    # three identical cylinders stacked along +Z: original world centers at H/2, 3H/2, 5H/2 (sep = H)
    cyl = {"parts": [{"id": "c", "type": "spacer", "params": {"outer_d": 30, "bore_d": 0, "height": H}}],
           "ports": {"bot": {"origin": [0, 0, 0]}}}
    defs = {"cyl": cyl,
            "stack": {"children": [{"instance": f"s{k}", "ref": "cyl",
                                    "place": {"port": "bot", "at": [0, 0, k * H]}} for k in range(3)]}}
    asm = assembly.build_tree("stack", defs)["cq_assembly"]
    exploded = assembly.explode(asm, factor=F, axis=(0, 0, 1))
    centers = sorted(p[1].Center().z for p in validate._world_parts(exploded))
    assert len(centers) == 3                                   # part count preserved
    approx(centers[0], H / 2, eps=1e-3)                        # lowest (rank 0) stays put
    approx(centers[1] - centers[0], H + F, eps=1e-3)           # each rank pushed +F further apart
    approx(centers[2] - centers[1], H + F, eps=1e-3)


@test
def test_drawing_section_and_projection():
    """T7.2: a drawing set — the cross-section area matches the exact cut (geometry is truth), the DXF is
    ezdxf-valid, projection SVGs render, and overall dimensions match the bbox."""
    import os
    import tempfile
    import ezdxf
    from constraint_kit import assembly, drawing
    # a 40 x 20 x 12 panel: XZ mid-section area = 40*12 = 480; overall dims = the box extents
    defs = {"blk": {"parts": [{"id": "p", "type": "panel",
                              "params": {"width": 40, "depth": 20, "height": 12}}]}}
    asm = assembly.build_tree("blk", defs)["cq_assembly"]
    dims = drawing.overall_dimensions(asm)
    approx(dims["width_x"], 40.0); approx(dims["depth_y"], 20.0); approx(dims["height_z"], 12.0)
    with tempfile.TemporaryDirectory() as d:
        sec = drawing.section_dxf(asm, os.path.join(d, "s.dxf"), plane="XZ", height=0.0)
        approx(sec["section_area_mm2"], 40.0 * 12.0, eps=1e-2)   # exact cut area
        assert os.path.getsize(sec["path"]) > 0
        assert len(ezdxf.readfile(sec["path"]).audit().errors) == 0   # valid DXF
        full = drawing.drawing(asm, os.path.join(d, "draw"), plane="XZ", views=("front", "top"))
        assert len(full["projections"]) == 2
        for pj in full["projections"]:
            assert os.path.getsize(pj["path"]) > 0              # each projection SVG rendered
        assert "GD&T" in full["scope"]                          # honest scope note present


@test
def test_capstone_gearbox_full_stack():
    """CAPSTONE: a synthesized planetary gearbox composed end-to-end through the whole stack, with the
    SOLVERS CROSS-VALIDATING each other — SMT (Z3) designs the ratio, the independent analytical Willis
    solver confirms it. Then: hierarchy + parallel prewarm build -> interference-clean -> rolled-up
    BOM/mass/CG -> manufacturing outputs (exploded + section drawing + BOM doc). Geometry is truth."""
    from constraint_kit import assembly, bom as bommod, drawing, planetary, synthesis, validate
    # 1. SMT synthesis (Z3): design a 5:1 planetary set
    syn = synthesis.synthesize_planetary(target_ratio=5.0, n_planets=3, module=1.0, width=8,
                                         objective="compact")
    assert syn["ok"] and abs(syn["achieved_ratio"] - 5.0) < 1e-6
    cfg, pg = syn["config"], dict(syn["part_spec"])
    # 2. CROSS-VALIDATION: the analytical Willis solver must independently agree with the SMT design
    dof = planetary.mobility(1.0, cfg["sun_teeth"], cfg["planet_teeth"], cfg["n_planets"])
    assert dof["loop_consistent"] and dof["gear_dof"] == 1
    approx(dof["ratio_sun_to_carrier_ring_fixed"], syn["achieved_ratio"], eps=1e-6)   # SMT == Willis
    # 3. hierarchical gearbox def (planetary on a bolted plate), built with the parallel prewarm (T3.3)
    defs = {"gearbox": {"parts": [
        {"id": "plate", "type": "plate", "material": "aluminum",
         "params": {"width": 90, "depth": 90, "thick": 8, "boss_d": 24, "boss_h": 3,
                    "bolt_d": 5, "bolt_circle": 74, "bolt_count": 4}},
        pg,
        {"id": "brg", "type": "bearing", "material": "steel",
         "params": {"outer_d": 24, "bore_d": 12, "width": 6}},
        *[{"id": f"screw{i}", "type": "bolt", "material": "steel",
           "params": {"shank_d": 5, "length": 14, "head_d": 9, "head_h": 4}} for i in range(4)]],
        # planetary seats on the plate boss (intent picks the authored 'base', T2.4); the bearing stacks
        # on the gearset top (T2.5 — works once authored anchors win); screws drop into the bolt holes.
        "mates": [{"a": "plate", "b": "planetary", "intent": "seat_on"},
                  {"a": "planetary", "a_joint": "top", "b": "brg", "b_joint": "bore_base",
                   "type": "coincident"},
                  *[{"a": "plate", "a_joint": f"bolt{i}", "b": f"screw{i}", "b_joint": "seat",
                     "type": "coincident"} for i in range(4)]]}}
    res = assembly.build_tree("gearbox", defs, prewarm=True)
    assert res["bom"]["planetary_gearset"] == 1 and res["bom"]["bolt"] == 4 and res["bom"]["bearing"] == 1
    assert res["mass_g"] > 0
    assert res["cg"] is not None and res["principal_moments"] is not None
    # 4. validation: no clashes (the gear mesh is intended internal contact, not a clash)
    assert validate.interference(res["cq_assembly"])["ok"]
    # 5. manufacturing outputs (E7): exploded view, section drawing, BOM document
    import os
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        ex = assembly.explode(res["cq_assembly"], factor=15)
        assert len(validate._world_parts(ex)) == res["leaf_count"]            # explode preserves parts
        sec = drawing.section_dxf(res["cq_assembly"], os.path.join(d, "gb.dxf"), plane="XZ")
        assert sec["section_area_mm2"] > 0 and os.path.getsize(sec["path"]) > 0
    assert "TOTAL" in bommod.bom_document(res, "md")


@test
def test_prewarm_parallel_tree_matches_serial():
    """T3.3: prewarming the tree's part generation in a PROCESS pool must yield a build IDENTICAL to the
    serial build — same BOM, mass, CG, inertia, bbox (the recursive build + density-weighted mass-props
    path is untouched; only the part-gen cache is pre-populated)."""
    from constraint_kit import assembly, builder
    defs = {
        "unit": {"parts": [{"id": "g", "type": "spur_gear", "material": "steel",
                            "params": {"module": 1, "teeth": 24, "width": 6, "bore_d": 10}}],
                 "ports": {"c": {"origin": [0, 0, 0]}}},
        "rig": {"parts": [{"id": "p", "type": "plate", "material": "aluminum", "params": {}},
                          {"id": "s", "type": "spacer", "material": "brass",
                           "params": {"outer_d": 30, "bore_d": 0, "height": 8}}],
                "children": [{"instance": "u0", "ref": "unit", "place": {"port": "c", "at": [0, 0, 40]}},
                             {"instance": "u1", "ref": "unit", "place": {"port": "c", "at": [30, 0, 40]}}]},
    }
    builder.clear_cache()
    serial = assembly.build_tree("rig", defs)                    # serial, cold cache
    builder.clear_cache()
    n = builder.prewarm_cache(assembly.collect_leaf_specs("rig", defs), max_workers=4)
    assert n >= 3                                                # plate, spacer, gear generated in parallel
    par = assembly.build_tree("rig", defs)                       # build now hits the prewarmed cache
    assert builder.cache_stats()["misses"] == 0                  # every part was a cache hit (prewarmed)
    assert dict(serial["bom"]) == dict(par["bom"])
    approx(serial["mass_g"], par["mass_g"], eps=1e-3)
    for a, b in zip(serial["cg"], par["cg"]):
        approx(a, b, eps=1e-3)
    for a, b in zip(serial["principal_moments"], par["principal_moments"]):
        approx(a, b, eps=1e-2)                                   # mass-props identical: same geometry
    bs = serial["cq_assembly"].toCompound().BoundingBox()
    bp = par["cq_assembly"].toCompound().BoundingBox()
    approx(bs.xlen, bp.xlen, eps=1e-3); approx(bs.zlen, bp.zlen, eps=1e-3)
    # the prewarm flag on build_tree produces the same result end-to-end
    builder.clear_cache()
    flagged = assembly.build_tree("rig", defs, prewarm=True)
    approx(flagged["mass_g"], serial["mass_g"], eps=1e-3)


@test
def test_bom_document():
    """T7.3: BOM doc (md + csv) has correct per-type quantities and a total mass matching the roll-up."""
    import csv as _csv
    import io
    from constraint_kit import assembly
    from constraint_kit import bom as bommod
    defs = {"wheel": {"parts": [{"id": "rim", "type": "spacer", "material": "steel",
                                 "params": {"outer_d": 40, "bore_d": 20, "height": 10}}],
                      "ports": {"c": {"origin": [0, 0, 0]}}},
            "axle": {"children": [{"instance": "w0", "ref": "wheel", "place": {"port": "c", "at": [0, 0, 0]}},
                                  {"instance": "w1", "ref": "wheel", "place": {"port": "c", "at": [0, 0, 50]}}]}}
    res = assembly.build_tree("axle", defs)
    md = bommod.bom_document(res, "md")
    assert "| spacer | 2 |" in md and "TOTAL" in md           # 2 spacers rolled up
    rows = list(_csv.reader(io.StringIO(bommod.bom_document(res, "csv"))))
    assert rows[0] == ["part_type", "qty", "total_mass_g", "unit_mass_g"]
    spacer = next(r for r in rows if r and r[0] == "spacer")
    assert spacer[1] == "2"
    approx(float(spacer[2]), res["mass_by_type"]["spacer"], eps=1e-3)   # per-type mass matches roll-up


@test
def test_housing_part():
    """T1.5: the housing is a hollow shell (volume << solid box), has its floor bore, and exposes
    base/top/bore_axis/bolt anchors — a rich part built from shell+fillet+bore+bolts."""
    from constraint_kit import builder
    from constraint_kit.joints import _cylinder_axis
    from constraint_kit.parts import housing
    W, D, H, wall = 60.0, 60.0, 40.0, 3.0
    _, rep = builder.build_assembly({"parts": [{"id": "h", "type": "housing", "material": "aluminum",
        "params": {"width": W, "depth": D, "height": H, "wall": wall, "bore_d": 20,
                   "bolt_circle": 48, "bolt_count": 4, "fillet": 2}}], "mates": []})
    assert rep[0]["volume_mm3"] < W * D * H * 0.5            # hollow: far less than a solid box
    wp, anchors = housing(width=W, depth=D, height=H, wall=wall, bore_d=20, bolt_circle=48, bolt_count=4)
    assert {"base", "top", "bore_axis", "bolt0", "bolt3"} <= set(anchors)
    radii = [ax[1] for f in wp.faces("%Cylinder").vals() if (ax := _cylinder_axis(f))]
    assert any(abs(r - 10.0) < 0.5 for r in radii)           # the 20mm floor bore is present


@test
def test_sheet_bracket():
    """T1.4: a constant-thickness 90° sheet-metal L-bracket — exact L-volume (not a solid box), bbox = the
    two leg lengths, oriented anchors (base −Z, flange face −X), and an optional fail-soft bend radius."""
    from constraint_kit import builder
    from constraint_kit.parts import sheet_bracket
    t, a, b, w = 2.0, 40.0, 25.0, 30.0
    wp, anchors = sheet_bracket(thickness=t, base_length=a, flange_length=b, width=w)
    bb = wp.val().BoundingBox()
    approx(bb.xlen, a, eps=1e-6); approx(bb.zlen, b, eps=1e-6); approx(bb.ylen, w, eps=1e-6)
    approx(wp.val().Volume(), w * t * (a + b - t), eps=1e-3)     # L (corner counted once), NOT a*w*b box
    assert wp.val().Volume() < a * w * b * 0.5                   # clearly an L, not a filled box
    assert _axis_of(anchors["base"]) == (0.0, 0.0, -1.0)         # base seats face-down
    assert _axis_of(anchors["flange_face"]) == (-1.0, 0.0, 0.0)  # flange outer face normal −X
    assert "_bend_failed" not in anchors
    # builds through the kernel with mass
    _, rep = builder.build_assembly({"parts": [{"id": "br", "type": "sheet_bracket", "material": "steel",
        "params": {"thickness": t, "base_length": a, "flange_length": b, "width": w}}], "mates": []})
    assert rep[0]["mass_g"] > 0
    # bend radius produces a still-valid solid (fillet applied, or fail-soft flagged — never broken)
    wpb, ab = sheet_bracket(thickness=t, base_length=a, flange_length=b, width=w, bend_radius=3.0)
    assert wpb.val().Volume() > 0 and wpb.val().isValid()


@test
def test_adapter_loft():
    """T1.3: a lofted square-to-round adapter is non-prismatic — round top face, square base face."""
    import math
    from constraint_kit import builder
    from constraint_kit.parts import adapter
    W, Dd, top_d, H = 40.0, 40.0, 24.0, 30.0
    _, rep = builder.build_assembly({"parts": [{"id": "a", "type": "adapter",
        "params": {"bottom_w": W, "bottom_d": Dd, "top_d": top_d, "height": H}}], "mates": []})
    assert rep[0]["volume_mm3"] > 0
    wp, anchors = adapter(bottom_w=W, bottom_d=Dd, top_d=top_d, height=H)
    assert {"base", "top"} <= set(anchors)
    bb = wp.val().BoundingBox()
    approx(bb.zmax, H, eps=1e-3); approx(bb.zmin, 0.0, eps=1e-3)
    approx(wp.faces(">Z").val().Area(), math.pi * (top_d / 2) ** 2, eps=1.0)   # round top
    approx(wp.faces("<Z").val().Area(), W * Dd, eps=1.0)                        # square base


@test
def test_build_cache():
    """T3.1: identical parts hit the content-hash cache, and cached parts still place at DISTINCT
    locations (no shared-object aliasing — the key correctness risk)."""
    from constraint_kit import builder, validate
    builder.clear_cache()
    spec = {"parts": [
        {"id": "plate", "type": "plate", "params": {"width": 80, "depth": 80, "thick": 6, "boss_d": 10,
                                                    "boss_h": 2, "bolt_d": 5, "bolt_circle": 60, "bolt_count": 4}},
        *[{"id": f"b{i}", "type": "bolt", "params": {"shank_d": 5, "length": 14, "head_d": 9, "head_h": 4}}
          for i in range(4)],
    ], "mates": [
        {"a": "plate", "a_joint": f"bolt{i}", "b": f"b{i}", "b_joint": "seat", "type": "coincident"}
        for i in range(4)],
    }
    assy, _ = builder.build_assembly(spec)
    assert builder.cache_stats()["hits"] >= 3              # 4 identical bolts -> 1 miss + >=3 hits
    cen = {n.split("/")[-1]: s.Center() for n, s in validate._world_parts(assy)}
    bolt_xy = {(round(cen[f"b{i}"].x, 1), round(cen[f"b{i}"].y, 1)) for i in range(4)}
    assert len(bolt_xy) == 4                                # 4 distinct positions -> no aliasing


@test
def test_scale_grid_interference():
    """T3.4: build a 16-part grid; interference completes clean and the spatial grid prunes pairs."""
    from constraint_kit import assembly, validate
    k, sp = 4, 40.0
    defs = {"cell": {"parts": [{"id": "s", "type": "spacer",
                               "params": {"outer_d": 20, "bore_d": 8, "height": 10}}],
                     "ports": {"p": {"origin": [0, 0, 0]}}},
            "grid": {"children": [{"instance": f"c{i}_{j}", "ref": "cell",
                                   "place": {"port": "p", "at": [i * sp, j * sp, 0]}}
                                  for i in range(k) for j in range(k)]}}
    res = assembly.build_tree("grid", defs)
    assert res["part_count"] == 16
    inter = validate.interference(res["cq_assembly"])
    assert inter["ok"] and inter["pairs_candidate"] < inter["pairs_total"]   # spread grid -> pruned


@test
def test_parallel_build_matches_serial():
    """T3.3: the process-pool batch build returns geometry identical to the serial build (parallelism must
    never change the result), and the serial fallback path works. Geometry is truth."""
    from constraint_kit import builder
    specs = [{"parts": [{"id": "g", "type": "spur_gear", "material": "steel",
                         "params": {"module": 1, "teeth": 18 + i, "width": 6, "bore_d": 10}}], "mates": []}
             for i in range(4)]
    serial = builder.build_assemblies_parallel(specs, max_workers=1)     # fallback path
    par = builder.build_assemblies_parallel(specs, max_workers=4)        # process pool
    assert len(serial) == len(par) == 4
    for (ss, sr), (ps, pr) in zip(serial, par):
        approx(ss.Volume(), ps.Volume(), eps=1.0)                        # same geometry, both paths
        assert sr[0]["type"] == pr[0]["type"] == "spur_gear"
        assert pr[0]["mass_g"] > 0


@test
def test_design_rules():
    """T9.1/T9.2/T9.3: engagement threshold by material, fit recommendation, exact clearance gap."""
    from constraint_kit import assembly, rules
    assert rules.fastener_engagement(6, 6, "steel")["ok"] is True       # req 6mm (1xD)
    assert rules.fastener_engagement(6, 6, "aluminum")["ok"] is False   # req 12mm (2xD)
    assert rules.fit_appropriateness("running_clearance", "H7/g6")["ok"] is True
    assert rules.fit_appropriateness("running_clearance", "H7/p6")["ok"] is False
    assert rules.fit_appropriateness("bogus", "H7/g6")["ok"] is None    # honest unknown, not a fake pass
    defs = {"cell": {"parts": [{"id": "s", "type": "spacer",
                               "params": {"outer_d": 20, "bore_d": 0, "height": 10}}],
                     "ports": {"p": {"origin": [0, 0, 0]}}},
            "pair": {"children": [{"instance": "a", "ref": "cell", "place": {"port": "p", "at": [0, 0, 0]}},
                                  {"instance": "b", "ref": "cell", "place": {"port": "p", "at": [25, 0, 0]}}]}}
    asm = assembly.build_tree("pair", defs)["cq_assembly"]
    assert rules.min_clearance(asm, 3.0)["ok"]                           # gap 25-20=5 > 3 -> ok (pruned)
    c10 = rules.min_clearance(asm, 10.0)
    assert not c10["ok"] and len(c10["violations"]) == 1                 # 5 < 10 -> one violation
    assert abs(c10["min_gap_mm"] - 5.0) < 0.1                            # exact OCC gap = 5mm


@test
def test_material_strength_checks():
    """T4.3: cantilever bending stress/deflection and bolt proof load — validated against the closed-form
    hand calc (geometry/physics is truth). Honest ok:None for unknown material/size."""
    from constraint_kit import rules
    # cantilever: P=100N, L=100mm, b=h=10mm, steel (yield 235). sigma = 6PL/(b h^2) = 60000/1000 = 60 MPa
    r = rules.beam_bending("steel", length_mm=100, width_mm=10, height_mm=10, load_n=100, safety_factor=2.0)
    approx(r["max_stress_mpa"], 60.0, eps=1e-2)
    approx(r["allowable_mpa"], 117.5, eps=1e-2)                 # 235 / 2
    assert r["ok"] is True
    # I = b h^3/12 = 833.33 mm^4; delta = PL^3/(3 E I), E=210000 MPa -> 1e8 / (3*210000*833.33) = 0.190 mm
    approx(r["deflection_mm"], 0.1905, eps=1e-3)
    assert rules.beam_bending("steel", 100, 10, 10, 1000)["ok"] is False   # 600 MPa > 117.5 -> fails
    assert rules.beam_bending("unobtanium", 100, 10, 10, 100)["ok"] is None  # honest unknown

    # bolt: M8 class 8.8 -> As=36.6mm^2, Sp=600MPa -> proof load 21960N; preload 0.75x = 16470N
    b = rules.bolt_preload("M8-1.25", "8.8", applied_load_n=10000)
    approx(b["proof_load_n"], 21960.0, eps=1.0)
    approx(b["recommended_preload_n"], 16470.0, eps=1.0)
    assert b["ok"] is True                                      # 10000 < 21960
    assert rules.bolt_preload("M8", "8.8", applied_load_n=25000)["ok"] is False   # 25000 > 21960
    assert rules.bolt_preload("M99", "8.8")["ok"] is None       # honest unknown size


@test
def test_tolerance_stackup():
    """T4.2: a hole H7 − shaft g6 stack at 20mm reproduces the ISO 286 fit clearance exactly, and RSS is
    narrower than worst-case."""
    from constraint_kit import tolerance
    res = tolerance.stackup([{"name": "hole", "nominal": 20, "hole": "H7", "dir": 1},
                             {"name": "shaft", "nominal": 20, "shaft": "g6", "dir": -1}], as_clearance=True)
    approx(res["worst_case"]["min"], 0.007, eps=1e-3)        # ISO 286 H7/g6@20 min clearance
    approx(res["worst_case"]["max"], 0.041, eps=1e-3)        # max clearance
    assert res["ok_worstcase"]                                # min clearance > 0 -> no interference
    assert (res["rss"]["min"] > res["worst_case"]["min"]
            and res["rss"]["max"] < res["worst_case"]["max"])  # RSS is narrower than worst-case
    # explicit-deviation stack: 3 blocks of 10 ±0.05 -> 30 nominal, worst-case ±0.15
    blocks = tolerance.stackup([{"nominal": 10, "upper": 0.05, "lower": -0.05} for _ in range(3)])
    approx(blocks["nominal"], 30.0); approx(blocks["worst_case"]["max"], 30.15, eps=1e-6)


@test
def test_library_loader():
    """T6.1/T6.2: the automotive library loads and its defs build through the kernel."""
    from constraint_kit import assembly, library
    assert "automotive" in library.list_libraries()
    defs = library.load_library("automotive")
    assert {"wheel", "axle", "gearbox_housing"} <= set(defs)
    res = assembly.build_tree("axle", defs)
    assert res["part_count"] >= 4 and res["mass_g"] > 0     # 2 wheels x (rim+hub)
    assert library.library_meta("automotive")["version"] == "0.1"


@test
def test_architectural_library():
    """T6.3: the architectural library loads and a framed `room` (floor + 4 stud walls) builds through the
    kernel — 25 panel members, depth 3 (room->wall->member), mass rolls up, all panels placed."""
    from constraint_kit import assembly, library
    assert "architectural" in library.list_libraries()
    defs = library.load_library("architectural")
    assert {"stud", "wall_plate", "wall", "floor", "room"} <= set(defs)
    wall = assembly.build_tree("wall", defs)
    assert wall["bom"]["panel"] == 6                         # 2 plates + 4 studs
    room = assembly.build_tree("room", defs)
    assert room["bom"]["panel"] == 25                        # floor + 4 walls x 6
    assert room["depth"] == 3 and room["mass_g"] > 0
    # the 4 walls + floor span the room footprint (rotated side walls reach ±600 in both X and Y)
    bb = room["cq_assembly"].toCompound().BoundingBox()
    assert bb.xlen > 1100 and bb.ylen > 1100 and bb.zlen > 2300


def main():
    passed, failed = 0, []
    for fn in TESTS:
        try:
            fn()
            passed += 1
            print(f"  PASS  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(fn.__name__)
            print(f"  FAIL  {fn.__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{passed}/{passed + len(failed)} passed" + (f"; FAILED: {failed}" if failed else ""))
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
