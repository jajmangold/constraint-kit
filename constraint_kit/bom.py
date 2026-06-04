"""BOM document export (T7.3) — render a build-tree result as a Bill of Materials (Markdown or CSV) for
procurement/manufacturing. Pure formatting over the hierarchy roll-up (bom counts + mass_by_type + totals
+ CG); no geometry recompute. The numbers are the same ones the kernel already validated."""
from __future__ import annotations

import csv
import io


def _rows(result: dict) -> list:
    bom = result.get("bom", {})
    mbt = result.get("mass_by_type", {})
    out = []
    for ptype in sorted(bom):
        qty = bom[ptype]
        total = float(mbt.get(ptype, 0.0))
        out.append((ptype, qty, round(total, 3), round(total / qty, 3) if qty else 0.0))
    return out


def bom_document(result: dict, fmt: str = "md") -> str:
    """Render `result` (from assembly.build_tree) as a BOM. fmt: 'md' (default) or 'csv'."""
    rows = _rows(result)
    name = result.get("name", "assembly")
    part_count = result.get("part_count", sum(result.get("bom", {}).values()))
    total_mass = round(float(result.get("mass_g", 0.0)), 3)

    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["part_type", "qty", "total_mass_g", "unit_mass_g"])
        for r in rows:
            w.writerow(r)
        w.writerow([])
        w.writerow(["TOTAL", part_count, total_mass, ""])
        return buf.getvalue()

    if fmt != "md":
        raise ValueError(f"unknown BOM format {fmt!r}; use 'md' or 'csv'")

    lines = [
        f"# BOM — {name}", "",
        f"- parts: **{part_count}**  ·  mass: **{total_mass} g**  ·  depth: {result.get('depth', '?')}",
        f"- centre of gravity (mm): {result.get('cg')}", "",
        "| Part Type | Qty | Total Mass (g) | Unit Mass (g) |",
        "|---|---:|---:|---:|",
    ]
    for ptype, qty, total, unit in rows:
        lines.append(f"| {ptype} | {qty} | {total} | {unit} |")
    lines.append(f"| **TOTAL** | **{part_count}** | **{total_mass}** | |")
    return "\n".join(lines) + "\n"
