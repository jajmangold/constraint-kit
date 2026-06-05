"""The constraint-kit DSL — the verified recipe layer.

A *program* is the design language the system (and an LLM) emits; it compiles DETERMINISTICALLY to exact
geometry via the existing builder/assembly compilers. This module is the single validated entry point that
turns a program into (a) structured DIAGNOSTICS, (b) exact geometry, (c) a geometric VERIFICATION against a
known reference. One engine, three consumers: LLM self-correction (feed diagnostics back), human authoring
(the LSP backend), and training-corpus filtering (keep only programs whose geometry matches ground truth).

Program shapes (both already compile today):
  leaf:  {"parts": [{"id","type","params"?,"material"?}, ...], "mates": [{"a","b", ("intent" | "type"+"a_joint"+"b_joint"), ...}]}
  tree:  {"defs": {name: <node>, ...}, "root": name}        # hierarchical (assembly.build_tree)

Doctrine unchanged: the LLM proposes the program, the kernel disposes the geometry, the verifier is truth.
Diagnostics are LSP-shaped ({severity, code, message, path}) so the same output drives an editor or an
agent loop. `validate` is static + fast (no kernel); `verify` compiles and checks against reference volume.
"""
from __future__ import annotations

from . import mate_intent, mates
from .builder import ALL_PART_GENS


def _pid(p: dict):
    return p.get("id") or p.get("name")


def _part_anchor_names(part_type: str, params: dict) -> set:
    """Anchor/frame names a part actually exposes (authored anchors + derived ports), by generating it
    (cached, T3.1). Lets the checker reject mates that reference a non-existent anchor — the measured
    failure mode (a bolt has seat/tip/head_top, not 'shank')."""
    from . import builder
    wp, anchors = builder._generate(part_type, params)
    return set(mate_intent.available_frames({"wp": wp, "anchors": anchors}))


def validate(program: dict, geometry: bool = True) -> list[dict]:
    """Validation → LSP-shaped diagnostics ([] = clean). Two tiers: pure-static (structure, known part
    types, resolvable mate refs, valid type/intent) always; and — when `geometry=True` (default) — an
    anchor tier that GENERATES each part (cached) to confirm every explicit mate's a_joint/b_joint actually
    exists on its part and that the part's params produce buildable geometry. The geometry tier is what
    catches the check↔compile gap (a statically-valid program that fails to build); an LSP can pass
    geometry=False for instant keystroke feedback."""
    d: list[dict] = []

    def err(code, msg, path):
        d.append({"severity": "error", "code": code, "message": msg, "path": path})

    def warn(code, msg, path):
        d.append({"severity": "warning", "code": code, "message": msg, "path": path})

    if not isinstance(program, dict):
        return [{"severity": "error", "code": "not-an-object", "message": "program must be an object", "path": ""}]

    if "defs" in program:                                   # tree program
        defs = program.get("defs")
        if not isinstance(defs, dict) or not defs:
            err("empty-defs", "tree program needs a non-empty 'defs' object", "defs")
            return d
        root = program.get("root")
        if root not in defs:
            err("unknown-root", f"root {root!r} not in defs; have {sorted(defs)}", "root")
        for name, node in defs.items():
            for p in node.get("parts", []):
                _check_part(p, f"defs.{name}.parts", err)
            for ch in node.get("children", []):
                if ch.get("ref") not in defs:
                    err("unknown-ref", f"child ref {ch.get('ref')!r} not in defs", f"defs.{name}.children")
        return d

    parts = program.get("parts")
    if not isinstance(parts, list) or not parts:
        err("no-parts", "program needs a non-empty 'parts' list", "parts")
        return d
    ids = []
    for i, p in enumerate(parts):
        _check_part(p, f"parts[{i}]", err)
        pid = _pid(p)
        if pid:
            if pid in ids:
                err("duplicate-id", f"duplicate part id {pid!r}", f"parts[{i}].id")
            ids.append(pid)

    idset = set(ids)
    # geometry tier: generate each known part (cached) to get its real anchor set + catch param/build errors
    anchors_by_id: dict = {}
    if geometry:
        for i, p in enumerate(parts):
            pid, t = _pid(p), p.get("type")
            if pid and t in ALL_PART_GENS:
                try:
                    anchors_by_id[pid] = _part_anchor_names(t, p.get("params") or {})
                except Exception as exc:  # noqa: BLE001 -- bad params -> generator raises; that's an error
                    err("part-build-error", f"part {pid!r} ({t}) fails to build: "
                        f"{type(exc).__name__}: {exc}", f"parts[{i}].params")

    known_intents = set(mate_intent.INTENT_SYNONYMS)
    for j, m in enumerate(program.get("mates", []) or []):
        path = f"mates[{j}]"
        if not isinstance(m, dict):
            err("bad-mate", "mate must be an object", path); continue
        for side in ("a", "b"):
            if m.get(side) not in idset:
                err("unknown-part-ref", f"mate {side} {m.get(side)!r} is not a declared part id", f"{path}.{side}")
        if m.get("intent"):
            if str(m["intent"]).strip().lower().replace("-", "_").replace(" ", "_") not in known_intents:
                err("unknown-intent", f"intent {m['intent']!r} unknown; "
                    f"known: {sorted(set(mate_intent.INTENT_SYNONYMS.values()))}", f"{path}.intent")
        else:
            if m.get("type") not in mates.MATE_TYPES:
                err("bad-mate-type", f"type {m.get('type')!r} not in {list(mates.MATE_TYPES)}", f"{path}.type")
            for jk, side in (("a_joint", "a"), ("b_joint", "b")):
                if jk not in m:
                    if m.get("type") not in ("contact",):
                        warn("missing-joint", f"explicit mate without {jk} (ok only if a derived/contact mate)", f"{path}.{jk}")
                elif m.get(side) in anchors_by_id and m[jk] not in anchors_by_id[m[side]]:
                    err("unknown-anchor", f"part {m[side]!r} has no anchor {m[jk]!r}; "
                        f"available: {sorted(anchors_by_id[m[side]])}", f"{path}.{jk}")
    return d


def _check_part(p, path, err):
    if not isinstance(p, dict):
        err("bad-part", "part must be an object", path); return
    if not _pid(p):
        err("missing-id", "part missing 'id'", f"{path}.id")
    t = p.get("type")
    if t not in ALL_PART_GENS:
        # cheap "did you mean": prefix/substring suggestions
        sugg = [k for k in ALL_PART_GENS if t and (k.startswith(str(t)[:3]) or str(t) in k)][:5]
        err("unknown-part-type", f"unknown part type {t!r}" + (f"; did you mean {sugg}?" if sugg else ""),
            f"{path}.type")
    if "params" in p and not isinstance(p["params"], dict):
        err("bad-params", "'params' must be an object", f"{path}.params")


def check(program: dict, geometry: bool = True) -> dict:
    """Convenience wrapper: {ok, diagnostics, n_errors}. `ok` = no error-severity diagnostics. `geometry`
    enables the anchor/build tier (default on; pass False for instant pure-static LSP feedback)."""
    diags = validate(program, geometry=geometry)
    n_err = sum(1 for x in diags if x["severity"] == "error")
    return {"ok": n_err == 0, "diagnostics": diags, "n_errors": n_err}


def compile_program(program: dict):
    """Compile a (validated) program to exact geometry. leaf -> (cq.Assembly, report); tree -> build_tree
    result dict. Raises ValueError if the program is statically invalid (call `check` first to self-correct)."""
    res = check(program)
    if not res["ok"]:
        raise ValueError(f"invalid program: {[x['message'] for x in res['diagnostics'] if x['severity']=='error']}")
    from . import assembly, builder
    if "defs" in program:
        return assembly.build_tree(program["root"], program["defs"])
    return builder.build_assembly(program)


def _shape(obj):
    import cadquery as cq
    if isinstance(obj, cq.Assembly):
        return obj.toCompound()
    return obj.val() if hasattr(obj, "val") else obj


# continuous signature fields (compared with relative tolerance) vs integer-count fields (compared exactly)
_CONTINUOUS = ("volume", "area", "bbox_sorted")
_COUNTS = ("n_solids", "n_faces", "n_edges")


def signature(obj) -> dict:
    """A discriminating geometric-equivalence SIGNATURE of a built object — far stronger than volume alone
    (volume is non-discriminating: a cube and a cylinder can share it). Combines continuous measures
    (volume, surface area, orientation-agnostic sorted bbox extents) with exact topology counts
    (solids/faces/edges). To fool all of these you essentially have to be the same part. This is the
    'geometry is truth' check the whole generate->verify->corpus->reward chain rests on."""
    s = _shape(obj)
    bb = s.BoundingBox()
    return {
        "volume": round(float(s.Volume()), 4),
        "area": round(float(s.Area()), 4),
        "bbox_sorted": sorted(round(float(d), 4) for d in (bb.xlen, bb.ylen, bb.zlen)),
        "n_solids": len(s.Solids()), "n_faces": len(s.Faces()), "n_edges": len(s.Edges()),
    }


def reference_signature(part_type: str, params: dict) -> dict:
    """Ground-truth signature of a known reference part, from our exact generators."""
    wp, _ = ALL_PART_GENS[part_type](**params)
    return signature(wp)


def program_signature(program: dict) -> dict:
    """Compile a whole program (part OR assembly) and return its geometric signature — so a reference can
    be an authored PROGRAM, not just a single part (lets us verify assemblies too)."""
    built = compile_program(program)
    assy = built[0] if isinstance(built, tuple) else built["cq_assembly"]
    return signature(assy)


def reference_volume(part_type: str, params: dict) -> float:
    """Ground-truth volume only (kept for callers that just want the scalar)."""
    return reference_signature(part_type, params)["volume"]


def _compare_sig(sig: dict, ref: dict, tol_frac: float) -> dict:
    """Compare a signature to a reference, field by field. Reference may be partial (e.g. {volume:…}) —
    only its present fields are checked. Continuous fields use relative tol; counts must match exactly."""
    fails = []
    for k, rv in ref.items():
        sv = sig.get(k)
        if k in _COUNTS:
            if sv != rv:
                fails.append({"field": k, "got": sv, "expected": rv})
        elif k == "bbox_sorted":
            if sv is None or len(sv) != len(rv) or any(
                    abs(a - b) > tol_frac * max(abs(b), 1e-9) for a, b in zip(sv, rv)):
                fails.append({"field": k, "got": sv, "expected": rv})
        elif k in _CONTINUOUS:
            if sv is None or abs(sv - rv) > tol_frac * max(abs(rv), 1e-9):
                fails.append({"field": k, "got": sv, "expected": rv,
                              "rel_err": round(abs(sv - rv) / max(abs(rv), 1e-9), 6) if sv is not None else None})
    return {"match": not fails, "fails": fails}


def verify(program: dict, reference, tol_frac: float = 1e-3) -> dict:
    """Compile a program and check its geometry against a reference SIGNATURE (the corpus filter / RL
    reward). `reference` is a signature dict (full or partial) — or a bare float, treated as a
    volume-only reference for convenience. A program is a verified design only if every reference field
    matches (continuous within `tol_frac`, topology counts exactly). Returns {match, signature, reference,
    fails}."""
    ref = {"volume": float(reference)} if isinstance(reference, (int, float)) else dict(reference)
    res = check(program)
    if not res["ok"]:
        return {"match": False, "reason": "static-invalid", "diagnostics": res["diagnostics"]}
    try:
        built = compile_program(program)
        assy = built[0] if isinstance(built, tuple) else built["cq_assembly"]
        sig = signature(assy)
    except Exception as exc:  # noqa: BLE001 -- compile/geometry failure is an honest non-match
        return {"match": False, "reason": f"compile-error: {type(exc).__name__}: {exc}"}
    cmp = _compare_sig(sig, ref, tol_frac)
    return {"match": cmp["match"], "signature": sig, "reference": ref, "fails": cmp["fails"]}
