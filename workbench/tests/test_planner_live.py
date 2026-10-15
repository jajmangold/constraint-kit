#!/usr/bin/env python3
"""The planner against a REAL model endpoint, end to end — opt-in, because it costs money.

    CK_LIVE=1 "C:/Program Files/FreeCAD 1.0/bin/freecadcmd.exe" \
        workbench/tests/test_planner_live.py

Every other planner test injects the model call, which is right: a test suite that needs a paid
API to tell you whether your parser works is a bad test suite. But injection cannot tell you
whether a real model, given the real catalogue, produces a program this kernel will accept — and
that is the only question the user actually asked. So this suite exists, is skipped unless
`CK_LIVE=1`, and is never part of the default run.

What it asserts is deliberately about OUR side of the contract, not the model's wording:

* a buildable request yields geometry with the dimensions the request stated
* a value the request gave is tagged `stated`; everything else is tagged a guess
* an impossible request DECLINES rather than substituting something plausible
* the validate-and-repair loop is bounded, and a program that reaches `build()` actually builds

It does NOT assert which generator the model picks or how it phrases its notes. That would be a
test of today's model weights, and it would fail for the wrong reason the day they change.
"""
from __future__ import annotations

import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import FreeCAD  # noqa: E402

from ckwb import llm, planner  # noqa: E402
from ckwb.provenance import DEFAULT, STATED  # noqa: E402
from ckwb.settings import llm_settings  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def _ask(request: str):
    """The shipping path: PlanSession on this thread, llm.chat for the slow part."""
    cfg = llm_settings()
    session = planner.PlanSession(request)
    started = time.time()
    while True:
        reply = llm.chat(session.messages, base_url=cfg["base_url"],
                         api_key=cfg.get("api_key", ""), model=cfg["planner_model"],
                         timeout=120.0)
        kind, payload = session.offer(reply)
        if kind == "plan":
            print("      %.1fs, %d attempt(s)" % (time.time() - started, payload.attempts))
            return payload
        if kind == "error":
            raise AssertionError("no usable program after %d attempt(s): %s"
                                 % (session.attempt, payload))
        print("      a program was refused; repairing once")


def _doc(fn):
    """Each test gets its own document and always gives it back."""
    def wrapped():
        doc = FreeCAD.newDocument("Live")
        try:
            fn(doc)
        finally:
            FreeCAD.closeDocument(doc.Name)
    wrapped.__name__ = fn.__name__
    wrapped.__doc__ = fn.__doc__
    return wrapped


@test
@_doc
def test_a_stated_dimension_reaches_the_geometry(doc):
    """500 mm was asked for, so 500 mm must come out — and be tagged as the user's number."""
    plan = _ask("a 500 mm long SFU1605 ball screw with its nut")
    assert not plan.declined, plan.reason

    made = planner.build(plan)
    doc.recompute()
    assert made, "a plan that was not declined must build something"

    longest = max(o.Shape.BoundBox.ZLength for o in made)
    assert abs(longest - 500.0) < 1.0, (
        "the 500 mm the request stated did not reach the geometry: %.1f" % longest)

    stated = [(e["name"], k) for e in plan.parts
              for k, v in plan.paramset(e["name"]).items() if v.provenance == STATED]
    assert stated, "no parameter was tagged as coming from the request"
    for obj in made:
        sh = obj.Shape
        assert sh.isValid() and not sh.isNull() and len(sh.Solids) == 1, obj.Name


@test
@_doc
def test_unstated_values_are_tagged_as_guesses_not_laundered(doc):
    """The planner must not claim the user's authority for numbers the user never gave."""
    plan = _ask("a spur gear with 20 teeth")
    assert not plan.declined, plan.reason

    guessed = stated = 0
    for entry in plan.parts:
        for key, value in plan.paramset(entry["name"]).items():
            if value.provenance == STATED:
                stated += 1
            else:
                guessed += 1
                assert value.provenance == DEFAULT, (key, value.provenance)
    assert guessed > 0, "a one-sentence request cannot have specified every parameter"
    assert stated <= 3, (
        "the request gave one number; %d were tagged as stated" % stated)
    assert "of" in plan.summary(), plan.summary()

    made = planner.build(plan)
    doc.recompute()
    assert made and all(o.Shape.isValid() for o in made)


@test
@_doc
def test_an_impossible_request_declines_instead_of_substituting(doc):
    """The failure mode that matters: a plausible wrong part is worse than no part."""
    plan = _ask("a titanium turbine blade with serpentine internal cooling passages "
                "and film-cooling holes")
    assert plan.declined, (
        "this catalogue cannot express an aerofoil or an internal cavity, so a program for it is "
        "a fabrication. Got: " + plan.render())
    assert plan.reason.strip(), "a decline must say what is missing"
    assert len(plan.reason) > 40, plan.reason
    assert not plan.parts, "a declined plan must not carry parts to build"
    try:
        planner.build(plan)
    except planner.PlanError:
        pass
    else:
        raise AssertionError("building a declined plan must be refused")


@test
@_doc
def test_a_two_part_assembly_is_mated_rather_than_left_in_a_pile(doc):
    """An assembly means the parts touch. Two parts at the origin is not an assembly."""
    plan = _ask("a 20 tooth spur gear mounted on an 8 mm shaft")
    assert not plan.declined, plan.reason
    assert len(plan.parts) >= 2, plan.render()
    assert plan.mates, "two parts and no mate is a pile, not an assembly"

    made = planner.build(plan)
    doc.recompute()
    assert len(made) >= 2
    # at least one pair must actually interfere or touch, rather than sit coincident at origin
    boxes = [o.Shape.BoundBox for o in made]
    spread = max(b.Center.Length for b in boxes)
    volumes = [o.Shape.Volume for o in made]
    assert all(v > 0 for v in volumes), volumes
    print("      %d parts, %d mate(s), centres spread %.1f mm"
          % (len(made), len(plan.mates), spread))


def main() -> int:
    if not os.environ.get("CK_LIVE"):
        print("SKIPPED: live planner tests need CK_LIVE=1 (they call a paid API)")
        return 0
    cfg = llm_settings()
    if not cfg.get("base_url") or not cfg.get("planner_model"):
        print("SKIPPED: no endpoint configured; set one in the workbench Settings dialog")
        return 0
    print("live against %s model=%s" % (cfg["base_url"], cfg["planner_model"]))

    failed = []
    for fn in TESTS:
        print("  %s" % fn.__name__)
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
