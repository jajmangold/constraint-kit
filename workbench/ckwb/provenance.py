# -*- coding: utf-8 -*-
"""Where every value came from.

The workbench's central claim is that a generated model can be *trusted*, and trust is not a
feeling about the geometry — it is being able to point at any number and say who chose it. The
planner already has this information at the moment it resolves an under-specified request; the
old pipeline computed it and threw it away into a JSON response nobody read. Here it is a
first-class property of every parameter, carried into the document, the UI and the params file.

The rule that matters: **a guess must never be able to pass for a decision.** Everything else in
this module exists to make that enforceable rather than aspirational.

No FreeCAD import. This runs on any interpreter, which is what lets it be tested in
milliseconds and reused outside the workbench.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterator

STATED = "stated"        # a human said so
DEFAULT = "default"      # the planner or a generator signature guessed
STANDARD = "standard"    # resolved from a published standard, with a citation
DERIVED = "derived"      # computed from other values by a stated relation
SOLVED = "solved"        # chosen by a solver to satisfy a requirement
MEASURED = "measured"    # read off real hardware

PROVENANCES = (STATED, DEFAULT, STANDARD, DERIVED, SOLVED, MEASURED)

# Shown in the UI. Lightness differs as well as hue, so the groups are distinguishable without
# relying on colour alone -- and the labels are what the panel actually prints.
LABELS = {
    STATED: "You specified",
    DEFAULT: "Guessed for you",
    STANDARD: "From a standard",
    DERIVED: "Derived",
    SOLVED: "Solved",
    MEASURED: "Measured",
}
COLORS = {
    STATED: "#5B9FE8",
    DEFAULT: "#D99A3F",
    STANDARD: "#4FB3A3",
    DERIVED: "#A78BD4",
    SOLVED: "#C58FD0",
    MEASURED: "#8ED4C6",
}

#: Provenances that mean "nobody has actually decided this yet".
UNREVIEWED = (DEFAULT,)


@dataclass(frozen=True)
class Value:
    """One parameter value and its origin.

    `source` carries the citation for a STANDARD, the relation for a DERIVED value, and the
    requirement for a SOLVED one -- in each case the thing a reader would ask for next.
    `note` is prose for a human ("bearing width + 5 mm"); it is never parsed.
    """

    value: Any
    provenance: str
    source: str | None = None
    at: str | None = None
    confidence: float | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        if self.provenance not in PROVENANCES:
            raise ValueError(
                f"unknown provenance {self.provenance!r}; expected one of {PROVENANCES}")
        if self.confidence is not None and not (0.0 <= float(self.confidence) <= 1.0):
            raise ValueError(f"confidence must lie in [0, 1], got {self.confidence}")

    @property
    def unreviewed(self) -> bool:
        return self.provenance in UNREVIEWED

    @property
    def label(self) -> str:
        return LABELS[self.provenance]

    @property
    def color(self) -> str:
        return COLORS[self.provenance]

    def explain(self) -> str:
        """One line a human can read, for a tooltip or a terminal."""
        bits = [LABELS[self.provenance].lower()]
        if self.source:
            bits.append(f"from {self.source}")
        if self.at:
            bits.append(f"resolved {self.at}")
        if self.confidence is not None:
            bits.append(f"confidence {self.confidence:.2f}")
        if self.note:
            bits.append(f"- {self.note}")
        return " ".join(bits)


class ParamSet:
    """An ordered name -> Value mapping for one part, plus the questions the UI asks of it.

    Insertion-ordered on purpose: the order a generator declares its parameters is the order a
    human wants to read them, and a sorted panel scatters related dimensions.
    """

    def __init__(self, generator: str, values: dict[str, Value] | None = None):
        self.generator = str(generator)
        self._v: dict[str, Value] = dict(values or {})

    # -- mapping surface ------------------------------------------------------------------
    def __getitem__(self, name: str) -> Value:
        return self._v[name]

    def __contains__(self, name: object) -> bool:
        return name in self._v

    def __iter__(self) -> Iterator[str]:
        return iter(self._v)

    def __len__(self) -> int:
        return len(self._v)

    def items(self):
        return self._v.items()

    # -- building ------------------------------------------------------------------------
    def set(self, name: str, value: Any, provenance: str = STATED, **kw) -> "ParamSet":
        self._v[str(name)] = Value(value, provenance, **kw)
        return self

    def put(self, name: str, v: Value) -> "ParamSet":
        if not isinstance(v, Value):
            raise TypeError(f"put() takes a Value, got {type(v).__name__}")
        self._v[str(name)] = v
        return self

    def promote(self, name: str) -> "ParamSet":
        """Mark a guess as a decision. The value does not move; only who owns it does.

        This is the action behind the UI's "accept" on a guessed row, and it is the cheapest
        honest thing a user can do: it converts an unreviewed number into a reviewed one without
        pretending the geometry changed.
        """
        self._v[name] = replace(self._v[name], provenance=STATED)
        return self

    # -- questions the UI asks ------------------------------------------------------------
    def values(self) -> dict[str, Any]:
        """Plain values for a geometry generator. Provenance must not leak into the math."""
        return {k: v.value for k, v in self._v.items()}

    def counts(self) -> dict[str, int]:
        out = {p: 0 for p in PROVENANCES}
        for v in self._v.values():
            out[v.provenance] += 1
        return out

    def guessed(self) -> list[str]:
        return [k for k, v in self._v.items() if v.unreviewed]

    def summary(self) -> str:
        """The honesty line the property panel shows in its header."""
        return f"{len(self.guessed())} of {len(self._v)} guessed"

    def grouped(self) -> list[tuple[str, list[tuple[str, Value]]]]:
        """[(provenance, [(name, Value), ...]), ...] in display order, empty groups dropped."""
        out = []
        for p in PROVENANCES:
            rows = [(k, v) for k, v in self._v.items() if v.provenance == p]
            if rows:
                out.append((p, rows))
        return out

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ParamSet({self.generator!r}, {len(self._v)} params, {self.summary()})"
