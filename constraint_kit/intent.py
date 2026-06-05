"""Intent layer — the design front-end (first slice: the deterministic resolver).

Turns an under-specified, NL-derived *intent* into a CANONICAL, fully-specified, PROVENANCE-TRACKED design
intent BEFORE any geometry. The measured motivation: "a gear" has no single ground truth, so a generator
picks arbitrary defaults; here ambiguity is instead RESOLVED WITH PROVENANCE — `teeth=20 @ provenance:default`
is honest and inspectable, not pretended-as-stated.

An intent (thin, domain-neutral):
  {"entities":   [{"id","kind","requirements":{name:{value|range, unit?}}, "designation"?}],
   "interfaces": [{"a","b","relation"}],     # the canonical decomposition (seat_on/mesh/insert/...)
   "constraints":[{"expr","message"?}]}      # parameters.py relations/asserts (evaluated in a later slice)

resolve() fills each entity's missing requirements, tagging every value's provenance, and flags entities no
part/subsystem can express (an honest decline — the structured home for OOV). DETERMINISTIC: no LLM, no
geometry kernel — the defaults table IS the generator signatures, standards come from the spec compiler.
"""
from __future__ import annotations

import inspect

from .builder import ALL_PART_GENS

# resolved spec-compiler quantities -> the generator param they fill (where the names don't already align)
_QUANTITY_MAP = {
    "nominal_diameter": "shank_d", "nominal_thread_diameter": "shank_d",
    "bore": "bore_d", "bearing_bore": "bore_d", "outer_diameter": "outer_d",
    "bearing_outer_diameter": "outer_d", "bearing_width": "width",
}


# common descriptive names the LLM uses -> the canonical generator kind. Measured from the gauntlet:
# qwen says "pillow_block_bearing"/"2020_extrusion"/"weld_neck_flange" for parts that DO exist as
# bearing_block/extrusion/flange — a naming gap, not a missing capability. Aliases catch the tricky ones
# (where substring would mis-match, e.g. pillow_block_bearing must NOT become the simplified 'bearing').
_KIND_ALIASES = {
    "pillow_block": "bearing_block", "pillow_block_bearing": "bearing_block", "bearing_pillow_block": "bearing_block",
    "aluminum_extrusion": "extrusion", "v_slot_extrusion": "extrusion", "vslot_extrusion": "extrusion",
    "deep_groove_bearing": "ball_bearing", "deep_groove_ball_bearing": "ball_bearing",
    "cap_screw": "screw", "socket_head_cap_screw": "screw", "machine_screw": "screw", "hex_screw": "screw",
    "hex_bolt": "bolt", "cap_bolt": "bolt", "hex_head_bolt": "bolt",
    "pipe_flange": "flange", "weld_neck_flange": "flange", "slip_on_flange": "flange", "blind_flange": "flange",
    "chain_sprocket": "sprocket", "roller_chain_sprocket": "sprocket",
}


def _normalize_kind(kind):
    """Map an LLM-stated `kind` to a canonical generator kind, or None if nothing expresses it. Exact match
    first, then the alias table, then the longest part-gen name that appears as a substring (so
    '2020_extrusion'->extrusion, 'weld_neck_flange'->flange) — but 'helical_gear'/'jaw_coupling' match
    nothing and stay None (an honest decline). Records nothing here; the caller notes any remap."""
    if not isinstance(kind, str):
        return None
    k = kind.strip().lower()
    if k in ALL_PART_GENS:
        return k
    if k in _KIND_ALIASES:
        return _KIND_ALIASES[k]
    cands = [g for g in ALL_PART_GENS if g in k]          # part-gen name as a substring of the stated kind
    return max(cands, key=len) if cands else None         # longest wins (extrusion over a shorter accidental hit)


# requirement keys that are legitimately NOT generator params (so they don't count as unexpressible features)
_META_FIELDS = {"material", "designation", "name", "color", "tag", "finish", "note", "qty", "count"}
# common requirement-key synonyms -> a generator param name (applied only when the target IS a param of the
# chosen kind, so it never mis-maps; exact match always wins first). Avoids false 'unexpressible' flags.
_REQ_ALIASES = {
    "od": "outer_d", "outer_diameter": "outer_d", "outside_diameter": "outer_d", "diameter": "outer_d",
    "id": "bore_d", "inner_diameter": "bore_d", "bore": "bore_d", "inside_diameter": "bore_d",
    "thickness": "thick", "len": "length", "face_width": "width", "num_teeth": "teeth", "tooth_count": "teeth",
}


def _alias_req_key(key: str, defaults: dict) -> str:
    """Map a stated requirement key to the chosen kind's param name where possible (exact, then a guarded
    synonym, then a unique substring match). Returns the key unchanged if nothing fits — which then surfaces
    it as an UNEXPRESSIBLE feature rather than silently dropping it."""
    if key in defaults:
        return key
    a = _REQ_ALIASES.get(key)
    if a and a in defaults:
        return a
    cands = [d for d in defaults if key == d or key in d or d in key]
    return cands[0] if len(cands) == 1 else key


def _param_defaults(kind: str) -> dict:
    """{param: default} for a part kind, read straight from the generator signature — the defaults 'table'
    is the code itself, so it never drifts."""
    sig = inspect.signature(ALL_PART_GENS[kind])
    return {n: p.default for n, p in sig.parameters.items() if p.default is not inspect.Parameter.empty}


def _stated_value(req):
    if isinstance(req, dict):
        return req.get("value", req.get("range"))
    return req


def resolve(intent: dict) -> dict:
    """Resolve an intent into a fully-specified, provenance-tracked form. Per entity: keep stated requirements
    (provenance 'stated'), fill the rest from the generator defaults (provenance 'default'), and overlay any
    standard designation via the spec compiler (provenance 'standard' + source_ref). An entity whose `kind`
    no part/subsystem expresses goes to `unresolved` and makes the intent a `declined` (honest OOV stop).
    Returns {entities, interfaces, constraints, unresolved, declined, decline_reasons}. Fail-soft."""
    out_entities, unresolved = [], []
    for i, e in enumerate(intent.get("entities", []) or []):
        eid = e.get("id", f"e{i}")
        raw_kind = e.get("kind")
        kind = _normalize_kind(raw_kind)                 # remap descriptive names to canonical generator kinds
        if kind is None:
            unresolved.append({"id": eid, "kind": raw_kind, "reason": f"no part/subsystem expresses kind {raw_kind!r}"})
            out_entities.append({"id": eid, "kind": raw_kind, "requirements": {}, "resolvable": False})
            continue
        defaults = _param_defaults(kind)
        # alias stated keys toward the generator's param names; any leftover that's neither a param nor a
        # meta-field is an UNEXPRESSIBLE FEATURE the chosen part can't represent — flag it, don't drop it
        # silently (the gauntlet's 'keyed shaft' built a plain shaft because the keyway was dropped).
        stated, unexpressible = {}, []
        for k, v in (e.get("requirements", {}) or {}).items():
            ak = _alias_req_key(k, defaults)
            stated[ak] = v
            if ak not in defaults and ak not in _META_FIELDS:
                unexpressible.append(k)
        if unexpressible:
            unresolved.append({"id": eid, "kind": kind,
                               "reason": f"kind {kind!r} cannot express requirement(s) {unexpressible}"})
            out_entities.append({"id": eid, "kind": kind, "requirements": {}, "resolvable": False,
                                 "unexpressible": unexpressible})
            continue
        reqs = {}
        for name, dflt in defaults.items():
            if name in stated:
                reqs[name] = {"value": _stated_value(stated[name]), "provenance": "stated"}
            else:
                reqs[name] = {"value": dflt, "provenance": "default"}
        for name, v in stated.items():                       # legit meta extras (material, designation, ...)
            if name not in reqs:
                reqs[name] = {"value": _stated_value(v), "provenance": "stated"}
        if e.get("designation"):                             # standards fill, with provenance, via the spec compiler
            try:
                from . import spec_compiler
                r = spec_compiler.resolve(e["designation"], allow_live=False)
                if r.get("ok") and r.get("facts"):
                    for q, val in r["facts"][0].get("values", {}).items():
                        key = q if q in defaults else _QUANTITY_MAP.get(q, q)
                        reqs[key] = {"value": val["value"], "unit": val.get("unit"),
                                     "provenance": "standard", "source_ref": val.get("source_ref")}
            except Exception:  # noqa: BLE001 -- standards fill is best-effort; defaults already hold
                pass
        entry = {"id": eid, "kind": kind, "requirements": reqs, "resolvable": True}
        if kind != raw_kind:
            entry["original_kind"] = raw_kind            # transparency: we remapped the LLM's descriptive name
        out_entities.append(entry)
    return {"entities": out_entities, "interfaces": intent.get("interfaces", []) or [],
            "constraints": intent.get("constraints", []) or [], "unresolved": unresolved,
            "declined": bool(unresolved), "decline_reasons": [u["reason"] for u in unresolved]}


def to_program(resolved: dict) -> dict:
    """Lower a fully-resolved (non-declined) intent into a DSL program — the deterministic bridge from intent
    to geometry. Each entity's resolved requirements become the part's params (only those the generator
    accepts); interfaces become mates. Raises if the intent is declined."""
    if resolved.get("declined"):
        raise ValueError(f"declined intent is not buildable: {resolved['decline_reasons']}")
    parts = []
    for e in resolved["entities"]:
        defaults = _param_defaults(e["kind"])
        params = {n: r["value"] for n, r in e["requirements"].items() if n in defaults}
        mat = e["requirements"].get("material", {}).get("value", "steel")
        parts.append({"id": e["id"], "type": e["kind"], "material": mat, "params": params})
    mates = [{"a": f["a"], "b": f["b"], "intent": f["relation"]} for f in resolved.get("interfaces", [])]
    return {"parts": parts, "mates": mates}
