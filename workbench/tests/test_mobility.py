#!/usr/bin/env python3
"""Mechanism mobility and loop closure from the joint graph (closes #11-#14).

    "C:/Program Files/FreeCAD 1.0/bin/freecadcmd.exe" workbench/tests/test_mobility.py

These four issues asked for `prismatic`, `screw`, `belt` and `gear_rack` mates. `ckwb/motion.py`
already delivered the MOTION -- a nut at a travel, a carriage at a position, a pulley at an angle,
all in closed form and swept for interference through the travel. What the comments on those
issues said was still missing was *"a real joint with a degree of freedom the solver understands,
which is what would give DOF analysis and loop closure"*.

That turned out not to need FreeCAD's solver at all. **Mobility is a property of the joint graph**,
not of the geometry: how many bodies, connected by what joints, each removing how many degrees of
freedom. Grubler and Kutzbach wrote it down in the 1880s and it is arithmetic.

So the capability arrives without the blocker. `#28` is still blocked on an undocumented
`Reference` path format, and it is no longer in the way of the thing it was blocking.

**Every number here is a textbook value**, which makes these equality tests:

| mechanism | mobility |
|---|---|
| two bodies, one revolute (spatial) | `6(2-1) - (6-1) = 1` |
| four-bar (planar) | `3(4-1) - 4(3-1) = 1` |
| slider-crank (planar) | `3(4-1) - 3(3-1) - (3-1) = 1` |
| a free body | 6 |
| a rigid chain | 0 |

**And the honest part.** Grubler COUNTS; it does not analyse. For special geometries the count is
wrong -- a Bennett linkage counts as 0 and moves anyway -- and those live in closed loops. So the
module reports the loop count beside the mobility and says the number is a count rather than a
proof whenever a loop exists. A mobility figure quoted without that caveat is the kind of
confident wrong answer this project spends most of its effort avoiding.
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
from ckwb import mobility as M  # noqa: E402
from ckwb.registry import registry  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def new_doc(name="mob"):
    if name in FreeCAD.listDocuments():
        FreeCAD.closeDocument(name)
    doc = FreeCAD.newDocument(name)
    FreeCAD.setActiveDocument(name)
    return doc


# ------------------------------------------------------------------- the joint vocabulary

@test
def test_a_lower_pair_removes_d_minus_its_freedoms():
    """The first half of the table, checked against the definitions.

    A revolute leaves one rotation, so it removes five of six in space and two of three in the
    plane. A cylindrical leaves a rotation and a slide, so it removes four. A screw as a lower
    pair -- a bolt turning in a threaded hole -- leaves the one helical freedom.
    """
    assert M.LOWER_PAIRS["fixed"] == 0, M.LOWER_PAIRS
    assert M.LOWER_PAIRS["revolute"] == 1
    assert M.LOWER_PAIRS["prismatic"] == 1
    assert M.LOWER_PAIRS["screw"] == 1
    assert M.LOWER_PAIRS["cylindrical"] == 2
    assert M.LOWER_PAIRS["spherical"] == 3
    assert M.LOWER_PAIRS["planar"] == 3
    for kind, f in M.LOWER_PAIRS.items():
        assert 0 <= f <= 5, (kind, f)
        assert M.constraints_of(kind, 6) == 6 - f, kind
        assert M.constraints_of(kind, 3) == 3 - f, kind
        assert not M.is_coupling(kind), kind


@test
def test_a_COUPLING_removes_exactly_one_freedom_whatever_the_dimension():
    """The second half, and the distinction that got me.

    A gear mesh, a belt or a ballscrew nut is ONE EQUATION between two bodies that are already
    connected some other way -- not a surface-contact joint. Counting a belt as a lower pair
    that "leaves one freedom" removes two in the plane, and a two-pulley belt drive then counts
    as a STRUCTURE: zero mobility, for a mechanism anybody can turn by hand.
    """
    for kind in ("gear", "belt", "screw_coupling"):
        assert M.is_coupling(kind), kind
        assert M.constraints_of(kind, 6) == 1, kind
        assert M.constraints_of(kind, 3) == 1, (
            "a coupling is one equation, so it removes one freedom in the plane too: %s" % kind)


@test
def test_screw_means_two_different_things_and_both_are_sayable():
    """A bolt in a threaded hole and a ballscrew nut are not the same joint.

    The first is the only connection between two bodies and is a lower pair. The second ties a
    shaft that already has a revolute to a carriage that already has a prismatic, and is one
    equation. Conflating them makes a CNC axis count as over-constrained.
    """
    assert not M.is_coupling("screw")
    assert M.constraints_of("screw", 6) == 5
    assert M.is_coupling("screw_coupling")
    assert M.constraints_of("screw_coupling", 6) == 1
    for alias in ("nut", "ballscrew", "ballnut"):
        assert M.canonical(alias) == "screw_coupling", alias


@test
def test_gear_rack_is_a_named_kind_because_it_was_asked_for_by_name():
    for name in ("gear_rack", "rack", "rack_and_pinion"):
        assert M.canonical(name) == "gear", name
        assert M.constraints_of(name, 3) == 1, name


@test
def test_an_unknown_joint_kind_is_refused_with_the_vocabulary():
    try:
        M.freedoms_of("welded_sort_of")
    except ValueError as exc:
        assert "welded_sort_of" in str(exc), str(exc)
        assert "revolute" in str(exc), "the real kinds should be listed: " + str(exc)
        return
    raise AssertionError("an unknown joint kind must be refused")


# ----------------------------------------------------------------- textbook mobility values

@test
def test_two_bodies_and_one_revolute_is_one_degree_of_freedom():
    out = M.mobility(bodies=["ground", "arm"],
                     joints=[{"kind": "revolute", "a": "ground", "b": "arm"}],
                     grounded=["ground"])
    assert out.mobility == 1, out.report()
    assert out.loops == 0, out.report()
    assert out.spatial is True


@test
def test_a_four_bar_is_one_degree_of_freedom_in_the_plane():
    """`3(4-1) - 4(3-1) = 1`. The canonical closed loop, and the canonical answer."""
    out = M.mobility(
        bodies=["ground", "crank", "coupler", "rocker"],
        joints=[{"kind": "revolute", "a": "ground", "b": "crank"},
                {"kind": "revolute", "a": "crank", "b": "coupler"},
                {"kind": "revolute", "a": "coupler", "b": "rocker"},
                {"kind": "revolute", "a": "rocker", "b": "ground"}],
        grounded=["ground"], spatial=False)
    assert out.mobility == 1, out.report()
    assert out.loops == 1, out.report()


@test
def test_a_slider_crank_is_one_degree_of_freedom():
    """Three revolutes and a prismatic. The mechanism the CNC axis actually is."""
    out = M.mobility(
        bodies=["ground", "crank", "rod", "slider"],
        joints=[{"kind": "revolute", "a": "ground", "b": "crank"},
                {"kind": "revolute", "a": "crank", "b": "rod"},
                {"kind": "revolute", "a": "rod", "b": "slider"},
                {"kind": "prismatic", "a": "slider", "b": "ground"}],
        grounded=["ground"], spatial=False)
    assert out.mobility == 1, out.report()
    assert out.loops == 1, out.report()


@test
def test_a_free_body_has_six_degrees_of_freedom_and_an_ungrounded_mechanism_says_so():
    """`floating` means "not connected to ground", so with no ground it is empty by definition.

    The ungrounded case is reported in its own line instead. Listing the one body as
    disconnected would be noise -- it is not disconnected from anything, there is simply nothing
    to be connected to.
    """
    out = M.mobility(bodies=["floater"], joints=[], grounded=[])
    assert out.mobility == 6, out.report()
    assert out.floating == [], out.floating
    assert "nothing is grounded" in out.report().lower(), out.report()


@test
def test_a_rigid_chain_has_no_mobility():
    out = M.mobility(bodies=["a", "b", "c"],
                     joints=[{"kind": "fixed", "a": "a", "b": "b"},
                             {"kind": "fixed", "a": "b", "b": "c"}],
                     grounded=["a"])
    assert out.mobility == 0, out.report()
    assert "structure" in out.report().lower(), out.report()


@test
def test_a_ballscrew_axis_comes_out_at_one_degree_of_freedom():
    """#12's ballscrew, as a mechanism rather than as a placement.

    Three bodies: the frame, the screw shaft with a revolute to it, and the carriage with a
    prismatic to it. The NUT is the coupling that ties the shaft's rotation to the carriage's
    travel -- one equation, five millimetres per turn. One input, one output, one degree of
    freedom.

    Modelling the nut as a lower-pair `screw` instead counts it as removing two constraints in
    the plane and makes the whole axis read as over-constrained, which is how the distinction
    between a pair and a coupling got found.
    """
    out = M.mobility(
        bodies=["frame", "shaft", "carriage"],
        joints=[{"kind": "revolute", "a": "frame", "b": "shaft"},
                {"kind": "prismatic", "a": "frame", "b": "carriage"},
                {"kind": "nut", "a": "shaft", "b": "carriage"}],
        grounded=["frame"], spatial=False)
    assert out.mobility == 1, out.report()
    assert out.loops == 1, out.report()


@test
def test_a_bolt_in_a_threaded_hole_is_the_LOWER_PAIR_screw_and_also_one_freedom():
    """The other meaning of screw, and the arithmetic is different even though the answer is 1."""
    out = M.mobility(bodies=["plate", "bolt"],
                     joints=[{"kind": "screw", "a": "plate", "b": "bolt"}],
                     grounded=["plate"])
    assert out.mobility == 1, out.report()
    assert out.loops == 0, out.report()
    assert out.terms == [("screw", 5)], out.terms


@test
def test_a_belt_drive_between_two_pulleys_is_one_degree_of_freedom():
    """#13's HTD-8M knee drive: two pulleys on a frame, coupled by a belt."""
    out = M.mobility(
        bodies=["frame", "driver", "driven"],
        joints=[{"kind": "revolute", "a": "frame", "b": "driver"},
                {"kind": "revolute", "a": "frame", "b": "driven"},
                {"kind": "belt", "a": "driver", "b": "driven"}],
        grounded=["frame"], spatial=False)
    assert out.mobility == 1, out.report()
    assert out.loops == 1, out.report()


# ----------------------------------------------------------- over and under constraint

@test
def test_an_over_constrained_assembly_is_reported_as_such():
    """Negative mobility is a real and common mistake, and nothing used to notice it.

    Two revolutes between the same pair of bodies on different axes is the classic version: it
    cannot move and cannot be assembled without forcing something.
    """
    out = M.mobility(
        bodies=["ground", "arm"],
        joints=[{"kind": "revolute", "a": "ground", "b": "arm"},
                {"kind": "revolute", "a": "ground", "b": "arm"}],
        grounded=["ground"])
    assert out.mobility < 0, out.report()
    assert out.over_constrained is True
    text = out.report().lower()
    assert "over-constrained" in text, text
    assert "redundant" in text or "forcing" in text, text


@test
def test_an_under_constrained_body_is_named_rather_than_averaged_into_the_total():
    """A floating part makes the total mobility look fine while the model is wrong.

    Mobility is a sum, so one body with six free degrees of freedom and a mechanism that is
    over-constrained by six can cancel out exactly. Naming the disconnected bodies separately is
    the only way that is visible.
    """
    out = M.mobility(
        bodies=["ground", "arm", "orphan"],
        joints=[{"kind": "revolute", "a": "ground", "b": "arm"}],
        grounded=["ground"])
    assert out.floating == ["orphan"], out.floating
    assert "orphan" in out.report(), out.report()
    assert out.mobility == 7, (
        "one revolute plus a free body is 1 + 6; the sum hides nothing only because the body is "
        "named: %d" % out.mobility)


@test
def test_a_joint_naming_a_body_that_does_not_exist_is_refused():
    try:
        M.mobility(bodies=["a", "b"],
                   joints=[{"kind": "revolute", "a": "a", "b": "ghost"}],
                   grounded=["a"])
    except ValueError as exc:
        assert "ghost" in str(exc), str(exc)
        assert "a, b" in str(exc) or "'a'" in str(exc), "the real bodies should be listed: " + str(exc)
        return
    raise AssertionError("a joint on a non-existent body must be refused")


@test
def test_grounding_a_body_that_does_not_exist_is_refused():
    try:
        M.mobility(bodies=["a"], joints=[], grounded=["nope"])
    except ValueError as exc:
        assert "nope" in str(exc), str(exc)
        return
    raise AssertionError("grounding a non-existent body must be refused")


# ------------------------------------------------------------------------- honest doubt

@test
def test_a_loop_makes_the_report_say_the_number_is_a_count_and_not_a_proof():
    """Grubler counts; it does not analyse. The paradoxes live in closed loops.

    A Bennett linkage counts as zero mobility and moves anyway, because the formula assumes the
    constraints are independent and in special geometries they are not. A mobility figure quoted
    without that caveat is exactly the confident wrong answer this project spends its effort
    avoiding -- so the caveat appears whenever a loop does, not only when the answer is suspect.
    """
    looped = M.mobility(
        bodies=["ground", "crank", "coupler", "rocker"],
        joints=[{"kind": "revolute", "a": "ground", "b": "crank"},
                {"kind": "revolute", "a": "crank", "b": "coupler"},
                {"kind": "revolute", "a": "coupler", "b": "rocker"},
                {"kind": "revolute", "a": "rocker", "b": "ground"}],
        grounded=["ground"], spatial=False)
    text = looped.report().lower()
    assert "count" in text, looped.report()
    assert "geometry" in text or "paradox" in text or "independent" in text, looped.report()

    tree = M.mobility(bodies=["ground", "arm"],
                      joints=[{"kind": "revolute", "a": "ground", "b": "arm"}],
                      grounded=["ground"])
    assert tree.loops == 0
    assert "count" not in tree.report().lower() or "no loop" in tree.report().lower(), (
        "a tree has no paradoxes, so the caveat is noise there:\n" + tree.report())


@test
def test_the_report_is_plain_ascii_and_shows_the_arithmetic():
    out = M.mobility(
        bodies=["ground", "crank", "coupler", "rocker"],
        joints=[{"kind": "revolute", "a": "ground", "b": "crank"},
                {"kind": "revolute", "a": "crank", "b": "coupler"},
                {"kind": "revolute", "a": "coupler", "b": "rocker"},
                {"kind": "revolute", "a": "rocker", "b": "ground"}],
        grounded=["ground"], spatial=False)
    text = out.report()
    # the arithmetic must be visible, or the number is an oracle
    assert "3" in text and "4" in text, text
    assert "revolute" in text, text
    bad = sorted({c for c in text if ord(c) > 127})
    assert not bad, "non-ASCII %r in a console report:\n%s" % (bad, text)


# -------------------------------------------------------------------------- the command

@test
def test_mobility_is_a_registered_command_driven_from_the_document():
    """An agent can ask whether the thing it just assembled can actually move."""
    names = {t["name"] for t in registry.schema()}
    assert "mobility" in names, sorted(names)

    doc = new_doc()
    D.make_part(doc, "plate", None, name="Frame")
    D.make_part(doc, "shaft", None, name="Shaft")
    D.make_part(doc, "spacer", None, name="Hub")
    doc.recompute()

    out = registry.call("mobility",
                        joints=[{"kind": "revolute", "a": "Frame", "b": "Shaft"},
                                {"kind": "fixed", "a": "Shaft", "b": "Hub"}],
                        grounded=["Frame"])
    assert out["mobility"] == 1, out
    assert out["loops"] == 0, out
    assert out["bodies"] == 3, out
    assert out["floating"] == [], out
    assert out["report"], out
    assert out["fingerprint"], out


@test
def test_the_command_defaults_the_bodies_to_the_parts_in_the_document():
    """So the caller lists joints, not an inventory it already has."""
    doc = new_doc()
    D.make_part(doc, "plate", None, name="Frame")
    D.make_part(doc, "shaft", None, name="Shaft")
    doc.recompute()
    out = registry.call("mobility",
                        joints=[{"kind": "revolute", "a": "Frame", "b": "Shaft"}],
                        grounded=["Frame"])
    assert out["bodies"] == 2, out
    assert out["mobility"] == 1, out


@test
def test_the_command_refuses_a_joint_on_a_part_that_is_not_in_the_document():
    doc = new_doc()
    D.make_part(doc, "plate", None, name="Frame")
    doc.recompute()
    try:
        registry.call("mobility",
                      joints=[{"kind": "revolute", "a": "Frame", "b": "Imaginary"}],
                      grounded=["Frame"])
    except Exception as exc:                                     # noqa: BLE001
        assert "Imaginary" in str(exc), str(exc)
        return
    raise AssertionError("a joint on a part that is not there must be refused")


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
