#!/usr/bin/env python3
"""Lever-B proof: the OpenSCAD 'vitamin' tier end-to-end.

Builds an assembly of a native B-rep plate + a REAL 608 ball bearing rendered from BOSL2 (a 'vitamin' — a
sourced part we don't re-model), seated on the plate. Demonstrates render -> watertight cq solid ->
derived anchors -> mate through the EXISTING kernel -> one assembly + signature + export. Contrasts the real
vitamin against our simplified native `bearing` envelope.

Run in the container (needs openscad): docker exec cadkit python3 .../drivers/vitamin_demo.py
"""
from constraint_kit import builder, dsl

PROGRAM = {
    "parts": [
        {"id": "base", "type": "panel", "material": "aluminum",
         "params": {"width": 50, "depth": 50, "height": 6}},
        {"id": "brg", "type": "vitamin", "material": "steel",
         "params": {"scad": 'ball_bearing("608");', "name": "bearing_608", "fn": 48}},
    ],
    "mates": [{"a": "base", "a_joint": "top", "b": "brg", "b_joint": "base", "type": "coincident"}],
}


def main():
    chk = dsl.check(PROGRAM)
    print("check ok:", chk["ok"], "" if chk["ok"] else [d["message"] for d in chk["diagnostics"]])

    assy, _report = builder.build_assembly(PROGRAM)
    print("\n=== assembly (panel base + real BOSL2 608 vitamin, seated) ===")
    print("parts:", [p["type"] for p in PROGRAM["parts"]])
    print("signature:", dsl.signature(assy))

    # contrast: the REAL vitamin bearing vs our simplified native envelope, same nominal 608
    vwp, vanch = builder._generate("vitamin", {"scad": 'ball_bearing("608");', "fn": 48})
    nwp, _ = builder._generate("bearing", {"outer_d": 22, "bore_d": 8, "width": 7})
    vsig, nsig = dsl.signature(vwp), dsl.signature(nwp)
    print("\n=== vitamin (real BOSL2 608) vs native envelope ===")
    print(f"  vitamin: vol={vsig['volume']} faces={vsig['n_faces']} anchors={sorted(vanch)}")
    print(f"  native : vol={nsig['volume']} faces={nsig['n_faces']}")
    print(f"  native over-states vitamin by {round(100*(nsig['volume']-vsig['volume'])/vsig['volume'],1)}%")
    print(f"  vitamin tagged: {'_vitamin' in vanch}")


if __name__ == "__main__":
    main()
