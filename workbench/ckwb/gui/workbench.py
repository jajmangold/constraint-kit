# -*- coding: utf-8 -*-
"""The Workbench class FreeCAD instantiates when the user picks Constraint Kit.

Thin by design: it registers the commands declared in `spec.py`, builds one toolbar and one menu
from that same declaration, and opens the panels. Everything it offers routes through the command
registry, so a human and an agent drive the identical vocabulary.
"""
from __future__ import annotations

import FreeCAD
import FreeCADGui


class ConstraintKitWorkbench(FreeCADGui.Workbench):
    MenuText = "Constraint Kit"
    ToolTip = "Describe a mechanism; get exact, checked CAD back"
    Icon = "ck-workbench"

    def Initialize(self):
        """Called once, the first time the workbench is activated."""
        from ckwb import commands  # registers the command vocabulary  # noqa: F401
        from ckwb.gui import actions, spec

        ids = actions.register_all()
        self.appendToolbar(spec.TOOLBAR_NAME, ids)
        self.appendMenu(spec.MENU, ids)
        FreeCAD.Console.PrintMessage(
            f"[ConstraintKit] {len(ids)} toolbar commands, "
            f"{len(spec.PALETTE_ONLY)} more in the command palette\n")

    def Activated(self):
        from ckwb.gui import panels
        panels.install_observers()
        panels.show_panel("CK_ChecksPanel")
        panels.show_panel("CK_ProvenancePanel")

    def Deactivated(self):
        pass

    def GetClassName(self):
        return "Gui::PythonWorkbench"
