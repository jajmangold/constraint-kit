#!/usr/bin/env python3
"""Launch a real FreeCAD GUI, run `test_gui.py` inside it, and report.

    python workbench/tests/run_gui_tests.py

The only way to test the Qt layer: `FreeCADGui` does not exist under `freecadcmd`, so a main
window, a real workbench activation and a Qt event loop all require the actual application. It
opens a window for a few seconds and closes itself.

Two defects shipped in one day because this did not exist — `InitGui.py` raising on `__file__`
so the workbench never appeared, and nine missing icons — both invisible to 150 passing
headless tests. Hence this is a CI tier, not a manual ritual.

Exit code is the suite's, taken from a sentinel in the result file rather than from FreeCAD's
own exit status, which does not reflect what a script inside it did.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "test_gui.py")
RESULT = os.path.join(HERE, "gui_result.txt")

FREECAD = os.environ.get("FREECAD", r"C:\Program Files\FreeCAD 1.0\bin\FreeCAD.exe")
TIMEOUT = float(os.environ.get("CK_GUI_TIMEOUT", "420"))


def main() -> int:
    if not os.path.exists(FREECAD):
        print(f"[gui] FreeCAD not found at {FREECAD}")
        print("[gui] set FREECAD to its path; the Qt layer is NOT covered by this run")
        return 2                                  # distinct from a test failure

    if os.path.exists(RESULT):
        os.remove(RESULT)

    env = dict(os.environ, CK_GUI_RESULT=RESULT)
    started = time.time()
    proc = subprocess.Popen([FREECAD, SCRIPT], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        proc.wait(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        print(f"[gui] FreeCAD did not exit within {TIMEOUT:.0f}s; killed it")

    if not os.path.exists(RESULT):
        print("[gui] the suite produced no result file — it died before it could write one.")
        print("[gui] run it by hand to see the traceback:")
        print(f'[gui]   "{FREECAD}" "{SCRIPT}"')
        return 1

    with open(RESULT, encoding="utf-8") as fh:
        body = fh.read()
    print(body.replace("EXIT 0", "").replace("EXIT 1", "").rstrip())
    print(f"[gui] {time.time() - started:.0f}s in a real FreeCAD session")
    return 0 if body.rstrip().endswith("EXIT 0") else 1


if __name__ == "__main__":
    raise SystemExit(main())
