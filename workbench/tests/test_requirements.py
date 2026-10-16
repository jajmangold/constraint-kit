#!/usr/bin/env python3
"""Requirements drive parameters, and the design says whether it meets them (issue #29).

    "C:/Program Files/FreeCAD 1.0/bin/freecadcmd.exe" workbench/tests/test_requirements.py

Until now the workbench had a Requirements button that said "not implemented yet", which is the
one kind of interface element worth less than nothing. More importantly it had the dependency
running the wrong way: a parameter was a number somebody typed, and the reason for the number
lived in their head. Change the belt tension and nothing moves; change a plate and nothing
notices the belt tension it was sized for.

So: a **requirement** is a named value with a unit and a reason. A **derived** value is an
expression over requirements. And a parameter can be **driven** by an expression, which makes the
dependency real -- edit the requirement and the geometry follows, with the chain visible rather
than remembered.

Two properties are the whole point and both are tested here:

* **Provenance survives the whole way down.** A driven parameter is tagged `derived`, not
  `stated`, so the property editor shows it as something the design computed rather than
  something a human chose. A design where you cannot tell those apart is a design nobody can
  review.
* **A requirement can be VERIFIED, not just recorded.** A target on a measured quantity --
  "deflection <= 1 mm" -- is checked against the physics and comes back met or unmet. A
  requirement nobody checks is a comment.

The expression evaluator is `constraint_kit.parameters.safe_eval`, reused rather than rewritten:
it is a strict AST whitelist and never Python `eval`, which matters because an expression can
arrive from a planner.
"""
from __future__ import annotations

import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import FreeCAD  # noqa: E402

from ckwb import commands as K  # noqa: F401,E402  (registers them)
from ckwb import document as D  # noqa: E402
from ckwb import requirements as R  # noqa: E402
from ckwb.registry import registry  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def new_doc(name="req"):
    if name in FreeCAD.listDocuments():
        FreeCAD.closeDocument(name)
    doc = FreeCAD.newDocument(name)
    FreeCAD.setActiveDocument(name)
    return doc


# ------------------------------------------------------------------------- stated requirements

@test
def test_a_requirement_carries_its_value_unit_and_reason():
    """A number without a reason is the thing this replaces."""
    doc = new_doc()
    R.set_requirement(doc, "belt_tension", 764.0, unit="N",
                      note="measured on the bench at the knee drive")
    got = R.requirements(doc)
    assert "belt_tension" in got, sorted(got)
    entry = got["belt_tension"]
    assert entry["value"] == 764.0 and entry["unit"] == "N"
    assert "bench" in entry["note"]
    assert entry["kind"] == "stated"


@test
def test_requirements_survive_a_save_and_reload():
    """They are the design intent, so they belong in the document rather than in a session."""
    import tempfile

    doc = new_doc()
    R.set_requirement(doc, "span", 160.0, unit="mm")
    R.set_requirement(doc, "load", 80.0, unit="N")
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "req.FCStd")
        doc.saveAs(path)
        FreeCAD.closeDocument(doc.Name)
        again = FreeCAD.openDocument(path)
        try:
            got = R.requirements(again)
            assert got["span"]["value"] == 160.0, got
            assert got["load"]["unit"] == "N", got
        finally:
            FreeCAD.closeDocument(again.Name)


@test
def test_a_requirement_name_must_be_usable_in_an_expression():
    doc = new_doc()
    for bad in ("", "  ", "2fast", "has space", "with-dash", "class"):
        try:
            R.set_requirement(doc, bad, 1.0)
        except ValueError as exc:
            assert "name" in str(exc).lower(), (bad, str(exc))
            continue
        raise AssertionError("requirement name %r should have been refused" % bad)


# -------------------------------------------------------------------------- derived values

@test
def test_a_derived_value_is_computed_from_requirements_and_records_its_relation():
    doc = new_doc()
    R.set_requirement(doc, "span", 160.0, unit="mm")
    R.set_requirement(doc, "load", 80.0, unit="N")
    R.derive(doc, "moment", "load * span / 1000", unit="Nm",
             note="peak bending moment at the root")

    values = R.solve(doc)
    assert abs(values["moment"] - 12.8) < 1e-9, values
    entry = R.requirements(doc)["moment"]
    assert entry["kind"] == "derived"
    assert entry["expression"] == "load * span / 1000"
    assert set(entry["depends_on"]) == {"load", "span"}


@test
def test_a_derived_value_follows_when_its_requirement_changes():
    """The dependency that was running the wrong way before."""
    doc = new_doc()
    R.set_requirement(doc, "load", 80.0)
    R.derive(doc, "doubled", "load * 2")
    assert R.solve(doc)["doubled"] == 160.0
    R.set_requirement(doc, "load", 100.0)
    assert R.solve(doc)["doubled"] == 200.0


@test
def test_derived_values_can_chain_and_a_cycle_is_refused():
    doc = new_doc()
    R.set_requirement(doc, "a", 2.0)
    R.derive(doc, "b", "a * 3")
    R.derive(doc, "c", "b + 1")
    assert R.solve(doc)["c"] == 7.0

    R.derive(doc, "loop1", "loop2 + 1")
    R.derive(doc, "loop2", "loop1 + 1")
    try:
        R.solve(doc)
    except R.RequirementError as exc:
        text = str(exc).lower()
        assert "cycle" in text or "circular" in text or "resolve" in text, str(exc)
        assert "loop1" in str(exc) or "loop2" in str(exc), str(exc)
        return
    raise AssertionError("a circular derivation must be refused")


@test
def test_an_expression_over_an_unknown_name_says_which_name():
    doc = new_doc()
    R.set_requirement(doc, "span", 160.0)
    R.derive(doc, "bad", "span * wibble")
    try:
        R.solve(doc)
    except R.RequirementError as exc:
        assert "wibble" in str(exc), str(exc)
        assert "span" in str(exc), "the names that DO exist should be listed: " + str(exc)
        return
    raise AssertionError("an unknown name in an expression must be refused")


@test
def test_an_expression_cannot_reach_outside_arithmetic():
    """It may arrive from a planner, so it is a whitelist and never Python eval."""
    doc = new_doc()
    R.set_requirement(doc, "x", 1.0)
    for nasty in ("__import__('os').system('echo')", "open('/tmp/x','w')",
                  "x.__class__", "[i for i in range(3)]"):
        R.derive(doc, "attack", nasty)
        try:
            R.solve(doc)
        except (R.RequirementError, ValueError):
            # Refused, which is the only thing that matters. TWO different guards catch these
            # and that is a feature rather than a redundancy: the free-name check rejects
            # `__import__` and `range` before anything is evaluated at all, and the AST
            # whitelist rejects an attribute access or a comprehension if one ever got past it.
            # Asserting a particular message would be asserting which guard fired.
            continue
        raise AssertionError("%r must not evaluate" % nasty)


# ------------------------------------------------------------------------ driven parameters

@test
def test_a_driven_parameter_moves_when_the_requirement_moves():
    """The headline: requirements drive geometry, and the chain is in the document."""
    doc = new_doc()
    obj = D.make_part(doc, "plate", None, name="Deck")
    doc.recompute()

    R.set_requirement(doc, "deck_span", 150.0, unit="mm")
    R.drive(doc, "Deck", "width", "deck_span")
    R.apply(doc)
    assert abs(obj.Width - 150.0) < 1e-9, obj.Width

    R.set_requirement(doc, "deck_span", 200.0)
    R.apply(doc)
    assert abs(obj.Width - 200.0) < 1e-9, obj.Width
    assert obj.Shape.BoundBox.XLength > 199.0, "the geometry must have rebuilt, not just the value"


@test
def test_a_driven_parameter_is_tagged_derived_rather_than_stated():
    """A design where you cannot tell a computed value from a chosen one is unreviewable."""
    doc = new_doc()
    D.make_part(doc, "plate", None, name="Deck")
    doc.recompute()
    R.set_requirement(doc, "deck_span", 150.0)
    R.drive(doc, "Deck", "width", "deck_span")
    R.apply(doc)

    params = registry.call("get_params", part="Deck")["params"]
    assert params["width"]["provenance"] == "derived", params["width"]
    assert "deck_span" in params["width"].get("note", ""), params["width"]
    # and a value nobody drove is still whatever it was
    assert params["depth"]["provenance"] == "default", params["depth"]


@test
def test_driving_an_expression_not_just_a_name():
    doc = new_doc()
    obj = D.make_part(doc, "plate", None, name="Deck")
    doc.recompute()
    R.set_requirement(doc, "span", 100.0)
    R.set_requirement(doc, "margin", 8.0)
    R.drive(doc, "Deck", "width", "span + 2 * margin")
    R.apply(doc)
    assert abs(obj.Width - 116.0) < 1e-9, obj.Width


@test
def test_driving_an_unknown_part_or_parameter_is_refused_with_what_exists():
    doc = new_doc()
    D.make_part(doc, "plate", None, name="Deck")
    doc.recompute()
    R.set_requirement(doc, "span", 100.0)
    try:
        R.drive(doc, "Nope", "width", "span")
    except Exception as exc:                                     # noqa: BLE001
        assert "Nope" in str(exc), str(exc)
    else:
        raise AssertionError("an unknown part must be refused")
    try:
        R.drive(doc, "Deck", "wibble", "span")
    except Exception as exc:                                     # noqa: BLE001
        assert "wibble" in str(exc) and "width" in str(exc), str(exc)
        return
    raise AssertionError("an unknown parameter must be refused, naming the real ones")


@test
def test_a_driven_parameter_can_be_released_back_to_a_typed_value():
    doc = new_doc()
    obj = D.make_part(doc, "plate", None, name="Deck")
    doc.recompute()
    R.set_requirement(doc, "span", 150.0)
    R.drive(doc, "Deck", "width", "span")
    R.apply(doc)
    assert abs(obj.Width - 150.0) < 1e-9

    R.release(doc, "Deck", "width")
    R.set_requirement(doc, "span", 999.0)
    R.apply(doc)
    assert abs(obj.Width - 150.0) < 1e-9, (
        "a released parameter must stop following the requirement: %s" % obj.Width)


# ------------------------------------------------------------------------------- verification

@test
def test_a_target_on_a_measured_quantity_is_verified_against_the_physics():
    """A requirement nobody checks is a comment. This one is checked.

    Two runs of the same target against two thicknesses: the thin deck misses it and the thick
    deck meets it, so the verdict is coming from the geometry rather than from the requirement
    being restated.
    """
    doc = new_doc()
    obj = D.make_part(doc, "plate", None, name="Deck")
    obj.Width, obj.Depth, obj.Thickness = 120.0, 20.0, 4.0
    doc.recompute()

    R.set_requirement(doc, "tip_load", 60.0, unit="N")
    # 3 mm, not 1: a 120 x 20 PETG deck reads 2.39 mm even at 16 mm thick on this mesh, so a
    # 1 mm target is unreachable anywhere in the fixture's range and the test would prove only
    # that everything fails. The point is the SEPARATION -- 4 mm misses, 16 mm meets.
    R.target(doc, "stiff enough", metric="max_disp_mm", op="<=", limit=3.0,
             note="the drive cannot take more than three millimetres of give")

    out = R.verify(doc, part="Deck", material="petg", pitch=4.0,
                   fix="x<=-58", load="x>=58", force=(0.0, 0.0, -60.0))
    thin = {v["name"]: v for v in out["verdicts"]}["stiff enough"]
    assert thin["met"] is False, thin
    assert thin["measured"] > 3.0, thin

    obj.Thickness = 16.0
    doc.recompute()
    out = R.verify(doc, part="Deck", material="petg", pitch=4.0,
                   fix="x<=-58", load="x>=58", force=(0.0, 0.0, -60.0))
    thick = {v["name"]: v for v in out["verdicts"]}["stiff enough"]
    assert thick["met"] is True, thick
    assert thick["measured"] < 3.0, thick
    assert out["ok"] is True, out


@test
def test_a_target_on_a_metric_the_analysis_does_not_report_is_refused():
    doc = new_doc()
    D.make_part(doc, "plate", None, name="Deck")
    doc.recompute()
    R.target(doc, "impossible", metric="vibration_hz", op="<=", limit=10.0)
    try:
        R.verify(doc, part="Deck", material="petg", pitch=6.0,
                 fix="x<=-28", load="x>=28", force=(0.0, 0.0, -10.0))
    except R.RequirementError as exc:
        assert "vibration_hz" in str(exc), str(exc)
        return
    raise AssertionError("a target on an unreported metric must be refused")


@test
def test_a_target_refuses_a_nonsense_operator():
    doc = new_doc()
    try:
        R.target(doc, "bad", metric="mass_g", op="~=", limit=1.0)
    except ValueError as exc:
        assert "operator" in str(exc).lower() or "~=" in str(exc), str(exc)
        return
    raise AssertionError("an unknown operator must be refused")


# -------------------------------------------------------------------------------- the commands

@test
def test_every_requirement_operation_is_a_registered_command():
    """Agents drive the same surface the panel does, which is the one architectural rule here."""
    names = {t["name"] for t in registry.schema()}
    for expected in ("set_requirement", "derive_requirement", "drive_param", "release_param",
                     "get_requirements", "verify_requirements"):
        assert expected in names, "%s is not registered; have %s" % (expected, sorted(names))

    doc = new_doc()
    obj = D.make_part(doc, "plate", None, name="Deck")
    doc.recompute()
    registry.call("set_requirement", name="span", value=140.0, unit="mm", note="the frame pitch")
    registry.call("derive_requirement", name="half", expression="span / 2")
    registry.call("drive_param", part="Deck", param="width", expression="span")
    out = registry.call("get_requirements")

    assert out["values"]["half"] == 70.0, out
    assert out["requirements"]["span"]["unit"] == "mm", out
    assert out["driven"][0]["part"] == "Deck", out["driven"]
    assert abs(obj.Width - 140.0) < 1e-9, obj.Width
    assert out["fingerprint"], out

    registry.call("release_param", part="Deck", param="width")
    assert registry.call("get_requirements")["driven"] == []


@test
def test_get_requirements_reports_a_stale_chain_rather_than_a_cached_green():
    """An edited requirement that has not been applied is a design in two states at once."""
    doc = new_doc()
    D.make_part(doc, "plate", None, name="Deck")
    doc.recompute()
    registry.call("set_requirement", name="span", value=140.0)
    registry.call("drive_param", part="Deck", param="width", expression="span")
    assert registry.call("get_requirements")["stale"] is False

    # set_requirement applies on its way out, so force the divergence directly
    R.set_requirement(doc, "span", 300.0, apply=False)
    out = registry.call("get_requirements")
    assert out["stale"] is True, out
    assert any("Deck" in s for s in out["stale_params"]), out["stale_params"]


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


# No __main__ guard: freecadcmd imports a script as a module named after its file.
if main():
    sys.exit(1)
