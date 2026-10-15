# -*- coding: utf-8 -*-
"""GUI init: register the Constraint Kit workbench with FreeCAD."""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import FreeCAD
import FreeCADGui

try:
    from ckwb.gui.workbench import ConstraintKitWorkbench
    FreeCADGui.addWorkbench(ConstraintKitWorkbench())
except Exception as exc:  # noqa: BLE001
    # Report it rather than leaving a workbench silently missing from the selector.
    FreeCAD.Console.PrintError(
        f"[ConstraintKit] workbench failed to load: {type(exc).__name__}: {exc}\n")
