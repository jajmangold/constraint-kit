#!/usr/bin/env python3
"""The planner: plain English -> a reviewable program -> geometry. Runs INSIDE FreeCAD.

    "C:/Program Files/FreeCAD 1.0/bin/freecadcmd.exe" workbench/tests/test_planner.py

The model call is injected, so every test here runs with no network, no API key and no
non-determinism. That is deliberate and it is where the value is: the LLM is a thin layer, and
what has to be right is the part around it —

* **a proposed program is validated before it can build anything.** An unknown generator, an
  unknown parameter or an unknown frame is refused with a message naming what does exist.
* **a refusal becomes one repair attempt**, with the error handed back, per the execute-and-repair
  pattern the 2026 text-to-CAD work converged on. One retry, not an unbounded loop.
* **a decline passes through untouched.** "I cannot model that" is a first-class answer and must
  never be dressed up as geometry.
* **provenance is preserved.** A value the request actually stated becomes `stated`; a value the
  model chose becomes `default`, so it shows up as a guess in the property editor. The planner
  does not get to launder its own guesses into decisions.
"""
from __future__ import annotations

import json
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import FreeCAD  # noqa: E402

from ckwb import document as D  # noqa: E402
from ckwb import planner as PL  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def new_doc(name="plan"):
    if name in FreeCAD.listDocuments():
        FreeCAD.closeDocument(name)
    doc = FreeCAD.newDocument(name)
    FreeCAD.setActiveDocument(name)
    return doc


def canned(*replies):
    """A fake model: returns each reply in turn, and records what it was asked."""
    state = {"calls": [], "left": list(replies)}

    def chat(messages, **_kw):
        state["calls"].append(messages)
        if not state["left"]:
            raise AssertionError("the planner asked the model more times than expected")
        return state["left"].pop(0)

    chat.state = state
    return chat


GOOD = json.dumps({
    "parts": [
        {"name": "Screw", "generator": "ball_screw",
         "params": {"nominal_d": {"value": 16, "from_request": True},
                    "lead": {"value": 5, "from_request": True},
                    "length": {"value": 500, "from_request": True}}},
        {"name": "Nut", "generator": "ball_nut", "params": {}},
    ],
    "mates": [{"kind": "align", "target": "Screw", "target_frame": "nut_travel",
               "moving": "Nut", "moving_frame": "bore"}],
    "notes": "SFU1605: 16 mm nominal, 5 mm lead. Thread groove not modelled.",
})


# ----------------------------------------------------------------------------- happy path

@test
def test_a_valid_program_parses_and_keeps_its_notes():
    plan = PL.plan("a 500mm sfu1605 ball screw assembly", chat=canned(GOOD))
    assert not plan.declined, plan
    assert [p["name"] for p in plan.parts] == ["Screw", "Nut"], plan.parts
    assert plan.parts[0]["generator"] == "ball_screw"
    assert len(plan.mates) == 1
    assert "5 mm lead" in plan.notes


@test
def test_values_from_the_request_are_stated_and_the_rest_are_guesses():
    """The planner must not launder its own guesses into decisions."""
    plan = PL.plan("a 500mm sfu1605 ball screw assembly", chat=canned(GOOD))
    screw = plan.paramset("Screw")
    assert screw["length"].provenance == "stated", screw["length"]
    assert screw["lead"].provenance == "stated"
    # end_d was never mentioned, so it comes from the generator signature as a guess
    assert screw["end_d"].provenance == "default", screw["end_d"]
    nut = plan.paramset("Nut")
    assert nut.guessed(), "a part with no stated params should be all guesses"
    assert plan.summary().endswith("guessed"), plan.summary()


@test
def test_the_prompt_carries_the_generator_catalogue():
    """The model cannot be expected to avoid inventing parts if it is not told what exists."""
    chat = canned(GOOD)
    PL.plan("anything", chat=chat)
    prompt = json.dumps(chat.state["calls"][0])
    for expected in ("ball_screw", "spur_gear", "extrusion_2020", "nut_travel", "align", "seat"):
        assert expected in prompt, f"{expected!r} missing from the prompt"
    assert "unsupported" in prompt, "the model must be told how to decline"


# -------------------------------------------------------------------------------- declines

@test
def test_a_decline_passes_through_and_builds_nothing():
    reply = json.dumps({"unsupported": "no ball-screw thread form is modelled",
                        "nearest": "a plain 16 mm shaft"})
    plan = PL.plan("a ground C5 preloaded screw", chat=canned(reply))
    assert plan.declined is True
    assert "thread form" in plan.reason
    assert plan.nearest == "a plain 16 mm shaft"
    assert plan.parts == [] and plan.mates == []


@test
def test_a_decline_is_never_retried():
    """Asking again after an honest refusal is how a refusal gets talked out of."""
    reply = json.dumps({"unsupported": "outside the vocabulary"})
    chat = canned(reply)
    PL.plan("something impossible", chat=chat)
    assert len(chat.state["calls"]) == 1, "a decline must not trigger a repair attempt"


# -------------------------------------------------------------------- validation and repair

@test
def test_an_unknown_generator_is_refused_and_repaired_once():
    bad = json.dumps({"parts": [{"name": "X", "generator": "flux_capacitor", "params": {}}],
                      "mates": []})
    chat = canned(bad, GOOD)
    plan = PL.plan("a screw", chat=chat)
    assert not plan.declined
    assert len(chat.state["calls"]) == 2, "exactly one repair attempt"
    repair = json.dumps(chat.state["calls"][1])
    assert "flux_capacitor" in repair, "the repair prompt must quote the error"


@test
def test_two_bad_programs_raise_with_both_errors_rather_than_a_third_try():
    bad = json.dumps({"parts": [{"name": "X", "generator": "nope", "params": {}}], "mates": []})
    try:
        PL.plan("a screw", chat=canned(bad, bad))
    except PL.PlanError as exc:
        assert "nope" in str(exc), str(exc)
        assert "twice" in str(exc).lower() or "two" in str(exc).lower(), str(exc)
        return
    raise AssertionError("two invalid programs must raise, not loop")


@test
def test_an_unknown_parameter_is_refused_and_names_the_real_ones():
    bad = json.dumps({"parts": [{"name": "S", "generator": "spacer",
                                 "params": {"diameter": {"value": 10}}}], "mates": []})
    try:
        PL.plan("a spacer", chat=canned(bad, bad))
    except PL.PlanError as exc:
        assert "diameter" in str(exc) and "od" in str(exc), str(exc)
        return
    raise AssertionError("an unknown parameter must be refused")


@test
def test_an_unknown_frame_is_refused_against_the_real_geometry():
    """Frames are checked by BUILDING the proposed part, not against a hardcoded list."""
    bad = json.dumps({
        "parts": [{"name": "A", "generator": "plate", "params": {}},
                  {"name": "B", "generator": "spacer", "params": {}}],
        "mates": [{"kind": "seat", "target": "A", "target_frame": "ceiling",
                   "moving": "B", "moving_frame": "bottom"}]})
    try:
        PL.plan("stack them", chat=canned(bad, bad))
    except PL.PlanError as exc:
        assert "ceiling" in str(exc) and "top" in str(exc), str(exc)
        return
    raise AssertionError("an unknown frame must be refused and the real ones listed")


@test
def test_malformed_json_gets_one_repair_attempt():
    chat = canned("here is your part!  {not json", GOOD)
    plan = PL.plan("a screw", chat=chat)
    assert not plan.declined
    assert len(chat.state["calls"]) == 2


@test
def test_json_wrapped_in_a_code_fence_is_accepted():
    """Models fence their JSON constantly; refusing over punctuation would be pedantic."""
    plan = PL.plan("a screw", chat=canned("```json\n" + GOOD + "\n```"))
    assert not plan.declined and len(plan.parts) == 2


@test
def test_a_mate_onto_an_unplaced_part_is_refused_before_anything_is_built():
    bad = json.dumps({
        "parts": [{"name": "A", "generator": "plate", "params": {}},
                  {"name": "B", "generator": "spacer", "params": {}}],
        "mates": [{"kind": "seat", "target": "B", "target_frame": "top",
                   "moving": "A", "moving_frame": "bottom"}]})
    try:
        PL.plan("stack them", chat=canned(bad, bad))
    except PL.PlanError as exc:
        assert "B" in str(exc), str(exc)
        return
    raise AssertionError("mate order must be checked before the document is touched")


# ------------------------------------------------------------------------------- building

@test
def test_building_a_plan_creates_the_parts_and_applies_the_mates():
    doc = new_doc()
    plan = PL.plan("a 500mm sfu1605 ball screw assembly", chat=canned(GOOD))
    made = PL.build(plan)
    assert [o.Name for o in made] == ["Screw", "Nut"], [o.Name for o in made]
    doc.recompute()
    screw = doc.getObject("Screw")
    assert abs(screw.Length - 500.0) < 1e-9, screw.Length
    assert abs(screw.Lead - 5.0) < 1e-9
    # The nut was aligned onto the screw's mid-travel frame, so the NUT'S CENTRE sits at z=250.
    # Checked on the bounding box, not on Placement.Base: the nut's bore frame is at its
    # mid-length, so its placement origin is correctly 24 mm lower, at 226. And not on
    # CenterOfMass either — the flange is at one end, so the centroid is deliberately off-centre.
    nut = doc.getObject("Nut")
    bb = nut.Shape.BoundBox
    assert abs((bb.ZMin + bb.ZMax) / 2.0 - 250.0) < 1e-6, (bb.ZMin, bb.ZMax)
    assert abs(nut.Placement.Base.z - 226.0) < 1e-6, nut.Placement.Base
    assert D.paramset_of(screw)["length"].provenance == "stated"
    assert D.paramset_of(screw)["end_d"].provenance == "default"


@test
def test_building_a_declined_plan_raises_instead_of_building_nothing_quietly():
    new_doc()
    plan = PL.plan("impossible", chat=canned(json.dumps({"unsupported": "no"})))
    try:
        PL.build(plan)
    except PL.PlanError as exc:
        assert "declined" in str(exc).lower(), str(exc)
        return
    raise AssertionError("building a decline must raise")


@test
def test_the_plan_renders_as_a_readable_program():
    """What the human reviews before pressing Build; a plan it cannot show cannot be trusted."""
    plan = PL.plan("a 500mm sfu1605 ball screw assembly", chat=canned(GOOD))
    text = plan.render()
    assert "ball_screw" in text and "500" in text
    assert "align" in text and "nut_travel" in text
    for marker in ("stated", "guessed"):
        assert marker in text.lower(), f"{marker} not visible in the rendered program"


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
