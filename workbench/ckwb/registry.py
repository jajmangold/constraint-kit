# -*- coding: utf-8 -*-
"""The command registry — one vocabulary for the toolbar, the palette and agents.

The single architectural rule of this workbench: **everything the interface does is a command,
and agents call the same ones.** There is no button that reaches past the registry into the
document, because the moment one exists, an agent cannot reproduce what a human did and the two
drift apart. The toolbar is a thin layer of buttons that call `registry.call(...)`; the MCP
server is a thin layer of JSON that calls `registry.call(...)`.

Three properties follow from that and are each tested:

* **Arguments are validated, and unexpected ones are refused.** Silently dropping an argument is
  how an agent comes to believe it changed something it did not.
* **Writes are transactional.** A command that mutates the document opens an undo transaction and
  aborts it on any exception, so a failed multi-step edit leaves nothing half-applied.
* **The registry describes itself.** The MCP tool list is generated from it, so a command the
  registry cannot describe is a command no agent can reach -- which keeps the two in step by
  construction rather than by discipline.

No FreeCAD import: the transaction hooks are injected, which is what makes this testable on a
bare interpreter and keeps the registry usable headlessly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


class CommandError(ValueError):
    """The call was malformed: bad arguments, or a bad registration."""


class UnknownCommand(KeyError):
    """No such command. Raised rather than ignored, so a typo is never a silent no-op."""

    def __str__(self) -> str:  # KeyError would quote the message
        return self.args[0] if self.args else ""


# Python type -> JSON Schema type, for the generated tool list.
_JSON_TYPES = {str: "string", float: "number", int: "integer", bool: "boolean",
               list: "array", dict: "object"}


@dataclass
class Command:
    name: str
    summary: str
    fn: Callable[..., Any]
    params: dict[str, dict] = field(default_factory=dict)
    writes: bool = False

    def input_schema(self) -> dict:
        props, required = {}, []
        for pname, spec in self.params.items():
            ptype = spec.get("type", str)
            entry: dict[str, Any] = {"type": _JSON_TYPES.get(ptype, "string")}
            if spec.get("doc"):
                entry["description"] = spec["doc"]
            if "default" in spec:
                entry["default"] = spec["default"]
            if spec.get("enum"):
                entry["enum"] = list(spec["enum"])
            props[pname] = entry
            if spec.get("required"):
                required.append(pname)
        return {"type": "object", "properties": props, "required": required}


def _coerce(name: str, cmd: str, want: type, got: Any) -> Any:
    """Coerce where it is lossless and obvious; refuse otherwise."""
    if want is float and isinstance(got, bool):
        raise CommandError(f"{cmd}: {name} must be a number, got a boolean")
    if want is float and isinstance(got, int):
        return float(got)
    if want is float and isinstance(got, str):
        raise CommandError(f"{cmd}: {name} must be a number, got {got!r}")
    if want is int and isinstance(got, bool):
        raise CommandError(f"{cmd}: {name} must be an integer, got a boolean")
    if want is int and isinstance(got, float) and got.is_integer():
        return int(got)
    if not isinstance(got, want):
        raise CommandError(
            f"{cmd}: {name} must be {want.__name__}, got {type(got).__name__}")
    return got


class Registry:
    """A name -> Command table with validation and transaction handling."""

    def __init__(self) -> None:
        self._cmds: dict[str, Command] = {}
        self._open = self._commit = self._abort = None

    def transaction_hooks(self, *, open=None, commit=None, abort=None) -> None:
        """Inject the document's undo transaction. Unset = no transaction (headless, or tests)."""
        self._open, self._commit, self._abort = open, commit, abort

    def command(self, name: str, summary: str, *, params: dict | None = None,
                writes: bool = False):
        """Decorator. `params` maps argument name -> {type, required, default, doc, enum}."""
        if name in self._cmds:
            raise CommandError(f"command {name!r} is already registered")
        if not summary:
            raise CommandError(f"command {name!r} needs a summary; agents read it to choose")

        def deco(fn):
            self._cmds[name] = Command(name, summary, fn, dict(params or {}), writes)
            return fn

        return deco

    def __contains__(self, name: object) -> bool:
        return name in self._cmds

    def get(self, name: str, /) -> Command:
        try:
            return self._cmds[name]
        except KeyError:
            near = ", ".join(sorted(n for n in self._cmds if name[:4] in n)[:4])
            raise UnknownCommand(
                f"no command named {name!r}" + (f"; did you mean: {near}" if near else "")
            ) from None

    def names(self) -> list[str]:
        return sorted(self._cmds)

    def call(self, name: str, /, **kwargs) -> Any:
        """Invoke a command. `name` is POSITIONAL-ONLY, deliberately.

        With a normal signature, `call("set_param", name="t", value=1.0)` raises
        "got multiple values for argument 'name'" -- and `name` is about the most natural
        argument a CAD command can have. The `/` keeps the whole keyword namespace available to
        commands instead of reserving words from it.
        """
        cmd = self.get(name)

        unexpected = set(kwargs) - set(cmd.params)
        if unexpected:
            raise CommandError(
                f"{name}: unexpected argument(s) {', '.join(sorted(unexpected))}; "
                f"accepts {', '.join(sorted(cmd.params)) or 'none'}")

        args = {}
        for pname, spec in cmd.params.items():
            if pname in kwargs:
                args[pname] = _coerce(pname, name, spec.get("type", str), kwargs[pname])
            elif spec.get("required"):
                raise CommandError(f"{name}: missing required argument {pname!r}")
            elif "default" in spec:
                args[pname] = spec["default"]

        if not cmd.writes:
            return cmd.fn(**args)

        if self._open:
            self._open(name)
        try:
            out = cmd.fn(**args)
        except BaseException:
            if self._abort:
                self._abort()
            raise
        if self._commit:
            self._commit()
        return out

    def schema(self) -> list[dict]:
        """The MCP tool list. Stable order, so a diff of it is a real change."""
        return [{"name": c.name, "description": c.summary, "writes": c.writes,
                 "input_schema": c.input_schema()}
                for c in (self._cmds[n] for n in self.names())]


#: The process-wide registry. Commands register against this at import time.
registry = Registry()
