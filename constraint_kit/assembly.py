"""Hierarchical assemblies — the composition foundation that scales from a part to a car to a home.

A node is an assembly definition:
    {
      "name": "wheel",
      "parts":  [ <builder leaf-part specs> ],          # optional: this node's own leaf geometry
      "mates":  [ <builder mates> ],                     # optional: places those leaf parts
      "children":[                                        # optional: sub-assemblies, each an instance
         {"instance":"wheel_l","ref":"wheel",            # ref -> another def in `defs` (reuse!)
          "place":{"port":"hub","at":[0,0,0],"axis":[0,0,1]}}],  # land child's port at this local frame
      "ports": {"hub":{"origin":[0,0,0],"z_axis":[0,0,1]}}  # frames this node EXPOSES to its parent
    }

Each node is built in its OWN local frame; a parent relocates a whole child as a unit by mating the
child's named **port** to a frame in the parent's space (via the same deterministic `mates.coincident`
that places leaf parts — ports are just assembly-level joints). Roll-ups (BOM, mass) accumulate bottom-up.
Geometry reuses `cadquery.Assembly` nesting and `builder.export`; no new geometry engine.
"""
from __future__ import annotations

from collections import Counter

import cadquery as cq

from . import builder, mates, parameters
from .joints import frame


def _port_frames(node: dict) -> dict:
    out = {}
    for name, p in (node.get("ports") or {}).items():
        out[name] = frame(p.get("origin", [0, 0, 0]), p.get("z_axis", [0, 0, 1]), p.get("x_axis"))
    return out


def build_tree(root: str, defs: dict, _stack: tuple = ()) -> dict:
    """Recursively build assembly `root` from the `defs` registry. Returns:
       {name, cq_assembly, ports{name:Location}, bom Counter, mass_g, part_count, leaf_count, depth}.
    Raises on unknown ref or a cyclic definition."""
    if root not in defs:
        raise ValueError(f"unknown assembly ref {root!r}; known: {sorted(defs)}")
    if root in _stack:
        raise ValueError(f"cyclic assembly definition: {' -> '.join((*_stack, root))}")
    node = defs[root]
    assy = cq.Assembly()
    bom: Counter = Counter()
    mass = 0.0
    leaf_count = 0
    depth = 1

    # this node's own leaf parts (via the existing deterministic builder)
    if node.get("parts"):
        leaf_assy, report = builder.build_assembly({"parts": node["parts"], "mates": node.get("mates", [])})
        assy.add(leaf_assy, name=f"{root}__parts")
        for rp in report:
            bom[rp["type"]] += 1
            mass += rp.get("mass_g", 0.0) or 0.0
            leaf_count += 1

    # child sub-assemblies (each an instance; same ref may appear many times)
    for child in node.get("children", []):
        cres = build_tree(child["ref"], defs, _stack=(*_stack, root))
        place = child.get("place", {})
        loc = cq.Location()
        if place.get("port"):
            port_local = cres["ports"][place["port"]]
            target = frame(place.get("at", [0, 0, 0]), place.get("axis", [0, 0, 1]), place.get("x_axis"))
            loc = mates.coincident(target, port_local)
        assy.add(cres["cq_assembly"], name=child.get("instance", child["ref"]), loc=loc)
        bom.update(cres["bom"])           # recursive roll-up
        mass += cres["mass_g"]
        leaf_count += cres["leaf_count"]
        depth = max(depth, cres["depth"] + 1)

    return {"name": root, "cq_assembly": assy, "ports": _port_frames(node),
            "bom": bom, "mass_g": round(mass, 2), "part_count": sum(bom.values()),
            "leaf_count": leaf_count, "depth": depth}


def report(result: dict) -> dict:
    """JSON-friendly summary (no cadquery objects): rolled-up BOM, mass, depth, exposed ports."""
    return {
        "name": result["name"], "mass_g": result["mass_g"], "part_count": result["part_count"],
        "depth": result["depth"], "ports": sorted(result["ports"]),
        "bom": dict(sorted(result["bom"].items())),
    }


def build_and_export(root: str, defs: dict, out_base: str) -> dict:
    """Build the tree, export one combined STEP+GLB, and return the report + artifact paths + bbox."""
    result = build_tree(root, defs)
    exported = builder.export(result["cq_assembly"], out_base)
    return {**report(result), **exported}


def check_interference(root: str, defs: dict, tol_volume: float = 0.01) -> dict:
    """Build the tree and run an interference/clash check on it (Phase C)."""
    from . import validate
    return validate.interference(build_tree(root, defs)["cq_assembly"], tol_volume)


def build_design(requirements: dict, relations: list[dict] | None, defs: dict, root: str,
                 asserts: list[dict] | None = None) -> dict:
    """TOP-DOWN design (Phase B): resolve requirements + relations into parameters (with provenance),
    substitute them into the assembly `defs` (geometry reads derived values), then build the tree.
    Refuses to build if a requirement assertion is violated or relations don't resolve.
    Returns {ok, parameters, warnings, tree}. `tree` is the AssemblyResult (or None if not built)."""
    res = parameters.resolve(requirements, relations or [])
    violations = parameters.check_asserts(asserts, res["values"])
    if not res["ok"] or violations:
        return {"ok": False, "parameters": res["params"], "warnings": res["warnings"] + violations,
                "tree": None}
    defs2 = parameters.substitute(defs, res["values"])    # requirements -> geometry params
    return {"ok": True, "parameters": res["params"], "warnings": [],
            "tree": build_tree(root, defs2)}


def design_and_export(requirements: dict, relations: list[dict] | None, defs: dict, root: str,
                      out_base: str, asserts: list[dict] | None = None) -> dict:
    """build_design + one combined STEP+GLB export. Returns report + parameter table + artifacts, or a
    structured ok:false (with warnings) if the design is invalid — never a half-built export."""
    d = build_design(requirements, relations, defs, root, asserts)
    if not d["ok"] or d["tree"] is None:
        return {"ok": False, "parameters": d["parameters"], "warnings": d["warnings"]}
    exported = builder.export(d["tree"]["cq_assembly"], out_base)
    return {"ok": True, **report(d["tree"]), "parameters": d["parameters"], **exported}
