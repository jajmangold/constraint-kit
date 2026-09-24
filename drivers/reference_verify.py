#!/usr/bin/env python3
"""Reference-signature recorder + native-vs-bd_warehouse cross-verify.

First slice of: use known-good parts WE DIDN'T MAKE + the verify process to ground coverage in REALITY
(and break the circularity of verifying our builds only against our own generators).

bd_warehouse / build123d is real, standards-accurate geometry we wrap but did not author. This:
  (a) records reference SIGNATURES for those real parts (provenance-tagged) — the ground-truth catalog that
      future new-part authoring / auto-screening will target;
  (b) cross-verifies our simplified NATIVE generators against the real equivalent at matching nominal size
      and reports the geometric DELTA — turning a hidden simplification (our 'bearing' is a bare envelope)
      into a measured, honest number.

Runs IN the container (needs the geometry kernel):
  docker exec cadkit python3 ${CK_WORK_DIR:-/tmp/constraint-kit}/drivers/reference_verify.py
"""
import json

from constraint_kit import builder, dsl

OUT = os.path.join(os.environ.get("CK_WORK_DIR", "/tmp/constraint-kit"), "cadkit/output/reference_signatures.json")

# (our native part) vs (the bd_warehouse REAL equivalent) at matching nominal size -> quantify our simplification
CROSS = [
    {"label": "deep-groove bearing 608 (8x22x7)",
     "native": ("bearing", {"outer_d": 22, "bore_d": 8, "width": 7}),
     "real":   ("ball_bearing", {"size": "M8-22-7"})},
    {"label": "M5x20 fastener (our hex bolt vs real socket-head screw)",
     "native": ("bolt", {"shank_d": 5, "length": 20, "head_d": 8.5, "head_h": 5}),
     "real":   ("screw", {"size": "M5-0.8", "length": 20})},
]

# real bd_warehouse parts recorded as ground-truth reference signatures (we wrap them, didn't author them)
REAL_REFS = [
    ("ball_bearing", {"size": "M8-22-7"}),
    ("ball_bearing", {"size": "M10-30-9"}),
    ("screw", {"size": "M5-0.8", "length": 20}),
    ("screw", {"size": "M3-0.5", "length": 10}),
    ("flange", {"nps": "2", "flange_class": 150, "kind": "weld_neck"}),
    ("pipe", {"nps": "2", "length": 100}),
    ("extrusion", {"rail_size": "20x20", "length": 100}),
]


def sig(ptype, params):
    wp, _ = builder._generate(ptype, params)
    return dsl.signature(wp)


def delta(ours, real):
    d = {}
    for k in ("volume", "area"):
        d[f"{k}_rel"] = round((ours[k] - real[k]) / real[k], 3) if real[k] else None
    d["faces_ours_real"] = (ours["n_faces"], real["n_faces"])
    d["solids_ours_real"] = (ours["n_solids"], real["n_solids"])
    return d


def main():
    db = {"real_references": [], "cross_verify": []}

    for ptype, params in REAL_REFS:
        try:
            db["real_references"].append({
                "type": ptype, "params": params, "signature": sig(ptype, params),
                "provenance": {"source": "bd_warehouse/build123d", "license": "Apache-2.0",
                               "note": "real standards-accurate geometry, not authored by us"}})
        except Exception as exc:  # noqa: BLE001 -- fail-soft per part
            db["real_references"].append({"type": ptype, "params": params,
                                          "error": f"{type(exc).__name__}: {exc}"})

    print("=== native (ours) vs bd_warehouse (real) — geometric delta ===")
    for c in CROSS:
        try:
            ns, rs = sig(*c["native"]), sig(*c["real"])
            dl = delta(ns, rs)
            db["cross_verify"].append({"label": c["label"], "native": ns, "real": rs, "delta": dl})
            print(f"  {c['label']}")
            print(f"    ours: vol={ns['volume']} area={ns['area']} faces={ns['n_faces']} solids={ns['n_solids']}")
            print(f"    real: vol={rs['volume']} area={rs['area']} faces={rs['n_faces']} solids={rs['n_solids']}")
            print(f"    delta: vol_rel={dl['volume_rel']} area_rel={dl['area_rel']} faces(ours,real)={dl['faces_ours_real']}")
        except Exception as exc:  # noqa: BLE001
            print(f"  {c['label']}: ERROR {type(exc).__name__}: {exc}")
            db["cross_verify"].append({"label": c["label"], "error": str(exc)})

    json.dump(db, open(OUT, "w"), indent=2)
    ok = len([r for r in db["real_references"] if "signature" in r])
    print(f"\nrecorded {ok}/{len(REAL_REFS)} real reference signatures -> {OUT}")


if __name__ == "__main__":
    main()
