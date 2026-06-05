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


def validate(program: dict) -> list[dict]:
    """STATIC validation (no geometry kernel): structure, known part types, resolvable mate references,
    valid mate type/intent, required frames. Returns LSP-shaped diagnostics ([] = clean). This is the
    fast tier an LSP runs on every edit and an LLM loop reads to self-correct."""
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
            for jk in ("a_joint", "b_joint"):
                if jk not in m and m.get("type") not in ("contact",):
                    warn("missing-joint", f"explicit mate without {jk} (ok only if a derived/contact mate)", f"{path}.{jk}")
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


def check(program: dict) -> dict:
    """Convenience wrapper: {ok, diagnostics, n_errors}. `ok` = no error-severity diagnostics."""
    diags = validate(program)
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


def _volume(obj) -> float:
    import cadquery as cq
    comp = obj.toCompound() if isinstance(obj, cq.Assembly) else obj
    return float(comp.Volume())


def reference_volume(part_type: str, params: dict) -> float:
    """Ground-truth volume of a known reference part (compiled by our exact generators)."""
    wp, _ = ALL_PART_GENS[part_type](**params)
    return _volume(wp.val() if hasattr(wp, "val") else wp)


def verify(program: dict, expected_volume: float, tol_frac: float = 1e-3) -> dict:
    """Compile the program and compare its TOTAL geometry volume to a known reference (relative tol). This
    is the corpus filter / RL reward: a program is a verified design only if its exact geometry matches
    ground truth. Returns {match, volume, expected, rel_err, diagnostics}."""
    res = check(program)
    if not res["ok"]:
        return {"match": False, "reason": "static-invalid", "diagnostics": res["diagnostics"]}
    try:
        built = compile_program(program)
        assy = built[0] if isinstance(built, tuple) else built["cq_assembly"]
        vol = _volume(assy)
    except Exception as exc:  # noqa: BLE001 -- compile/geometry failure is an honest non-match
        return {"match": False, "reason": f"compile-error: {type(exc).__name__}: {exc}"}
    rel = abs(vol - expected_volume) / expected_volume if expected_volume else float("inf")
    return {"match": rel <= tol_frac, "volume": round(vol, 4), "expected": round(expected_volume, 4),
            "rel_err": round(rel, 6)}
