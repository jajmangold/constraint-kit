# -*- coding: utf-8 -*-
"""Plain English in, a reviewable program out. The model never touches geometry.

The doctrine, in one sentence: **the model chooses which parts, which values and what connects
to what; the kernel builds it.** So this module's output is a `Plan` — a data structure a human
reads and approves — and `build()` is a separate, explicit step. Nothing in here can place a
vertex even if the model asked it to.

Four properties are the whole point, and each is tested with the model call injected:

* **Validated before it builds.** An unknown generator, parameter or frame is refused, with a
  message naming what actually exists. Frames are checked by *building the proposed part* rather
  than against a hardcoded list, so the check cannot drift from the geometry.
* **One repair attempt.** A refusal is handed back to the model once, which is the
  execute-and-repair pattern the 2026 text-to-CAD work converged on. Once, not a loop: a model
  that cannot fix its own program given the exact error will not fix it on the fifth try either,
  and an unbounded loop spends money proving that.
* **A decline is a first-class answer.** "I cannot model that" passes through untouched and is
  never retried, because retrying an honest refusal is how a refusal gets talked out of.
* **Provenance survives.** A value the request stated becomes `stated`; a value the model chose
  becomes `default` and shows up as a guess in the property editor. The planner does not get to
  launder its own guesses into decisions.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import document as _doc
from . import mates as _mates
from . import parts as _parts
from .provenance import DEFAULT, STATED, ParamSet

MAX_ATTEMPTS = 2       # the first try, plus exactly one repair

SYSTEM = """You turn a description of a mechanical part or assembly into a PROGRAM that a
deterministic CAD kernel will build. You never produce geometry, coordinates or vertices.

Reply with JSON only, in one of two shapes.

To build something:
{"parts": [{"name": "<CamelCase document name>", "generator": "<from the catalogue>",
            "params": {"<param>": {"value": <number or string>, "from_request": <bool>}}}],
 "mates": [{"kind": "seat|align", "target": "<part name>", "target_frame": "<frame>",
            "moving": "<part name>", "moving_frame": "<frame>"}],
 "notes": "<anything the user should know, especially what you approximated>"}

To decline, which is a perfectly good answer:
{"unsupported": "<what cannot be modelled, specifically>", "nearest": "<the closest thing you
 CAN build, or omit this key>"}

Rules that matter:
- Only use generators and parameters from the catalogue. Never invent either.
- Omit any parameter you have no reason to change; the generator's own default is used and is
  correctly recorded as a guess.
- Set "from_request": true ONLY when the user's words actually gave you that value. This is how
  the interface shows which numbers are theirs and which are yours. Do not claim their authority.
- "seat" puts two faces in contact. "align" makes two frames share an axis. Mates are applied in
  order and each one moves its "moving" part, so a part must already be placed before it can be
  a "target". The first part listed is the grounded one.
- If the request needs something the catalogue cannot express, DECLINE and say precisely what is
  missing. A plausible wrong part is worse than no part. Do not substitute silently; if you
  approximate, say so in "notes".
"""


class PlanError(ValueError):
    """The model's program could not be used, or a declined plan was asked to build."""


@dataclass
class Plan:
    """A proposed program: what a human reviews before pressing Build."""

    request: str = ""
    parts: list = field(default_factory=list)
    mates: list = field(default_factory=list)
    notes: str = ""
    declined: bool = False
    reason: str = ""
    nearest: str = ""
    raw: str = ""
    attempts: int = 1

    def paramset(self, part_name: str) -> ParamSet:
        """The full ParamSet for one part: the model's values over the generator's defaults.

        Anything the model did not state comes from the generator signature and is tagged a
        guess, so the count of guesses is honest rather than flattering.
        """
        for entry in self.parts:
            if entry["name"] != part_name:
                continue
            generator = entry["generator"]
            params = ParamSet(generator)
            proposed = entry.get("params", {})
            for key, default in _parts.defaults_for(generator).items():
                if key in proposed:
                    spec = proposed[key]
                    value = _coerce(spec["value"], default)
                    stated = bool(spec.get("from_request"))
                    params.set(key, value, STATED if stated else DEFAULT,
                               note=None if stated else "chosen by the planner")
                else:
                    params.set(key, default, DEFAULT, note="generator default")
            return params
        raise KeyError(f"no part named {part_name!r} in this plan; "
                       f"have {[p['name'] for p in self.parts]}")

    def summary(self) -> str:
        total = guessed = 0
        for entry in self.parts:
            ps = self.paramset(entry["name"])
            total += len(ps)
            guessed += len(ps.guessed())
        return f"{guessed} of {total} guessed"

    def render(self) -> str:
        """The program as text, for the panel and the terminal. Provenance is visible."""
        if self.declined:
            out = [f"Declined: {self.reason}"]
            if self.nearest:
                out.append(f"Nearest thing I can build: {self.nearest}")
            return "\n".join(out)

        lines = []
        for entry in self.parts:
            ps = self.paramset(entry["name"])
            lines.append(f"{entry['name']}  =  {entry['generator']}")
            for key, value in ps.items():
                mark = "stated " if value.provenance == STATED else "guessed"
                lines.append(f"      {mark}  {key} = {value.value}")
        for mate in self.mates:
            lines.append(f"{mate['kind']}  {mate['target']}.{mate['target_frame']}"
                         f"  <-  {mate['moving']}.{mate['moving_frame']}")
        if self.notes:
            lines.append("")
            lines.append(self.notes)
        lines.append("")
        lines.append(self.summary())
        return "\n".join(lines)


def _coerce(value, default):
    """Match the model's JSON scalar to the generator's own parameter type."""
    if isinstance(default, bool):
        return bool(value)
    if isinstance(default, int) and not isinstance(default, bool):
        return int(round(float(value)))
    if isinstance(default, float):
        return float(value)
    return str(value)


def catalogue() -> dict:
    """Every generator, its parameter defaults, its one-line doc and its frame names.

    Frames come from actually building each part with its defaults, so the catalogue cannot claim
    a frame the geometry does not have.
    """
    out = {}
    for name in sorted(_parts.PARTS):
        defaults = _parts.defaults_for(name)
        try:
            _, frames = _parts.build(name, **defaults)
            frame_names = sorted(frames)
        except Exception:  # noqa: BLE001 - a generator that cannot build its own defaults
            frame_names = []
        doc = (_parts.PARTS[name].__doc__ or "").strip().split("\n")[0]
        out[name] = {"params": defaults, "frames": frame_names, "doc": doc}
    return out


def _prompt(request: str, catalogue_json: str) -> list:
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content":
            f"CATALOGUE (the only generators, parameters and frames that exist):\n"
            f"{catalogue_json}\n\n"
            f"MATE KINDS: {sorted(_mates.KINDS)}\n\n"
            f"REQUEST: {request}"},
    ]


_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


def _parse(text: str) -> dict:
    """Pull JSON out of a reply, tolerating a code fence or surrounding chatter."""
    candidate = text.strip()
    fenced = _FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    start, end = candidate.find("{"), candidate.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(candidate[start:end + 1])
        except json.JSONDecodeError as exc:
            raise PlanError(f"the reply was not valid JSON: {exc}") from exc
    raise PlanError("the reply contained no JSON object")


def _validate(raw: dict) -> list:
    """Every problem with a proposed program, as readable sentences. Empty means usable."""
    errors: list[str] = []
    parts = raw.get("parts")
    if not isinstance(parts, list) or not parts:
        return ["the program lists no parts"]

    built: dict[str, list] = {}
    for i, entry in enumerate(parts):
        if not isinstance(entry, dict):
            errors.append(f"part {i} is not an object")
            continue
        name, generator = entry.get("name"), entry.get("generator")
        if not name or not isinstance(name, str) or not name.isidentifier():
            errors.append(f"part {i} has an unusable name {name!r}; use a CamelCase identifier")
            continue
        if name in built:
            errors.append(f"two parts are both called {name!r}")
            continue
        if generator not in _parts.PARTS:
            errors.append(
                f"{name}: no generator called {generator!r}. "
                f"The catalogue has: {', '.join(sorted(_parts.PARTS))}")
            built[name] = []
            continue

        defaults = _parts.defaults_for(generator)
        values = dict(defaults)
        proposed = entry.get("params") or {}
        if not isinstance(proposed, dict):
            errors.append(f"{name}: params must be an object")
            proposed = {}
        for key, spec in proposed.items():
            if key not in defaults:
                errors.append(
                    f"{name}: {generator} has no parameter {key!r}. "
                    f"It accepts: {', '.join(sorted(defaults))}")
                continue
            if not isinstance(spec, dict) or "value" not in spec:
                errors.append(f"{name}.{key} must be {{\"value\": ..., \"from_request\": ...}}")
                continue
            try:
                values[key] = _coerce(spec["value"], defaults[key])
            except (TypeError, ValueError):
                errors.append(f"{name}.{key}: {spec['value']!r} is not a usable value")

        # Build it. This is what makes the frame check real rather than a list to maintain.
        try:
            _, frames = _parts.build(generator, **values)
            built[name] = sorted(frames)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {generator} refused these values — {exc}")
            built[name] = []

    placed = {parts[0].get("name")} if parts and isinstance(parts[0], dict) else set()
    for i, mate in enumerate(raw.get("mates") or []):
        if not isinstance(mate, dict):
            errors.append(f"mate {i} is not an object")
            continue
        kind = mate.get("kind")
        if kind not in _mates.KINDS:
            errors.append(f"mate {i}: no mate kind {kind!r}; have {sorted(_mates.KINDS)}")
        for role in ("target", "moving"):
            if mate.get(role) not in built:
                errors.append(
                    f"mate {i}: no part named {mate.get(role)!r}; have {sorted(built)}")
        if mate.get("target") in built and mate.get("moving") in built:
            for role, frame_key in (("target", "target_frame"), ("moving", "moving_frame")):
                want = mate.get(frame_key)
                have = built[mate[role]]
                if have and want not in have:
                    errors.append(
                        f"mate {i}: {mate[role]} has no frame {want!r}; "
                        f"it has {', '.join(have)}")
            if mate["target"] not in placed:
                errors.append(
                    f"mate {i}: {mate['target']!r} has no placement yet — mates apply in order, "
                    f"so place it before mating onto it")
            if mate["moving"] in placed:
                errors.append(
                    f"mate {i}: {mate['moving']!r} is already placed; a second mate on it is an "
                    "over-constraint")
            placed.add(mate.get("moving"))
    return errors


def plan(request: str, *, chat=None, base_url: str = "", api_key: str = "",
         model: str = "") -> Plan:
    """Ask the model for a program, validate it, and repair once if needed.

    `chat` is injectable so the whole path above the network is testable; left unset, the real
    OpenAI-compatible client is used.
    """
    if not request or not request.strip():
        raise PlanError("nothing was asked")

    if chat is None:
        from .llm import chat as real_chat

        def chat(messages, **kw):  # noqa: A001 - deliberately shadows the parameter
            return real_chat(messages, base_url=base_url, api_key=api_key, model=model, **kw)

    messages = _prompt(request, json.dumps(catalogue(), sort_keys=True))
    problems: list[str] = []

    for attempt in range(1, MAX_ATTEMPTS + 1):
        reply = chat(messages)
        try:
            raw = _parse(reply)
        except PlanError as exc:
            problems.append(str(exc))
            if attempt == MAX_ATTEMPTS:
                raise PlanError(
                    "the model failed twice to return a usable program. "
                    + " | ".join(problems))
            messages = messages + [
                {"role": "assistant", "content": reply},
                {"role": "user", "content":
                    f"That could not be read: {exc}. Reply with JSON only, in one of the two "
                    "shapes described, and nothing else."}]
            continue

        if raw.get("unsupported"):
            # An honest refusal. Never retried.
            return Plan(request=request, declined=True, reason=str(raw["unsupported"]),
                        nearest=str(raw.get("nearest") or ""), raw=reply, attempts=attempt)

        errors = _validate(raw)
        if not errors:
            return Plan(request=request, parts=raw["parts"], mates=raw.get("mates") or [],
                        notes=str(raw.get("notes") or ""), raw=reply, attempts=attempt)

        problems.extend(errors)
        if attempt == MAX_ATTEMPTS:
            raise PlanError(
                "the model produced an unusable program twice. " + " | ".join(problems))
        messages = messages + [
            {"role": "assistant", "content": reply},
            {"role": "user", "content":
                "That program will not build. Fix exactly these problems and reply with JSON "
                "only:\n- " + "\n- ".join(errors)}]

    raise PlanError("unreachable")  # pragma: no cover


def build(a_plan: Plan, doc=None) -> list:
    """Create the plan's parts in the document and apply its mates. Explicit, never automatic."""
    if a_plan.declined:
        raise PlanError(
            f"this plan was declined and has nothing to build: {a_plan.reason}")
    if not a_plan.parts:
        raise PlanError("this plan has no parts")

    if doc is None:
        import FreeCAD

        doc = FreeCAD.ActiveDocument
        if doc is None:
            raise PlanError("no active document to build into")

    made = []
    for entry in a_plan.parts:
        obj = _doc.make_part(doc, entry["generator"], a_plan.paramset(entry["name"]),
                             name=entry["name"])
        made.append(obj)
    doc.recompute()

    by_name = {o.Name: o for o in made}
    for mate in a_plan.mates:
        target, moving = by_name.get(mate["target"]), by_name.get(mate["moving"])
        if target is None or moving is None:
            raise PlanError(
                f"mate references {mate['target']!r} -> {mate['moving']!r}, which this plan "
                "did not create")
        _, tf = _parts.build(getattr(target, _doc.GENERATOR_PROP),
                             **_doc.paramset_of(target).values())
        _, mf = _parts.build(getattr(moving, _doc.GENERATOR_PROP),
                             **_doc.paramset_of(moving).values())
        moving.Placement = _mates.KINDS[mate["kind"]](
            target.Placement, tf[mate["target_frame"]], mf[mate["moving_frame"]])
    doc.recompute()
    return made
