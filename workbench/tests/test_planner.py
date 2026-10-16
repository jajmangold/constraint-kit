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


@test
def test_a_bare_yes_is_recognised_and_a_yes_with_instructions_is_not():
    """The distinction that keeps an accepted offer honest.

    "do it" means *build what you offered*. "do it but make it 300 mm" is a different request,
    and silently replacing it with the offer would throw away the only new information in it.
    """
    for yes in ("do it", "Do it.", "yes", "yes please", "OK do it", "go ahead", "build that",
                "sure", "y", "do it build that", "  yep  ", "please do", "do it then",
                "okay, build it"):
        assert PL.is_affirmative(yes), yes
    for no in ("do it with 300mm", "yes but make it longer", "build a plate", "no",
               "do not build that", "", "   ", "what can you build?", "build it 300 long",
               "yes, and add a nut"):
        assert not PL.is_affirmative(no), no


@test
def test_do_it_after_a_declined_offer_plans_THE_OFFER():
    """The exchange this whole mechanism exists for.

    Recorded from a real session: the planner declined a linear-rail request, offered a nearest
    thing it could build, the user answered "do it build that", and the planner -- having no
    memory of its own offer -- declined again saying the request contained no part. An offer you
    cannot accept is not an offer.
    """
    prior = PL.Prior(request="a 200mm linear rail on a c beam",
                     declined=True, reason="no C-beam section existed",
                     nearest="a 200 mm c_beam with a 1605 ball screw down its channel")
    session = PL.PlanSession("do it build that", prior=prior)
    assert session.accepted_offer is True
    assert session.request == prior.nearest, session.request
    assert session.asked == "do it build that", session.asked
    # and the model is told, in the prompt, not to decline its own offer a second time
    blob = " ".join(m["content"] for m in session.messages)
    assert prior.nearest in blob, "the offer must reach the model"
    assert "accepting that offer" in blob.lower(), blob[:400]
    assert "do not decline it again" in blob.lower(), blob[:400]


@test
def test_a_bare_yes_with_NOTHING_offered_is_refused_without_a_network_call():
    """The other half of the original failure: "do it" with no conversation behind it.

    It used to cost a model round trip and come back as a confusing decline about there being
    no part in the request. There is nothing to interpret here, so it is answered immediately
    and says what to do instead.
    """
    for prior in (None, PL.Prior(), PL.Prior(request="a plate", declined=True,
                                             reason="something", nearest="")):
        try:
            PL.PlanSession("do it", prior=prior)
        except PL.PlanError as exc:
            assert "nothing to agree to" in str(exc), str(exc)
            continue
        raise AssertionError("a yes with no offer behind it must be refused: %r" % (prior,))


@test
def test_a_yes_after_a_SUCCESSFUL_plan_says_so_rather_than_replanning():
    prior = PL.Prior(request="a plate", declined=False, program="Plate  =  plate")
    try:
        PL.PlanSession("yes", prior=prior)
    except PL.PlanError as exc:
        assert "already built" in str(exc) or "Build button" in str(exc), str(exc)
        return
    raise AssertionError("a yes after a built plan has no offer to accept")


@test
def test_a_follow_up_carries_the_previous_PROGRAM_as_context():
    """"Make it longer" has to know what "it" is."""
    prior = PL.Prior(request="a 60 mm plate", declined=False,
                     program="Plate  =  plate\n      stated   width = 60.0")
    session = PL.PlanSession("make it 120 wide instead", prior=prior)
    assert session.accepted_offer is False
    assert session.request == "make it 120 wide instead", session.request
    blob = " ".join(m["content"] for m in session.messages)
    assert "a 60 mm plate" in blob, "the earlier request must reach the model"
    assert "stated   width = 60.0" in blob, "the earlier program must reach the model"
    assert "may be a change to that" in blob.lower(), blob[:400]


@test
def test_a_follow_up_carries_a_previous_DECLINE_as_context():
    prior = PL.Prior(request="a linear rail", declined=True,
                     reason="there is no profile rail in the catalogue",
                     nearest="a shaft in a bearing block")
    session = PL.PlanSession("then use a plain round rod instead", prior=prior)
    assert session.accepted_offer is False
    blob = " ".join(m["content"] for m in session.messages)
    assert "no profile rail in the catalogue" in blob, blob[:300]
    assert "a shaft in a bearing block" in blob, blob[:300]


@test
def test_with_no_prior_the_planner_is_exactly_as_stateless_as_before():
    """The command and agent surfaces pass no prior, and must be unchanged by all of this."""
    plain = PL.PlanSession("a plate with four bolt holes")
    assert plain.accepted_offer is False
    assert plain.prior is None
    assert len(plain.messages) == 2, [m["role"] for m in plain.messages]
    assert plain.messages[0]["role"] == "system"
    blob = " ".join(m["content"] for m in plain.messages)
    assert "EARLIER IN THIS CONVERSATION" not in blob


@test
def test_prior_of_turns_a_finished_plan_into_the_next_turns_context():
    declined = PL.Plan(request="a rail", declined=True, reason="no rail",
                       nearest="a shaft", raw="{}")
    p = PL.prior_of(declined)
    assert p.declined and p.has_offer and p.nearest == "a shaft", p
    assert p.request == "a rail", p

    ok = PL.Plan(request="a plate",
                 parts=[{"name": "Plate", "generator": "plate", "params": {}}], raw="{}")
    q = PL.prior_of(ok)
    assert not q.declined and not q.has_offer, q
    assert "plate" in q.program, q.program


@test
def test_plan_threads_a_prior_through_to_the_model():
    """End to end above the network: an accepted offer reaches the real plan() path."""
    seen = {}

    def fake_chat(messages, **_kw):
        seen["messages"] = messages
        return json.dumps({"parts": [{"name": "Beam", "generator": "c_beam",
                                      "params": {"length": {"value": 200.0,
                                                            "from_request": True}}}],
                           "mates": [], "notes": "the offer, built"})

    prior = PL.Prior(request="a 200mm linear rail on a c beam", declined=True,
                     reason="no C-beam section existed",
                     nearest="a 200 mm c_beam on its own")
    result = PL.plan("do it", prior=prior, chat=fake_chat)
    assert not result.declined, result.reason
    assert result.parts[0]["generator"] == "c_beam", result.parts
    blob = " ".join(m["content"] for m in seen["messages"])
    assert "a 200 mm c_beam on its own" in blob, blob[:300]


@test
def test_an_offset_mate_with_no_offset_is_refused_and_repaired():
    """The typo worth catching: without a displacement an offset IS an align.

    So a program could claim to put two rods either side of a screw and then stack all three on
    one axis -- which is precisely the failure the offset mate was added to fix, reappearing as
    a missing key. It is caught by validation rather than discovered in the viewport.
    """
    bad = json.dumps({
        "parts": [{"name": "Beam", "generator": "c_beam", "params": {}},
                  {"name": "Rod", "generator": "shaft", "params": {}}],
        "mates": [{"kind": "offset", "target": "Beam", "target_frame": "axis",
                   "moving": "Rod", "moving_frame": "axis"}],
        "notes": ""})
    good = json.dumps({
        "parts": [{"name": "Beam", "generator": "c_beam", "params": {}},
                  {"name": "Rod", "generator": "shaft", "params": {}}],
        "mates": [{"kind": "offset", "target": "Beam", "target_frame": "axis",
                   "moving": "Rod", "moving_frame": "axis",
                   "offset": {"dx": 30.0, "dy": 30.0}}],
        "notes": ""})
    replies = iter([bad, good])
    result = PL.plan("a rod beside a beam", chat=lambda *_a, **_k: next(replies))
    assert not result.declined, result.reason
    assert result.attempts == 2, result.attempts
    assert result.mates[0]["offset"] == {"dx": 30.0, "dy": 30.0}, result.mates


@test
def test_an_all_zero_offset_is_refused_as_a_mislabelled_align():
    bad = json.dumps({
        "parts": [{"name": "Beam", "generator": "c_beam", "params": {}},
                  {"name": "Rod", "generator": "shaft", "params": {}}],
        "mates": [{"kind": "offset", "target": "Beam", "target_frame": "axis",
                   "moving": "Rod", "moving_frame": "axis",
                   "offset": {"dx": 0.0, "dy": 0.0, "dz": 0.0}}],
        "notes": ""})
    try:
        PL.plan("a rod on a beam", chat=lambda *_a, **_k: bad)
    except PL.PlanError as exc:
        assert "all zeros" in str(exc) and "align" in str(exc), str(exc)
        return
    raise AssertionError("an all-zero offset must be refused")


@test
def test_a_seat_or_align_carrying_an_offset_is_refused_rather_than_ignored():
    """Silently dropping it would place the part somewhere the program does not describe."""
    for kind in ("seat", "align"):
        bad = json.dumps({
            "parts": [{"name": "Beam", "generator": "c_beam", "params": {}},
                      {"name": "Rod", "generator": "shaft", "params": {}}],
            "mates": [{"kind": kind, "target": "Beam", "target_frame": "axis",
                       "moving": "Rod", "moving_frame": "axis",
                       "offset": {"dx": 30.0}}],
            "notes": ""})
        try:
            PL.plan("a rod on a beam", chat=lambda *_a, **_k: bad)
        except PL.PlanError as exc:
            assert "takes no offset" in str(exc), (kind, str(exc))
            continue
        raise AssertionError("%s must refuse an offset" % kind)


@test
def test_the_rendered_program_SHOWS_the_displacement():
    """A reviewer who cannot see the offset cannot review the assembly."""
    plan = PL.Plan(
        request="rods beside a screw",
        parts=[{"name": "Beam", "generator": "c_beam", "params": {}},
               {"name": "Rod", "generator": "shaft", "params": {}}],
        mates=[{"kind": "offset", "target": "Beam", "target_frame": "axis",
                "moving": "Rod", "moving_frame": "axis",
                "offset": {"dx": 30.0, "dy": 28.0}}],
        raw="{}")
    text = plan.render()
    assert "offset" in text and "Beam.axis" in text and "Rod.axis" in text, text
    assert "dx 30" in text and "dy 28" in text, text


@test
def test_the_declined_rail_request_can_now_be_PLANNED_AND_BUILT_end_to_end():
    """The transcript's request, all the way through to geometry in a document.

    A 200 mm C-beam, a 1605 ball screw down its channel on the channel centreline, and a 16 mm
    rod either side on the flat face. Every one of those was impossible before: there was no
    C-beam section, and no mate that could put anything beside anything.
    """
    program = json.dumps({
        "parts": [
            {"name": "Beam", "generator": "c_beam",
             "params": {"length": {"value": 200.0, "from_request": True}}},
            {"name": "Screw", "generator": "ball_screw",
             "params": {"nominal_d": {"value": 16.0, "from_request": True},
                        "lead": {"value": 5.0, "from_request": True},
                        "length": {"value": 200.0, "from_request": True}}},
            {"name": "RodA", "generator": "shaft",
             "params": {"diameter": {"value": 16.0, "from_request": True},
                        "length": {"value": 200.0, "from_request": True}}},
            {"name": "RodB", "generator": "shaft",
             "params": {"diameter": {"value": 16.0, "from_request": True},
                        "length": {"value": 200.0, "from_request": True}}}],
        "mates": [
            {"kind": "offset", "target": "Beam", "target_frame": "channel",
             "moving": "Screw", "moving_frame": "axis", "offset": {"dy": -10.0}},
            {"kind": "offset", "target": "Beam", "target_frame": "axis",
             "moving": "RodA", "moving_frame": "axis",
             "offset": {"dx": 30.0, "dy": 30.0}},
            {"kind": "offset", "target": "Beam", "target_frame": "axis",
             "moving": "RodB", "moving_frame": "axis",
             "offset": {"dx": -30.0, "dy": 30.0}}],
        "notes": "the screw sits on the channel centreline, not its floor"})

    result = PL.plan("a 200mm linear rail with 1605 ball screw and 16mm stainless linear rods, "
                     "on a c beam aluminium extrusion",
                     chat=lambda *_a, **_k: program)
    assert not result.declined, result.reason
    assert len(result.mates) == 3, result.mates

    doc = new_doc()
    made = PL.build(result, doc)
    assert len(made) == 4, [o.Name for o in made]
    by_name = {o.Name: o for o in made}

    # the displacements really reached the geometry
    assert abs(by_name["RodA"].Placement.Base.x - 30.0) < 1e-9, by_name["RodA"].Placement
    assert abs(by_name["RodB"].Placement.Base.x + 30.0) < 1e-9, by_name["RodB"].Placement
    assert abs(by_name["Screw"].Placement.Base.x) < 1e-9, by_name["Screw"].Placement

    # and nothing interferes, which is a different claim from "it placed them"
    for a, b in (("RodA", "RodB"), ("RodA", "Screw"), ("RodB", "Screw"), ("Screw", "Beam")):
        shared = by_name[a].Shape.common(by_name[b].Shape).Volume
        assert shared < 1e-6, (a, b, shared)


@test
def test_a_structurally_PERFECT_program_that_does_not_FIT_is_refused():
    """The measured failure this check exists for, reproduced from a live model reply.

    Asked for a rail with a ball screw and two 16 mm rods in a C-beam channel, the real planner
    produced a program in which every generator was real, every frame was real, every mate was
    in order -- and both rods were driven straight through the channel wall, 20,106 mm3 each.
    Validation checked the FORM of the program and never its fit, so the first anyone knew was
    looking at the result.

    A 40 mm channel with a rod centred on its edge puts half the rod in the wall, which is an
    easy thing to get wrong and an easy thing to check.
    """
    bad = json.dumps({
        "parts": [{"name": "Beam", "generator": "c_beam",
                   "params": {"length": {"value": 200.0, "from_request": True}}},
                  {"name": "RodA", "generator": "shaft",
                   "params": {"diameter": {"value": 16.0, "from_request": True},
                              "length": {"value": 200.0, "from_request": True}}}],
        "mates": [{"kind": "offset", "target": "Beam", "target_frame": "axis",
                   "moving": "RodA", "moving_frame": "axis",
                   "offset": {"dx": 20.0, "dy": -8.0, "dz": 100.0}}],
        "notes": "down the channel"})
    try:
        PL.plan("rods in a c beam channel", chat=lambda *_a, **_k: bad)
    except PL.PlanError as exc:
        text = str(exc)
        assert "occupy the same space" in text, text
        assert "Beam" in text and "RodA" in text, text
        assert "mm3" in text, ("say how much, so the repair can be sized", text)
        return
    raise AssertionError("a program whose parts overlap must not be accepted")


@test
def test_a_part_that_only_TOUCHES_another_one_is_perfectly_legal():
    """Volume, not distance, and the distinction is load-bearing.

    A seated face touches. A gear pair at its exact centre distance touches. Both are correct
    assemblies, and a check that measured distance instead of shared volume would reject every
    one of them -- so the tolerance is on overlapping MATERIAL.
    """
    seated = json.dumps({
        "parts": [{"name": "Plate", "generator": "plate", "params": {}},
                  {"name": "Spacer", "generator": "spacer", "params": {}}],
        "mates": [{"kind": "seat", "target": "Plate", "target_frame": "top",
                   "moving": "Spacer", "moving_frame": "bottom"}],
        "notes": ""})
    result = PL.plan("a spacer on a plate", chat=lambda *_a, **_k: seated)
    assert not result.declined, result.reason
    assert result.attempts == 1, result.attempts


@test
def test_the_fit_check_repairs_a_near_miss_in_one_more_try():
    """What the loop is for: tell the model what overlapped and let it move the part."""
    through_the_wall = json.dumps({
        "parts": [{"name": "Beam", "generator": "c_beam", "params": {}},
                  {"name": "Rod", "generator": "shaft",
                   "params": {"diameter": {"value": 16.0, "from_request": True}}}],
        "mates": [{"kind": "offset", "target": "Beam", "target_frame": "axis",
                   "moving": "Rod", "moving_frame": "axis", "offset": {"dx": 20.0}}],
        "notes": ""})
    clear_of_it = json.dumps({
        "parts": [{"name": "Beam", "generator": "c_beam", "params": {}},
                  {"name": "Rod", "generator": "shaft",
                   "params": {"diameter": {"value": 16.0, "from_request": True}}}],
        "mates": [{"kind": "offset", "target": "Beam", "target_frame": "axis",
                   "moving": "Rod", "moving_frame": "axis",
                   "offset": {"dx": 20.0, "dy": 30.0}}],
        "notes": "lifted clear of the beam"})
    replies = iter([through_the_wall, clear_of_it])
    result = PL.plan("a rod beside a beam", chat=lambda *_a, **_k: next(replies))
    assert not result.declined, result.reason
    assert result.attempts == 2, result.attempts
    assert result.mates[0]["offset"]["dy"] == 30.0, result.mates

    # and the repair message named the overlap, which is what made the fix possible
    assert any("occupy the same space" in p for p in result.problems), result.problems


@test
def test_a_program_with_no_mates_is_not_fit_checked_into_nonsense():
    """One part cannot overlap anything, and two unmated parts are a different error already."""
    one = json.dumps({
        "parts": [{"name": "Plate", "generator": "plate", "params": {}}],
        "mates": [], "notes": ""})
    result = PL.plan("a plate", chat=lambda *_a, **_k: one)
    assert not result.declined, result.reason
    assert result.attempts == 1, result.attempts


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
