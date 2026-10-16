# -*- coding: utf-8 -*-
"""Dock panels: Describe, Checks, and the settings dialog.

Three deliberate choices, each from the design:

* **Checks are a dock, not a modal.** They are always visible, and STALE is shown as its own
  state — a green row never outlives the edit that invalidated it. A modal would mean verdicts
  are only true at the instant you asked.
* **The model proposes; the kernel disposes.** The Describe panel shows the program it is about
  to run and waits for Build. Geometry never appears straight out of a language model.
* **Guesses look like guesses.** Provenance rides on the property group, so FreeCAD's own
  property editor is the provenance inspector and this module does not reimplement one.
"""
from __future__ import annotations

import json
import os

import FreeCAD
import FreeCADGui
from PySide import QtCore, QtGui

from .. import document as _doc
from ..provenance import COLORS, LABELS
from ..registry import registry

_PANELS: dict[str, QtGui.QDockWidget] = {}
_AREAS = {"left": QtCore.Qt.LeftDockWidgetArea, "right": QtCore.Qt.RightDockWidgetArea,
          "bottom": QtCore.Qt.BottomDockWidgetArea, "top": QtCore.Qt.TopDockWidgetArea}

from ..settings import llm_settings, prefs as _prefs, save_llm_settings


# ------------------------------------------------------------------------------------- checks

class ChecksPanel(QtGui.QWidget):
    """A compact verdict list. One row per check, and staleness is a first-class row state."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtGui.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        bar = QtGui.QHBoxLayout()
        self.summary = QtGui.QLabel("No checks have run on this document yet.")
        self.summary.setWordWrap(True)
        bar.addWidget(self.summary, 1)
        run = QtGui.QPushButton("Run all")
        run.clicked.connect(self.run)
        bar.addWidget(run)
        layout.addLayout(bar)

        self.tree = QtGui.QTreeWidget()
        self.tree.setHeaderLabels(["Check", "Result", "Detail"])
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.header().setStretchLastSection(True)
        layout.addWidget(self.tree, 1)

    def run(self) -> None:
        try:
            registry.call("run_checks")
        except Exception as exc:  # noqa: BLE001
            FreeCAD.Console.PrintError(f"[ConstraintKit] checks failed: {exc}\n")
            self.summary.setText(str(exc))
            return
        self.refresh()

    def refresh(self) -> None:
        self.tree.clear()
        try:
            state = registry.call("get_checks")
        except Exception as exc:  # noqa: BLE001
            self.summary.setText(str(exc))
            return
        if not state.get("ran"):
            self.summary.setText(state.get("note", "No checks have run yet."))
            return

        stale = state.get("stale", True)
        self.summary.setText(
            "The model has changed since these ran — re-run before trusting them."
            if stale else
            ("Everything passes." if state.get("ok") else "Something is failing; see below."))
        self.summary.setStyleSheet("color: #D99A3F;" if stale else "")

        for name, res in state.get("checks", {}).items():
            ok = res.get("ok", False)
            mark = "stale" if stale else ("pass" if ok else "FAIL")
            item = QtGui.QTreeWidgetItem([name, mark, self._detail(name, res)])
            if stale:
                item.setForeground(1, QtGui.QBrush(QtGui.QColor("#8B929C")))
            elif not ok:
                item.setForeground(1, QtGui.QBrush(QtGui.QColor("#E0705F")))
            else:
                item.setForeground(1, QtGui.QBrush(QtGui.QColor("#4FB3A3")))
            for clash in res.get("clashes", []):
                QtGui.QTreeWidgetItem(
                    item, ["", "clash",
                           f"{clash['a']} ↔ {clash['b']}  {clash['volume_mm3']:.3f} mm³"])
            for bad in res.get("bad", []):
                QtGui.QTreeWidgetItem(
                    item, ["", "bad", f"{bad['name']}: {bad['solids']} solids, "
                                      f"valid={bad['valid']}, closed={bad['closed']}"])
            self.tree.addTopLevelItem(item)
        self.tree.expandAll()
        self.tree.resizeColumnToContents(0)
        self.tree.resizeColumnToContents(1)

    @staticmethod
    def _detail(name: str, res: dict) -> str:
        if name == "interference":
            return (f"{len(res.get('clashes', []))} clash(es); "
                    f"{res.get('pairs_exact_checked', 0)} of {res.get('pairs_total', 0)} pairs "
                    "needed the exact boolean")
        if name == "mass":
            return f"{res.get('mass_g', 0):.1f} g at {res.get('density_g_cm3', 0)} g/cm³"
        if name == "seating":
            return f"{res.get('n_seated', 0)} of {len(res.get('pairs', []))} adjacent pairs seated"
        if name == "solids":
            return f"{res.get('n_parts', 0)} part(s) checked"
        return ""


# ----------------------------------------------------------------------------------- describe

class DescribePanel(QtGui.QWidget):
    """Say what you want; see the program; then build it.

    The order is the trust model, not a convenience. A plan is shown and nothing exists until
    Build is pressed, so a wrong program is something you read rather than something you then
    have to undo. The model writes a program; the kernel builds it.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._plan = None
        self._session = None
        self._worker = None
        self._cfg = {}
        self._waited = 0
        self._ticker = QtCore.QTimer(self)
        self._ticker.setInterval(1000)
        self._ticker.timeout.connect(self._tick)
        layout = QtGui.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        self.transcript = QtGui.QTextBrowser()
        layout.addWidget(self.transcript, 3)

        self.program = QtGui.QPlainTextEdit()
        self.program.setReadOnly(True)
        self.program.setPlaceholderText("The proposed program appears here before it is built.")
        self.program.setVisible(False)
        layout.addWidget(self.program, 2)

        row = QtGui.QHBoxLayout()
        self.build_btn = QtGui.QPushButton("Build it")
        self.build_btn.clicked.connect(self.build)
        self.build_btn.setEnabled(False)
        self.discard_btn = QtGui.QPushButton("Discard")
        self.discard_btn.clicked.connect(self.discard)
        self.discard_btn.setEnabled(False)
        self.cancel_btn = QtGui.QPushButton("Stop waiting")
        self.cancel_btn.clicked.connect(self.cancel)
        self.cancel_btn.setVisible(False)
        # A visible, counting wait. This is the fix for a problem that was previously fixed by
        # shortening the deadline instead: at 180 s with no sign of life the panel "looked
        # exactly like a hung application", so the timeout was cut to 60 s -- and then real
        # completions, measured at 80 to 130 s, started failing. The deadline has to fit the
        # work; it is the WAIT that has to be legible.
        self.waiting_label = QtGui.QLabel()
        self.waiting_label.setVisible(False)
        row.addWidget(self.waiting_label)
        row.addWidget(self.build_btn, 1)
        row.addWidget(self.discard_btn)
        row.addWidget(self.cancel_btn)
        layout.addLayout(row)

        self.entry = QtGui.QLineEdit()
        self.entry.setPlaceholderText("Describe a part or an assembly...")
        self.entry.returnPressed.connect(self.submit)
        layout.addWidget(self.entry)

        bottom = QtGui.QHBoxLayout()
        self.model_label = QtGui.QLabel()
        self.model_label.setWordWrap(True)
        bottom.addWidget(self.model_label, 1)
        send = QtGui.QPushButton("Send")
        send.clicked.connect(self.submit)
        bottom.addWidget(send)
        layout.addLayout(bottom)

        self.refresh_model_label()
        self._say("Constraint Kit",
                  "Describe a part and you will see the program before anything is built. "
                  "Nothing is placed by a language model.")

    def refresh_model_label(self) -> None:
        cfg = llm_settings()
        if cfg["base_url"] and cfg["planner_model"]:
            self.model_label.setText(f"{cfg['planner_model']} · {cfg['base_url']}")
        else:
            self.model_label.setText("No model configured — see Settings")

    def _say(self, who: str, text: str) -> None:
        self.transcript.append(f"<p><b>{who}</b><br>{text}</p>")
        self.transcript.verticalScrollBar().setValue(
            self.transcript.verticalScrollBar().maximum())

    def discard(self) -> None:
        self._plan = None
        self.program.setVisible(False)
        self.build_btn.setEnabled(False)
        self.discard_btn.setEnabled(False)
        self._say("Constraint Kit", "Discarded. Nothing was built.")

    def submit(self) -> None:
        text = self.entry.text().strip()
        if not text:
            return
        if self._worker is not None and self._worker.isRunning():
            self._say("Constraint Kit", "Still waiting on the last request.")
            return
        self.entry.clear()
        self._say("You", text)

        cfg = llm_settings()
        if not cfg["base_url"] or not cfg["planner_model"]:
            self._say("Constraint Kit",
                      "No model endpoint is configured, so I will not guess at what you meant. "
                      "Open <b>Settings</b> and point it at any OpenAI-compatible endpoint. A "
                      "local server needs no key at all - Ollama is "
                      "<code>http://127.0.0.1:11434/v1</code>, LM Studio "
                      "<code>http://127.0.0.1:1234/v1</code>.")
            return

        from .. import planner as _planner

        try:
            self._session = _planner.PlanSession(text, prior=getattr(self, "_prior", None))
        except Exception as exc:  # noqa: BLE001
            self._say("Constraint Kit", str(exc))
            return
        self._cfg = cfg
        if self._session.accepted_offer:
            # The exchange this exists for: a decline that offers a nearest thing, answered
            # with a bare "do it". Say out loud what is being planned, because substituting a
            # different request for the one they typed must never be invisible.
            self._say("Constraint Kit",
                      "Taking that as yes. Planning what I offered: <i>"
                      + self._session.request + "</i>")
        self._say("Constraint Kit", f"Asking {cfg['planner_model']}...")
        self._ask()

    def _ask(self) -> None:
        """Send the session's current messages off-thread. The GUI stays responsive."""
        from .worker import ChatWorker

        self._set_busy(True)
        self._worker = ChatWorker(self._session.messages, self._cfg, self)
        self._worker.replied.connect(self._on_reply)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _set_busy(self, busy: bool) -> None:
        self.entry.setEnabled(not busy)
        self.cancel_btn.setVisible(busy)
        self.build_btn.setEnabled(not busy and self._plan is not None)
        self.waiting_label.setVisible(busy)
        if busy:
            self._waited = 0
            self._tick()
            self._ticker.start()
        else:
            self._ticker.stop()

    def _tick(self) -> None:
        """One second of waiting, said out loud. A silent wait is indistinguishable from a hang."""
        self._waited += 1
        note = ""
        if self._waited > 20:
            # Said once it has gone past quick, because that is when somebody starts wondering.
            note = "  (a full plan usually takes one to two minutes on a hosted model)"
        self.waiting_label.setText("waiting %ds...%s" % (self._waited, note))

    def cancel(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._say("Constraint Kit",
                      "Stopped waiting. The request may still finish in the background; its "
                      "answer will be discarded.")
        self._set_busy(False)

    def _on_failed(self, message: str) -> None:
        self._set_busy(False)
        FreeCAD.Console.PrintError("[ConstraintKit] planner: " + message + chr(10))
        self._say("Constraint Kit", f"That did not work: {message}")
        # Put their words back. A hosted model that stalls costs a minute of waiting and the
        # failure is not theirs, so making them retype a sentence they already typed is adding
        # insult to it. The entry is empty by now because `submit` cleared it.
        if not self.entry.text().strip():
            self.entry.setText(getattr(self._session, "asked", "") or "")
            self.entry.selectAll()

    def _on_reply(self, reply: str) -> None:
        """Back on the GUI thread, where it is safe to build geometry for validation."""
        self._set_busy(False)
        try:
            kind, payload = self._session.offer(reply)
        except Exception as exc:  # noqa: BLE001
            self._say("Constraint Kit", f"That did not work: {exc}")
            return

        if kind == "retry":
            self._say("Constraint Kit",
                      "That program would not build; telling it exactly what was wrong and "
                      "asking once more.")
            self._ask()
            return
        if kind == "error":
            FreeCAD.Console.PrintError("[ConstraintKit] planner: " + str(payload) + chr(10))
            self._say("Constraint Kit", f"I could not get a usable program: {payload}")
            return

        from .. import planner as _planner

        plan = payload
        # Remember this turn, so the next one can refer to it. Without this the panel looks
        # like a conversation and is not one: an offer was made and forgotten in the same
        # breath, and "do it" came back as "the request contains no part or assembly".
        self._prior = _planner.prior_of(plan)

        if plan.declined:
            msg = f"<b>I will not build that.</b> {plan.reason}"
            if plan.nearest:
                msg += (f"<br><br>Nearest thing I can build: {plan.nearest}"
                        "<br><br><i>Say \u201cdo it\u201d and I will build that instead.</i>")
            self._say("Constraint Kit", msg)
            self.discard()
            return

        self._plan = plan
        self.program.setPlainText(plan.render())
        self.program.setVisible(True)
        self.build_btn.setEnabled(True)
        self.discard_btn.setEnabled(True)
        note = f" {plan.notes}" if plan.notes else ""
        extra = " (repaired once)" if plan.attempts > 1 else ""
        self._say("Constraint Kit",
                  f"Here is the program{extra} - {plan.summary()}. "
                  f"Read it, then press <b>Build it</b>.{note}")

    def build(self) -> None:
        if self._plan is None:
            return
        from .. import planner as _planner

        try:
            made = _planner.build(self._plan)
        except Exception as exc:  # noqa: BLE001
            FreeCAD.Console.PrintError("[ConstraintKit] build: %s\n" % exc)
            self._say("Constraint Kit", f"That would not build: {exc}")
            return
        names = ", ".join(o.Name for o in made)
        self._say("Constraint Kit", f"Built: {names}. Every guessed value is marked in the "
                                    "property editor.")
        self._plan = None
        self.build_btn.setEnabled(False)
        self.discard_btn.setEnabled(False)
        try:
            FreeCADGui.SendMsgToActiveView("ViewFit")
        except Exception:  # noqa: BLE001
            pass
        refresh_checks()


# ------------------------------------------------------------------------------- provenance

class ProvenancePanel(QtGui.QWidget):
    """What the selected part's values are, and who chose each one."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtGui.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        self.header = QtGui.QLabel("Select a part.")
        self.header.setWordWrap(True)
        layout.addWidget(self.header)
        self.tree = QtGui.QTreeWidget()
        self.tree.setHeaderLabels(["Parameter", "Value", "Where it came from"])
        self.tree.header().setStretchLastSection(True)
        self.tree.setAlternatingRowColors(True)
        layout.addWidget(self.tree, 1)
        accept = QtGui.QPushButton("Accept the selected guess")
        accept.clicked.connect(self.accept_selected)
        layout.addWidget(accept)

    def refresh(self) -> None:
        self.tree.clear()
        sel = FreeCADGui.Selection.getSelection()
        objs = [o for o in sel if _doc.is_ck_part(o)]
        if not objs:
            self.header.setText("Select a constraint-kit part.")
            return
        obj = objs[0]
        params = _doc.paramset_of(obj)
        self.header.setText(f"<b>{obj.Name}</b> — {params.generator} — {params.summary()}")
        for provenance, rows in params.grouped():
            group = QtGui.QTreeWidgetItem([LABELS[provenance], "", ""])
            group.setForeground(0, QtGui.QBrush(QtGui.QColor(COLORS[provenance])))
            for name, value in rows:
                child = QtGui.QTreeWidgetItem(group, [name, f"{value.value}", value.explain()])
                child.setData(0, QtCore.Qt.UserRole, name)
            self.tree.addTopLevelItem(group)
        self.tree.expandAll()
        self.tree.resizeColumnToContents(0)

    def accept_selected(self) -> None:
        items = self.tree.selectedItems()
        sel = [o for o in FreeCADGui.Selection.getSelection() if _doc.is_ck_part(o)]
        if not items or not sel:
            return
        name = items[0].data(0, QtCore.Qt.UserRole)
        if not name:
            return
        try:
            registry.call("promote_param", part=sel[0].Name, name=name)
        except Exception as exc:  # noqa: BLE001
            FreeCAD.Console.PrintError(f"[ConstraintKit] {exc}\n")
            return
        self.refresh()


# -------------------------------------------------------------------------------- settings

class RequirementsPanel(QtGui.QWidget):
    """What the design has to achieve, what follows from it, and whether the geometry agrees.

    The Requirements button used to open a dialog saying "not implemented yet", which is the one
    kind of interface element worth less than nothing. What it has to show is the **chain**: a
    requirement, the values derived from it, the parameters it drives, and the targets it will be
    judged against.

    **Staleness is the state that matters in use.** An edited requirement that has not been
    propagated leaves the design in two states at once -- the number says one thing and the
    geometry another -- and a panel showing a green tick for that would be actively misleading.
    So the summary says stale, the divergent rows say what they are against what they should be,
    and Apply is right there.

    Nothing here reads the document directly. Every row comes from `get_requirements`, the same
    command an agent calls, because a panel that reached past the registry would drift from the
    API the moment either changed.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtGui.QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        bar = QtGui.QHBoxLayout()
        self.summary = QtGui.QLabel("No requirements have been set on this document yet.")
        self.summary.setWordWrap(True)
        bar.addWidget(self.summary, 1)
        self.apply_btn = QtGui.QPushButton("Apply")
        self.apply_btn.setToolTip("Push every requirement through to the geometry it drives")
        self.apply_btn.clicked.connect(self.apply)
        bar.addWidget(self.apply_btn)
        refresh = QtGui.QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        bar.addWidget(refresh)
        layout.addLayout(bar)

        self.tree = QtGui.QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Value", "Where it comes from, or goes"])
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.header().setStretchLastSection(True)
        layout.addWidget(self.tree, 1)

        hint = QtGui.QLabel(
            "A requirement is a value with a reason. A derived value follows from it by a "
            "relation. A driven parameter follows from that into the geometry, and is tagged "
            "Derived in the property editor rather than pretending you typed it.")
        hint.setWordWrap(True)
        layout.addWidget(hint)

    # ---- actions -------------------------------------------------------------------------

    def apply(self) -> None:
        from .. import requirements as _req

        doc = FreeCAD.ActiveDocument
        if doc is None:
            return
        try:
            _req.apply_chain(doc)
        except Exception as exc:  # noqa: BLE001
            FreeCAD.Console.PrintError("[ConstraintKit] requirements: %s\n" % exc)
            self.summary.setText(str(exc))
            return
        self.refresh()

    def refresh(self) -> None:
        self.tree.clear()
        doc = FreeCAD.ActiveDocument
        if doc is None:
            self.summary.setText("No document is open, so there is nothing to require.")
            self.apply_btn.setEnabled(False)
            return
        try:
            out = registry.call("get_requirements")
        except Exception as exc:  # noqa: BLE001
            self.summary.setText(str(exc))
            self.apply_btn.setEnabled(False)
            return

        stated = {n: e for n, e in out["requirements"].items() if e.get("kind") == "stated"}
        derived = {n: e for n, e in out["requirements"].items() if e.get("kind") == "derived"}
        values = out.get("values") or {}

        if stated:
            group = self._group("Requirements", "what this design has to achieve")
            for name in sorted(stated):
                entry = stated[name]
                self._row(group, name, self._fmt(entry.get("value"), entry.get("unit")),
                          entry.get("note") or "stated")

        if derived:
            group = self._group("Derived", "computed from the requirements above")
            for name in sorted(derived):
                entry = derived[name]
                where = entry.get("expression", "")
                if entry.get("note"):
                    where += "   -- " + entry["note"]
                self._row(group, name, self._fmt(values.get(name), entry.get("unit")), where)

        if out.get("driven"):
            group = self._group("Drives geometry", "parameters that follow the requirements")
            for binding in out["driven"]:
                label = "%s.%s" % (binding["part"], binding["param"])
                self._row(group, label, "= " + binding["expression"],
                          "tagged Derived in the property editor")

        if out.get("targets"):
            group = self._group("Targets", "checked against the physics by verify_requirements")
            for spec in out["targets"]:
                self._row(group, spec["name"],
                          "%s %s %g" % (spec["metric"], spec["op"], spec["limit"]),
                          spec.get("note") or "")

        if out.get("stale_params"):
            group = self._group("Out of date", "the geometry has not caught up")
            for text in out["stale_params"]:
                self._row(group, "", "", text)

        self.tree.expandAll()
        self._summarise(out, stated, derived)

    # ---- helpers -------------------------------------------------------------------------

    def _group(self, title: str, detail: str):
        item = QtGui.QTreeWidgetItem(self.tree, [title, "", detail])
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        return item

    @staticmethod
    def _row(group, name: str, value: str, detail: str):
        return QtGui.QTreeWidgetItem(group, [name, value, detail])

    @staticmethod
    def _fmt(value, unit) -> str:
        if value is None:
            return "unresolved"
        text = "%g" % float(value)
        return text + (" " + unit if unit else "")

    def _summarise(self, out: dict, stated: dict, derived: dict) -> None:
        if out.get("error"):
            self.summary.setText("The requirements do not resolve: " + out["error"])
            self.apply_btn.setEnabled(False)
            return
        if not stated and not derived:
            self.summary.setText(
                "No requirements have been set on this document yet. A requirement is a value "
                "with a reason, and it is what makes a parameter traceable to something other "
                "than somebody's memory.")
            self.apply_btn.setEnabled(False)
            return
        self.apply_btn.setEnabled(True)
        count = "%d requirement%s, %d derived, %d driven parameter%s" % (
            len(stated), "" if len(stated) == 1 else "s", len(derived),
            len(out.get("driven") or []), "" if len(out.get("driven") or []) == 1 else "s")
        if out.get("stale"):
            self.summary.setText(
                count + " -- STALE. A requirement has changed and the geometry has not caught "
                        "up, so the design is in two states at once. Press Apply.")
        else:
            self.summary.setText(count + " -- up to date.")


class AnalyseDialog(QtGui.QDialog):
    """Boundary conditions, proposed from the part's own geometry and editable before they run.

    The line this walks is worth stating, because the previous version got one half right and
    the other half wrong. It refused to guess a restraint — correct, because choosing where a
    part is held IS the engineering and a tool that infers it produces something that looks like
    analysis — and then told the user to go and type Python in the console, which is not a
    reasonable thing to ask of anyone.

    So the selectors arrive **pre-filled from the bounding box and sitting in editable fields**,
    beside the span they refer to. That is a proposal, not a guess: the difference is whether a
    human had to look at it before it ran, and here they do. Nothing solves until Analyse is
    pressed.
    """

    def __init__(self, parent=None, obj=None):
        super().__init__(parent)
        from .. import physics as _physics

        self._obj = obj
        self.setWindowTitle("Analyse %s" % getattr(obj, "Name", ""))
        form = QtGui.QFormLayout(self)

        bb = obj.Shape.BoundBox
        self.span = QtGui.QLabel(
            "x %.1f to %.1f    y %.1f to %.1f    z %.1f to %.1f  (mm)"
            % (bb.XMin, bb.XMax, bb.YMin, bb.YMax, bb.ZMin, bb.ZMax))
        self.span.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        form.addRow("This part spans", self.span)

        # Propose along the LONGEST axis: it is the one a part is usually held at one end of and
        # loaded at the other, and being wrong about it is visible in a field the user is reading.
        spans = {"x": (bb.XMin, bb.XMax), "y": (bb.YMin, bb.YMax), "z": (bb.ZMin, bb.ZMax)}
        axis = max(spans, key=lambda k: spans[k][1] - spans[k][0])
        lo, hi = spans[axis]
        inset = max((hi - lo) * 0.02, 0.5)

        self.fix = QtGui.QLineEdit("%s<=%.3g" % (axis, lo + inset))
        self.load = QtGui.QLineEdit("%s>=%.3g" % (axis, hi - inset))
        self.fix.setToolTip("Where the part is HELD. Add '@xz' to restrain two axes only, which "
                            "is what a roller or a V-wheel actually does.")
        self.load.setToolTip("Where the force is applied. Same selector language.")
        form.addRow("Held where", self.fix)
        form.addRow("Loaded where", self.load)

        row = QtGui.QHBoxLayout()
        self.fx, self.fy, self.fz = (self._force_box(0.0), self._force_box(0.0),
                                     self._force_box(-100.0))
        for label, box in (("X", self.fx), ("Y", self.fy), ("Z", self.fz)):
            row.addWidget(QtGui.QLabel(label))
            row.addWidget(box)
        holder = QtGui.QWidget()
        holder.setLayout(row)
        form.addRow("Total force, N", holder)

        self.material = QtGui.QComboBox()
        self.material.addItems(sorted(_physics.MATERIALS))
        self.material.setCurrentIndex(max(0, self.material.findText("petg")))
        form.addRow("Material", self.material)

        self.pitch = QtGui.QDoubleSpinBox()
        self.pitch.setRange(0.1, 50.0)
        self.pitch.setDecimals(2)
        self.pitch.setSingleStep(0.5)
        self.pitch.setValue(2.0)
        self.pitch.setToolTip("Voxel pitch in mm. One mesh is not a converged answer -- run two "
                              "or three and watch whether the number moves.")
        form.addRow("Mesh pitch, mm", self.pitch)

        note = QtGui.QLabel(
            "These are proposals from the bounding box, not a judgement about how the part is "
            "mounted. Choosing where it is held and pushed is the engineering; read them before "
            "you run them.")
        note.setWordWrap(True)
        form.addRow(note)

        buttons = QtGui.QDialogButtonBox(QtGui.QDialogButtonBox.Ok
                                         | QtGui.QDialogButtonBox.Cancel)
        buttons.button(QtGui.QDialogButtonBox.Ok).setText("Analyse")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    @staticmethod
    def _force_box(value: float):
        box = QtGui.QDoubleSpinBox()
        box.setRange(-1e6, 1e6)
        box.setDecimals(1)
        box.setValue(value)
        return box

    def arguments(self) -> dict:
        return {
            "part": getattr(self._obj, "Name", ""),
            "material": self.material.currentText(),
            "pitch": float(self.pitch.value()),
            "fix": self.fix.text().strip(),
            "load": self.load.text().strip(),
            "force": [float(self.fx.value()), float(self.fy.value()), float(self.fz.value())],
        }


class SettingsDialog(QtGui.QDialog):
    """Model endpoint and agent access. Keys go to FreeCAD's parameter store, never the document."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Constraint Kit settings")
        form = QtGui.QFormLayout(self)

        p = _prefs()
        self.base = QtGui.QLineEdit(p.GetString("BaseUrl", "https://api.deepseek.com"))
        self.key = QtGui.QLineEdit(p.GetString("ApiKey", ""))
        self.key.setEchoMode(QtGui.QLineEdit.Password)
        self.planner = QtGui.QLineEdit(p.GetString("PlannerModel", "deepseek-v4-pro"))
        self.vlm = QtGui.QLineEdit(p.GetString("VisionModel", "deepseek-flash"))
        self.density = QtGui.QDoubleSpinBox()
        self.density.setRange(0.1, 25.0)
        self.density.setDecimals(3)
        self.density.setValue(p.GetFloat("Density", 1.27))

        form.addRow("Base URL (OpenAI-compatible)", self.base)
        form.addRow("API key", self.key)
        form.addRow("Planner model", self.planner)
        form.addRow("Vision model (QA only)", self.vlm)
        form.addRow("Density g/cm³", self.density)

        self.agent_state = QtGui.QLabel()
        start = QtGui.QPushButton("Start agent access")
        start.clicked.connect(self.start_agent)
        form.addRow(self.agent_state, start)
        self.refresh_agent_state()

        note = QtGui.QLabel(
            "Vision QA is a second opinion only; it can never overrule a geometric check.")
        note.setWordWrap(True)
        form.addRow(note)

        buttons = QtGui.QDialogButtonBox(
            QtGui.QDialogButtonBox.Save | QtGui.QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def refresh_agent_state(self) -> None:
        from .. import server
        current = server.current()
        if current is not None and current.port:
            self.agent_state.setText(
                f"Agents: 127.0.0.1:{current.port}\n{current.registry_file}")
        else:
            self.agent_state.setText("Agents: not running")

    def start_agent(self) -> None:
        from .. import server
        try:
            srv = server.ensure_started()
        except Exception as exc:  # noqa: BLE001
            FreeCAD.Console.PrintError(f"[ConstraintKit] agent server: {exc}\n")
            return
        FreeCAD.Console.PrintMessage(
            f"[ConstraintKit] agent access on 127.0.0.1:{srv.port}, "
            f"published to {srv.registry_file}\n")
        self.refresh_agent_state()

    def save(self) -> None:
        p = _prefs()
        p.SetString("BaseUrl", self.base.text().strip())
        p.SetString("ApiKey", self.key.text().strip())
        p.SetString("PlannerModel", self.planner.text().strip())
        p.SetString("VisionModel", self.vlm.text().strip())
        p.SetFloat("Density", float(self.density.value()))
        panel = _PANELS.get("CK_DescribePanel")
        if panel is not None:
            panel.widget().refresh_model_label()
        self.accept()


# ------------------------------------------------------------------------------ dock plumbing

_WIDGETS = {"CK_ChecksPanel": ChecksPanel, "CK_DescribePanel": DescribePanel,
            "CK_ProvenancePanel": ProvenancePanel,
            "CK_RequirementsPanel": RequirementsPanel}


def show_panel(panel_id: str) -> QtGui.QDockWidget:
    from . import spec
    if panel_id not in _WIDGETS:
        raise KeyError(f"no panel {panel_id!r}; have {sorted(_WIDGETS)}")
    dock = _PANELS.get(panel_id)
    if dock is None:
        title, area = next((t, a) for pid, t, a in spec.PANELS if pid == panel_id)
        dock = QtGui.QDockWidget(title, FreeCADGui.getMainWindow())
        dock.setObjectName(panel_id)
        dock.setWidget(_WIDGETS[panel_id](dock))
        FreeCADGui.getMainWindow().addDockWidget(_AREAS[area], dock)
        _PANELS[panel_id] = dock
    dock.show()
    dock.raise_()
    return dock


def refresh_checks() -> None:
    dock = _PANELS.get("CK_ChecksPanel")
    if dock is not None:
        dock.widget().refresh()


def refresh_provenance() -> None:
    dock = _PANELS.get("CK_ProvenancePanel")
    if dock is not None:
        dock.widget().refresh()


class _SelectionWatcher:
    """Keeps the provenance panel in step with the selection, with no polling."""

    def setSelection(self, *_args):
        refresh_provenance()

    addSelection = clearSelection = removeSelection = setSelection


_WATCHER = None


def install_observers() -> None:
    global _WATCHER
    if _WATCHER is None:
        _WATCHER = _SelectionWatcher()
        FreeCADGui.Selection.addObserver(_WATCHER)
