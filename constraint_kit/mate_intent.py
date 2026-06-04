"""Mate-by-intent resolver (T2.2) — the bridge from semantic intent to the deterministic kernel.

The doctrine is "LLM proposes, exact geometry disposes": the planner (or a human) says WHAT should
connect to WHAT and HOW ("seat the gear bore on the plate boss"), and this resolver — DETERMINISTICALLY,
no LLM — turns that intent into a concrete `{a_joint, b_joint, type}` mate the builder already knows how
to solve. It does so by matching a small intent vocabulary to the parts' available oriented frames:
their AUTHORED anchors first (named, meaningful — `plate.mount`, `gear.bore_base`), falling back to the
geometry-DERIVED ports from `joints.derive_ports` (top/bottom/center/bore_axis) whose stability across
parameter changes T2.3 proved. So intent grounds onto durable frames, never selector-derived faces.

Honest by construction: if a needed role can't be matched on a part, it RAISES with the available frame
names (no silently-wrong mate). An explicit `a_joint`/`b_joint` in the intent mate overrides the auto-pick,
so intent and hand-authored frames compose. The resolver is the only place that needs to know the intent
vocabulary; the builder/kernel stay frame-based and unchanged.
"""
from __future__ import annotations

from .joints import derive_ports

# free-text / planner verbs -> canonical intent
INTENT_SYNONYMS = {
    "seat_on": "seat_on", "seat": "seat_on", "stack": "seat_on", "stack_on": "seat_on",
    "place_on": "seat_on", "rest_on": "seat_on", "on_top_of": "seat_on", "mount_on": "seat_on",
    "insert": "insert", "insert_in_bore": "insert", "into_bore": "insert", "journal": "insert",
    "journal_in": "insert", "shaft_in_bore": "insert", "fit_in_bore": "insert",
    "fasten": "fasten", "fasten_to": "fasten", "bolt": "fasten", "bolt_into": "fasten",
    "bolt_to": "fasten", "screw_into": "fasten", "fasten_into": "fasten",
    "mesh": "mesh", "mesh_with": "mesh", "gear_mesh": "mesh", "engage": "mesh",
}

# canonical intent -> (default mate type, ordered A-role frame candidates, ordered B-role candidates).
# A = the fixed/base part (already positioned), B = the moving part landed onto A.
INTENT_RULES = {
    # B's base/underside lands exactly on A's upward seat (a part on a boss/top/stack)
    "seat_on": ("coincident", ["mount", "top", "bore_top"], ["bore_base", "bottom", "base", "seat"]),
    # B's axis drops onto A's bore axis (a shaft/bearing into a bore); rigid static fit by default
    "insert":  ("rigid", ["bore", "bore_axis", "bore_base", "axis"], ["base", "mid", "bore_base", "bottom"]),
    # B (a fastener) seats with its under-head frame in A's bolt hole; rigid (oriented joint mate)
    "fasten":  ("rigid", ["bolt0", "mount", "bore", "bore_axis"], ["seat", "bottom", "bore_base"]),
    # two spur gears mesh at the exact involute center distance (param-aware, handled by the builder)
    "mesh":    ("mesh", ["bore_base"], ["bore_base"]),
}

# intents whose mate type the caller may legitimately escalate (e.g. a journalled shaft that SPINS)
_TYPE_OVERRIDABLE = {"insert": {"rigid", "revolute", "cylindrical"}, "seat_on": {"coincident", "contact"}}


def canonical_intent(raw: str) -> str:
    """Normalize a free-text/planner intent verb to a canonical key, or raise if unknown."""
    key = (raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    canon = INTENT_SYNONYMS.get(key)
    if canon is None:
        raise ValueError(f"unknown mate intent {raw!r}; known: {sorted(set(INTENT_SYNONYMS.values()))}")
    return canon


def available_frames(part: dict) -> dict:
    """All oriented frames a part offers, AUTHORED anchors first then geometry-DERIVED ports (T2.3) for any
    name the author didn't already provide. Derived ports are fail-soft (skipped if the query errors)."""
    frames = {k: v for k, v in part["anchors"].items() if not k.startswith("_")}
    try:
        for name, loc in derive_ports(part["wp"]).items():
            frames.setdefault(name, loc)        # authored wins; derived fills the gaps
    except Exception:  # noqa: BLE001 -- derivation is best-effort
        pass
    return frames


def _pick(candidates: list[str], frames: dict, part_id: str, side: str, intent: str) -> str:
    for name in candidates:
        if name in frames:
            return name
    raise ValueError(
        f"intent {intent!r}: cannot resolve the {side}-side frame on part {part_id!r} "
        f"(wanted one of {candidates}; part offers {sorted(frames)})")


def resolve_intent(parts: dict, m: dict) -> dict:
    """Resolve ONE intent mate {a, b, intent, ...} into a concrete {a, b, a_joint, b_joint, type, ...}.

    `parts` is the builder's generated part map (each {wp, anchors, type, ...}). If a chosen frame comes
    from a DERIVED port (not in the part's authored anchors), it is injected into that part's anchors so
    the existing frame-based `_apply_mate` can solve it unchanged. Explicit a_joint/b_joint override the
    auto-pick. Carries angle_deg/slide through for kinematic intents."""
    a, b = m["a"], m["b"]
    if a not in parts or b not in parts:
        raise ValueError(f"intent mate references unknown part: {m}")
    canon = canonical_intent(m.get("intent", ""))
    default_type, a_cands, b_cands = INTENT_RULES[canon]

    a_frames, b_frames = available_frames(parts[a]), available_frames(parts[b])
    # a specific bolt hole, e.g. {"intent":"fasten","hole_index":2} -> prefer plate.bolt2
    if canon == "fasten" and m.get("hole_index") is not None:
        a_cands = [f"bolt{int(m['hole_index'])}"] + a_cands

    a_joint = m.get("a_joint") or _pick(a_cands, a_frames, a, "a", canon)
    b_joint = m.get("b_joint") or _pick(b_cands, b_frames, b, "b", canon)

    # make any DERIVED frame the resolver chose visible to the frame-based solver
    for pid, joint, frames in ((a, a_joint, a_frames), (b, b_joint, b_frames)):
        if joint not in parts[pid]["anchors"]:
            parts[pid]["anchors"][joint] = frames[joint]

    mtype = m.get("type", default_type)
    if "type" in m and m["type"] != default_type and m["type"] not in _TYPE_OVERRIDABLE.get(canon, set()):
        raise ValueError(f"intent {canon!r} does not allow mate type {m['type']!r} "
                         f"(default {default_type!r}; allowed {_TYPE_OVERRIDABLE.get(canon, {default_type})})")

    resolved = {"a": a, "b": b, "a_joint": a_joint, "b_joint": b_joint, "type": mtype,
                "resolved_from_intent": canon}
    for k in ("angle_deg", "slide"):
        if k in m:
            resolved[k] = m[k]
    return resolved


def normalize_mates(parts: dict, mates: list[dict]) -> list[dict]:
    """Resolve every intent-carrying mate (explicit a_joint/b_joint still override the auto-pick); pass
    fully-explicit mates through untouched."""
    return [resolve_intent(parts, m) if m.get("intent") else m for m in mates]
