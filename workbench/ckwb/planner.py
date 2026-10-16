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
* **A deterministic repair first, then one model attempt.** A misspelled parameter or frame
  name has exactly one plausible correction, so `diagnose.repair` makes it here and the model is
  never asked -- cheaper, and more reliable than inviting a model to be creative about a typo.
  Only when nothing is repairable without guessing is the refusal handed back, once, which is the
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
 "mates": [{"kind": "seat|align|offset", "target": "<part name>", "target_frame": "<frame>",
            "moving": "<part name>", "moving_frame": "<frame>",
            "offset": {"dx": <mm>, "dy": <mm>, "dz": <mm>}}],
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
- "seat" puts two faces in contact. "align" makes two frames share an axis. "offset" aligns
  them and then displaces by (dx, dy, dz) **in the target frame's own axes**, which is the only
  way to put something BESIDE something else -- two parallel rods 60 mm apart are two offset
  mates with dx of +30 and -30. Include the "offset" key for an offset mate and leave it out
  otherwise; an offset of all zeros is an "align" and should be written as one.
- An axis frame's +Z runs ALONG the part, so for a rod parallel to a beam, offset in dx/dy and
  leave dz alone. Mating a long part to a FACE frame instead (whose +Z points out of the face)
  lays it across the target rather than along it.
- Mates are applied in order and each one moves its "moving" part, so a part must already be
  placed before it can be a "target". The first part listed is the grounded one.
- The assembly is SOLVED and checked for interference before you are believed. Two parts
  occupying the same space is an error you will be asked to fix, so think about the sizes: a rod
  offset to the edge of a 40 mm channel still has its own radius, and half of it ends up inside
  the wall.
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
    #: What was wrong with the earlier tries, when there were any. Carried on the plan rather
    #: than left on the session because a plan that needed repairing should be able to say what
    #: it got wrong -- "repaired once" without the reason tells a reviewer nothing.
    problems: list = field(default_factory=list)

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
            line = (f"{mate['kind']}  {mate['target']}.{mate['target_frame']}"
                    f"  <-  {mate['moving']}.{mate['moving_frame']}")
            shift = mate.get("offset") or {}
            if shift:
                # Shown in the program because a displacement is a decision, and a reviewer
                # who cannot see it cannot review it.
                line += "  by " + ", ".join(
                    f"{k} {float(v):g}" for k, v in sorted(shift.items()))
            lines.append(line)
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


#: Bare agreement, and nothing else. This exists because of a real exchange: the planner
#: declined a request, offered a nearest thing it *could* build, the user answered **"do it
#: build that"** -- and the planner had no memory of its own offer, so it declined again with
#: *"the request contains no part or assembly"*. An offer you cannot accept is not an offer.
#:
#: Deliberately a closed vocabulary rather than a model call: accepting a one-word yes must not
#: depend on a network round trip or on the model's mood, and there is nothing to interpret.
AFFIRMATIVES = {
    "do it", "do that", "build it", "build that", "yes", "yes please", "yep", "yeah",
    "ok", "okay", "ok do it", "okay do it", "go", "go ahead", "go on", "please",
    "please do", "please do it", "sure", "sounds good", "that one", "the nearest",
    "nearest", "do the nearest", "build the nearest", "accepted", "accept", "y",
    "do it build that", "yes do it", "yes build it", "build", "make it", "make that",
}

#: Filler that does not change the meaning of a bare yes.
_FILLER = ("please", "then", "thanks", "thank you", "ok", "okay", "just", "now")


def is_affirmative(text: str) -> bool:
    """True for a BARE yes, and false for a yes with new instructions in it.

    The distinction is the whole value. "do it" means *build the thing you offered*; "do it but
    make it 300 mm" is a different request and must go to the model with the offer as context,
    not be silently replaced by the offer. So anything left over after stripping punctuation and
    filler disqualifies it.
    """
    cleaned = " ".join(str(text or "").lower().replace("!", " ").replace(".", " ")
                       .replace(",", " ").replace("?", " ").split())
    if not cleaned:
        return False
    if cleaned in AFFIRMATIVES:
        return True
    words = cleaned.split()
    while words and words[-1] in _FILLER:
        words.pop()
    while words and words[0] in _FILLER:
        words.pop(0)
    return " ".join(words) in AFFIRMATIVES


@dataclass(frozen=True)
class Prior:
    """What happened on the previous turn, so this turn can refer to it.

    The planner is otherwise stateless, and for the command and agent surfaces that is right --
    an agent has its own context and can simply restate the whole request. A person typing into
    a chat panel cannot be expected to, which is why the panel keeps one of these and passes it
    along.
    """

    request: str = ""
    declined: bool = False
    reason: str = ""
    nearest: str = ""
    program: str = ""

    @property
    def has_offer(self) -> bool:
        return bool(self.declined and self.nearest.strip())


def _prompt(request: str, catalogue_json: str, prior: "Prior | None" = None,
            accepted: bool = False) -> list:
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content":
            f"CATALOGUE (the only generators, parameters and frames that exist):\n"
            f"{catalogue_json}\n\n"
            f"MATE KINDS: {sorted(_mates.KINDS)}\n\n"
            f"REQUEST: {request}"},
    ]
    if prior is not None and (prior.request or prior.program or prior.reason):
        lines = ["EARLIER IN THIS CONVERSATION, so that a follow-up makes sense."]
        if prior.request:
            lines.append(f"They asked: {prior.request}")
        if prior.declined:
            lines.append(f"You declined, because: {prior.reason}")
            if prior.nearest:
                lines.append(f"You offered instead: {prior.nearest}")
        elif prior.program:
            lines.append("You produced this program:")
            lines.append(prior.program)
        if accepted:
            lines.append(
                "THE REQUEST ABOVE IS THEM ACCEPTING THAT OFFER. Build the thing you offered. "
                "Do not decline it again -- you have already said you can build it.")
        else:
            lines.append(
                "The REQUEST above may be a change to that, or a new thing entirely. If it "
                "plainly refers back ('make it longer', 'without the nut'), apply it to the "
                "above. If it stands alone, ignore the above.")
        messages.insert(1, {"role": "user", "content": "\n".join(lines)})
    return messages


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


def _offset_errors(i: int, kind: str, raw) -> list:
    """Check a mate's displacement. An offset mate without one is the mistake worth catching.

    A missing offset silently becomes an `align`, so the program would claim to put two parts
    side by side and then stack them on one axis -- which is exactly the failure this mate was
    added to fix, reappearing as a typo.
    """
    allowed = _mates.KIND_OPTIONS.get(kind, ())
    if not allowed:
        if raw:
            return [f"mate {i}: {kind!r} takes no offset; use kind \"offset\" to displace one"]
        return []
    if raw is None:
        return [f"mate {i}: an \"offset\" mate needs an \"offset\" of "
                f"{{{', '.join(allowed)}}}; without one it is an \"align\""]
    if not isinstance(raw, dict):
        return [f"mate {i}: \"offset\" must be an object of {{{', '.join(allowed)}}}, "
                f"got {raw!r}"]
    out = []
    unknown = sorted(set(raw) - set(allowed))
    if unknown:
        out.append(f"mate {i}: offset has no {', '.join(unknown)}; it takes "
                   f"{', '.join(allowed)}")
    for key in sorted(set(raw) & set(allowed)):
        try:
            float(raw[key])
        except (TypeError, ValueError):
            out.append(f"mate {i}: offset {key} = {raw[key]!r} is not a distance")
    if not out and not any(abs(float(raw.get(k, 0.0))) > 1e-12 for k in allowed):
        out.append(f"mate {i}: this offset is all zeros, which is an \"align\" — say so, or "
                   "give it a real displacement")
    return out


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
        else:
            errors.extend(_offset_errors(i, kind, mate.get("offset")))
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

    # Only worth solving if the program is structurally sound; otherwise the solver would
    # raise for a reason already in `errors` and say it worse.
    if not errors:
        errors.extend(_fit_errors(parts, raw.get("mates") or []))
    return errors


#: Overlapping volume below this is contact rather than interference. Not zero, because a gear
#: pair at its exact centre distance touches, a seated face touches, and OCCT puts a few cubic
#: microns on both.
FIT_TOL_MM3 = 1.0


def _fit_errors(parts: list, mates: list) -> list:
    """Solve the assembly and report parts that overlap. "It placed them" is not "they fit".

    This exists because of a measured failure. Asked for a rail with a ball screw and two 16 mm
    rods in a C-beam channel, the model produced a structurally perfect program -- every
    generator real, every frame real, every mate in order -- that drove both rods straight
    through the channel wall, 20,106 mm3 each. Nothing caught it, because validation checked
    the FORM of the program and never its fit, so the first anyone knew was looking at it.

    Volume rather than distance, deliberately: a seated face and a gear pair at its exact centre
    distance both TOUCH, and must stay legal. Material in the same place is what is illegal.
    """
    import FreeCAD

    specs, shapes = {}, {}
    for entry in parts:
        if not isinstance(entry, dict):
            return []
        name, generator = entry.get("name"), entry.get("generator")
        values = {}
        try:
            for key, default in _parts.defaults_for(generator).items():
                given = (entry.get("params") or {}).get(key)
                values[key] = _coerce(given["value"], default) if given else default
            shape, frames = _parts.build(generator, **values)
        except Exception:  # noqa: BLE001 - already reported by the caller's own checks
            return []
        specs[name] = (shape, frames)
        shapes[name] = shape

    tuples = []
    for mate in mates:
        spec = [mate.get("kind"), mate.get("target"), mate.get("target_frame"),
                mate.get("moving"), mate.get("moving_frame")]
        shift = {k: float(v) for k, v in (mate.get("offset") or {}).items()
                 if k in _mates.KIND_OPTIONS.get(mate.get("kind"), ())}
        tuples.append(tuple(spec) + ((shift,) if shift else ()))

    ground = parts[0].get("name") if parts else None
    try:
        places = _mates.solve(specs, ground, tuples)
    except Exception as exc:  # noqa: BLE001
        return [f"the mates do not solve: {exc}"]

    placed = {}
    for name, shape in shapes.items():
        copy = shape.copy()
        copy.Placement = places.get(name, FreeCAD.Placement())
        placed[name] = copy

    out = []
    names = sorted(placed)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            try:
                volume = placed[a].common(placed[b]).Volume
            except Exception:  # noqa: BLE001 - a boolean that will not run is not a fit error
                continue
            if volume > FIT_TOL_MM3:
                out.append(
                    f"{a} and {b} occupy the same space: {volume:.1f} mm3 of overlap. Move one "
                    "of them, or reconsider which frame it is mated to")
    return out


class PlanSession:
    """The plan/validate/repair state machine, with the network call left to the caller.

    Exists because of a real defect: the first version ran the whole thing synchronously, so a
    slow model froze FreeCAD's GUI thread for as long as the HTTP request took. The fix cannot be
    "run plan() in a worker thread" either — validation BUILDS the proposed parts to check their
    frames, and driving OCCT from a non-GUI thread is not safe.

    So the two halves are separated. The caller owns the slow, thread-safe part (one HTTP call)
    and this object owns the fast, thread-bound part (prompting, parsing, validating, building).
    `plan()` drives it on one thread; the GUI drives the same object across a worker and a signal.
    One implementation, so the interactive path and the scripted path cannot diverge.

    Usage:
        session = PlanSession(request, prior=prior)
        while True:
            reply = chat(session.messages)        # slow; anywhere you like
            kind, payload = session.offer(reply)  # fast; must be the geometry thread
            if kind == "plan":  return payload
            if kind == "error": raise PlanError(payload)
            # kind == "retry": loop, session.messages has been updated
    """

    def __init__(self, request: str, prior: "Prior | None" = None):
        if not request or not request.strip():
            raise PlanError("nothing was asked")
        self.asked = request
        self.prior = prior
        self.accepted_offer = False

        #: A bare yes after an offer becomes a request for the offer itself. Resolved HERE,
        #: before any network call, so accepting an offer cannot fail for a reason the user
        #: has no way to understand.
        if is_affirmative(request):
            if prior is not None and prior.has_offer:
                request = prior.nearest
                self.accepted_offer = True
            elif prior is not None and not prior.declined and prior.program:
                raise PlanError(
                    "that is already built, or waiting on the Build button -- there is no "
                    "offer to accept. Say what to change, or describe something new.")
            else:
                raise PlanError(
                    "there is nothing to agree to yet. I have not offered anything I could "
                    "build, so say what you want and I will either plan it or tell you "
                    "precisely what is missing.")

        self.request = request
        self.messages = _prompt(request, json.dumps(catalogue(), sort_keys=True),
                                prior=prior, accepted=self.accepted_offer)
        self.attempt = 0
        self.problems: list[str] = []

    def offer(self, reply: str):
        """Judge one model reply. Returns ("plan", Plan) | ("retry", None) | ("error", str)."""
        self.attempt += 1
        last = self.attempt >= MAX_ATTEMPTS

        try:
            raw = _parse(reply)
        except PlanError as exc:
            self.problems.append(str(exc))
            if last:
                return "error", ("the model failed twice to return a usable program. "
                                 + " | ".join(self.problems))
            self.messages = self.messages + [
                {"role": "assistant", "content": reply},
                {"role": "user", "content":
                    f"That could not be read: {exc}. Reply with JSON only, in one of the two "
                    "shapes described, and nothing else."}]
            return "retry", None

        if raw.get("unsupported"):
            # An honest refusal. Never retried.
            return "plan", Plan(request=self.request, declined=True,
                                reason=str(raw["unsupported"]),
                                nearest=str(raw.get("nearest") or ""), raw=reply,
                                attempts=self.attempt)

        errors = _validate(raw)
        if not errors:
            return "plan", Plan(request=self.request, parts=raw["parts"],
                                mates=raw.get("mates") or [],
                                notes=str(raw.get("notes") or ""), raw=reply,
                                attempts=self.attempt, problems=list(self.problems))

        # A DETERMINISTIC REPAIR FIRST, and only then the model (issue #20).
        #
        # A misspelled parameter has exactly one plausible correction, and spending a round trip
        # to have a language model make that correction is both slower and less reliable than
        # making it here -- asking a model to fix a typo invites it to be creative instead.
        # `diagnose.repair` only returns a program it has RE-VERIFIED, so this cannot smuggle a
        # broken plan past the validator, and it refuses outright where a fix would be a
        # fabrication (an unknown generator is not a misspelling of a real one).
        #
        # The attempt counter is deliberately not incremented: nothing was asked of the model, so
        # charging the repair budget for it would shorten the budget for the thing it is for.
        from . import diagnose as _diagnose

        mended = _diagnose.repair({"parts": raw.get("parts") or [],
                                   "mates": raw.get("mates") or [],
                                   "notes": str(raw.get("notes") or "")})
        if mended.fixed is not None and not _validate(mended.fixed):
            # `attempt` stays where it is. One model call WAS made -- the reply came from it --
            # and what the repair avoided is the SECOND call. Decrementing would misreport the
            # first one as never having happened.
            fixed_note = "; ".join(r.summary for r in mended.repairs if r.verified)
            notes = str(raw.get("notes") or "")
            notes = (notes + "  " if notes else "") + (
                "Repaired without asking the model again: %s. These were unambiguous -- each "
                "name was within an edit or two of a real one -- and the repaired program was "
                "re-validated before being offered." % fixed_note)
            return "plan", Plan(request=self.request, parts=mended.fixed["parts"],
                                mates=mended.fixed.get("mates") or [],
                                notes=notes, raw=reply, attempts=self.attempt,
                                problems=list(self.problems))

        self.problems.extend(errors)
        if last:
            return "error", ("the model produced an unusable program twice. "
                             + " | ".join(self.problems))
        self.messages = self.messages + [
            {"role": "assistant", "content": reply},
            {"role": "user", "content":
                "That program will not build. Fix exactly these problems and reply with JSON "
                "only:\n- " + "\n- ".join(errors)}]
        return "retry", None


def plan(request: str, *, prior=None, chat=None, base_url: str = "", api_key: str = "",
         model: str = "") -> Plan:
    """Ask the model for a program, validate it, and repair once if needed.

    `chat` is injectable so the whole path above the network is testable; left unset, the real
    OpenAI-compatible client is used.

    `prior` is the previous turn (a `Prior`), which makes a follow-up mean something: a bare
    "do it" after a declined offer builds the offer, and "make it longer" refers to the last
    program. Left unset the planner is stateless, which is right for the command and agent
    surfaces -- an agent has its own context and can restate.
    """
    if chat is None:
        from .llm import chat as real_chat

        def chat(messages, **kw):  # noqa: A001 - deliberately shadows the parameter
            return real_chat(messages, base_url=base_url, api_key=api_key, model=model, **kw)

    session = PlanSession(request, prior=prior)
    for _ in range(MAX_ATTEMPTS):
        kind, payload = session.offer(chat(session.messages))
        if kind == "plan":
            return payload
        if kind == "error":
            raise PlanError(payload)
    raise PlanError("unreachable")  # pragma: no cover


def prior_of(a_plan: Plan) -> Prior:
    """The `Prior` a finished plan leaves behind, for the next turn to refer to."""
    if a_plan.declined:
        return Prior(request=a_plan.request, declined=True, reason=a_plan.reason,
                     nearest=a_plan.nearest)
    return Prior(request=a_plan.request, declined=False, program=a_plan.render())


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
        opts = {k: float(v) for k, v in (mate.get("offset") or {}).items()
                if k in _mates.KIND_OPTIONS.get(mate["kind"], ())}
        moving.Placement = _mates.KINDS[mate["kind"]](
            target.Placement, tf[mate["target_frame"]], mf[mate["moving_frame"]], **opts)
    doc.recompute()
    return made
