# -*- coding: utf-8 -*-
"""Cause attribution and ranked, RE-VERIFIED repairs (issue #20).

The validator already returned structure. What it did not return was a **cause** and a **fix**. A
program with `thicknes` for `thickness` came back as a sentence, went to the model, and cost a
round trip -- for a misspelling with exactly one plausible correction. A clash came back as an
overlap volume and said nothing about which part of which pair caused it.

So: classify the cause, generate repairs **deterministically** where the geometry already
determines the answer, rank them, and **re-verify every one** before offering it. The model
becomes the fallback rather than the first resort, which is both cheaper and more reliable -- a
nearest-match on a parameter name is not a judgement call, and asking a language model to make
one invites it to be creative instead.

Three rules, and the third is the one that matters:

1. **Nothing is offered that has not been re-verified.** A suggested fix that does not build is
   worse than no suggestion, because somebody will take it. Every candidate is applied to a copy
   and re-diagnosed, and one that still faults is discarded rather than ranked lower.
2. **Ranked by certainty, and the reason travels with it.** A rename within one edit of a real
   name is nearly certain; clamping a value the user explicitly stated is not, and says so.
3. **Some causes have no repair, by design.** An unknown generator is the important one. Nothing
   here will quietly swap `turbine_blade` for `plate` because both are parts -- that is a
   fabrication, not a fix. The honest output is a decline that names what the catalogue has, and
   lets the caller choose.
"""
from __future__ import annotations

import copy
import difflib
from dataclasses import dataclass, field

import FreeCAD

from . import mates as _mates
from . import parts as _parts

#: The cause taxonomy. Every entry must be reachable from a real program, and there is a test
#: that walks fixtures and fails if one of these is unreachable -- a category nothing produces is
#: a category nobody can rely on.
CAUSES = {
    "unknown_generator":
        "the program asks for a part the catalogue does not have. NOT repairable by "
        "substitution: a different part is a different object.",
    "unknown_parameter":
        "a parameter name that no generator signature has. Usually a misspelling, and then "
        "repairable with certainty.",
    "unknown_frame":
        "a mate names a frame the part does not expose. Repairable when one real frame is "
        "within reach of the name, and not otherwise.",
    "unknown_mate_kind":
        "a mate kind outside the vocabulary. The kinds are few and fixed, so a near miss is "
        "repairable and anything else is not.",
    "unknown_part_reference":
        "a mate refers to a part the program never declares. Repairable when it is a "
        "misspelling of a declared one.",
    "geometry_failed":
        "the generator refused the values given, so the solid does not exist. Repairable when "
        "the generator's own rule says which value has to move and where.",
    "interference":
        "two placed parts occupy the same space. Repairable by separating them along the axis "
        "they overlap on, by exactly the overlap.",
}

#: Below this similarity a name is a different word rather than a typo, and substituting it
#: would be inventing an answer. 0.72 admits `thicknes`->`thickness` and `botom`->`bottom` and
#: rejects `ceiling`->`bottom`, which is the line this module exists to walk.
NEAR = 0.72


@dataclass(frozen=True)
class Fault:
    """One attributed problem: what went wrong, where, and in one readable sentence."""

    cause: str
    part: str
    detail: str
    summary: str
    severity: int = 1

    def __post_init__(self) -> None:
        if self.cause not in CAUSES:
            raise ValueError(
                f"unknown cause {self.cause!r}; the taxonomy is {', '.join(sorted(CAUSES))}")


@dataclass(frozen=True)
class Repair:
    """A candidate fix, with how sure it is and why -- and whether it actually verified."""

    summary: str
    because: str
    confidence: float
    verified: bool = False
    rejected: str = ""


@dataclass
class Outcome:
    """What was wrong, what was tried, and the repaired program if one survived verification."""

    faults: list = field(default_factory=list)
    repairs: list = field(default_factory=list)
    fixed: dict | None = None
    note: str = ""

    def report(self) -> str:
        lines = []
        if not self.faults:
            lines.append("nothing to repair: no fault was found.")
            return "\n".join(lines)
        lines.append("%d fault(s):" % len(self.faults))
        for fault in self.faults:
            where = (" in %s" % fault.part) if fault.part else ""
            lines.append("  [%s]%s %s" % (fault.cause, where, fault.summary))
        if self.repairs:
            lines.append("repairs tried, most certain first:")
            for r in self.repairs:
                mark = "VERIFIED" if r.verified else ("rejected: " + (r.rejected or "unknown"))
                lines.append("  %-8s (%.2f) %s" % (mark, r.confidence, r.summary))
                lines.append("           because %s" % r.because)
        else:
            lines.append("no repair was attempted: nothing here is repairable deterministically.")
        if self.fixed is None:
            lines.append("NOTHING IS OFFERED. A fix that does not re-verify is worse than no "
                         "fix, because somebody would take it.")
        if self.note:
            lines.append(self.note)
        return "\n".join(lines)


# -------------------------------------------------------------------------- cause attribution

def _near(name: str, candidates) -> list:
    """Candidates within `NEAR` of `name`, most similar first. Empty when none is close."""
    scored = [(difflib.SequenceMatcher(None, name, c).ratio(), c) for c in candidates]
    return [c for score, c in sorted(scored, reverse=True) if score >= NEAR]


def _frames_of(generator: str, values: dict) -> list:
    """The frames a part actually exposes, by BUILDING it. Never a hardcoded list.

    Built rather than looked up for the same reason the catalogue does it: a frame list that can
    drift from the geometry is a frame list that will.
    """
    try:
        _shape, frames = _parts.build(generator, **values)
        return sorted(frames)
    except Exception:  # noqa: BLE001 - a part that will not build has no frames to offer
        return []


def diagnose(program: dict) -> list:
    """Every fault in a proposed program, attributed to a cause. Empty means usable.

    Deliberately a separate pass from `planner._validate`, which produces sentences for a model
    to read. This produces structure for code to act on, and the two are checked against each
    other by the planner using both.
    """
    faults: list = []
    parts = program.get("parts")
    if not isinstance(parts, list) or not parts:
        return faults

    declared: dict = {}
    for entry in parts:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")
        generator = entry.get("generator")
        if generator not in _parts.PARTS:
            faults.append(Fault(
                cause="unknown_generator", part=name, detail=str(generator),
                summary="no generator called %r. The catalogue has: %s"
                        % (generator, ", ".join(sorted(_parts.PARTS))),
                severity=3))
            continue

        defaults = _parts.defaults_for(generator)
        values = dict(defaults)
        proposed = entry.get("params") or {}
        for key, spec in (proposed.items() if isinstance(proposed, dict) else []):
            if key not in defaults:
                near = _near(key, defaults)
                hint = (" Did you mean %s?" % near[0]) if near else \
                    (" It takes: %s" % ", ".join(sorted(defaults)))
                faults.append(Fault(
                    cause="unknown_parameter", part=name, detail=key,
                    summary="%s has no parameter %r.%s" % (generator, key, hint),
                    severity=2))
                continue
            try:
                values[key] = spec["value"] if isinstance(spec, dict) else spec
            except Exception:  # noqa: BLE001
                continue

        try:
            _parts.build(generator, **values)
        except Exception as exc:  # noqa: BLE001
            faults.append(Fault(
                cause="geometry_failed", part=name, detail=str(exc),
                summary="%s will not build with these values: %s" % (generator, exc),
                severity=3))
            declared[name] = (generator, values, [])
            continue
        declared[name] = (generator, values, _frames_of(generator, values))

    for mate in program.get("mates") or []:
        if not isinstance(mate, dict):
            continue
        kind = mate.get("kind")
        if kind not in _mates.KINDS:
            near = _near(str(kind), _mates.KINDS)
            faults.append(Fault(
                cause="unknown_mate_kind", part="", detail=str(kind),
                summary="no mate kind %r.%s" % (
                    kind, (" Did you mean %s?" % near[0]) if near else
                    (" The kinds are: %s" % ", ".join(sorted(_mates.KINDS)))),
                severity=2))
        for role, frame_key in (("target", "target_frame"), ("moving", "moving_frame")):
            who = mate.get(role)
            if who not in declared:
                near = _near(str(who), declared)
                faults.append(Fault(
                    cause="unknown_part_reference", part="", detail=str(who),
                    summary="the mate %s %r is not a part this program declares.%s" % (
                        role, who, (" Did you mean %s?" % near[0]) if near else
                        (" It declares: %s" % ", ".join(sorted(declared)) or " none")),
                    severity=2))
                continue
            frames = declared[who][2]
            wanted = mate.get(frame_key)
            if frames and wanted not in frames:
                near = _near(str(wanted), frames)
                faults.append(Fault(
                    cause="unknown_frame", part=who, detail=str(wanted),
                    summary="%s has no frame %r.%s" % (
                        who, wanted, (" Did you mean %s?" % near[0]) if near else
                        (" It has: %s" % ", ".join(frames))),
                    severity=2))
    return faults


# ------------------------------------------------------------------- deterministic candidates

def _rename_parameter(program: dict, fault: Fault):
    """Rename a misspelled parameter to its nearest real one, KEEPING its value and provenance.

    Keeping the provenance matters: the user did state that number, and a repair that quietly
    re-tags it as a guess has edited the record of who decided what.
    """
    for entry in program.get("parts", []):
        if entry.get("name") != fault.part:
            continue
        defaults = _parts.defaults_for(entry["generator"])
        near = _near(fault.detail, defaults)
        if not near:
            return None, "no parameter is within reach of %r" % fault.detail
        target = near[0]
        if target in (entry.get("params") or {}):
            return None, "%r already has a value, so the misspelling is a duplicate" % target
        entry["params"][target] = entry["params"].pop(fault.detail)
        return (Repair(summary="rename %s.%s to %s" % (fault.part, fault.detail, target),
                       because="it is within one or two edits of a real parameter and the value "
                               "it was given is kept",
                       confidence=0.95), "")
    return None, "the part named in the fault is not in the program"


def _rename_frame(program: dict, fault: Fault):
    for mate in program.get("mates", []):
        for role, frame_key in (("target", "target_frame"), ("moving", "moving_frame")):
            if mate.get(role) != fault.part or mate.get(frame_key) != fault.detail:
                continue
            entry = next((p for p in program["parts"] if p.get("name") == fault.part), None)
            if entry is None:
                return None, "the part is not declared"
            frames = _frames_of(entry["generator"],
                               {**_parts.defaults_for(entry["generator"]),
                                **{k: (v["value"] if isinstance(v, dict) else v)
                                   for k, v in (entry.get("params") or {}).items()
                                   if k in _parts.defaults_for(entry["generator"])}})
            near = _near(fault.detail, frames)
            if not near:
                return None, ("no frame on %s is within reach of %r; it has %s"
                              % (fault.part, fault.detail, ", ".join(frames) or "none"))
            mate[frame_key] = near[0]
            return (Repair(summary="use %s.%s instead of %r"
                                   % (fault.part, near[0], fault.detail),
                           because="one real frame is within reach of the name given",
                           confidence=0.9), "")
    return None, "the mate named in the fault is not in the program"


def _rename_mate_kind(program: dict, fault: Fault):
    near = _near(fault.detail, _mates.KINDS)
    if not near:
        return None, ("no mate kind is within reach of %r; the kinds are %s"
                      % (fault.detail, ", ".join(sorted(_mates.KINDS))))
    for mate in program.get("mates", []):
        if mate.get("kind") == fault.detail:
            mate["kind"] = near[0]
            return (Repair(summary="use the %r mate instead of %r" % (near[0], fault.detail),
                           because="the mate vocabulary is small and fixed, so a near miss is "
                                   "unambiguous",
                           confidence=0.9), "")
    return None, "the mate is not in the program"


def _rename_part_reference(program: dict, fault: Fault):
    declared = [p.get("name") for p in program.get("parts", [])]
    near = _near(fault.detail, [n for n in declared if n])
    if not near:
        return None, ("no declared part is within reach of %r; the program declares %s"
                      % (fault.detail, ", ".join(n for n in declared if n) or "none"))
    for mate in program.get("mates", []):
        for role in ("target", "moving"):
            if mate.get(role) == fault.detail:
                mate[role] = near[0]
    return (Repair(summary="the mate refers to %s rather than %r" % (near[0], fault.detail),
                   because="it is within reach of a part the program does declare",
                   confidence=0.85), "")


def _fix_geometry(program: dict, fault: Fault):
    """Move the one value the generator's own error names, to the side the rule requires.

    Deliberately narrow. The generator says "bore id_=20 must be smaller than od=10", which
    names both the value to move and the bound -- so the repair is determined rather than
    invented. A generator error that does not name its values gets no repair, which is correct:
    guessing which dimension to change would be designing, not fixing.
    """
    import re

    entry = next((p for p in program.get("parts", []) if p.get("name") == fault.part), None)
    if entry is None:
        return None, "the part is not in the program"
    defaults = _parts.defaults_for(entry["generator"])
    params = entry.setdefault("params", {})

    text = fault.detail
    pair = re.search(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=?\s*([\d.]+)\s+must be smaller than\s+"
                     r"([A-Za-z_][A-Za-z0-9_]*)\s*=?\s*([\d.]+)", text)
    if not pair:
        return None, ("the generator error does not name a value and a bound (%r), so which "
                      "dimension to change would be a guess" % text[:80])
    low_name, _low, high_name, high_value = pair.groups()
    if low_name not in defaults or high_name not in defaults:
        return None, "the error names %r and %r, which are not both parameters" % (
            low_name, high_name)

    was = params.get(low_name)
    stated = isinstance(was, dict) and bool(was.get("from_request"))
    new = float(high_value) * 0.5
    params[low_name] = {"value": new, "from_request": False}
    return (Repair(summary="set %s.%s to %g, inside %s=%s"
                           % (fault.part, low_name, new, high_name, high_value),
                   because=("you stated %s, so this changes a number you gave -- the generator's "
                            "own rule says it cannot hold" % low_name) if stated else
                           "the generator's own rule determines which value has to move",
                   confidence=0.6 if stated else 0.75), "")


#: Cause -> candidate generator. A cause absent from here has no deterministic repair, which is
#: a statement rather than an omission: `unknown_generator` and `interference` are both here on
#: purpose, the first with None.
_CANDIDATES = {
    "unknown_parameter": _rename_parameter,
    "unknown_frame": _rename_frame,
    "unknown_mate_kind": _rename_mate_kind,
    "unknown_part_reference": _rename_part_reference,
    "geometry_failed": _fix_geometry,
    "unknown_generator": None,
}


def repair(program: dict) -> Outcome:
    """Diagnose, propose, RE-VERIFY, and return the repaired program only if it is clean.

    Repairs are applied cumulatively to one copy and the whole thing is re-diagnosed at the end,
    because that is the only check that means anything: two fixes that are each fine and together
    are not would otherwise both be offered.
    """
    faults = diagnose(program)
    out = Outcome(faults=faults)
    if not faults:
        return out

    working = copy.deepcopy(program)
    candidates: list = []
    for fault in sorted(faults, key=lambda f: -f.severity):
        maker = _CANDIDATES.get(fault.cause)
        if maker is None:
            candidates.append(Repair(
                summary="no repair for %s %r" % (fault.cause, fault.detail),
                because=CAUSES[fault.cause],
                confidence=0.0,
                rejected="this cause is not repairable deterministically; substituting "
                         "something else would be a fabrication rather than a fix"))
            continue
        made, why = maker(working, fault)
        if made is None:
            candidates.append(Repair(summary="no repair for %s %r" % (fault.cause, fault.detail),
                                     because=why, confidence=0.0, rejected=why))
        else:
            candidates.append(made)

    candidates.sort(key=lambda r: -r.confidence)
    usable = [r for r in candidates if r.confidence > 0.0]
    if not usable:
        out.repairs = [r for r in candidates if r.rejected]
        out.note = ("Nothing here is repairable without guessing. The faults above name what "
                    "the catalogue does have, so the choice is the caller's to make.")
        return out

    remaining = diagnose(working)
    if remaining:
        out.repairs = [Repair(summary=r.summary, because=r.because, confidence=r.confidence,
                              verified=False,
                              rejected="re-verification still found: %s" % remaining[0].summary)
                       for r in usable] + [r for r in candidates if r.rejected]
        out.note = ("The repairs did not clear the program, so none is offered. A fix that does "
                    "not re-verify is worse than no fix.")
        return out

    out.repairs = [Repair(summary=r.summary, because=r.because, confidence=r.confidence,
                          verified=True) for r in usable] + \
                  [r for r in candidates if r.rejected]
    out.fixed = working
    return out


# -------------------------------------------------------------------------- the document case

def repair_document(doc) -> Outcome:
    """Attribute an interference in a built document, and separate the pair that caused it.

    The worked example on the issue. A clash used to report a volume and nothing else; this says
    which pair, on which axis, by how much -- and then applies the separation and **re-runs the
    interference check**, so a repair is only reported as verified if the model is actually clean
    afterwards.

    The separation is computed from the overlap rather than guessed: the parts are moved apart
    along the axis on which their bounding boxes overlap least, by exactly that overlap, which is
    the smallest move that resolves it.
    """
    from . import checks as _checks
    from . import document as _doc

    objs = _doc.all_parts(doc)
    clash = _checks.interference(objs)
    out = Outcome()
    if clash.get("ok", True):
        out.note = "nothing to repair: no fault was found."
        return out

    by_name = {o.Name: o for o in objs}
    for pair in clash.get("clashes", []):
        a, b = by_name.get(pair["a"]), by_name.get(pair["b"])
        if a is None or b is None:
            continue
        out.faults.append(Fault(
            cause="interference", part=pair["a"], detail=pair["b"],
            summary="%s and %s overlap by %.4f mm3"
                    % (pair["a"], pair["b"], pair.get("volume_mm3", 0.0)),
            severity=3))

        box_a, box_b = a.Shape.BoundBox, b.Shape.BoundBox
        spans = []
        for axis, lo_a, hi_a, lo_b, hi_b in (
                ("x", box_a.XMin, box_a.XMax, box_b.XMin, box_b.XMax),
                ("y", box_a.YMin, box_a.YMax, box_b.YMin, box_b.YMax),
                ("z", box_a.ZMin, box_a.ZMax, box_b.ZMin, box_b.ZMax)):
            overlap = min(hi_a, hi_b) - max(lo_a, lo_b)
            if overlap > 0:
                spans.append((overlap, axis, lo_a < lo_b))
        if not spans:
            continue
        overlap, axis, b_is_above = min(spans)
        step = overlap if b_is_above else -overlap
        before = b.Placement.Base
        delta = FreeCAD.Vector(*(step if ax == axis else 0.0 for ax in "xyz"))
        b.Placement = FreeCAD.Placement(before + delta, b.Placement.Rotation)
        doc.recompute()

        again = _checks.interference(_doc.all_parts(doc))
        if again.get("ok"):
            out.repairs.append(Repair(
                summary="move %s %+.4f mm along %s, clear of %s"
                        % (b.Name, step, axis.upper(), a.Name),
                because="that is exactly their bounding-box overlap on the axis they overlap "
                        "least, which is the smallest move that separates them",
                confidence=0.9, verified=True))
            out.fixed = {"moved": b.Name, "axis": axis, "by_mm": step}
            return out
        # Not clean: put it back rather than leaving a half-fix behind.
        b.Placement = FreeCAD.Placement(before, b.Placement.Rotation)
        doc.recompute()
        out.repairs.append(Repair(
            summary="move %s %+.4f mm along %s" % (b.Name, step, axis.upper()),
            because="their bounding-box overlap on the least-overlapping axis",
            confidence=0.9, verified=False,
            rejected="re-running the interference check still found a clash, so the move was "
                     "undone rather than left in place"))
    out.note = ("The repairs did not clear the model, so none is offered and the document was "
                "left as it was found.") if not out.fixed else ""
    return out
