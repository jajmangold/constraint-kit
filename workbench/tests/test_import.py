#!/usr/bin/env python3
"""STEP import: geometry the kernel did not generate, participating as a first-class part.

    "C:/Program Files/FreeCAD 1.0/bin/freecadcmd.exe" workbench/tests/test_import.py

Until now the kernel could only reason about shapes it made itself, which ruled out the most
useful geometry there is: a vendor's own STEP for a bearing, a profile, a motor. An approximation
is fine for mass and envelope and wrong for anything that has to fit.

The design question this answers is not "can we read a file" — `Part.read()` does that — but
**what an imported part IS**. It has no generator and no parameters, so it cannot be parametric;
but it has a shape, so it must be checked, weighed, analysed and exported alongside everything
else. Hence two collections: `ck_parts()` stays parametric, and `all_parts()` is everything with
geometry. Getting that split wrong would mean a bought component silently escaping clash
detection, which is precisely the failure that makes CAD worthless.

Frames are derived from the bounding box and NAMED so the approximation is visible: `bbox_top`,
not `top`. A vendor STEP carries no semantics about which face is a mounting face, and inventing
one would be the kind of plausible lie this project exists to avoid.
"""
from __future__ import annotations

import math
import os
import sys
import tempfile
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import FreeCAD  # noqa: E402

from ckwb import checks as C  # noqa: E402
from ckwb import commands as K  # registers the command vocabulary  # noqa: E402,F401
from ckwb import document as D  # noqa: E402
from ckwb.registry import registry  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def new_doc(name="imp"):
    for existing in list(FreeCAD.listDocuments()):
        FreeCAD.closeDocument(existing)
    doc = FreeCAD.newDocument(name)
    FreeCAD.setActiveDocument(name)
    return doc


def a_step_file(tmp, generator="ball_bearing", **params):
    """Write a STEP file by exporting a generated part — a real file, no fixture to maintain."""
    import Part

    from ckwb import parts as P

    shape, _ = P.build(generator, **(params or P.defaults_for(generator)))
    path = os.path.join(tmp, generator + ".step")
    shape.exportStep(path)
    return path, shape


# --------------------------------------------------------------------------- reading a file

@test
def test_a_step_file_round_trips_with_its_volume_intact():
    doc = new_doc()
    with tempfile.TemporaryDirectory() as tmp:
        path, original = a_step_file(tmp)
        obj = D.import_step(doc, path, name="Bearing")
        doc.recompute()
    rel = abs(obj.Shape.Volume - original.Volume) / original.Volume
    assert rel < 1e-6, (obj.Shape.Volume, original.Volume, rel)
    assert len(obj.Shape.Solids) == 1
    assert obj.Name == "Bearing"


@test
def test_an_imported_part_records_where_it_came_from():
    """Without the source path on the object, nobody can tell generated from bought."""
    doc = new_doc()
    with tempfile.TemporaryDirectory() as tmp:
        path, _ = a_step_file(tmp)
        obj = D.import_step(doc, path)
        assert D.is_imported(obj) is True
        assert obj.CkSource == os.path.abspath(path), obj.CkSource
        assert D.is_ck_part(obj) is False, "an import has no generator, so it is not parametric"


@test
def test_import_refuses_a_missing_file_and_a_file_it_cannot_read():
    doc = new_doc()
    try:
        D.import_step(doc, os.path.join(tempfile.gettempdir(), "nope-does-not-exist.step"))
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("a missing file must raise")

    with tempfile.TemporaryDirectory() as tmp:
        junk = os.path.join(tmp, "not-really.step")
        with open(junk, "w", encoding="utf-8") as fh:
            fh.write("this is not a STEP file\n")
        try:
            D.import_step(doc, junk)
        except ValueError as exc:
            assert "solid" in str(exc).lower() or "read" in str(exc).lower(), str(exc)
            return
    raise AssertionError("a file with no solids in it must raise, not make an empty part")


@test
def test_a_multi_solid_file_can_be_kept_whole_or_split():
    """A vendor assembly legitimately holds many solids; a generated part never should."""
    import Part

    from ckwb import parts as P

    doc = new_doc()
    with tempfile.TemporaryDirectory() as tmp:
        a, _ = P.plate(width=20.0, depth=20.0, thickness=5.0)
        b, _ = P.plate(width=20.0, depth=20.0, thickness=5.0)
        b.Placement = FreeCAD.Placement(FreeCAD.Vector(0, 0, 40), FreeCAD.Rotation())
        path = os.path.join(tmp, "two.step")
        Part.makeCompound([a, b]).exportStep(path)

        whole = D.import_step(doc, path, name="Whole")
        assert len(whole.Shape.Solids) == 2, len(whole.Shape.Solids)

        pieces = D.import_step(doc, path, name="Split", split=True)
        assert isinstance(pieces, list) and len(pieces) == 2, pieces
        for piece in pieces:
            assert len(piece.Shape.Solids) == 1
            assert D.is_imported(piece)


# ------------------------------------------------------------------------ joining the model

@test
def test_an_import_is_in_all_parts_but_not_in_the_parametric_set():
    doc = new_doc()
    registry.call("insert_part", generator="plate", name="Plate")
    with tempfile.TemporaryDirectory() as tmp:
        path, _ = a_step_file(tmp)
        D.import_step(doc, path, name="Bearing")
    doc.recompute()
    assert [o.Name for o in D.ck_parts(doc)] == ["Plate"]
    assert sorted(o.Name for o in D.all_parts(doc)) == ["Bearing", "Plate"]


@test
def test_an_imported_part_is_clash_checked_like_any_other():
    """The failure this guards: a bought component quietly escaping interference detection."""
    doc = new_doc()
    registry.call("insert_part", generator="plate", name="Plate")   # 60 x 40 x 8 from z=0
    with tempfile.TemporaryDirectory() as tmp:
        path, _ = a_step_file(tmp, "spacer", od=20.0, id_=8.0, height=6.0)
        obj = D.import_step(doc, path, name="Spacer")
    # drop it halfway into the plate
    obj.Placement = FreeCAD.Placement(FreeCAD.Vector(0, 0, 5.0), FreeCAD.Rotation())
    doc.recompute()

    res = C.interference(D.all_parts(doc))
    assert res["ok"] is False, res
    assert len(res["clashes"]) == 1, res
    names = {res["clashes"][0]["a"], res["clashes"][0]["b"]}
    assert names == {"Plate", "Spacer"}, names


@test
def test_mass_includes_imported_geometry():
    doc = new_doc()
    with tempfile.TemporaryDirectory() as tmp:
        path, original = a_step_file(tmp, "spacer", od=20.0, id_=8.0, height=6.0)
        D.import_step(doc, path, name="Spacer")
    doc.recompute()
    res = C.mass(D.all_parts(doc), density_g_cm3=7.85)
    assert abs(res["volume_cm3"] - original.Volume / 1000.0) < 1e-6, res


@test
def test_the_solids_check_tolerates_a_multi_solid_import_but_not_a_generated_one():
    """One solid is a correctness invariant for generated parts: a boolean that severs a part
    returns a valid shape with two solids rather than an error, and that has slipped through
    before. A vendor assembly is a different thing and must not be reported as broken."""
    import Part

    from ckwb import parts as P

    doc = new_doc()
    with tempfile.TemporaryDirectory() as tmp:
        a, _ = P.plate(width=10.0, depth=10.0, thickness=4.0)
        b, _ = P.plate(width=10.0, depth=10.0, thickness=4.0)
        b.Placement = FreeCAD.Placement(FreeCAD.Vector(0, 0, 50), FreeCAD.Rotation())
        path = os.path.join(tmp, "two.step")
        Part.makeCompound([a, b]).exportStep(path)
        imported = D.import_step(doc, path, name="Vendor")
    doc.recompute()

    res = C.solids(D.all_parts(doc))
    assert res["ok"] is True, res
    noted = [n for n in res.get("multi_solid_imports", []) if n == "Vendor"]
    assert noted, res


@test
def test_get_tree_reports_an_import_as_imported_with_its_source():
    doc = new_doc()
    with tempfile.TemporaryDirectory() as tmp:
        path, _ = a_step_file(tmp)
        D.import_step(doc, path, name="Bearing")
        doc.recompute()
        tree = registry.call("get_tree")
    entry = next(p for p in tree["parts"] if p["name"] == "Bearing")
    assert entry["generator"] == "(imported)", entry
    assert entry["source"].endswith(".step"), entry
    assert entry["volume_mm3"] > 0
    assert entry["guessed"] == [], "an import has no parameters, so nothing is guessed"


@test
def test_export_includes_imported_geometry():
    doc = new_doc()
    registry.call("insert_part", generator="plate", name="Plate")
    with tempfile.TemporaryDirectory() as tmp:
        path, _ = a_step_file(tmp)
        D.import_step(doc, path, name="Bearing")
        doc.recompute()
        out_path = os.path.join(tmp, "both.step")
        out = registry.call("export", path=out_path)
        assert out["parts"] == 2, out
        assert os.path.getsize(out_path) > 1000


# ----------------------------------------------------------------------------- bbox frames

@test
def test_bbox_frames_are_derived_and_honestly_named():
    """A vendor STEP says nothing about which face is a mounting face. Inventing `top` would be
    a plausible lie; `bbox_top` tells the reader exactly how much it means."""
    doc = new_doc()
    with tempfile.TemporaryDirectory() as tmp:
        path, original = a_step_file(tmp, "plate", width=60.0, depth=40.0, thickness=8.0)
        obj = D.import_step(doc, path, name="Plate")
    doc.recompute()

    frames = D.frames_of(obj)
    assert set(frames) == {"bbox_top", "bbox_bottom", "bbox_x+", "bbox_x-",
                           "bbox_y+", "bbox_y-", "bbox_centre"}, sorted(frames)
    assert not any(f in frames for f in ("top", "bottom", "bore")), \
        "derived frames must not masquerade as semantic ones"

    bb = obj.Shape.BoundBox
    assert abs(frames["bbox_top"].Base.z - bb.ZMax) < 1e-6
    assert abs(frames["bbox_bottom"].Base.z - bb.ZMin) < 1e-6
    assert frames["bbox_top"].Rotation.multVec(FreeCAD.Vector(0, 0, 1)).z > 0.999
    assert frames["bbox_bottom"].Rotation.multVec(FreeCAD.Vector(0, 0, 1)).z < -0.999
    centre = frames["bbox_centre"].Base
    assert abs(centre.z - (bb.ZMin + bb.ZMax) / 2.0) < 1e-6


@test
def test_an_import_can_be_seated_on_a_generated_part_by_its_bbox_frame():
    doc = new_doc()
    base = D.make_part(doc, "plate", None, name="Base")
    with tempfile.TemporaryDirectory() as tmp:
        path, _ = a_step_file(tmp, "spacer", od=20.0, id_=8.0, height=6.0)
        imported = D.import_step(doc, path, name="Spacer")
    doc.recompute()

    out = registry.call("join", kind="seat", target="Base", target_frame="top",
                        moving="Spacer", moving_frame="bbox_bottom")
    assert abs(out["gap_mm"]) < 1e-9, out
    assert abs(imported.Shape.BoundBox.ZMin - 8.0) < 1e-6, imported.Shape.BoundBox.ZMin
    assert C.interference(D.all_parts(doc))["ok"] is True


# -------------------------------------------------------------------------- via the registry

@test
def test_import_is_a_registered_command_an_agent_can_call():
    doc = new_doc()
    with tempfile.TemporaryDirectory() as tmp:
        path, original = a_step_file(tmp)
        out = registry.call("import_step", path=path, name="Bearing")
    assert out["name"] == "Bearing", out
    assert out["solids"] == 1 and out["imported"] is True, out
    assert abs(out["volume_mm3"] - original.Volume) < 1e-3, out
    assert sorted(out["frames"]) == sorted(D.frames_of(doc.getObject("Bearing"))), out


@test
def test_the_import_command_is_marked_as_a_write():
    by_name = {t["name"]: t for t in registry.schema()}
    assert by_name["import_step"]["writes"] is True
    assert by_name["import_step"]["input_schema"]["required"] == ["path"]


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
    if failed:
        sys.exit(1)


# No __main__ guard: freecadcmd imports a script as a module named after its file.
main()
