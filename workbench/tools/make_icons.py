#!/usr/bin/env python3
"""Generate the workbench's SVG icons.

    python workbench/tools/make_icons.py

Written as a generator rather than nine hand-edited files so the set stays consistent: one
viewBox, one stroke weight, one palette, and a change to the house style is one edit here.

Colour choice matters more than it looks. FreeCAD ships a light theme and a dark one, and a
great many addons pick a stroke that vanishes in whichever they were not designed against. These
use a mid-tone blue (#4F8DBF) that holds contrast on both white and #2B2B2B, with amber (#D99A3F)
reserved for the one action that creates something.
"""
from __future__ import annotations

import os

STROKE = "#4F8DBF"
ACCENT = "#D99A3F"
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "icons")

HEAD = ('<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">'
        '<g fill="none" stroke="{c}" stroke-width="4" '
        'stroke-linecap="round" stroke-linejoin="round">')
TAIL = "</g></svg>"

ICONS = {
    # the workbench mark: a solid with an oriented frame on it, which is the whole doctrine
    "ck-workbench": (STROKE, """
        <path d="M10 22 32 11l22 11-22 11z"/>
        <path d="M10 22v20l22 11V33z"/>
        <path d="M54 22v20L32 53V33z"/>
        <path d="M32 33v14" stroke-width="3"/>"""),

    # describe: a speech bubble, with a spark for the thing that answers
    "ck-describe": (ACCENT, """
        <path d="M11 16h42v27H30L17 54v-11h-6z"/>
        <path d="M32 24v11M26.5 29.5h11" stroke-width="3.5"/>"""),

    # insert: a part dropped into place
    "ck-insert": (STROKE, """
        <rect x="11" y="24" width="30" height="29" rx="2"/>
        <path d="M49 11v22M38 22h22" stroke-width="3.5"/>"""),

    # join: two frames brought nose to nose
    "ck-join": (STROKE, """
        <circle cx="20" cy="32" r="10"/>
        <circle cx="44" cy="32" r="10"/>
        <path d="M30 32h4" stroke-width="3.5"/>"""),

    # requirements: claims, each with a box to satisfy
    "ck-requirements": (STROKE, """
        <rect x="9" y="13" width="12" height="12" rx="2"/>
        <rect x="9" y="39" width="12" height="12" rx="2"/>
        <path d="M29 19h26M29 45h26" stroke-width="3.5"/>"""),

    # check: the verdict
    "ck-check": (STROKE, """
        <circle cx="32" cy="32" r="21"/>
        <path d="M22 33l7.5 7.5L43 25"/>"""),

    # analyse: a loaded beam, deflecting
    "ck-analyse": (STROKE, """
        <path d="M10 18v28"/>
        <path d="M10 24c16 0 28 6 44 20" />
        <path d="M46 44h8v8" stroke-width="3.5"/>"""),

    # export: out of the document
    "ck-export": (STROKE, """
        <path d="M14 38v12h36V38"/>
        <path d="M32 44V12M21 23 32 12l11 11"/>"""),

    # settings
    "ck-settings": (STROKE, """
        <circle cx="32" cy="32" r="8"/>
        <path d="M32 9v8M32 47v8M9 32h8M47 32h8M16 16l6 6M42 42l6 6M48 16l-6 6M22 42l-6 6"
              stroke-width="3.5"/>"""),
}


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    for name, (colour, body) in ICONS.items():
        svg = HEAD.format(c=colour) + "".join(
            line.strip() for line in body.strip().splitlines()) + TAIL
        path = os.path.join(OUT, name + ".svg")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(svg + "\n")
        print(f"  {name}.svg  {len(svg)} bytes")
    print(f"\n{len(ICONS)} icons in {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
