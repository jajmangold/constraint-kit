# -*- coding: utf-8 -*-
"""The interface, declared as data. No Qt here, so it can be checked headlessly.

Keeping the toolbar a declaration rather than a pile of class definitions buys two things. It can
be *tested* — every button is checked against the command registry, which enforces the rule that
no button reaches past the registry — and it can be read, so the shape of the UI is reviewable
without tracing Qt wiring.

**Eight toolbar entries, and that is the whole toolbar.** FreeCAD workbenches habitually grow
forty buttons across six sparse groups, most greyed out most of the time. The rest of the
vocabulary lives in the command palette and in tree context menus, which is how the full command
set stays reachable without the button farm. `PALETTE_ONLY` names the commands that deliberately
have no button.
"""
from __future__ import annotations

#: (id, label, tooltip, registry command or None, pixmap name, shortcut)
#: `command=None` means the action opens a panel or a picker rather than running one command.
TOOLBAR = (
    ("CK_Describe", "Describe", "Say what you want to build; review the program before it runs",
     None, "ck-describe", "Ctrl+Shift+D"),
    ("CK_Insert", "Insert", "Insert a standard part — one searchable picker, not fifteen buttons",
     None, "ck-insert", "Ctrl+Shift+I"),
    ("CK_Join", "Join", "Mate two parts by their named frames",
     None, "ck-join", "Ctrl+Shift+J"),
    ("CK_Requirements", "Requirements", "Show what this design has to achieve, and whether it does",
     None, "ck-requirements", ""),
    ("CK_Check", "Check", "Run every geometric check and show the verdicts",
     "run_checks", "ck-check", "Ctrl+Shift+K"),
    ("CK_Analyse", "Analyse", "Stiffness and stress for the selected part",
     None, "ck-analyse", ""),
    ("CK_Export", "Export", "Write STEP, STL, or the parameters file",
     None, "ck-export", ""),
    ("CK_Settings", "Settings", "Model endpoint, units, and agent access",
     None, "ck-settings", ""),
)

#: Reachable from the command palette and context menus, never from a button.
PALETTE_ONLY = (
    "list_generators", "get_tree", "get_params", "get_checks",
    "set_param", "promote_param", "save_params", "load_params", "undo",
    # plan without building: useful from the palette and essential to agents, who should be able
    # to see a program before committing to it exactly as a human does.
    "plan",
    "import_step",
    "sweep",
    # Physics from the palette rather than the toolbar, because it needs boundary conditions
    # typed in and those are a decision, not a click. A one-button "is it strong enough?" would
    # have to guess where the part is held, and a guessed restraint produces a number that looks
    # like analysis and is not.
    "analyse",
    # Optimise from the palette for the same reason as analyse, doubled: it needs requirements,
    # and a requirement nobody typed is a requirement nobody chose.
    "optimise",
)

#: Dockable panels. `area` is a Qt dock area name resolved in panels.py.
PANELS = (
    ("CK_DescribePanel", "Describe", "right"),
    ("CK_ChecksPanel", "Checks", "bottom"),
    ("CK_ProvenancePanel", "Provenance", "left"),
)

MENU = "Constraint Kit"
TOOLBAR_NAME = "Constraint Kit"


def toolbar_ids() -> list:
    return [row[0] for row in TOOLBAR]


def backing_commands() -> list:
    """Registry commands a toolbar button runs directly (excludes panel/picker openers)."""
    return [row[3] for row in TOOLBAR if row[3]]
