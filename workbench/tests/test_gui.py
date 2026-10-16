# -*- coding: utf-8 -*-
"""The Qt layer, driven inside a real FreeCAD GUI session.

Launched by `run_gui_tests.py`, which starts FreeCAD, runs this, and reports. Not by freecadcmd:
every assertion here needs a live `FreeCADGui`, a main window and a Qt event loop.

This suite exists because the untestable layer is where the real bugs were. Two shipped in one
day — `InitGui.py` raising on `__file__` so the workbench never appeared in the selector, and
nine missing icons — and both were invisible to 150 passing tests because `FreeCADGui` cannot be
imported headlessly. "Needs a human to click" was the problem, not an excuse for it.

The model call is replaced with a fake, so the Describe panel's worker-thread path is exercised
with no network: a plan is proposed, shown, approved and built, and the repair round trip is
driven too. The settings lookup is patched rather than written, so the user's own configuration
and API key are never touched.

Results are written to a file because FreeCAD's stdout is not reliably captured.
"""
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.dirname(HERE) not in sys.path:
    sys.path.insert(0, os.path.dirname(HERE))

import FreeCAD
import FreeCADGui
from PySide import QtCore, QtWidgets

OUT = os.environ.get("CK_GUI_RESULT") or os.path.join(HERE, "gui_result.txt")

TESTS = []
LOG = []


def test(fn):
    TESTS.append(fn)
    return fn


def pump(ms=60):
    """Let Qt deliver queued signals. Needed because there is no running event loop here."""
    deadline = time.time() + ms / 1000.0
    while time.time() < deadline:
        QtWidgets.QApplication.instance().processEvents()
        time.sleep(0.005)


def wait_for(predicate, timeout=20.0, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        QtWidgets.QApplication.instance().processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def main_window():
    return FreeCADGui.getMainWindow()


def new_doc(name="GuiTest"):
    for existing in list(FreeCAD.listDocuments()):
        FreeCAD.closeDocument(existing)
    doc = FreeCAD.newDocument(name)
    FreeCAD.setActiveDocument(name)
    return doc


def report_view_text():
    for w in main_window().findChildren(QtWidgets.QTextEdit):
        if w.objectName() == "Report view":
            return w.toPlainText()
    return ""


# ------------------------------------------------------------------- registration and chrome

@test
def test_the_workbench_registers_and_activates():
    """The bug that shipped: InitGui raised, so this was simply absent from the selector."""
    assert "ConstraintKitWorkbench" in FreeCADGui.listWorkbenches(), \
        sorted(FreeCADGui.listWorkbenches())
    FreeCADGui.activateWorkbench("ConstraintKitWorkbench")
    assert FreeCADGui.activeWorkbench().__class__.__name__ == "ConstraintKitWorkbench"


@test
def test_the_toolbar_has_eight_buttons_and_every_icon_resolves():
    """The other bug that shipped: nine named pixmaps with no files, nine console warnings."""
    from ckwb.gui import spec

    bars = [b for b in main_window().findChildren(QtWidgets.QToolBar)
            if b.windowTitle() == spec.TOOLBAR_NAME]
    assert bars, f"no {spec.TOOLBAR_NAME!r} toolbar"
    actions = [a for a in bars[0].actions() if not a.isSeparator()]
    assert len(actions) == 8, [a.text() for a in actions]
    blank = [a.text() for a in actions if a.icon().isNull()]
    assert not blank, f"buttons with no icon: {blank}"


@test
def test_the_menu_is_present():
    from ckwb.gui import spec

    titles = [m.title() for m in main_window().menuBar().findChildren(QtWidgets.QMenu)]
    assert spec.MENU in titles, titles


@test
def test_actions_are_disabled_when_there_is_nothing_to_act_on():
    """Greying out honestly beats eight live buttons that all error."""
    from ckwb.gui import actions, spec

    for existing in list(FreeCAD.listDocuments()):
        FreeCAD.closeDocument(existing)
    pump()
    bars = [b for b in main_window().findChildren(QtWidgets.QToolBar)
            if b.windowTitle() == spec.TOOLBAR_NAME]
    by_text = {a.text(): a for a in bars[0].actions() if not a.isSeparator()}
    # Describe and Settings work with no document; the rest have nothing to do.
    assert actions.DescribeCommand().IsActive() is True
    assert actions.SettingsCommand().IsActive() is True
    assert actions.InsertCommand().IsActive() is False
    assert actions.CheckCommand().IsActive() is False


# ----------------------------------------------------------------------------- checks panel

@test
def test_the_checks_panel_shows_not_run_then_results_then_stale():
    """Staleness is the panel's whole claim; a green row must not outlive the edit."""
    from ckwb.gui import panels
    from ckwb.registry import registry

    doc = new_doc()
    panel = panels.show_panel("CK_ChecksPanel").widget()
    panel.refresh()
    pump()
    assert "no checks" in panel.summary.text().lower(), panel.summary.text()

    registry.call("insert_part", generator="plate", name="A")
    panel.run()
    pump()
    assert panel.tree.topLevelItemCount() >= 3, panel.tree.topLevelItemCount()
    rows = {panel.tree.topLevelItem(i).text(0): panel.tree.topLevelItem(i).text(1)
            for i in range(panel.tree.topLevelItemCount())}
    assert rows.get("interference") == "pass", rows
    assert "passes" in panel.summary.text().lower(), panel.summary.text()

    registry.call("set_param", part="A", name="thickness", value=20.0)
    panel.refresh()
    pump()
    rows = {panel.tree.topLevelItem(i).text(0): panel.tree.topLevelItem(i).text(1)
            for i in range(panel.tree.topLevelItemCount())}
    assert set(rows.values()) == {"stale"}, rows
    assert "changed" in panel.summary.text().lower(), panel.summary.text()


@test
def test_the_checks_panel_shows_a_clash_as_a_child_row():
    from ckwb.gui import panels
    from ckwb.registry import registry

    doc = new_doc()
    registry.call("insert_part", generator="plate", name="A")
    registry.call("insert_part", generator="plate", name="B")
    doc.getObject("B").Placement = FreeCAD.Placement(
        FreeCAD.Vector(0, 0, 4), FreeCAD.Rotation())
    doc.recompute()

    panel = panels.show_panel("CK_ChecksPanel").widget()
    panel.run()
    pump()
    found = None
    for i in range(panel.tree.topLevelItemCount()):
        item = panel.tree.topLevelItem(i)
        if item.text(0) == "interference":
            found = item
    assert found is not None and found.text(1) == "FAIL", found and found.text(1)
    assert found.childCount() == 1, found.childCount()
    assert "A" in found.child(0).text(2) and "B" in found.child(0).text(2)


# ------------------------------------------------------------------------- provenance panel

@test
def test_the_provenance_panel_groups_by_origin_and_can_accept_a_guess():
    from ckwb.gui import panels
    from ckwb.registry import registry

    new_doc()
    registry.call("insert_part", generator="spacer", name="Sp")
    FreeCADGui.Selection.clearSelection()
    FreeCADGui.Selection.addSelection(FreeCAD.ActiveDocument.getObject("Sp"))
    pump()

    panel = panels.show_panel("CK_ProvenancePanel").widget()
    panel.refresh()
    pump()
    assert "Sp" in panel.header.text(), panel.header.text()
    assert "guessed" in panel.header.text(), panel.header.text()
    groups = [panel.tree.topLevelItem(i).text(0)
              for i in range(panel.tree.topLevelItemCount())]
    assert "Guessed for you" in groups, groups

    guessed = next(panel.tree.topLevelItem(i)
                   for i in range(panel.tree.topLevelItemCount())
                   if panel.tree.topLevelItem(i).text(0) == "Guessed for you")
    before = guessed.childCount()
    assert before >= 3, before
    panel.tree.setCurrentItem(guessed.child(0))
    panel.accept_selected()
    pump()
    assert "0 of" in panel.header.text() or str(before - 1) in panel.header.text(), \
        panel.header.text()


# ---------------------------------------------------------------------------- describe panel

class FakeChat:
    """Stands in for ckwb.llm.chat. Records calls; returns canned replies in order."""

    def __init__(self, *replies, delay=0.0):
        self.replies = list(replies)
        self.calls = []
        self.delay = delay

    def __call__(self, messages, **kw):
        self.calls.append(messages)
        if self.delay:
            time.sleep(self.delay)
        if not self.replies:
            raise AssertionError("the panel asked the model more often than expected")
        return self.replies.pop(0)


GOOD_PLAN = """{"parts": [
    {"name": "Screw", "generator": "ball_screw",
     "params": {"length": {"value": 500, "from_request": true},
                "lead": {"value": 5, "from_request": true}}},
    {"name": "Nut", "generator": "ball_nut", "params": {}}],
 "mates": [{"kind": "align", "target": "Screw", "target_frame": "nut_travel",
            "moving": "Nut", "moving_frame": "bore"}],
 "notes": "SFU1605. Thread groove not modelled."}"""


def describe_panel(cfg=None, chat=None):
    """The panel with its settings and model call replaced. Never touches the real config."""
    from ckwb import llm
    from ckwb.gui import panels

    panel = panels.show_panel("CK_DescribePanel").widget()
    panels.llm_settings = lambda: dict(
        cfg or {"base_url": "http://fake", "api_key": "", "planner_model": "fake-model",
                "vision_model": ""})
    if chat is not None:
        llm.chat = chat
    panel.refresh_model_label()
    return panel


@test
def test_describe_says_so_when_no_model_is_configured_and_starts_no_worker():
    new_doc()
    panel = describe_panel(cfg={"base_url": "", "api_key": "", "planner_model": "",
                                "vision_model": ""})
    panel.entry.setText("a 500mm sfu1605 ball screw assembly")
    panel.submit()
    pump()
    text = panel.transcript.toPlainText()
    assert "No model endpoint is configured" in text, text[-300:]
    assert panel._worker is None, "nothing should have been dispatched"
    assert panel.build_btn.isEnabled() is False


@test
def test_describe_plans_off_thread_shows_the_program_and_builds_on_approval():
    """The whole trust model, end to end: propose, show, approve, build.

    Also the thread fix: the plan happens on a worker, so this drives the signal path rather
    than the synchronous call that used to freeze the application.
    """
    doc = new_doc()
    fake = FakeChat(GOOD_PLAN)
    panel = describe_panel(chat=fake)
    panel.entry.setText("a 500mm sfu1605 ball screw assembly")
    panel.submit()

    wait_for(lambda: panel.build_btn.isEnabled(), 30.0, "the program to arrive")
    assert len(fake.calls) == 1, len(fake.calls)
    program = panel.program.toPlainText()
    assert panel.program.isVisible()
    assert "ball_screw" in program and "500" in program, program[:300]
    assert "stated" in program and "guessed" in program, program[:300]
    # nothing built yet: the program is shown first, on purpose
    assert not [o for o in doc.Objects if o.Name in ("Screw", "Nut")]

    panel.build()
    pump(200)
    names = [o.Name for o in doc.Objects]
    assert "Screw" in names and "Nut" in names, names
    assert abs(doc.getObject("Screw").Length - 500.0) < 1e-9
    assert panel.build_btn.isEnabled() is False, "Build must disarm once used"


@test
def test_describe_stays_responsive_while_the_model_is_slow():
    """The defect this suite was written for: the GUI froze for the length of the request."""
    new_doc()
    delay = 2.0
    fake = FakeChat(GOOD_PLAN, delay=delay)
    panel = describe_panel(chat=fake)
    panel.entry.setText("a plate")

    # The direct measurement: submit() must HAND OFF, not wait. If the request ran on this
    # thread, submit() could not return until the model did.
    #
    # An earlier version of this test counted event-loop spins after submit() returned, which
    # proved nothing — a blocking submit() has already finished blocking by then. Verified by
    # mutation: making the worker synchronous leaves the spin count untouched and trips this
    # assertion instead.
    t0 = time.time()
    panel.submit()
    handoff = time.time() - t0
    assert handoff < delay / 2.0, (
        f"submit() took {handoff:.2f}s with a {delay:.0f}s model; it is blocking the GUI thread")

    assert panel.cancel_btn.isVisible(), "a Stop waiting button should be offered while waiting"
    assert panel.entry.isEnabled() is False, "input should be disabled while waiting"

    # And the wait COUNTS. This is the other half of allowing a 180 s deadline: the number was
    # once cut to 60 s because a silent wait "looked exactly like a hung application", and that
    # broke real completions measured at 80 to 130 s. A deadline cannot do a progress
    # indicator's job, so the progress indicator has to exist.
    assert panel.waiting_label.isVisible(), "the wait must be visible, not silent"
    assert "waiting" in panel.waiting_label.text().lower(), panel.waiting_label.text()
    first = panel.waiting_label.text()
    deadline_t = time.time() + 2.5
    while time.time() < deadline_t and panel.waiting_label.text() == first:
        QtWidgets.QApplication.instance().processEvents()
        time.sleep(0.05)
    assert panel.waiting_label.text() != first, (
        "the counter must advance; a frozen number is no better than silence", first)

    # And the loop really is alive while the model is slow.
    spins = 0
    deadline = time.time() + delay / 2.0
    while time.time() < deadline:
        QtWidgets.QApplication.instance().processEvents()
        spins += 1
        time.sleep(0.005)
    assert spins > 20, f"the event loop only spun {spins} times while waiting"

    wait_for(lambda: panel.build_btn.isEnabled(), 30.0, "the slow plan to arrive")
    assert panel.cancel_btn.isVisible() is False


@test
def test_describe_repairs_once_and_says_that_it_did():
    bad = '{"parts": [{"name": "X", "generator": "flux_capacitor", "params": {}}], "mates": []}'
    new_doc()
    fake = FakeChat(bad, GOOD_PLAN)
    panel = describe_panel(chat=fake)
    panel.entry.setText("a screw")
    panel.submit()

    wait_for(lambda: panel.build_btn.isEnabled(), 40.0, "the repaired program")
    assert len(fake.calls) == 2, len(fake.calls)
    text = panel.transcript.toPlainText()
    assert "asking once more" in text.lower(), text[-400:]
    assert "repaired once" in text.lower(), text[-400:]
    repair_prompt = str(fake.calls[1])
    assert "flux_capacitor" in repair_prompt


@test
def test_describe_surfaces_a_decline_calmly_and_builds_nothing():
    doc = new_doc()
    fake = FakeChat('{"unsupported": "no ball-screw thread form is modelled",'
                    ' "nearest": "a plain 16 mm shaft"}')
    panel = describe_panel(chat=fake)
    panel.entry.setText("a ground C5 preloaded screw")
    panel.submit()

    wait_for(lambda: "will not build" in panel.transcript.toPlainText().lower(),
             30.0, "the decline")
    text = panel.transcript.toPlainText()
    assert "thread form" in text and "plain 16 mm shaft" in text, text[-400:]
    assert panel.build_btn.isEnabled() is False
    assert panel.program.isVisible() is False
    assert not doc.Objects, [o.Name for o in doc.Objects]


@test
def test_describe_reports_a_client_failure_as_a_sentence():
    from ckwb.llm import LLMError

    new_doc()

    def boom(messages, **kw):
        raise LLMError("the endpoint sent no response within 60s")

    panel = describe_panel(chat=boom)
    panel.entry.setText("a plate")
    panel.submit()
    wait_for(lambda: "did not work" in panel.transcript.toPlainText().lower(),
             30.0, "the failure message")
    assert "no response" in panel.transcript.toPlainText()
    assert panel.build_btn.isEnabled() is False


@test
def test_the_panel_ACCEPTS_the_offer_it_just_made():
    """The exchange this panel was failing at, end to end through the real Qt widgets.

    Recorded from a real session: the panel declined a linear-rail request, offered a nearest
    thing it could build, the user typed "do it build that" -- and it declined again with *"the
    request contains no part or assembly"*, because every submit built a fresh planner session
    with no memory of the turn before. An offer you cannot accept is not an offer.

    So: decline with an offer, then say yes, and check that what gets planned is the OFFER and
    that the panel says so rather than substituting it silently.
    """
    doc = new_doc()
    fake = FakeChat(
        '{"unsupported": "there is no profile rail with a recirculating carriage",'
        ' "nearest": "a 16 mm shaft 200 mm long in a bearing block"}',
        '{"parts": [{"name": "Rod", "generator": "shaft",'
        ' "params": {"diameter": {"value": 16.0, "from_request": true},'
        '            "length": {"value": 200.0, "from_request": true}}}],'
        ' "mates": [], "notes": "the offer, built"}')
    panel = describe_panel(chat=fake)

    panel.entry.setText("a 200mm linear rail on a c beam")
    panel.submit()
    wait_for(lambda: "will not build" in panel.transcript.toPlainText().lower(),
             30.0, "the decline")
    said = panel.transcript.toPlainText()
    assert "bearing block" in said, said[-400:]
    assert "do it" in said.lower(), ("the offer must say how to accept it", said[-400:])
    assert not doc.Objects, [o.Name for o in doc.Objects]

    panel.entry.setText("do it build that")
    panel.submit()
    wait_for(lambda: "taking that as yes" in panel.transcript.toPlainText().lower(),
             30.0, "the acceptance")
    wait_for(lambda: panel.build_btn.isEnabled(), 30.0, "the program")

    said = panel.transcript.toPlainText()
    # It must name what it is planning, because swapping the typed request for an earlier
    # offer is exactly the kind of helpfulness that must never happen invisibly.
    assert "16 mm shaft 200 mm long in a bearing block" in said, said[-500:]
    assert "shaft" in panel.program.toPlainText(), panel.program.toPlainText()

    panel.build()
    wait_for(lambda: bool(doc.Objects), 30.0, "the build")
    assert [o.Name for o in doc.Objects] == ["Rod"], [o.Name for o in doc.Objects]


@test
def test_a_bare_yes_with_nothing_offered_costs_no_model_call():
    """It used to spend a request and come back confusing. Now it answers at once."""
    new_doc()
    calls = []

    def counting(messages, **kw):
        calls.append(messages)
        return '{"unsupported": "nothing", "nearest": ""}'

    panel = describe_panel(chat=counting)
    # `show_panel` hands back the SAME panel every time, which is right for the application --
    # one panel per session is what makes it a conversation -- and means a test that needs a
    # fresh conversation has to say so. Without this the previous test's offer is still standing
    # and "do it" correctly accepts it.
    panel._prior = None
    panel.transcript.clear()

    panel.entry.setText("do it")
    panel.submit()
    text = panel.transcript.toPlainText().lower()
    assert "nothing to agree to" in text, text[-400:]
    assert calls == [], "a yes with no offer behind it must not reach the model"


# ------------------------------------------------------------------------------- settings

@test
def test_a_failed_request_LEAVES_YOUR_WORDS_IN_THE_BOX():
    """A hosted model that stalls costs a minute of waiting, and the failure is not the user's.

    Making them retype a sentence they already typed is adding insult to it. Recorded from a
    real session: a 60 s stall against DeepSeek wiped a long request out of the entry, and the
    endpoint answered normally seconds later.
    """
    from ckwb.llm import LLMError

    new_doc()
    asked = ("a 200mm linear rail with 1605 ball screw and 16mm stainless linear rods, "
             "on a c beam aluminum extrusion")

    def stall(messages, **kw):
        raise LLMError("accepted the connection but sent no response -- tried twice")

    panel = describe_panel(chat=stall)
    panel._prior = None
    panel.transcript.clear()
    panel.entry.setText(asked)
    panel.submit()
    wait_for(lambda: "did not work" in panel.transcript.toPlainText().lower(),
             30.0, "the failure message")

    assert panel.waiting_label.isVisible() is False, "the counter stops when the wait ends"
    assert panel.entry.text() == asked, (
        "the request must still be in the box, ready to send again", panel.entry.text())
    assert "tried twice" in panel.transcript.toPlainText(), panel.transcript.toPlainText()[-200:]
    assert panel.entry.isEnabled(), "and the box must be usable"
    assert panel.build_btn.isEnabled() is False


@test
def test_a_SUCCESSFUL_request_does_not_put_the_words_back():
    """Only a failure restores it; a success must leave the box clear for the next thing."""
    new_doc()
    panel = describe_panel(chat=FakeChat(GOOD_PLAN))
    panel._prior = None
    panel.transcript.clear()
    panel.entry.setText("a 500 mm ball screw with a nut")
    panel.submit()
    wait_for(lambda: panel.build_btn.isEnabled(), 30.0, "the program")
    assert panel.entry.text() == "", ("a success clears the box", panel.entry.text())


@test
def test_the_settings_dialog_builds_without_touching_the_real_config():
    from ckwb.gui import panels

    dialog = panels.SettingsDialog(main_window())
    try:
        assert dialog.base.text() is not None
        assert dialog.key.echoMode() == QtWidgets.QLineEdit.Password, \
            "the API key must not be displayed in clear text"
        assert "Agents" in dialog.agent_state.text()
    finally:
        dialog.reject()
        dialog.deleteLater()
    pump()


# -------------------------------------------------------------------------- requirements

@test
def test_the_requirements_panel_shows_the_chain_and_flags_a_stale_one():
    """The button used to say "not implemented yet", which is worth less than nothing.

    What it has to show is the CHAIN: a requirement, what it derives, what it drives, and
    whether the geometry has caught up. The last of those is the one that matters in use -- an
    edited requirement that has not propagated leaves the design in two states at once, and a
    panel that showed a green tick for that would be actively misleading.
    """
    from ckwb import document as D
    from ckwb import requirements as R
    from ckwb.gui import panels

    doc = new_doc()
    obj = D.make_part(doc, "plate", None, name="Deck")
    doc.recompute()
    R.set_requirement(doc, "deck_span", 150.0, unit="mm", note="the frame pitch")
    R.derive(doc, "half_span", "deck_span / 2", unit="mm")
    R.drive(doc, "Deck", "width", "deck_span")

    panel = panels.show_panel("CK_RequirementsPanel").widget()
    panel.refresh()
    pump()

    rows = {}
    for i in range(panel.tree.topLevelItemCount()):
        group = panel.tree.topLevelItem(i)
        for j in range(group.childCount()):
            child = group.child(j)
            rows[child.text(0)] = (group.text(0), child.text(1), child.text(2))
    assert "deck_span" in rows, sorted(rows)
    assert "150" in rows["deck_span"][1], rows["deck_span"]
    assert "half_span" in rows, sorted(rows)
    assert "75" in rows["half_span"][1], rows["half_span"]
    assert any("Deck" in k or "Deck" in v[2] for k, v in rows.items()), rows
    assert "up to date" in panel.summary.text().lower(), panel.summary.text()

    # now diverge the chain without applying it, and the panel must say so
    R.set_requirement(doc, "deck_span", 300.0, apply=False)
    panel.refresh()
    pump()
    assert "stale" in panel.summary.text().lower(), panel.summary.text()

    # and the panel can put it right
    panel.apply()
    pump()
    assert abs(obj.Width - 300.0) < 1e-9, obj.Width
    assert "up to date" in panel.summary.text().lower(), panel.summary.text()


@test
def test_the_requirements_panel_is_calm_about_an_empty_document():
    """The first thing anybody sees. It must explain itself rather than look broken."""
    from ckwb.gui import panels

    new_doc()
    panel = panels.show_panel("CK_RequirementsPanel").widget()
    panel.refresh()
    pump()
    assert panel.tree.topLevelItemCount() >= 0
    text = panel.summary.text().lower()
    assert "no requirement" in text or "nothing" in text, panel.summary.text()


# ------------------------------------------------------------------------------- analyse

@test
def test_the_analyse_dialog_proposes_boundary_conditions_without_committing_to_them():
    """The line this walks: do not guess the engineering, do not make them type Python either.

    Before this, Analyse printed a Python snippet to paste into the console — right about not
    inferring a restraint, wrong about what a user should have to do. The dialog pre-fills the
    selectors from the part's own bounding box, which is a *proposal* sitting in an editable
    field, and nothing runs until Analyse is pressed. The difference between a proposal and a
    guess is whether a human had to look at it.
    """
    from ckwb import document as D
    from ckwb.gui import panels

    doc = new_doc()
    obj = D.make_part(doc, "plate", None, name="Beam")
    obj.Width, obj.Depth, obj.Thickness = 120.0, 12.0, 12.0
    doc.recompute()

    dialog = panels.AnalyseDialog(main_window(), obj)
    try:
        # the span is shown, so the selectors can be read against something
        assert "120" in dialog.span.text() or "60" in dialog.span.text(), dialog.span.text()
        # and the proposals are real selectors over this part, not placeholders
        for field in (dialog.fix, dialog.load):
            assert field.text().strip(), "the dialog must propose a selector, not an empty box"
            assert field.text().strip()[0] in "xyz", field.text()
            assert field.isReadOnly() is False, "a proposal the user cannot edit is a guess"
        assert dialog.material.currentText() in ("petg", "pla", "abs", "al6061", "steel")
        assert dialog.pitch.value() > 0.0

        kwargs = dialog.arguments()
        assert kwargs["part"] == "Beam", kwargs
        assert len(kwargs["force"]) == 3, kwargs
        assert any(v != 0.0 for v in kwargs["force"]), "a zero load would solve nothing"
    finally:
        dialog.reject()
        dialog.deleteLater()
    pump()


@test
def test_analyse_runs_off_the_gui_thread_and_reports_a_safety_factor():
    """A 60k-DOF solve on the GUI thread freezes FreeCAD, which is the defect already fixed once."""
    import time

    from ckwb import document as D
    from ckwb.gui import actions

    doc = new_doc()
    obj = D.make_part(doc, "plate", None, name="Beam")
    obj.Width, obj.Depth, obj.Thickness = 120.0, 12.0, 12.0
    doc.recompute()

    worker = actions.AnalyseWorker(
        {"part": "Beam", "material": "al6061", "pitch": 3.0,
         "fix": "x<=-59", "load": "x>=59", "force": [0.0, 0.0, -200.0]})
    done = {}
    worker.finished_ok.connect(lambda out: done.update(out))
    worker.failed.connect(lambda msg: done.update({"error": msg}))

    started = time.time()
    worker.start()
    assert time.time() - started < 0.5, (
        "start() took %.2fs; the solve is running on the GUI thread" % (time.time() - started))

    wait_for(lambda: bool(done), timeout=120.0, what="the analysis to report back")
    assert "error" not in done, done["error"]
    assert done["safety_factor"] > 0, done
    assert done["dof"] > 1000, done
    worker.wait(5000)
    pump()


# ------------------------------------------------------------------------------ the console

@test
def test_nothing_in_this_run_logged_an_error():
    """A console full of warnings is how a real error gets overlooked."""
    text = report_view_text()
    bad = [ln for ln in text.splitlines()
           if "Cannot find icon" in ln or "Traceback" in ln
           or "[ConstraintKit] workbench failed" in ln]
    assert not bad, "the Report view logged problems:\n" + "\n".join(bad[:10])


def run():
    passed, failed = 0, []
    for fn in TESTS:
        try:
            fn()
            passed += 1
            LOG.append(f"  PASS  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(fn.__name__)
            LOG.append(f"  FAIL  {fn.__name__}: {exc}")
            LOG.append(traceback.format_exc())
    LOG.append("")
    LOG.append(f"{passed}/{passed + len(failed)} passed"
               + (f"; FAILED: {failed}" if failed else ""))
    LOG.append("EXIT 1" if failed else "EXIT 0")
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("\n".join(LOG) + "\n")


try:
    run()
finally:
    # Always quit, or a failing run leaves a FreeCAD window open forever.
    QtCore.QTimer.singleShot(400, QtWidgets.QApplication.instance().quit)
