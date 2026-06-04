"""Top-down design parameters — requirements drive derived parameters drive geometry.

This is the Phase-B foundation (see ROADMAP.md): a car's wheelbase / a home's footprint are *requirements*;
subassembly dimensions are *derived* from them by explicit relations; the geometry then reads the derived
values. No AI in the math — deterministic, with provenance: every derived value records its relation and
the input values that produced it, so any dimension traces back to a requirement.

Relations are arithmetic expressions over parameter names. They are evaluated by a SAFE AST evaluator
(strict node + name whitelist) — never Python `eval` — because relations may come from specs or the LLM.
"""
from __future__ import annotations

import ast
import math
import operator

_BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
           ast.Pow: operator.pow}
_UNARYOPS = {ast.USub: operator.neg, ast.UAdd: operator.pos}
_CMP = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
        ast.Eq: operator.eq, ast.NotEq: operator.ne}
_FUNCS = {"min": min, "max": max, "abs": abs, "round": round, "sqrt": math.sqrt,
          "sin": math.sin, "cos": math.cos, "tan": math.tan, "radians": math.radians, "pi": math.pi}


def safe_eval(expr: str, names: dict):
    """Evaluate an arithmetic/comparison expression over `names` with a strict whitelist. Raises
    ValueError on anything outside numbers, the given names, +−*/%//**, comparisons, and a few math
    functions. NEVER uses Python eval()."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"bad expression {expr!r}: {exc}") from exc
    return _ev(tree.body, names)


def _ev(node, names):
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError(f"only numeric constants allowed (got {node.value!r})")
        return node.value
    if isinstance(node, ast.Name):
        if node.id in names:
            return names[node.id]
        if node.id == "pi":
            return math.pi
        raise ValueError(f"unknown parameter {node.id!r}")
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        return _BINOPS[type(node.op)](_ev(node.left, names), _ev(node.right, names))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARYOPS:
        return _UNARYOPS[type(node.op)](_ev(node.operand, names))
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _CMP:
        return _CMP[type(node.ops[0])](_ev(node.left, names), _ev(node.comparators[0], names))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS:
        return _FUNCS[node.func.id](*[_ev(a, names) for a in node.args])
    raise ValueError(f"disallowed expression element: {type(node).__name__}")


def _free_names(expr: str) -> set:
    """Parameter names referenced by an expression (excludes whitelisted function names)."""
    return {n.id for n in ast.walk(ast.parse(expr, mode="eval"))
            if isinstance(n, ast.Name) and n.id not in _FUNCS}


def resolve(requirements: dict, relations: list[dict] | None = None) -> dict:
    """Resolve requirements + derived relations into a parameter table with provenance.

    requirements: {name: value} or {name: {"value":v, "unit":u}}  (the inputs / top-level spec)
    relations:    [{"name","expr","unit"?}]  derived params; expr is arithmetic over other params.

    Returns {ok, params, values, warnings}. `params[name]` carries value/unit/source(requirement|derived)/
    expr/inputs (the input values used). Evaluation is fixpoint with cycle/unresolved detection.
    """
    relations = relations or []
    params: dict = {}
    values: dict = {}
    warnings: list[str] = []
    for name, spec in requirements.items():
        v = spec.get("value") if isinstance(spec, dict) else spec
        unit = spec.get("unit") if isinstance(spec, dict) else None
        params[name] = {"value": v, "unit": unit, "source": "requirement"}
        values[name] = v

    pending = list(relations)
    while pending:
        progressed = []
        for rel in pending:
            name, expr = rel["name"], rel["expr"]
            try:
                needed = _free_names(expr)
            except SyntaxError:
                warnings.append(f"relation {name!r}: bad expression {expr!r}"); progressed.append(rel)
                continue
            if not needed <= set(values):
                continue  # inputs not all known yet
            try:
                val = safe_eval(expr, values)
            except ValueError as exc:
                warnings.append(f"relation {name!r}: {exc}"); progressed.append(rel); continue
            params[name] = {"value": val, "unit": rel.get("unit"), "source": "derived",
                            "expr": expr, "inputs": {n: values[n] for n in sorted(needed)}}
            values[name] = val
            progressed.append(rel)
        if not progressed:   # nothing resolvable this pass -> cycle or missing inputs
            warnings.extend(f"unresolved relation {r['name']!r} (cycle or missing input): {r['expr']!r}"
                            for r in pending)
            break
        pending = [r for r in pending if r not in progressed]

    return {"ok": not warnings, "params": params, "values": values, "warnings": warnings}


def substitute(obj, values: dict):
    """Recursively replace expression strings (a leading '=') with their evaluated number, against
    `values`. Lists/dicts are walked; plain strings/numbers pass through. e.g. "=track - 2*hub_w"."""
    if isinstance(obj, dict):
        return {k: substitute(v, values) for k, v in obj.items()}
    if isinstance(obj, list):
        return [substitute(v, values) for v in obj]
    if isinstance(obj, str) and obj.startswith("="):
        return safe_eval(obj[1:], values)
    return obj


def check_asserts(asserts: list[dict] | None, values: dict) -> list[str]:
    """Evaluate requirement constraints; return failure messages ([] = all hold)."""
    failures = []
    for a in asserts or []:
        try:
            if not safe_eval(a["expr"], values):
                failures.append(a.get("message") or f"assertion failed: {a['expr']}")
        except ValueError as exc:
            failures.append(f"assertion error in {a['expr']!r}: {exc}")
    return failures
