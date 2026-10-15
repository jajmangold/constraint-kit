# -*- coding: utf-8 -*-
"""GUI init: register the Constraint Kit workbench with FreeCAD.

**`__file__` is NOT defined when FreeCAD executes this file.** Every statement here therefore
avoids it, and everything sits inside one try/except so that a failure is reported instead of
taking the workbench silently out of the selector.

That is not a hypothetical. The first version of this file computed
`os.path.dirname(os.path.abspath(__file__))` at module level, outside the try. It raised
`NameError`, FreeCAD logged one line and carried on, and the workbench simply did not appear --
no error dialog, no empty toolbar, nothing to notice. FreeCAD puts every Mod directory on
`sys.path` before running this, so no path work is needed to import `ckwb` at all.
"""
try:
    import os
    import sys

    import FreeCAD
    import FreeCADGui

    # Locate ourselves without __file__: FreeCAD has already added this Mod directory to
    # sys.path. realpath follows the install junction, so the sibling physics tier is findable.
    _here = None
    for _entry in list(sys.path):
        if _entry and os.path.isdir(os.path.join(_entry, "ckwb")):
            _here = os.path.realpath(_entry)
            break
    _repo = os.environ.get("CK_REPO") or (os.path.dirname(_here) if _here else None)
    if _repo and os.path.isdir(os.path.join(_repo, "constraint_kit")) and _repo not in sys.path:
        sys.path.insert(0, _repo)

    from ckwb.gui.workbench import ConstraintKitWorkbench

    FreeCADGui.addWorkbench(ConstraintKitWorkbench())
    FreeCAD.Console.PrintLog("[ConstraintKit] workbench registered\n")
except Exception as _exc:  # noqa: BLE001
    import traceback

    try:
        import FreeCAD

        FreeCAD.Console.PrintError(
            f"[ConstraintKit] workbench failed to load: {type(_exc).__name__}: {_exc}\n"
            f"{traceback.format_exc()}\n")
    except Exception:
        print("[ConstraintKit] workbench failed to load:", traceback.format_exc())
