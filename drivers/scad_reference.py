#!/usr/bin/env python3
"""Record OpenSCAD/BOSL2 ground-truth signatures (open-licensed, parametric, BSD-2) + cross-verify our
native generators against them.

Renders each BOSL2 module to STL headless, takes the representation-INDEPENDENT mesh signature
(volume+area+bbox via dsl.mesh_signature), and writes them to constraint_kit/reference_signatures.json with
provenance. Render-to-verify: a signature is recorded ONLY if the part rendered to a watertight, non-empty
mesh — wrong module params fail loudly and are logged, never faked. For parts we already have natively
(spur_gear, bearing) it also reports the fidelity DELTA; for DEMANDED parts BOSL2 provides (knuckle_hinge)
the signature is the acceptance TARGET a future authored generator must hit.

Run in the container (openscad is a ground-truth GENERATION tool, not a runtime dep):
  docker exec cadkit python3 /srv/nvme-data/containers/constraint-kit/drivers/scad_reference.py
"""
import json
import os
import subprocess
import tempfile

from constraint_kit import builder, dsl

OUT = "/srv/nvme-data/containers/constraint-kit/constraint_kit/reference_signatures.json"
PRELUDE = ("$fn=64;\ninclude <BOSL2/std.scad>\ninclude <BOSL2/gears.scad>\n"
           "include <BOSL2/hinges.scad>\ninclude <BOSL2/ball_bearings.scad>\n")

# BOSL2 ground-truth parts: name -> module call (render-to-verify; failures logged)
BOSL2_PARTS = {
    "spur_gear_m1_t24_w6_b8": "spur_gear(mod=1, teeth=24, thickness=6, shaft_diam=8);",
    "ring_gear_m1_t36_w6":    "ring_gear(mod=1, teeth=36, thickness=6, backing=3);",
    "bevel_gear_m1_t20":      "bevel_gear(mod=1, teeth=20, face_width=6, pitch_angle=45, shaft_diam=6);",
    "ball_bearing_608":       'ball_bearing("608");',
    "knuckle_hinge_L60":      "knuckle_hinge(length=60, segs=5, offset=5, knuckle_diam=6, pin_diam=3);",
}
# BOSL2 part name -> (our native part, params) for a fidelity cross-verify against open ground truth
CROSS = {
    "spur_gear_m1_t24_w6_b8": ("spur_gear", {"module": 1, "teeth": 24, "width": 6, "bore_d": 8}),
    "ball_bearing_608":       ("bearing", {"outer_d": 22, "bore_d": 8, "width": 7}),
}


def render(body):
    scad, stl = tempfile.mktemp(suffix=".scad"), tempfile.mktemp(suffix=".stl")
    with open(scad, "w") as f:
        f.write(PRELUDE + body + "\n")
    r = subprocess.run(["openscad", "-o", stl, scad], capture_output=True, text=True, timeout=300)
    try:
        if not os.path.exists(stl) or os.path.getsize(stl) == 0:
            return None, (r.stderr.strip().splitlines() or ["no stl produced"])[-1]
        import trimesh
        m = trimesh.load(stl)
        if not m.is_watertight or m.volume <= 0:
            return None, "not watertight / empty"
        return dsl.mesh_signature(stl), None
    finally:
        for p in (scad, stl):
            if os.path.exists(p):
                os.remove(p)


def main():
    db = {"mesh_references": [], "cross_verify": []}
    for name, body in BOSL2_PARTS.items():
        sig, err = render(body)
        if sig is None:
            print(f"  [FAIL] {name}: {err}")
            db["mesh_references"].append({"name": name, "scad": body, "error": str(err)})
            continue
        db["mesh_references"].append({"name": name, "scad": body, "signature": sig,
                                      "provenance": {"source": "BOSL2", "license": "BSD-2-Clause", "fn": 64}})
        print(f"  [OK]   {name}: vol={sig['volume']} bbox={sig['bbox_sorted']}")
        if name in CROSS:
            pt, pp = CROSS[name]
            ours = dsl.signature(builder._generate(pt, pp)[0])
            dv = round((ours["volume"] - sig["volume"]) / sig["volume"], 3)
            db["cross_verify"].append({"name": name, "ours": pt, "our_vol": ours["volume"],
                                       "bosl2_vol": sig["volume"], "vol_rel": dv})
            print(f"         cross-verify our {pt}: vol_rel={dv:+}")
    with open(OUT, "w") as f:
        json.dump(db, f, indent=2)
    ok = len([r for r in db["mesh_references"] if "signature" in r])
    print(f"\nrecorded {ok}/{len(BOSL2_PARTS)} BOSL2 mesh reference signatures -> {OUT}")


if __name__ == "__main__":
    main()
