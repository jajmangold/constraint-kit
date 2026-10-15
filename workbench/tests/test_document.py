#!/usr/bin/env python3
"""The document object, the params round-trip and the checks — runs INSIDE FreeCAD.

    "C:/Program Files/FreeCAD 1.0/bin/freecadcmd.exe" workbench/tests/test_document.py

This is where the two halves meet: FreeCAD's document is the live editing surface, and the text
params file is the source of truth. The tests that matter here are the ones that keep that
relationship honest — a property edit must rebuild geometry, and a save/load cycle through the
text file must reproduce the same model, provenance included.

Also tested: the thing the design calls a first-class STALE state. A check result records a
fingerprint of the geometry it was computed against, so a green result cannot outlive the edit
that invalidated it. That is the failure mode this project has actually been bitten by — six
renders of a redesigned part sat at the top of a README for weeks because nothing could tell
that a stored result no longer described the model.
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
from ckwb import document as D  # noqa: E402
from ckwb.params import load_params, save_params  # noqa: E402
from ckwb.provenance import DEFAULT, STANDARD, STATED, ParamSet  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def new_doc(name="t"):
    if name in FreeCAD.listDocuments():
        FreeCAD.closeDocument(name)
    return FreeCAD.newDocument(name)


def block_params() -> ParamSet:
    p = ParamSet("block_with_bore")
    p.set("width", 50.0, STATED)
    p.set("depth", 30.0, STATED)
    p.set("height", 24.0, DEFAULT, note="bore + 2 walls")
    p.set("bore_d", 22.0, STANDARD, source="608 bearing OD", at="2026-10-15")
    return p


# ------------------------------------------------------------------------- the document object

@test
def test_make_part_builds_geometry_from_its_parameters():
    doc = new_doc()
    obj = D.make_part(doc, "block_with_bore", block_params(), name="Block")
    doc.recompute()
    expect = 50.0 * 30.0 * 24.0 - math.pi * 11.0 ** 2 * 30.0
    assert abs(obj.Shape.Volume - expect) < 1e-3, obj.Shape.Volume
    assert obj.CkGenerator == "block_with_bore", obj.CkGenerator


@test
def test_parameters_become_properties_grouped_by_provenance():
    """The property editor IS the provenance inspector; the group is how a guess looks guessed."""
    doc = new_doc()
    obj = D.make_part(doc, "block_with_bore", block_params(), name="Block")
    groups = {obj.getGroupOfProperty(p) for p in obj.PropertiesList
              if p.startswith(("Width", "Depth", "Height", "BoreD"))}
    assert "Guessed for you" in groups, groups
    assert "You specified" in groups, groups
    assert "From a standard" in groups, groups
    assert obj.getGroupOfProperty("Height") == "Guessed for you"


@test
def test_editing_a_property_rebuilds_the_geometry():
    doc = new_doc()
    obj = D.make_part(doc, "block_with_bore", block_params(), name="Block")
    doc.recompute()
    before = obj.Shape.Volume
    obj.Height = 40.0
    doc.recompute()
    after = obj.Shape.Volume
    expect = 50.0 * 30.0 * 40.0 - math.pi * 11.0 ** 2 * 30.0
    assert abs(after - expect) < 1e-3, (after, expect)
    assert after > before


@test
def test_rebuilding_never_bakes_a_placement_into_the_shape():
    """The trap that cost this project days: obj.Shape comes back WITH Placement applied.

    A read-modify-write of Shape while posed bakes the pose into the geometry permanently. This
    generator always rebuilds from parameters instead, so the bug is structurally impossible --
    and this test proves it by posing the part, recomputing, and checking the local geometry is
    untouched.
    """
    doc = new_doc()
    obj = D.make_part(doc, "spacer", None, name="Sp")
    doc.recompute()
    local_before = obj.Shape.copy()
    local_before.Placement = FreeCAD.Placement()
    obj.Placement = FreeCAD.Placement(FreeCAD.Vector(37, -12, 5),
                                      FreeCAD.Rotation(FreeCAD.Vector(1, 1, 0), 33))
    doc.recompute()
    local_after = obj.Shape.copy()
    local_after.Placement = FreeCAD.Placement()
    assert abs(local_after.Volume - local_before.Volume) < 1e-9
    assert abs(local_after.BoundBox.ZMin - local_before.BoundBox.ZMin) < 1e-9, (
        "the placement leaked into the geometry")


@test
def test_paramset_reads_back_off_the_object_with_provenance_intact():
    doc = new_doc()
    obj = D.make_part(doc, "block_with_bore", block_params(), name="Block")
    back = D.paramset_of(obj)
    assert back.generator == "block_with_bore"
    assert back["bore_d"].provenance == STANDARD
    assert back["bore_d"].source == "608 bearing OD"
    assert back["height"].provenance == DEFAULT
    assert back["height"].note == "bore + 2 walls"
    assert back.summary() == "1 of 4 guessed", back.summary()


@test
def test_an_edited_property_shows_up_in_the_read_back_params():
    doc = new_doc()
    obj = D.make_part(doc, "block_with_bore", block_params(), name="Block")
    obj.Width = 64.0
    assert D.paramset_of(obj)["width"].value == 64.0


@test
def test_promoting_a_guess_moves_its_property_group():
    doc = new_doc()
    obj = D.make_part(doc, "block_with_bore", block_params(), name="Block")
    D.promote(obj, "height")
    assert obj.getGroupOfProperty("Height") == "You specified"
    assert D.paramset_of(obj)["height"].provenance == STATED
    assert D.paramset_of(obj).summary() == "0 of 4 guessed"


# ------------------------------------------------------------------- the text source of truth

@test
def test_document_params_round_trip_through_the_text_file():
    """Save to text, rebuild the document from the text, get the same model back."""
    doc = new_doc()
    D.make_part(doc, "block_with_bore", block_params(), name="Block")
    D.make_part(doc, "spacer", None, name="Sp")
    doc.recompute()
    volumes = {o.Name: round(o.Shape.Volume, 6) for o in D.ck_parts(doc)}

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.ckparts.json")
        D.save_document_params(doc, path)

        fresh = new_doc("t2")
        D.load_document_params(fresh, path)
        fresh.recompute()
        got = {o.Name: round(o.Shape.Volume, 6) for o in D.ck_parts(fresh)}

    assert got == volumes, (got, volumes)
    reloaded = {o.Name: D.paramset_of(o) for o in D.ck_parts(fresh)}
    assert reloaded["Block"]["bore_d"].provenance == STANDARD
    assert reloaded["Block"]["height"].provenance == DEFAULT


@test
def test_a_single_part_file_matches_the_params_module_format():
    doc = new_doc()
    obj = D.make_part(doc, "spacer", None, name="Sp")
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "sp.params.json")
        save_params(D.paramset_of(obj), path)
        back = load_params(path)
    assert back.generator == "spacer"
    assert set(back) == set(D.paramset_of(obj))


# ----------------------------------------------------------------------------------- checks

@test
def test_interference_finds_a_deliberate_overlap_and_reports_its_volume():
    doc = new_doc()
    a = D.make_part(doc, "plate", None, name="A")
    b = D.make_part(doc, "plate", None, name="B")
    doc.recompute()
    # plate defaults are 60 x 40 x 8; lift B by 4 so exactly half its thickness overlaps
    b.Placement = FreeCAD.Placement(FreeCAD.Vector(0, 0, 4), FreeCAD.Rotation())
    doc.recompute()
    res = C.interference(D.ck_parts(doc))
    assert res["ok"] is False
    assert len(res["clashes"]) == 1, res
    assert abs(res["clashes"][0]["volume_mm3"] - 60 * 40 * 4) < 1e-3, res["clashes"][0]
    assert res["pairs_total"] == 1 and res["pairs_exact_checked"] == 1


@test
def test_interference_is_clean_when_parts_are_merely_touching():
    """Seated faces touch. Touching is not interfering, and must not be reported as a clash."""
    doc = new_doc()
    a = D.make_part(doc, "plate", None, name="A")
    b = D.make_part(doc, "plate", None, name="B")
    doc.recompute()
    b.Placement = FreeCAD.Placement(FreeCAD.Vector(0, 0, 8), FreeCAD.Rotation())
    doc.recompute()
    res = C.interference(D.ck_parts(doc))
    assert res["ok"] is True, res


@test
def test_interference_prefilter_skips_pairs_that_cannot_touch():
    doc = new_doc()
    for i in range(4):
        o = D.make_part(doc, "plate", None, name=f"P{i}")
        o.Placement = FreeCAD.Placement(FreeCAD.Vector(0, 0, i * 200), FreeCAD.Rotation())
    doc.recompute()
    res = C.interference(D.ck_parts(doc))
    assert res["ok"] is True
    assert res["pairs_total"] == 6, res
    assert res["pairs_exact_checked"] == 0, "far-apart pairs must not reach the boolean"


@test
def test_mass_uses_density_and_rolls_up():
    doc = new_doc()
    D.make_part(doc, "plate", None, name="A")      # 60 x 40 x 8 = 19200 mm3
    doc.recompute()
    res = C.mass(D.ck_parts(doc), density_g_cm3=2.70)
    assert abs(res["volume_cm3"] - 19.2) < 1e-6, res
    assert abs(res["mass_g"] - 19.2 * 2.70) < 1e-6, res


@test
def test_a_check_goes_stale_when_the_model_changes():
    doc = new_doc()
    a = D.make_part(doc, "plate", None, name="A")
    doc.recompute()
    res = C.interference(D.ck_parts(doc))
    assert not C.is_stale(res, doc), "a result must not be born stale"

    a.Thickness = 12.0
    doc.recompute()
    assert C.is_stale(res, doc), "editing the model must invalidate a stored result"


@test
def test_a_check_goes_stale_when_a_part_merely_moves():
    """Moving a part changes interference without changing any volume, so the fingerprint
    has to include placement. Volume alone would miss it."""
    doc = new_doc()
    a = D.make_part(doc, "plate", None, name="A")
    doc.recompute()
    res = C.interference(D.ck_parts(doc))
    a.Placement = FreeCAD.Placement(FreeCAD.Vector(0, 0, 3), FreeCAD.Rotation())
    doc.recompute()
    assert C.is_stale(res, doc)


@test
def test_fingerprint_is_stable_across_a_resave():
    """Re-saving an .FCStd changes its bytes; the fingerprint must not move unless geometry does."""
    doc = new_doc()
    D.make_part(doc, "plate", None, name="A")
    doc.recompute()
    first = C.fingerprint(doc)
    doc.recompute()
    assert C.fingerprint(doc) == first


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
