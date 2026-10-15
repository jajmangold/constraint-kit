# -*- coding: utf-8 -*-
"""Qt command classes. Each is a thin shell that calls the registry — never the document.

Every action here does its work through `registry.call(...)`, the same entry point the agent
server uses. That is the rule the whole workbench is built on: if a button could reach past the
registry, an agent could not reproduce what a human did, and the two interfaces would drift.

So these classes contain no geometry, no parameter handling and no checks. They gather arguments
from the user and hand them over.

Imports Qt and FreeCADGui, so it is only imported when a GUI is actually up. The toolbar
declaration lives in `spec.py`, which has no Qt and can be tested headlessly.
"""
from __future__ import annotations

import FreeCAD
import FreeCADGui
from PySide import QtGui

from ..registry import registry
from . import spec


def _report(title: str, body: str, icon=QtGui.QMessageBox.Information) -> None:
    box = QtGui.QMessageBox(icon, title, body, QtGui.QMessageBox.Ok, FreeCADGui.getMainWindow())
    box.setTextInteractionFlags(QtGui.Qt.TextSelectableByMouse)
    box.exec_()


def _fail(title: str, exc: BaseException) -> None:
    """Show a failure as a sentence, and log it. Never a bare traceback in a dialog."""
    FreeCAD.Console.PrintError(f"[ConstraintKit] {title}: {exc}\n")
    _report(title, str(exc), QtGui.QMessageBox.Warning)


class _Base:
    """Shared plumbing for a FreeCAD Gui command."""

    cid = ""
    label = ""
    tip = ""
    pixmap = ""
    shortcut = ""

    def GetResources(self):
        res = {"MenuText": self.label, "ToolTip": self.tip}
        if self.pixmap:
            res["Pixmap"] = self.pixmap
        if self.shortcut:
            res["Accel"] = self.shortcut
        return res

    def IsActive(self):
        """Grey out honestly: an action with nothing to act on says so by being disabled."""
        return FreeCAD.ActiveDocument is not None


class DescribeCommand(_Base):
    cid, label = "CK_Describe", "Describe"
    tip = "Say what you want to build; review the program before it runs"
    pixmap, shortcut = "ck-describe", "Ctrl+Shift+D"

    def IsActive(self):
        return True          # the one action that works with no document open

    def Activated(self):
        from .panels import show_panel
        show_panel("CK_DescribePanel")


class InsertCommand(_Base):
    cid, label = "CK_Insert", "Insert"
    tip = "Insert a standard part"
    pixmap, shortcut = "ck-insert", "Ctrl+Shift+I"

    def Activated(self):
        try:
            gens = registry.call("list_generators")
        except Exception as exc:  # noqa: BLE001
            return _fail("Cannot list generators", exc)
        names = sorted(gens)
        labels = [f"{n}  —  {gens[n]['doc']}" for n in names]
        pick, ok = QtGui.QInputDialog.getItem(
            FreeCADGui.getMainWindow(), "Insert a part",
            "Part (type to search):", labels, 0, False)
        if not ok:
            return
        generator = names[labels.index(pick)]
        try:
            made = registry.call("insert_part", generator=generator)
        except Exception as exc:  # noqa: BLE001
            return _fail("Insert failed", exc)
        FreeCADGui.SendMsgToActiveView("ViewFit")
        FreeCAD.Console.PrintMessage(
            f"[ConstraintKit] inserted {made['name']} ({made['summary']})\n")


class JoinCommand(_Base):
    cid, label = "CK_Join", "Join"
    tip = "Mate two parts by their named frames"
    pixmap, shortcut = "ck-join", "Ctrl+Shift+J"

    def Activated(self):
        try:
            tree = registry.call("get_tree")
        except Exception as exc:  # noqa: BLE001
            return _fail("Cannot read the model", exc)
        parts = tree["parts"]
        if len(parts) < 2:
            return _report("Join", "Two parts are needed before anything can be mated.")

        sel = [o.Name for o in FreeCADGui.Selection.getSelection()]
        names = [p["name"] for p in parts]
        target = sel[0] if sel else names[0]
        moving = sel[1] if len(sel) > 1 else names[1]
        by_name = {p["name"]: p for p in parts}

        tf, ok = QtGui.QInputDialog.getItem(
            FreeCADGui.getMainWindow(), "Join",
            f"Frame on {target} (the part that stays put):",
            by_name[target]["frames"], 0, False)
        if not ok:
            return
        mf, ok = QtGui.QInputDialog.getItem(
            FreeCADGui.getMainWindow(), "Join",
            f"Frame on {moving} (the part that moves):",
            by_name[moving]["frames"], 0, False)
        if not ok:
            return
        kind, ok = QtGui.QInputDialog.getItem(
            FreeCADGui.getMainWindow(), "Join", "How:",
            ["seat — faces in contact", "align — share an axis"], 0, False)
        if not ok:
            return
        try:
            out = registry.call("join", kind=kind.split(" ")[0], target=target,
                               target_frame=tf, moving=moving, moving_frame=mf)
        except Exception as exc:  # noqa: BLE001
            return _fail("Join failed", exc)
        FreeCAD.Console.PrintMessage(
            f"[ConstraintKit] {out['moving']} joined to {out['target']}, "
            f"gap {out['gap_mm']:.6f} mm\n")


class CheckCommand(_Base):
    cid, label = "CK_Check", "Check"
    tip = "Run every geometric check and show the verdicts"
    pixmap, shortcut = "ck-check", "Ctrl+Shift+K"

    def Activated(self):
        from .panels import refresh_checks, show_panel
        try:
            registry.call("run_checks")
        except Exception as exc:  # noqa: BLE001
            return _fail("Checks could not run", exc)
        show_panel("CK_ChecksPanel")
        refresh_checks()


class AnalyseCommand(_Base):
    cid, label = "CK_Analyse", "Analyse"
    tip = "Stiffness and stress for the selected part"
    pixmap = "ck-analyse"

    def Activated(self):
        sel = FreeCADGui.Selection.getSelection()
        if not sel:
            return _report("Analyse", "Select one part first.")
        obj = sel[0]
        bb = obj.Shape.BoundBox
        _report(
            "Analyse",
            f"Structural analysis needs boundary conditions, and guessing them would produce "
            f"something that looks like analysis and is not.\n\n"
            f"{obj.Name} spans\n"
            f"  x {bb.XMin:.1f} … {bb.XMax:.1f}\n"
            f"  y {bb.YMin:.1f} … {bb.YMax:.1f}\n"
            f"  z {bb.ZMin:.1f} … {bb.ZMax:.1f}  (mm)\n\n"
            f"Run it with the restraint and load you intend, e.g. from the Python console:\n\n"
            f"  from ckwb import physics\n"
            f"  physics.stiffness(App.ActiveDocument.{obj.Name}, material='petg', pitch=2.0,\n"
            f"      fix='x<={bb.XMin + 1:.0f}', load='x>={bb.XMax - 1:.0f}',\n"
            f"      force=(0, 0, -100))\n\n"
            f"Use '@xz' on a restraint for a roller that reacts in two axes only.")


class ExportCommand(_Base):
    cid, label = "CK_Export", "Export"
    tip = "Write STEP, or the parameters file"
    pixmap = "ck-export"

    def Activated(self):
        choice, ok = QtGui.QInputDialog.getItem(
            FreeCADGui.getMainWindow(), "Export", "What:",
            ["STEP — geometry", "Parameters — the source of truth"], 0, False)
        if not ok:
            return
        try:
            if choice.startswith("STEP"):
                path, _ = QtGui.QFileDialog.getSaveFileName(
                    FreeCADGui.getMainWindow(), "Export STEP", "", "STEP (*.step *.stp)")
                if not path:
                    return
                out = registry.call("export", path=path)
                _report("Export", f"Wrote {out['parts']} part(s) to\n{out['path']}")
            else:
                out = registry.call("save_params")
                _report("Export",
                        f"Wrote the parameters for {out['parts']} part(s) to\n{out['path']}\n\n"
                        "This file is the source of truth; the .FCStd is a cache of it.")
        except Exception as exc:  # noqa: BLE001
            _fail("Export failed", exc)


class RequirementsCommand(_Base):
    cid, label = "CK_Requirements", "Requirements"
    tip = "Show what this design has to achieve, and whether it does"
    pixmap = "ck-requirements"

    def Activated(self):
        _report("Requirements",
                "Requirements are not implemented yet.\n\n"
                "They are the next piece: a requirement is a checkable claim about the design "
                "(max deflection, mass ceiling, a fit class) that drives parameters rather than "
                "being compared against them afterwards.\n\n"
                "Until then, Check runs the geometric verdicts and Analyse covers stiffness.")


class SettingsCommand(_Base):
    cid, label = "CK_Settings", "Settings"
    tip = "Model endpoint, units, and agent access"
    pixmap = "ck-settings"

    def IsActive(self):
        return True

    def Activated(self):
        from .panels import SettingsDialog
        SettingsDialog(FreeCADGui.getMainWindow()).exec_()


COMMANDS = (DescribeCommand, InsertCommand, JoinCommand, RequirementsCommand,
            CheckCommand, AnalyseCommand, ExportCommand, SettingsCommand)


def register_all() -> list:
    """Register every Gui command and return the ids, in toolbar order."""
    by_id = {}
    for cls in COMMANDS:
        FreeCADGui.addCommand(cls.cid, cls())
        by_id[cls.cid] = cls
    missing = [cid for cid in spec.toolbar_ids() if cid not in by_id]
    if missing:
        raise RuntimeError(
            f"the toolbar declares {missing} but no command class provides them; "
            "spec.TOOLBAR and actions.COMMANDS have diverged")
    return list(spec.toolbar_ids())
