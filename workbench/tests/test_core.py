#!/usr/bin/env python3
"""Workbench core: provenance, the params source-of-truth file, the command registry.

    python workbench/tests/test_core.py

Written BEFORE ckwb/{provenance,params,registry}.py exist. This layer is deliberately free of
any FreeCAD import so it runs on the host interpreter in milliseconds; the geometry layers get
their own suite inside freecadcmd.

The three things under test are the three load-bearing ideas of the workbench:

* **Provenance** — every value knows whether a human stated it, the planner guessed it, a
  standard supplied it or a relation derived it. A guess that cannot be told from a decision is
  the failure mode this whole design exists to prevent.
* **The params file** — a text file is the source of truth and the .FCStd is a cache of it.
  Without an exact round-trip that discipline is a slogan, so it is tested as an invariant.
* **The registry** — one command vocabulary for the toolbar, the palette and agents. If the GUI
  can do something the registry cannot, agents cannot reproduce it, so the registry is the only
  way anything happens.
"""
from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ckwb.params import load_params, params_path_for, save_params  # noqa: E402
from ckwb.provenance import DEFAULT, DERIVED, STANDARD, STATED, ParamSet, Value  # noqa: E402
from ckwb.registry import CommandError, Registry, UnknownCommand  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def sample() -> ParamSet:
    p = ParamSet("bearing_block")
    p.set("bore_d", 16.0, STATED)
    p.set("screw_size", "M5", STATED)
    p.set("thickness", 12.0, DEFAULT, note="bearing width + 5 mm")
    p.set("fillet_r", 1.5, DEFAULT)
    p.set("fit", "H7/p6", STANDARD, source="ISO 286-2", at="2026-10-14", confidence=0.94)
    p.set("wall", 4.8, DERIVED, source="bore_d * 0.3")
    return p


# ------------------------------------------------------------------------------- provenance

@test
def test_value_rejects_an_unknown_provenance():
    try:
        Value(1.0, "vibes")
    except ValueError as exc:
        assert "vibes" in str(exc), str(exc)
        return
    raise AssertionError("an unrecognised provenance must be refused, not stored")


@test
def test_value_rejects_a_confidence_outside_zero_to_one():
    for bad in (-0.1, 1.5):
        try:
            Value(1.0, STANDARD, confidence=bad)
        except ValueError:
            continue
        raise AssertionError(f"confidence {bad} must be refused")


@test
def test_paramset_counts_and_names_its_guesses():
    p = sample()
    assert len(p) == 6, len(p)
    assert p.counts()[DEFAULT] == 2, p.counts()
    assert p.counts()[STATED] == 2, p.counts()
    assert sorted(p.guessed()) == ["fillet_r", "thickness"], p.guessed()
    assert p.summary() == "2 of 6 guessed", p.summary()


@test
def test_paramset_exposes_plain_values_for_geometry():
    """A generator wants numbers, not Value objects; provenance must not leak into the math."""
    v = sample().values()
    assert v["bore_d"] == 16.0 and v["fit"] == "H7/p6", v
    assert not any(isinstance(x, Value) for x in v.values())


@test
def test_promoting_a_guess_to_stated_changes_provenance_not_value():
    p = sample()
    p.promote("thickness")
    assert p["thickness"].value == 12.0
    assert p["thickness"].provenance == STATED
    assert "thickness" not in p.guessed()


@test
def test_promote_refuses_an_unknown_name():
    try:
        sample().promote("nope")
    except KeyError:
        return
    raise AssertionError("promoting a name that is not in the set must raise")


@test
def test_derived_values_carry_their_relation():
    p = sample()
    assert p["wall"].provenance == DERIVED
    assert p["wall"].source == "bore_d * 0.3", p["wall"].source


# ------------------------------------------------------------------------------ params file

@test
def test_params_file_round_trips_exactly():
    """The .FCStd is a cache; THIS file is the model. A lossy round-trip breaks that claim."""
    p = sample()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "x.params.json")
        save_params(p, path)
        back = load_params(path)
    assert back.generator == p.generator
    assert set(back) == set(p)
    for k in p:
        assert back[k] == p[k], (k, back[k], p[k])


@test
def test_params_file_is_stable_on_disk():
    """Byte-stable output, or every rebuild shows up as a diff and git history becomes noise."""
    with tempfile.TemporaryDirectory() as d:
        a, b = os.path.join(d, "a.json"), os.path.join(d, "b.json")
        save_params(sample(), a)
        save_params(sample(), b)
        with open(a, "rb") as fa, open(b, "rb") as fb:
            assert fa.read() == fb.read(), "two saves of the same params differ byte-wise"


@test
def test_params_file_is_readable_json_with_provenance_visible():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "x.json")
        save_params(sample(), path)
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    assert raw["format"] == "constraint-kit/params", raw.get("format")
    assert raw["params"]["fit"]["provenance"] == STANDARD
    assert raw["params"]["fit"]["source"] == "ISO 286-2"
    assert raw["params"]["bore_d"]["value"] == 16.0
    # A stated value needs no extra keys; absent is cleaner than null.
    assert "source" not in raw["params"]["bore_d"], raw["params"]["bore_d"]


@test
def test_params_file_rejects_a_foreign_format():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "x.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"format": "something/else", "params": {}}, fh)
        try:
            load_params(path)
        except ValueError as exc:
            assert "format" in str(exc).lower(), str(exc)
            return
    raise AssertionError("a file that is not a params file must be refused")


@test
def test_params_file_rejects_an_unknown_future_version():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "x.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"format": "constraint-kit/params", "version": 99,
                       "generator": "x", "params": {}}, fh)
        try:
            load_params(path)
        except ValueError as exc:
            assert "version" in str(exc).lower(), str(exc)
            return
    raise AssertionError("a newer schema version must be refused, not guessed at")


@test
def test_params_path_sits_beside_the_document():
    assert params_path_for("/tmp/Bear.FCStd") == "/tmp/Bear.params.json"
    assert params_path_for("C:/w/Bear.FCStd").endswith("Bear.params.json")
    assert params_path_for("") is None, "an unsaved document has no params path yet"


# --------------------------------------------------------------------------------- registry

def fresh() -> Registry:
    r = Registry()

    @r.command("set_param", "Set one parameter", writes=True,
               params={"name": {"type": str, "required": True},
                       "value": {"type": float, "required": True}})
    def _set(name, value):
        return {"name": name, "value": value}

    @r.command("get_tree", "Read the model tree")
    def _tree():
        return {"parts": []}

    return r


@test
def test_registry_calls_a_command_with_validated_args():
    assert fresh().call("set_param", name="t", value=12.0) == {"name": "t", "value": 12.0}


@test
def test_registry_refuses_an_unknown_command():
    try:
        fresh().call("launch_missiles")
    except UnknownCommand as exc:
        assert "launch_missiles" in str(exc)
        return
    raise AssertionError("an unknown command must raise, never no-op")


@test
def test_registry_refuses_a_missing_required_argument():
    try:
        fresh().call("set_param", name="t")
    except CommandError as exc:
        assert "value" in str(exc), str(exc)
        return
    raise AssertionError("a missing required argument must raise")


@test
def test_registry_refuses_an_unexpected_argument():
    """Silently dropping an argument is how an agent thinks it changed something and did not."""
    try:
        fresh().call("set_param", name="t", value=1.0, colour="red")
    except CommandError as exc:
        assert "colour" in str(exc), str(exc)
        return
    raise AssertionError("an unexpected argument must raise")


@test
def test_registry_coerces_an_int_to_a_float_but_refuses_nonsense():
    assert fresh().call("set_param", name="t", value=12)["value"] == 12.0
    try:
        fresh().call("set_param", name="t", value="wide")
    except CommandError:
        return
    raise AssertionError("a non-numeric value for a float param must raise")


@test
def test_registry_refuses_a_duplicate_registration():
    r = fresh()
    try:
        @r.command("get_tree", "again")
        def _dupe():
            return None
    except CommandError:
        return
    raise AssertionError("registering the same command name twice must raise")


@test
def test_registry_describes_itself_for_agents():
    """The MCP tool list IS this schema; a command the registry cannot describe is unreachable."""
    schema = fresh().schema()
    names = [t["name"] for t in schema]
    assert names == sorted(names), "tool list must be stable-ordered"
    assert "set_param" in names and "get_tree" in names
    sp = next(t for t in schema if t["name"] == "set_param")
    assert sp["writes"] is True
    assert sp["input_schema"]["required"] == ["name", "value"], sp["input_schema"]
    assert sp["input_schema"]["properties"]["value"]["type"] == "number"


@test
def test_registry_wraps_writing_commands_in_a_transaction():
    """A half-applied agent edit is how a model ends up quietly wrong. Writes are atomic."""
    events = []
    r = fresh()
    r.transaction_hooks(open=lambda n: events.append(("open", n)),
                        commit=lambda: events.append(("commit",)),
                        abort=lambda: events.append(("abort",)))
    r.call("set_param", name="t", value=1.0)
    assert events == [("open", "set_param"), ("commit",)], events

    events.clear()
    r.call("get_tree")
    assert events == [], "a read-only command must not open a transaction"


@test
def test_registry_aborts_the_transaction_when_a_command_raises():
    events = []
    r = Registry()
    r.transaction_hooks(open=lambda n: events.append(("open", n)),
                        commit=lambda: events.append(("commit",)),
                        abort=lambda: events.append(("abort",)))

    @r.command("boom", "always fails", writes=True)
    def _boom():
        raise RuntimeError("geometry went sideways")

    try:
        r.call("boom")
    except RuntimeError:
        pass
    assert events == [("open", "boom"), ("abort",)], events


# ------------------------------------------------------------------- the FreeCAD entry points

_WB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@test
def test_the_entry_points_never_reference_dunder_file():
    """`__file__` is NOT defined when FreeCAD executes Init.py or InitGui.py.

    This cost a silent failure that took a user opening FreeCAD to find. `InitGui.py` computed
    `os.path.abspath(__file__)` at module level, raised NameError, and FreeCAD logged one line and
    carried on — so the workbench simply did not appear in the selector. No dialog, no empty
    toolbar, nothing to notice. The trap was already known and recorded in AGENTS.md, and applied
    to Init.py but not to the file two directories away.

    Checked by parsing, not by grepping. The first version of this test grepped and failed
    against correct code, because both files *document* the trap in their docstrings — the word
    appears in prose that never executes. An AST walk sees only real references.
    """
    for name in ("Init.py", "InitGui.py"):
        with open(os.path.join(_WB, name), encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=name)
        hits = [n.lineno for n in ast.walk(tree)
                if isinstance(n, ast.Name) and n.id == "__file__"]
        assert not hits, (
            f"{name} references __file__ at line(s) {hits}, and it is undefined when FreeCAD "
            "execs the file; locate the directory by searching sys.path for the package instead")


@test
def test_initgui_wraps_everything_so_a_failure_is_reported():
    """A bare statement outside the try takes the whole workbench down with one logged line.

    FreeCAD reports an init failure as a single log line and carries on, so the only symptom is a
    workbench missing from the selector. Nothing may sit outside the handler.
    """
    with open(os.path.join(_WB, "InitGui.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename="InitGui.py")
    body = list(tree.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body.pop(0)                        # the module docstring is inert
    kinds = [type(n).__name__ for n in body]
    assert kinds == ["Try"], (
        f"InitGui.py's module body is {kinds}; it must be exactly one Try (plus a docstring). "
        "Anything outside it removes the workbench with no visible error.")


@test
def test_every_named_pixmap_has_an_icon_file():
    """Nine missing icons logged nine "Cannot find icon" lines to the Report view on startup.

    They were not errors -- the workbench worked -- but a console full of warnings is how real
    errors get overlooked, and blank buttons read as a broken install. A rename must not be able
    to quietly undo this.
    """
    from ckwb.gui import spec

    icons = os.path.join(_WB, "icons")
    assert os.path.isdir(icons), f"no icons directory at {icons}"
    named = [row[4] for row in spec.TOOLBAR if row[4]] + ["ck-workbench"]
    missing = [n for n in named if not os.path.exists(os.path.join(icons, n + ".svg"))]
    assert not missing, f"named in the spec but no SVG on disk: {missing}"
    for name in named:
        with open(os.path.join(icons, name + ".svg"), encoding="utf-8") as fh:
            body = fh.read()
        assert body.startswith("<svg") and "viewBox" in body, f"{name}.svg is not usable SVG"


@test
def test_initgui_registers_the_icon_path_before_adding_the_workbench():
    """The workbench's own Icon resolves at registration time; a later addIconPath is too late."""
    with open(os.path.join(_WB, "InitGui.py"), encoding="utf-8") as fh:
        body = fh.read()
    assert "addIconPath" in body, "InitGui.py must register the icon directory"
    assert body.index("addIconPath") < body.index("addWorkbench("), (
        "addIconPath must come before addWorkbench, or ck-workbench cannot be found")


@test
def test_the_toolbar_spec_has_no_qt_import():
    """The spec must stay importable without a GUI, or it cannot be tested at all."""
    with open(os.path.join(_WB, "ckwb", "gui", "spec.py"), encoding="utf-8") as fh:
        body = fh.read()
    for forbidden in ("import FreeCADGui", "from PySide", "import PySide"):
        assert forbidden not in body, f"spec.py must not {forbidden}; it is the testable layer"


def main():
    passed, failed = 0, []
    for fn in TESTS:
        try:
            fn()
            passed += 1
            print(f"  PASS  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(fn.__name__)
            print(f"  FAIL  {fn.__name__}: {exc}")
            traceback.print_exc()
    print(f"\n{passed}/{passed + len(failed)} passed" + (f"; FAILED: {failed}" if failed else ""))
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
