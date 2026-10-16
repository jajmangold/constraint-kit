#!/usr/bin/env python3
"""Cause attribution and ranked, RE-VERIFIED repairs (issue #20).

    "C:/Program Files/FreeCAD 1.0/bin/freecadcmd.exe" workbench/tests/test_diagnose.py

The validator already returned structure. What it did not return was a **cause** and a **fix**.
A program with `thicknes` for `thickness` came back as a sentence, went to the model, and cost a
round trip -- for a misspelling with exactly one plausible correction. A clash came back as a
volume and said nothing about which feature of which part caused it.

So the shape of this is: classify the cause, generate repairs **deterministically** where the
geometry already determines the answer, rank them, and **re-verify every one** before offering
it. The model is the fallback rather than the first resort, which is both cheaper and more
reliable -- a nearest-match on a parameter name is not a judgement call.

**Nothing is offered that has not been re-verified.** That is the acceptance criterion on the
issue and it is also the only defensible rule: a suggested fix that does not build is worse than
no suggestion, because somebody will take it.

**And some causes have no repair, by design.** An unknown generator is the important one. Nothing
here will quietly swap `turbine_blade` for `plate` because the names are both parts -- that is a
fabrication, not a fix, and the honest output is a decline naming what the catalogue has. The
test for that is the one to read first.
"""
from __future__ import annotations

import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import FreeCAD  # noqa: E402

from ckwb import commands as K  # noqa: F401,E402  (registers them)
from ckwb import diagnose as Dg  # noqa: E402
from ckwb import document as D  # noqa: E402
from ckwb.registry import registry  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def new_doc(name="diag"):
    if name in FreeCAD.listDocuments():
        FreeCAD.closeDocument(name)
    doc = FreeCAD.newDocument(name)
    FreeCAD.setActiveDocument(name)
    return doc


def _program(**over) -> dict:
    """A program that validates, so a test can break exactly one thing about it.

    Overriding `parts` without `mates` drops the default mate, because the default one refers to
    both default parts by name. Leaving it in place meant every single-part fixture also produced
    a spurious `unknown_part_reference`, and three tests failed against entirely correct code --
    a fixture that breaks TWO things while claiming to break one.
    """
    out = {
        "parts": [
            {"name": "Base", "generator": "plate",
             "params": {"width": {"value": 80.0, "from_request": True}}},
            {"name": "Post", "generator": "spacer", "params": {}},
        ],
        "mates": [{"kind": "seat", "target": "Base", "target_frame": "top",
                   "moving": "Post", "moving_frame": "bottom"}],
        "notes": "",
    }
    if "parts" in over and "mates" not in over:
        over = dict(over, mates=[])
    out.update(over)
    return out


# --------------------------------------------------------------------------- cause attribution

@test
def test_a_misspelled_parameter_is_attributed_to_its_nearest_match():
    program = _program(parts=[
        {"name": "Base", "generator": "plate",
         "params": {"thicknes": {"value": 10.0, "from_request": True}}}])
    found = Dg.diagnose(program)
    assert len(found) == 1, [f.summary for f in found]
    fault = found[0]
    assert fault.cause == "unknown_parameter", fault.cause
    assert fault.part == "Base" and fault.detail == "thicknes", (fault.part, fault.detail)
    assert "thickness" in fault.summary, fault.summary


@test
def test_an_unknown_frame_is_attributed_with_the_frames_that_exist():
    program = _program(mates=[{"kind": "seat", "target": "Base", "target_frame": "ceiling",
                               "moving": "Post", "moving_frame": "bottom"}])
    found = Dg.diagnose(program)
    assert [f.cause for f in found] == ["unknown_frame"], [f.cause for f in found]
    assert found[0].detail == "ceiling", found[0].detail
    assert "top" in found[0].summary, found[0].summary


@test
def test_an_unknown_generator_is_attributed_and_is_NOT_a_misspelling():
    """The cause that must never be "repaired" by substitution."""
    program = _program(parts=[{"name": "Blade", "generator": "turbine_blade", "params": {}}])
    found = Dg.diagnose(program)
    assert [f.cause for f in found] == ["unknown_generator"], [f.cause for f in found]
    assert found[0].detail == "turbine_blade", found[0].detail


@test
def test_a_geometry_failure_is_attributed_to_the_values_that_caused_it():
    """A spacer whose bore exceeds its outside diameter. The generator refuses; so does this."""
    program = _program(parts=[
        {"name": "Ring", "generator": "spacer",
         "params": {"od": {"value": 10.0, "from_request": True},
                    "id_": {"value": 20.0, "from_request": True}}}])
    found = Dg.diagnose(program)
    assert found, "a geometry failure must be attributed, not swallowed"
    assert found[0].cause == "geometry_failed", found[0].cause
    assert found[0].part == "Ring", found[0].part
    assert "id_" in found[0].summary or "bore" in found[0].summary, found[0].summary


@test
def test_a_valid_program_has_nothing_to_diagnose():
    assert Dg.diagnose(_program()) == [], Dg.diagnose(_program())


@test
def test_every_cause_in_the_taxonomy_is_reachable_and_documented():
    """A taxonomy with a category nothing produces is a taxonomy nobody can rely on."""
    assert Dg.CAUSES, "the taxonomy is empty"
    for cause, text in Dg.CAUSES.items():
        assert text and len(text) > 20, (cause, text)
    produced = set()
    for program in (
            _program(parts=[{"name": "A", "generator": "nope", "params": {}}]),
            _program(parts=[{"name": "A", "generator": "plate",
                             "params": {"wibble": {"value": 1.0}}}]),
            _program(mates=[{"kind": "seat", "target": "Base", "target_frame": "nope",
                             "moving": "Post", "moving_frame": "bottom"}]),
            _program(mates=[{"kind": "weld", "target": "Base", "target_frame": "top",
                             "moving": "Post", "moving_frame": "bottom"}]),
            _program(parts=[{"name": "R", "generator": "spacer",
                             "params": {"od": {"value": 5.0}, "id_": {"value": 9.0}}}]),
            _program(mates=[{"kind": "seat", "target": "Ghost", "target_frame": "top",
                             "moving": "Post", "moving_frame": "bottom"}])):
        produced.update(f.cause for f in Dg.diagnose(program))

    # `interference` is the one cause that cannot come from a PROGRAM -- it needs placed
    # geometry -- so the document path has to be walked too, or the taxonomy test would be
    # asserting that a reachable category is unreachable.
    doc = new_doc("taxonomy")
    lower = D.make_part(doc, "plate", None, name="Lower")
    upper = D.make_part(doc, "plate", None, name="Upper")
    upper.Placement = FreeCAD.Placement(FreeCAD.Vector(0, 0, 4.0), FreeCAD.Rotation())
    doc.recompute()
    produced.update(f.cause for f in Dg.repair_document(doc).faults)

    missing = set(Dg.CAUSES) - produced
    assert not missing, "no fixture produces %s" % sorted(missing)


# -------------------------------------------------------------------- ranked, verified repairs

@test
def test_a_misspelled_parameter_is_repaired_deterministically_and_re_verified():
    """The headline: a fix that needs no model call, and is proved to build before it is offered."""
    program = _program(parts=[
        {"name": "Base", "generator": "plate",
         "params": {"thicknes": {"value": 10.0, "from_request": True}}}])
    out = Dg.repair(program)
    assert out.fixed is not None, out.report()
    assert out.repairs, out.report()

    best = out.repairs[0]
    assert best.verified is True, best
    assert "thickness" in best.summary, best.summary
    params = out.fixed["parts"][0]["params"]
    assert "thickness" in params and "thicknes" not in params, params
    # the VALUE must survive the rename, or the repair has thrown away what was asked for
    assert params["thickness"]["value"] == 10.0, params
    assert params["thickness"]["from_request"] is True, (
        "a renamed parameter keeps its provenance; the user did state this number")
    assert Dg.diagnose(out.fixed) == [], Dg.diagnose(out.fixed)


@test
def test_an_unknown_frame_is_repaired_only_when_the_choice_is_unambiguous():
    """`botom` has one plausible correction. `ceiling` has none, and guessing would be a fiction.

    This is the line the whole module walks. A nearest-match within an edit or two of a real
    name is a typo and fixing it is safe; a word that simply is not in the vocabulary is a
    different request, and picking the alphabetically-first frame would be inventing an answer.
    """
    typo = _program(mates=[{"kind": "seat", "target": "Base", "target_frame": "botom",
                            "moving": "Post", "moving_frame": "bottom"}])
    out = Dg.repair(typo)
    assert out.fixed is not None, out.report()
    assert out.fixed["mates"][0]["target_frame"] == "bottom", out.fixed["mates"]
    assert out.repairs[0].verified is True

    nonsense = _program(mates=[{"kind": "seat", "target": "Base", "target_frame": "ceiling",
                                "moving": "Post", "moving_frame": "bottom"}])
    out = Dg.repair(nonsense)
    assert out.fixed is None, (
        "no frame is within reach of 'ceiling', so there is nothing to offer: %s" % out.report())
    assert "ceiling" in out.report(), out.report()
    assert any("no repair" in n.lower() or "cannot" in n.lower() for n in [out.report()]), \
        out.report()


@test
def test_an_unknown_generator_is_NEVER_repaired_by_substitution():
    """The honesty guard, and the first test to read.

    Swapping a requested part for a different one that happens to be in the catalogue is a
    fabrication, not a repair. `turbine_blade` is not a misspelling of `plate`, and a tool that
    quietly made it one would build the wrong object and report success.
    """
    program = _program(parts=[{"name": "Blade", "generator": "turbine_blade", "params": {}}])
    out = Dg.repair(program)
    assert out.fixed is None, "an unknown generator must not be substituted: %s" % out.report()
    # Not an EMPTY list -- an explicit rejection, which is better. An empty list leaves a reader
    # wondering whether the case was even considered; a recorded refusal says it was looked at
    # and why nothing is being offered.
    assert out.repairs, "the refusal should be recorded, not silent"
    assert not any(r.verified for r in out.repairs), [r.summary for r in out.repairs]
    assert all(r.confidence == 0.0 for r in out.repairs), [r.confidence for r in out.repairs]
    assert any("fabrication" in r.rejected.lower() for r in out.repairs), \
        [r.rejected for r in out.repairs]
    text = out.report()
    assert "turbine_blade" in text, text
    assert "plate" in text, "the catalogue should be listed so the caller can choose: " + text
    lowered = text.lower()
    assert "nothing is offered" in lowered, text
    assert "without guessing" in lowered or "fabrication" in lowered, text


@test
def test_a_value_out_of_range_is_repaired_toward_the_bound_it_broke():
    """A spacer whose bore exceeds its OD. The fix is determined by the generator's own rule."""
    program = _program(parts=[
        {"name": "Ring", "generator": "spacer",
         "params": {"od": {"value": 10.0, "from_request": True},
                    "id_": {"value": 20.0, "from_request": True}}}])
    out = Dg.repair(program)
    assert out.fixed is not None, out.report()
    fixed = {k: v["value"] for k, v in out.fixed["parts"][0]["params"].items()}
    assert fixed["id_"] < fixed["od"], fixed
    assert Dg.diagnose(out.fixed) == [], Dg.diagnose(out.fixed)
    # And it must say that it changed a number the user STATED, which is not a free action.
    # The warning belongs in `because` rather than in `summary`: the summary is the one-line
    # "what", and "you asked for this and I overrode it" is a reason.
    assert any("stated" in r.because.lower() or "you gave" in r.because.lower()
               for r in out.repairs), [(r.summary, r.because) for r in out.repairs]
    assert out.repairs[0].confidence < 0.8, (
        "overriding a value the user stated must rank BELOW a plain rename: %.2f"
        % out.repairs[0].confidence)


@test
def test_repairs_are_ranked_and_the_ranking_is_explained():
    """Cheapest and most certain first, and the reason is in the text rather than implied."""
    program = _program(parts=[
        {"name": "Base", "generator": "plate",
         "params": {"thicknes": {"value": 10.0, "from_request": True},
                    "widht": {"value": 90.0, "from_request": True}}}])
    out = Dg.repair(program)
    assert out.fixed is not None, out.report()
    assert len(out.repairs) >= 2, [r.summary for r in out.repairs]
    scores = [r.confidence for r in out.repairs]
    assert scores == sorted(scores, reverse=True), scores
    for r in out.repairs:
        assert 0.0 < r.confidence <= 1.0, r
        assert r.because, r.summary
    # both misspellings fixed in one pass
    params = out.fixed["parts"][0]["params"]
    assert {"thickness", "width"} <= set(params), sorted(params)


@test
def test_a_repair_that_does_not_verify_is_discarded_rather_than_offered():
    """The acceptance criterion on the issue, tested by sabotaging the verifier.

    A candidate is generated, re-verification is forced to fail, and nothing must come back. The
    alternative -- offering it anyway -- is worse than offering nothing, because somebody takes it.
    """
    program = _program(parts=[
        {"name": "Base", "generator": "plate",
         "params": {"thicknes": {"value": 10.0, "from_request": True}}}])
    original = Dg.diagnose

    def always_broken(_program):
        return [Dg.Fault(cause="geometry_failed", part="Base", detail="",
                         summary="sabotaged", severity=1)]

    Dg.diagnose = always_broken
    try:
        out = Dg.repair(program)
    finally:
        Dg.diagnose = original
    assert out.fixed is None, "a repair that does not re-verify must not be returned"
    assert all(not r.verified for r in out.repairs), [r.summary for r in out.repairs]
    assert "did not" in out.report().lower() or "rejected" in out.report().lower(), out.report()


@test
def test_the_report_says_what_was_tried_and_why_each_one_failed():
    program = _program(parts=[{"name": "Blade", "generator": "turbine_blade", "params": {}}],
                       mates=[])
    text = Dg.repair(program).report()
    assert "turbine_blade" in text, text
    bad = sorted({c for c in text if ord(c) > 127})
    assert not bad, "non-ASCII %r in a console report:\n%s" % (bad, text)


# ----------------------------------------------------------------------- the clash case (#20)

@test
def test_a_clash_is_attributed_to_the_overlapping_pair_with_a_re_verified_fix():
    """The worked example from the issue: two parts overlapping, and what to do about it.

    The repair is a separation along the mate axis, computed from the overlap rather than
    guessed, and re-verified by re-running the interference check -- which is the whole point.
    """
    doc = new_doc()
    a = D.make_part(doc, "plate", None, name="Lower")
    b = D.make_part(doc, "plate", None, name="Upper")
    b.Placement = FreeCAD.Placement(FreeCAD.Vector(0, 0, 4.0), FreeCAD.Rotation())
    doc.recompute()

    before = registry.call("run_checks")["checks"]["interference"]
    assert before["ok"] is False, "the fixture must actually clash"

    out = Dg.repair_document(doc)
    assert out.repairs, out.report()
    best = out.repairs[0]
    assert best.verified is True, out.report()
    assert "Lower" in best.summary and "Upper" in best.summary, best.summary

    after = registry.call("run_checks")["checks"]["interference"]
    assert after["ok"] is True, (
        "the repair was verified, so applying it must leave the model clean: %s" % after)
    assert abs(b.Placement.Base.z - 8.0) < 1e-6, (
        "the separation should be exactly the plate thickness: %s" % b.Placement.Base.z)


@test
def test_a_clean_document_has_nothing_to_repair_and_is_left_alone():
    doc = new_doc()
    obj = D.make_part(doc, "plate", None, name="Only")
    doc.recompute()
    where = obj.Placement.Base.z
    out = Dg.repair_document(doc)
    assert out.repairs == [], [r.summary for r in out.repairs]
    assert out.fixed is None
    assert abs(obj.Placement.Base.z - where) < 1e-12, "a no-op must move nothing"
    assert "nothing" in out.report().lower() or "no fault" in out.report().lower(), out.report()


# ------------------------------------------------------------------------------- integration

@test
def test_the_planner_tries_a_deterministic_repair_before_asking_the_model():
    """The point of the whole exercise: a typo costs no round trip.

    The session is given a reply with one misspelled parameter and must come back with a usable
    plan WITHOUT another model call -- which is observable, because `offer` returns "plan"
    instead of "retry".
    """
    import json

    from ckwb import planner

    session = planner.PlanSession("a plate 80 wide and 10 thick")
    reply = json.dumps({"parts": [
        {"name": "Base", "generator": "plate",
         "params": {"width": {"value": 80.0, "from_request": True},
                    "thicknes": {"value": 10.0, "from_request": True}}}], "mates": []})
    kind, payload = session.offer(reply)
    assert kind == "plan", (kind, payload)
    assert payload.declined is False, payload.reason
    params = payload.paramset("Base")
    assert abs(params["thickness"].value - 10.0) < 1e-9, params["thickness"]
    assert session.attempt == 1, (
        "a deterministic repair must not consume a model attempt: %d" % session.attempt)
    assert any("thickness" in note for note in [payload.notes]), payload.notes


@test
def test_the_planner_still_asks_the_model_when_no_repair_is_deterministic():
    """The fallback has to survive, or an unrepairable program would be reported as impossible."""
    import json

    from ckwb import planner

    session = planner.PlanSession("a turbine blade")
    reply = json.dumps({"parts": [
        {"name": "Blade", "generator": "turbine_blade", "params": {}}], "mates": []})
    kind, payload = session.offer(reply)
    assert kind == "retry", (kind, payload)
    assert session.problems, session.problems
    assert any("turbine_blade" in p for p in session.problems), session.problems


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
