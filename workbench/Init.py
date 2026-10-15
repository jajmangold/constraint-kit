# -*- coding: utf-8 -*-
"""Headless init: make the sibling physics tier importable.

FreeCAD runs this for every Mod directory, in GUI and headless sessions alike, before InitGui.py.
Nothing here imports Qt, so a headless run stays headless.

**`__file__` is NOT defined when FreeCAD executes this file.** It is exec'd without one, so the
usual `os.path.dirname(__file__)` raises `name '__file__' is not defined` — and FreeCAD reports
that as a one-line init error and carries on, so the symptom is a workbench that half works. This
module therefore locates itself by looking for its own package on `sys.path`, which FreeCAD has
already populated with every Mod directory.

`realpath` matters as much: the default install is a directory junction, so the path FreeCAD knows
is `.../FreeCAD/Mod/ConstraintKit` and one level up from that is `Mod`, not the checkout. Following
the link is what makes `constraint_kit/` findable beside `workbench/`.
"""
import os
import sys

try:
    _here = None
    for _entry in list(sys.path):
        if _entry and os.path.isdir(os.path.join(_entry, "ckwb")):
            _here = os.path.realpath(_entry)
            break

    if _here and _here not in sys.path:
        sys.path.insert(0, _here)

    # The physics tier lives in the parent checkout (constraint_kit/fea). Putting it on the path
    # here means the Python console can `from constraint_kit import fea` with no further setup;
    # ckwb.physics also finds it on its own, so this is a convenience, not a requirement.
    _repo = os.environ.get("CK_REPO") or (os.path.dirname(_here) if _here else None)
    if _repo and os.path.isdir(os.path.join(_repo, "constraint_kit")) and _repo not in sys.path:
        sys.path.insert(0, _repo)
except Exception as _exc:  # noqa: BLE001
    # Never let a path problem take the workbench down silently.
    import FreeCAD
    FreeCAD.Console.PrintError(
        f"[ConstraintKit] Init.py could not set up paths: {type(_exc).__name__}: {_exc}\n")
