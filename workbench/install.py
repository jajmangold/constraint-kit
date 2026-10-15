#!/usr/bin/env python3
"""Install the workbench into FreeCAD's Mod directory.

    python workbench/install.py              # link it (edit in place, no reinstall)
    python workbench/install.py --copy       # copy it instead
    python workbench/install.py --uninstall
    python workbench/install.py --status

Linking is the default because the point is to edit the workbench and restart FreeCAD, not to
reinstall after every change. On Windows a directory JUNCTION is used rather than a symlink:
junctions need no administrator rights, symlinks do unless Developer Mode is on.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

NAME = "ConstraintKit"
HERE = os.path.dirname(os.path.abspath(__file__))


def mod_dir() -> str:
    """FreeCAD's user Mod directory, which is where an addon belongs.

    Not the installation's own Mod directory: that one needs admin rights and is wiped by an
    upgrade.
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~/AppData/Roaming")
        return os.path.join(base, "FreeCAD", "Mod")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Preferences/FreeCAD/Mod")
    return os.path.expanduser("~/.local/share/FreeCAD/Mod")


def target() -> str:
    return os.path.join(mod_dir(), NAME)


def status() -> int:
    dest = target()
    if not os.path.exists(dest):
        print(f"not installed. Target would be:\n  {dest}")
        return 1
    kind = "junction/symlink" if os.path.islink(dest) or _is_junction(dest) else "copy"
    print(f"installed as a {kind}:\n  {dest}")
    if kind == "copy":
        print("  (a copy does not track edits; re-run install.py after changing the source)")
    print(f"source:\n  {HERE}")
    return 0


def _is_junction(path: str) -> bool:
    try:
        return bool(os.readlink(path))
    except OSError:
        return False


def uninstall() -> int:
    dest = target()
    if not os.path.exists(dest):
        print("nothing to remove")
        return 0
    if os.path.islink(dest) or _is_junction(dest):
        os.rmdir(dest) if sys.platform == "win32" else os.unlink(dest)
    else:
        shutil.rmtree(dest)
    print(f"removed {dest}")
    return 0


def install(copy: bool = False) -> int:
    dest = target()
    os.makedirs(mod_dir(), exist_ok=True)
    if os.path.exists(dest):
        print(f"already present at {dest}; removing it first")
        uninstall()

    if copy:
        shutil.copytree(HERE, dest, ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", "tests"))
        print(f"copied to {dest}")
    elif sys.platform == "win32":
        # /J = directory junction: no administrator rights needed, unlike /D symlinks.
        res = subprocess.run(["cmd", "/c", "mklink", "/J", dest, HERE],
                             capture_output=True, text=True)
        if res.returncode != 0:
            print(f"junction failed ({res.stderr.strip()}); falling back to a copy")
            return install(copy=True)
        print(f"linked {dest} -> {HERE}")
    else:
        os.symlink(HERE, dest)
        print(f"linked {dest} -> {HERE}")

    print("\nNext:")
    print("  1. Restart FreeCAD.")
    print("  2. Pick 'Constraint Kit' in the workbench selector.")
    print("  3. Settings > Start agent access, to let Claude Code or Codex drive it.")
    print("\nFor agents, register the bridge once:")
    print(f'  claude mcp add freecad -- python "{os.path.join(HERE, "mcp_bridge.py")}"')
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--copy", action="store_true", help="copy instead of linking")
    group.add_argument("--uninstall", action="store_true")
    group.add_argument("--status", action="store_true")
    args = ap.parse_args(argv)

    if args.status:
        return status()
    if args.uninstall:
        return uninstall()
    return install(copy=args.copy)


if __name__ == "__main__":
    raise SystemExit(main())
