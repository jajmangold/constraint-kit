# -*- coding: utf-8 -*-
"""Requirements drive parameters, and the design says whether it meets them (issue #29).

Until this existed the workbench had the dependency running the wrong way. A parameter was a
number somebody typed, and the *reason* for the number lived in their head: change the belt
tension and nothing moves; change a plate and nothing notices the belt tension it was sized for.
The Requirements button said "not implemented yet", which is the one kind of interface element
worth less than nothing.

So there are three things, and the order matters:

* a **requirement** is a named value with a unit and a reason -- `belt_tension = 764 N, measured
  on the bench`
* a **derived** value is an expression over requirements -- `moment = load * span / 1000`
* a parameter can be **driven** by an expression, which makes the dependency real: edit the
  requirement and the geometry follows, with the chain in the document rather than in somebody's
  memory

And a fourth that turns intent into a verdict: a **target** on a measured quantity --
`max_disp_mm <= 1.0` -- is checked against the physics and comes back met or unmet. A requirement
nobody checks is a comment.

**Provenance survives the whole way down.** A driven parameter is tagged `derived`, never
`stated`, so the property editor shows it as something the design computed rather than something
a human chose, and the note names the expression it came from. A design where those cannot be
told apart is a design nobody can review.

The evaluator is `constraint_kit.parameters.safe_eval`, reused rather than rewritten: a strict
AST whitelist, never Python `eval`. That matters because an expression can arrive from a planner.
"""
from __future__ import annotations

import json
import keyword
import os
import re
import sys

import FreeCAD

from . import document as _doc
from .provenance import DERIVED

#: One hidden document property holds the whole requirement set, like the parameter metadata.
REQUIREMENTS_PROP = "CkRequirements"
GROUP = "Constraint Kit"

#: A name has to be usable in an expression, so it is a Python identifier and not a keyword.
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

OPS = {">=": lambda v, limit: v >= limit, "<=": lambda v, limit: v <= limit}


class RequirementError(ValueError):
    """A requirement set that cannot be resolved, or a target that cannot be checked.

    Raised rather than returning a partial answer: a design whose requirements half-resolve is in
    two states at once, and the half that worked is the dangerous half.
    """


def _safe_eval():
    """`constraint_kit.parameters.safe_eval`, imported the same way the physics bridge does.

    Reused rather than reimplemented. It is pure stdlib `ast` so it runs inside FreeCAD, and it
    is already the thing the kernel trusts with an expression that came from a language model.
    """
    try:
        from constraint_kit.parameters import safe_eval
        return safe_eval
    except ImportError:
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
        if root not in sys.path:
            sys.path.insert(0, root)
        try:
            from constraint_kit.parameters import safe_eval
            return safe_eval
        except ImportError as exc:
            raise RequirementError(
                "cannot import constraint_kit.parameters, which holds the safe expression "
                "evaluator. It is pure stdlib, so this is a path problem: set CK_REPO to the "
                "constraint-kit checkout.") from exc


# ------------------------------------------------------------------------------- the store

def _store(doc) -> dict:
    """The whole requirement set, as a plain dict. Created on first use."""
    holder = _holder(doc)
    try:
        raw = getattr(holder, REQUIREMENTS_PROP) or "{}"
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        data = {}
    data.setdefault("requirements", {})
    data.setdefault("driven", [])
    data.setdefault("targets", [])
    return data


def _holder(doc):
    """The object the requirement set lives on.

    A document property would be tidier and FreeCAD does not offer one that survives a save, so
    this uses a hidden `App::FeaturePython`-free plain object: one `App::DocumentObjectGroup`
    named once per document, carrying a single string property. Hidden from the tree, because it
    is bookkeeping rather than geometry.
    """
    name = "CkRequirementStore"
    obj = doc.getObject(name)
    if obj is None:
        obj = doc.addObject("App::FeaturePython", name)
        obj.Label = "Requirements"
        obj.addProperty("App::PropertyString", REQUIREMENTS_PROP, GROUP,
                        "the design's requirements, derived values and targets")
        setattr(obj, REQUIREMENTS_PROP, "{}")
        obj.setEditorMode(REQUIREMENTS_PROP, 2)      # authoritative, not hand-editable
        try:
            obj.ViewObject.Visibility = False
        except Exception:  # noqa: BLE001 - headless has no ViewObject
            pass
    return obj


def _write(doc, data: dict) -> None:
    setattr(_holder(doc), REQUIREMENTS_PROP,
            json.dumps(data, sort_keys=True, separators=(",", ":")))


def _check_name(name: str) -> str:
    text = str(name or "").strip()
    if not _NAME.match(text) or keyword.iskeyword(text):
        raise ValueError(
            f"a requirement name must be usable in an expression -- a plain identifier, not a "
            f"Python keyword -- and {name!r} is not. Use something like belt_tension or span.")
    return text


# -------------------------------------------------------------------- stated and derived

def set_requirement(doc, name: str, value: float, unit: str = "", note: str = "",
                    apply: bool = True) -> dict:
    """State a requirement. Applies the chain afterwards unless told not to.

    `apply=False` exists for the staleness test and for a caller setting several at once; the
    default applies, because a requirement that has been changed and not propagated leaves the
    design in two states.
    """
    key = _check_name(name)
    data = _store(doc)
    data["requirements"][key] = {
        "kind": "stated",
        "value": float(value),
        "unit": str(unit or ""),
        "note": str(note or ""),
    }
    _write(doc, data)
    if apply:
        apply_chain(doc)
    return data["requirements"][key]


def derive(doc, name: str, expression: str, unit: str = "", note: str = "",
           apply: bool = True) -> dict:
    """Define a value computed from other requirements by an explicit relation."""
    key = _check_name(name)
    text = str(expression or "").strip()
    if not text:
        raise ValueError(f"derived value {key!r} needs an expression")
    data = _store(doc)
    data["requirements"][key] = {
        "kind": "derived",
        "expression": text,
        "unit": str(unit or ""),
        "note": str(note or ""),
    }
    _write(doc, data)
    if apply:
        try:
            apply_chain(doc)
        except RequirementError:
            # Defining a value whose inputs do not exist yet is a normal intermediate state --
            # somebody is building the set up. It fails loudly at `solve()`, which is where a
            # caller is asking for an answer rather than recording an intention.
            pass
    return data["requirements"][key]


def requirements(doc) -> dict:
    """Every requirement, stated and derived, as stored."""
    out = {}
    for name, entry in _store(doc)["requirements"].items():
        item = dict(entry)
        if entry.get("kind") == "derived":
            item["depends_on"] = sorted(_free_names(entry["expression"]))
        out[name] = item
    return out


def _free_names(expression: str) -> set:
    """The names an expression reads. Uses the kernel's own extractor where available."""
    try:
        from constraint_kit.parameters import _free_names as kernel_names
        return set(kernel_names(expression))
    except Exception:  # noqa: BLE001
        import ast
        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError:
            return set()
        return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}


def solve(doc) -> dict:
    """Resolve every requirement to a number. Raises rather than half-answering.

    Repeated substitution to a fixpoint: a derived value may read another derived value, so one
    pass is not enough. A pass that resolves nothing new while work remains is a cycle, and it is
    named rather than iterated to a limit.
    """
    evaluate = _safe_eval()
    entries = _store(doc)["requirements"]
    values = {n: float(e["value"]) for n, e in entries.items() if e.get("kind") == "stated"}
    pending = {n: e for n, e in entries.items() if e.get("kind") == "derived"}

    # The whitelist's own function names must not be mistaken for requirements.
    builtin = {"min", "max", "abs", "round", "sqrt", "sin", "cos", "tan", "radians", "pi"}

    while pending:
        progressed = []
        for name, entry in list(pending.items()):
            needs = _free_names(entry["expression"]) - builtin
            missing = needs - set(values)
            if missing & set(pending):
                continue                       # another derived value first
            if missing:
                raise RequirementError(
                    "derived value %r reads %s, which no requirement defines. Defined: %s"
                    % (name, ", ".join(sorted(missing)), ", ".join(sorted(values)) or "none"))
            try:
                values[name] = float(evaluate(entry["expression"], dict(values)))
            except ValueError as exc:
                raise RequirementError(
                    "derived value %r has a bad expression %r: %s"
                    % (name, entry["expression"], exc)) from exc
            progressed.append(name)
        for name in progressed:
            pending.pop(name)
        if not progressed:
            raise RequirementError(
                "cannot resolve %s: they depend on each other in a cycle, so there is no order "
                "that computes them" % ", ".join(sorted(pending)))
    return values


# ------------------------------------------------------------------------ driven parameters

def drive(doc, part: str, param: str, expression: str, apply: bool = True) -> dict:
    """Bind a part's parameter to an expression over requirements.

    Validated against the real part now rather than at apply time, and the error names what does
    exist -- a binding that fails silently until the next edit is worse than one that fails here.
    """
    obj = doc.getObject(part)
    if obj is None:
        have = ", ".join(o.Name for o in _doc.all_parts(doc)) or "none"
        raise KeyError(f"no part named {part!r} in {doc.Name}; have {have}")
    names = sorted(name for name, _v in _doc.paramset_of(obj).items())
    if param not in names:
        raise KeyError(
            f"{part} has no parameter {param!r}; it has {', '.join(names)}")
    text = str(expression or "").strip()
    if not text:
        raise ValueError(f"driving {part}.{param} needs an expression")

    data = _store(doc)
    data["driven"] = [d for d in data["driven"]
                      if not (d["part"] == part and d["param"] == param)]
    data["driven"].append({"part": part, "param": param, "expression": text})
    data["driven"].sort(key=lambda d: (d["part"], d["param"]))
    _write(doc, data)
    if apply:
        apply_chain(doc)
    return {"part": part, "param": param, "expression": text}


def release(doc, part: str, param: str) -> dict:
    """Stop a parameter following a requirement. Its current value stays as it is."""
    data = _store(doc)
    before = len(data["driven"])
    data["driven"] = [d for d in data["driven"]
                      if not (d["part"] == part and d["param"] == param)]
    if len(data["driven"]) == before:
        raise KeyError(f"{part}.{param} is not driven by anything")
    _write(doc, data)
    return {"part": part, "param": param, "released": True}


def driven(doc) -> list:
    return [dict(d) for d in _store(doc)["driven"]]


def apply_chain(doc) -> dict:
    """Resolve the requirements and push every driven parameter into the geometry.

    The parameter is tagged `derived` and its note names the expression, so the property editor
    shows a computed value as computed. `set_param` would tag it `stated`, which would be a lie
    told by the tool about its own work.
    """
    values = solve(doc)
    changed = []
    for binding in _store(doc)["driven"]:
        obj = doc.getObject(binding["part"])
        if obj is None:
            continue
        evaluate = _safe_eval()
        try:
            value = float(evaluate(binding["expression"], dict(values)))
        except ValueError as exc:
            raise RequirementError(
                "cannot evaluate %r for %s.%s: %s"
                % (binding["expression"], binding["part"], binding["param"], exc)) from exc
        current = _doc.paramset_of(obj)[binding["param"]].value
        _set_derived(obj, binding["param"], value, binding["expression"])
        if abs(float(current) - value) > 1e-12:
            changed.append("%s.%s = %g" % (binding["part"], binding["param"], value))
    doc.recompute()
    return {"values": values, "changed": changed}


def _set_derived(obj, param: str, value: float, expression: str) -> None:
    """Write a value and tag it DERIVED, naming the relation it came from."""
    meta = _doc._meta(obj)                                  # noqa: SLF001 - same package
    if param not in meta:
        raise KeyError(f"{obj.Name} has no parameter {param!r}; have {sorted(meta)}")
    pname = meta[param].get("prop") or _doc.prop_name(param)
    current = getattr(obj, pname)
    setattr(obj, pname, type(current)(value) if not isinstance(value, type(current)) else value)
    meta[param]["provenance"] = DERIVED
    meta[param]["note"] = "driven by the requirement expression %r" % expression
    _doc._write_meta(obj, meta)                             # noqa: SLF001
    try:
        from .provenance import LABELS
        obj.setGroupOfProperty(pname, LABELS[DERIVED])
    except Exception:  # noqa: BLE001
        pass


def stale(doc) -> tuple:
    """Which driven parameters no longer match their expression. `(bool, [descriptions])`.

    An edited requirement that has not been applied leaves the design in two states at once, so
    this is reported rather than quietly corrected on read -- a getter with a side effect on the
    geometry is a trap.
    """
    try:
        values = solve(doc)
    except RequirementError:
        return True, ["the requirement set does not currently resolve"]
    out = []
    evaluate = _safe_eval()
    for binding in _store(doc)["driven"]:
        obj = doc.getObject(binding["part"])
        if obj is None:
            out.append("%s is gone but still driven" % binding["part"])
            continue
        try:
            want = float(evaluate(binding["expression"], dict(values)))
        except ValueError:
            out.append("%s.%s has an expression that no longer evaluates"
                       % (binding["part"], binding["param"]))
            continue
        have = float(_doc.paramset_of(obj)[binding["param"]].value)
        if abs(have - want) > 1e-9:
            out.append("%s.%s is %g but its expression gives %g"
                       % (binding["part"], binding["param"], have, want))
    return bool(out), out


# -------------------------------------------------------------------------------- targets

def target(doc, name: str, *, metric: str, op: str, limit: float, note: str = "") -> dict:
    """Record a requirement on a MEASURED quantity, to be checked against the physics."""
    if op not in OPS:
        raise ValueError(
            f"a target operator must be {' or '.join(sorted(OPS))}, got {op!r}. An equality "
            f"target needs a tolerance, and that tolerance is the caller's decision.")
    text = str(name or "").strip()
    if not text:
        raise ValueError("a target needs a name, so a verdict can say which one failed")
    data = _store(doc)
    data["targets"] = [t for t in data["targets"] if t["name"] != text]
    data["targets"].append({"name": text, "metric": str(metric), "op": op,
                            "limit": float(limit), "note": str(note or "")})
    data["targets"].sort(key=lambda t: t["name"])
    _write(doc, data)
    return data["targets"][-1]


def targets(doc) -> list:
    return [dict(t) for t in _store(doc)["targets"]]


def verify(doc, *, part: str, **analysis) -> dict:
    """Check every target against a real analysis of `part`. Met or unmet, with the number.

    `analysis` is passed straight to `physics.stiffness`, because choosing the load case IS the
    engineering and this is not the place to start guessing it. A target naming a metric the
    analysis does not report is refused rather than skipped: a target silently not checked is
    indistinguishable from one that passed.
    """
    from . import physics as _physics

    obj = doc.getObject(part)
    if obj is None:
        have = ", ".join(o.Name for o in _doc.all_parts(doc)) or "none"
        raise KeyError(f"no part named {part!r} in {doc.Name}; have {have}")

    wanted = targets(doc)
    if not wanted:
        return {"ok": True, "verdicts": [], "note": "no targets have been recorded"}

    measured = _physics.stiffness(obj, **analysis)
    verdicts = []
    for spec in wanted:
        if spec["metric"] not in measured:
            raise RequirementError(
                "target %r is on %r, which this analysis does not report. It reports: %s"
                % (spec["name"], spec["metric"],
                   ", ".join(sorted(k for k, v in measured.items()
                                    if isinstance(v, (int, float))))))
        value = measured[spec["metric"]]
        if value is None:
            verdicts.append({**spec, "measured": None, "met": None,
                             "note": "the analysis reported no value for this metric"})
            continue
        verdicts.append({**spec, "measured": float(value),
                         "met": bool(OPS[spec["op"]](float(value), spec["limit"]))})
    return {
        "ok": all(v["met"] for v in verdicts if v["met"] is not None),
        "part": part,
        "verdicts": verdicts,
        "analysis": {k: v for k, v in measured.items()
                     if isinstance(v, (int, float)) or v is None},
    }


#: `apply` is what a caller means; `apply_chain` is what it does. Both names, one function,
#: because "apply the requirements" is the sentence and the longer name is the explanation.
apply = apply_chain
