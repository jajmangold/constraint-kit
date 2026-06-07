"""Constructive geometry tier — imagination's medium [imagination Phase 1].

The part-catalog DSL caps the model at our vocabulary. This tier lets a model IMAGINE novel shapes as
programs over a small set of constructive ops (primitives -> booleans -> features), compiled to cadquery.
Verification changes accordingly: there is no reference to match, so truth comes from
  1. VALIDITY  — compiles, solid, watertight, positive volume (`validity_report`)
  2. SPEC      — checkable properties of the REQUEST hold on the built solid (`spec_check`):
                 bbox dims, volume/mass bounds, hole count+diameter — properties, not equality.
Expressiveness is proven by DECOMPILING native generators into construction sequences and verifying
signature-equivalence (tests) — the existing verifier closes the loop on day one.

Program form (JSON, LLM-friendly): {"construct": [ {op...}, ... ]} — ops act on named solids
("id" names an output; "of"/"a"/"b" reference inputs; default chains the previous result; the final
result is the last op's output or the solid named "result").

Ops (v1, deliberately small but spanning):
  box{w,d,h,at?}                cyl{d,h,at?}
  sketch_extrude{profile:[[x,y],...], dist}          revolve{profile:[[x,z],...], angle?}
  union{a?,b}  cut{a?,b}  intersect{a?,b}
  polar{of?, n, r}              -- n copies of a tool about Z at radius r (bolt circles etc.)
  fillet{r, edges?}  chamfer{r, edges?}   -- edges: all|top|bottom|vertical
  shell{t, open?}               -- open: top|bottom|none
  move{of?, to:[x,y,z], rot?:[rx,ry,rz]}
Caps: <= MAX_OPS ops; parameter sanity-checked; unknown op/reference RAISES (honest, never silent).
"""
from __future__ import annotations

import cadquery as cq

MAX_OPS = 48
_EDGE_SEL = {"all": None, "top": ">Z", "bottom": "<Z", "vertical": "|Z"}


def _num(op, key, default=None, lo=1e-6, hi=1e5):
    v = op.get(key, default)
    if v is None:
        raise ValueError(f"op {op.get('op')!r} missing required param {key!r}")
    v = float(v)
    if not (lo <= abs(v) <= hi) and v != 0:
        raise ValueError(f"op {op.get('op')!r} param {key!r}={v} out of sane range")
    return v


def _at(wp, op):
    at = op.get("at")
    if at:
        wp = wp.translate(tuple(float(x) for x in at))
    return wp


def compile_construct(program: dict):
    """Compile a constructive program -> cq.Workplane solid. Raises on any invalid op/reference."""
    ops = program.get("construct")
    if not isinstance(ops, list) or not ops:
        raise ValueError("program has no 'construct' op list")
    if len(ops) > MAX_OPS:
        raise ValueError(f"too many ops ({len(ops)} > {MAX_OPS})")
    solids: dict[str, cq.Workplane] = {}
    last: cq.Workplane | None = None

    def ref(key, op, default_last=True):
        name = op.get(key)
        if name is None:
            if default_last and last is not None:
                return last
            raise ValueError(f"op {op.get('op')!r} missing reference {key!r}")
        if name not in solids:
            raise ValueError(f"op {op.get('op')!r} references unknown solid {name!r}")
        return solids[name]

    for op in ops:
        kind = op.get("op")
        if kind == "box":
            out = cq.Workplane("XY").box(_num(op, "w"), _num(op, "d"), _num(op, "h"),
                                         centered=(True, True, False))
            out = _at(out, op)
        elif kind == "cyl":
            out = cq.Workplane("XY").circle(_num(op, "d") / 2).extrude(_num(op, "h"))
            out = _at(out, op)
        elif kind == "sketch_extrude":
            pts = [(float(x), float(y)) for x, y in op["profile"]]
            if len(pts) < 3:
                raise ValueError("sketch_extrude profile needs >=3 points")
            out = cq.Workplane("XY").polyline(pts).close().extrude(_num(op, "dist"))
            out = _at(out, op)
        elif kind == "revolve":
            pts = [(float(x), float(z)) for x, z in op["profile"]]
            out = cq.Workplane("XZ").polyline(pts).close().revolve(float(op.get("angle", 360)))
            out = _at(out, op)
        elif kind in ("union", "cut", "intersect"):
            a = ref("a", op)
            b = ref("b", op, default_last=False)
            out = {"union": a.union, "cut": a.cut, "intersect": a.intersect}[kind](b)
        elif kind == "polar":
            tool = ref("of", op)
            n = int(op["n"])
            if not 1 <= n <= 64:
                raise ValueError("polar n out of range")
            r = _num(op, "r", lo=0)
            out = None
            for k in range(n):
                c = tool.translate((r, 0, 0)).val().rotate((0, 0, 0), (0, 0, 1), 360.0 * k / n)
                w = cq.Workplane(obj=c)
                out = w if out is None else out.union(w)
        elif kind in ("fillet", "chamfer"):
            base = ref("of", op)
            sel = _EDGE_SEL.get(op.get("edges", "all"), "__bad__")
            if sel == "__bad__":
                raise ValueError(f"unknown edges selector {op.get('edges')!r}")
            edges = base.edges(sel) if sel else base.edges()
            out = edges.fillet(_num(op, "r")) if kind == "fillet" else edges.chamfer(_num(op, "r"))
        elif kind == "shell":
            base = ref("of", op)
            open_face = op.get("open", "none")
            if open_face == "none":
                out = base.shell(-abs(_num(op, "t")))
            else:
                sel = {"top": ">Z", "bottom": "<Z"}.get(open_face)
                if sel is None:
                    raise ValueError(f"unknown shell open face {open_face!r}")
                out = base.faces(sel).shell(-abs(_num(op, "t")))
        elif kind == "move":
            base = ref("of", op)
            rot = op.get("rot")
            if rot:
                v = base.val()
                for axis, ang in zip(((1, 0, 0), (0, 1, 0), (0, 0, 1)), rot):
                    if ang:
                        v = v.rotate((0, 0, 0), axis, float(ang))
                base = cq.Workplane(obj=v)
            out = base.translate(tuple(float(x) for x in op.get("to", (0, 0, 0))))
        else:
            raise ValueError(f"unknown op {kind!r}; known: box,cyl,sketch_extrude,revolve,union,cut,"
                             f"intersect,polar,fillet,chamfer,shell,move")
        if op.get("id"):
            solids[op["id"]] = out
        last = out
    return solids.get("result", last)


def validity_report(wp) -> dict:
    """Tier-1 truth for imagined geometry: is it a real solid at all?"""
    from constraint_kit import dsl
    try:
        sig = dsl.signature(wp)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"signature failed: {type(exc).__name__}: {exc}"}
    solids = wp.solids().vals()
    closed = all(s.isValid() for s in solids)
    ok = bool(solids) and closed and sig["volume"] > 1e-6
    return {"ok": ok, "n_solids": len(solids), "valid_solids": closed, "signature": sig}


def spec_check(wp, spec: dict, *, tol_frac: float = 0.03) -> dict:
    """Tier-2 truth: do the REQUEST's checkable properties hold? (verification without a reference)
    spec keys (all optional): bbox_sorted [a,b,c] | volume_mm3 [min,max] | mass_g {max|min, material}
    | holes {count, diameter?} — counted as distinct cylindrical-face axes of matching radius."""
    from constraint_kit import dsl
    from constraint_kit.builder import DENSITY_G_MM3
    sig = dsl.signature(wp)
    checks = []

    def add(name, ok, got, want):
        checks.append({"check": name, "ok": bool(ok), "got": got, "want": want})

    if "bbox_sorted" in spec:
        want = sorted(float(x) for x in spec["bbox_sorted"])
        got = sig["bbox_sorted"]
        add("bbox_sorted", all(abs(g - w) <= tol_frac * max(w, 1) for g, w in zip(got, want)), got, want)
    if "volume_mm3" in spec:
        lo, hi = spec["volume_mm3"]
        add("volume_mm3", lo <= sig["volume"] <= hi, sig["volume"], [lo, hi])
    if "mass_g" in spec:
        density = DENSITY_G_MM3.get(str(spec["mass_g"].get("material", "steel")).lower(),
                                    DENSITY_G_MM3["steel"])
        mass = sig["volume"] * density
        ok = mass <= float(spec["mass_g"].get("max", 1e12)) and mass >= float(spec["mass_g"].get("min", 0))
        add("mass_g", ok, round(mass, 2), spec["mass_g"])
    if "holes" in spec:
        want_n = int(spec["holes"]["count"])
        want_d = spec["holes"].get("diameter")
        axes = set()
        for f in wp.faces().vals():
            if f.geomType() == "CYLINDER":
                ad = f._geomAdaptor()
                r = ad.Cylinder().Radius()
                if want_d is not None and abs(2 * r - float(want_d)) > tol_frac * float(want_d) + 0.05:
                    continue
                axis = ad.Cylinder().Axis()
                loc, direction = axis.Location(), axis.Direction()
                axes.add((round(loc.X(), 1), round(loc.Y(), 1),
                          round(abs(direction.X()), 2), round(abs(direction.Y()), 2),
                          round(abs(direction.Z()), 2), round(r, 2)))
        add("holes", len(axes) >= want_n, len(axes), want_n)
    return {"ok": all(c["ok"] for c in checks) and bool(checks), "checks": checks}
