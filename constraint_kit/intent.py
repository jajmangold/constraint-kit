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
        kind = e.get("kind")
        if kind not in ALL_PART_GENS:
            unresolved.append({"id": eid, "kind": kind, "reason": f"no part/subsystem expresses kind {kind!r}"})
            out_entities.append({"id": eid, "kind": kind, "requirements": {}, "resolvable": False})
            continue
        stated = e.get("requirements", {}) or {}
        defaults = _param_defaults(kind)
        reqs = {}
        for name, dflt in defaults.items():
            if name in stated:
                reqs[name] = {"value": _stated_value(stated[name]), "provenance": "stated"}
            else:
                reqs[name] = {"value": dflt, "provenance": "default"}
        for name, v in stated.items():                       # stated extras the generator doesn't take (e.g. material)
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
        out_entities.append({"id": eid, "kind": kind, "requirements": reqs, "resolvable": True})
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
