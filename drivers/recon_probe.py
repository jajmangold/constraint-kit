#!/usr/bin/env python3
"""Stage-0 reconstruction de-risk: can a capable LLM reverse-engineer a KNOWN shape into a construct.py
op-list that builds and SIGNATURE-MATCHES it? Targets are OUR OWN parts (mesh + signature known, controlled
difficulty, zero external download). This isolates the constructive-generation+verify loop BEFORE touching
external CAD (ABC etc.). The trained model can't be probed here yet — it doesn't speak construct.py — so
DeepSeek is the generator stand-in measuring whether the LOOP is viable at all.

Measures: (1) reconstruction yield by complexity tier, (2) attempts-to-success (best-of-N),
(3) verifier sufficiency (decoy with matching bbox must be REJECTED). Run IN cadkit:
  DEEPSEEK_API_KEY=... python3 drivers/recon_probe.py
"""
from __future__ import annotations

import json
import os
import urllib.request

from constraint_kit import construct, dsl
from constraint_kit.parts import PART_GENS

KEY = os.environ.get("DEEPSEEK_API_KEY")
OUT = os.path.join(os.environ.get("CK_WORK_DIR", "/tmp/constraint-kit"), "cadkit/output/recon_probe.jsonl")
N_ATTEMPTS = 4

# (tier, name, generator-kwargs, one-line description) — spanning easy -> hard
TARGETS = [
    ("easy", "spacer", {"outer_d": 24, "bore_d": 10, "height": 16}, "a round spacer/sleeve with a through bore"),
    ("easy", "washer", {"outer_d": 20, "bore_d": 9, "thick": 2.5}, "a flat round washer with a center hole"),
    ("easy", "spacer", {"outer_d": 40, "bore_d": 0, "height": 8}, "a solid round disc, no hole"),
    ("easy", "shaft", {"diameter": 12, "length": 80}, "a plain cylindrical shaft"),
    ("med", "panel", {"width": 60, "depth": 40, "height": 8}, "a rectangular plate/panel block"),
    ("med", "plate", {"width": 60, "depth": 60, "thick": 6, "boss_d": 16, "boss_h": 5,
                      "bolt_d": 5.2, "bolt_circle": 44, "bolt_count": 4}, "a square mounting plate with a central round boss and 4 bolt holes on a circle"),
    ("med", "plate", {"width": 80, "depth": 50, "thick": 5, "boss_d": 0, "boss_h": 0,
                      "bolt_d": 6, "bolt_circle": 60, "bolt_count": 6}, "a rectangular plate with 6 bolt holes on a circle, no boss"),
    ("med", "spacer", {"outer_d": 30, "bore_d": 20, "height": 25}, "a thick-walled tube/sleeve"),
    ("hard", "pulley", {"outer_d": 40, "groove_d": 30, "width": 12, "groove_width": 6, "bore_d": 8},
     "a belt pulley: a cylinder with a V/U groove cut around its circumference and a center bore"),
    ("hard", "housing", {"width": 60, "depth": 60, "height": 40, "wall": 4, "bore_d": 20,
                        "bolt_d": 5, "bolt_circle": 44, "bolt_count": 4, "fillet": 3}, "an open-top box enclosure, hollowed to a wall thickness, with a bore in the floor and mount holes"),
    ("hard", "adapter", {"bottom_w": 50, "bottom_d": 50, "top_d": 30, "height": 40}, "a square-to-round transition/loft"),
    ("hard", "coupling", {"outer_d": 25, "bore_d": 8, "length": 30, "set_screw_d": 4}, "a shaft coupler: a cylinder with an axial bore and radial set-screw holes"),
]

OPS = ("box{w,d,h,at?} cyl{d,h,at?} sketch_extrude{profile:[[x,y]...],dist} revolve{profile:[[x,z]...],angle?} "
       "union{a,b} cut{a,b} intersect{a,b} polar{of,n,r} fillet{r,edges?} chamfer{r,edges?} shell{t,open?} move{of,to,rot?}")
FEWSHOT = (
    'EXAMPLE — "round spacer outer_d 20 bore 8 height 12":\n'
    '{"construct":[{"op":"cyl","d":20,"h":12,"id":"body"},{"op":"cyl","d":8,"h":12,"id":"bore"},'
    '{"op":"cut","a":"body","b":"bore","id":"result"}]}\n'
    'EXAMPLE — "square plate 60x60x6, central boss d16 h5, 4 bolt holes d5 on a 44mm circle":\n'
    '{"construct":[{"op":"box","w":60,"d":60,"h":6,"id":"base"},{"op":"cyl","d":16,"h":5,"at":[0,0,6],"id":"boss"},'
    '{"op":"union","a":"base","b":"boss","id":"p"},{"op":"cyl","d":5,"h":6,"id":"drill"},'
    '{"op":"polar","of":"drill","n":4,"r":22,"id":"holes"},{"op":"cut","a":"p","b":"holes","id":"result"}]}')
SYS = (f"You reverse-engineer a mechanical part into a CONSTRUCTIVE program. Ops act on named solids "
       f"(id names output; a/b/of reference inputs; centered XY, +Z up; angles deg). OPS: {OPS}. "
       f"{FEWSHOT}\nGiven the part description and its MEASURED bounding box + volume, output ONLY the JSON "
       f'program {{"construct":[...]}} whose built solid matches that shape. No prose.')


def ds(user, temp):
    body = {"model": "deepseek-v4-flash", "temperature": temp, "max_tokens": 900,
            "response_format": {"type": "json_object"}, "thinking": {"type": "disabled"},
            "messages": [{"role": "system", "content": SYS}, {"role": "user", "content": user}]}
    req = urllib.request.Request("https://api.deepseek.com/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    return json.loads(urllib.request.urlopen(req, timeout=180).read())["choices"][0]["message"]["content"]


def matches(sig, ref, tol=0.03):
    if not sig:
        return False
    dv = abs(sig["volume"] - ref["volume"]) / max(ref["volume"], 1e-9)
    da = abs(sig["area"] - ref["area"]) / max(ref["area"], 1e-9)
    db = max(abs(a - b) / max(b, 1.0) for a, b in zip(sig["bbox_sorted"], ref["bbox_sorted"]))
    return dv <= tol and da <= tol and db <= tol


def attempt(target):
    tier, name, params, desc = target
    try:
        wp, _ = PART_GENS[name](**params)
        ref = dsl.signature(wp)
    except Exception as exc:  # noqa: BLE001
        return {"tier": tier, "name": name, "desc": desc, "success": False,
                "attempts": [{"ok": False, "error": f"target-build: {exc}"}]}
    bb = ref["bbox_sorted"]
    hint = (f"{desc}. Measured bounding box (sorted) = [{bb[0]:.1f}, {bb[1]:.1f}, {bb[2]:.1f}] mm; "
            f"volume = {ref['volume']:.0f} mm^3.")
    rec = {"tier": tier, "name": name, "desc": desc, "ref": ref, "attempts": []}
    for i in range(N_ATTEMPTS):
        try:
            prog = json.loads(ds(hint, 0.2 if i == 0 else 0.6))
            wp2 = construct.compile_construct(prog)
            v = construct.validity_report(wp2)
            ok = v["ok"] and matches(v.get("signature"), ref)
            rec["attempts"].append({"ok": ok, "valid": v["ok"], "sig": v.get("signature")})
            if ok:
                rec["success_at"] = i + 1
                rec["program"] = prog
                break
        except Exception as exc:  # noqa: BLE001
            rec["attempts"].append({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:80]}"})
    rec["success"] = "success_at" in rec
    return rec


def main():
    if not KEY:
        print("DEEPSEEK_API_KEY not set"); return
    results = [attempt(t) for t in TARGETS]   # SERIAL: OCC geometry is not thread-safe (project doctrine)
    with open(OUT, "w") as fh:
        for r in results:
            fh.write(json.dumps(r) + "\n")

    # DECOY: a box with a target plate's bbox but wrong shape must be REJECTED (verifier sufficiency)
    plate = next(r for r in results if r["name"] == "plate" and r["ref"]["bbox_sorted"][1] >= 60)
    bb = plate["ref"]["bbox_sorted"]
    decoy = construct.compile_construct({"construct": [{"op": "box", "w": bb[2], "d": bb[1], "h": bb[0], "id": "result"}]})
    decoy_rejected = not matches(dsl.signature(decoy), plate["ref"])

    from collections import Counter
    tot = Counter(); win = Counter()
    for r in results:
        tot[r["tier"]] += 1; tot["ALL"] += 1
        if r["success"]:
            win[r["tier"]] += 1; win["ALL"] += 1
    print("=== Stage-0 reconstruction probe (DeepSeek -> construct.py -> signature match) ===")
    for tier in ("easy", "med", "hard", "ALL"):
        if tot[tier]:
            print(f"  {tier:5} {win[tier]}/{tot[tier]} reconstructed")
    succ = [r["success_at"] for r in results if r["success"]]
    if succ:
        print(f"  mean attempts-to-success: {sum(succ)/len(succ):.1f} (of {N_ATTEMPTS})")
    print(f"  decoy (same-bbox wrong-shape) REJECTED by signature: {decoy_rejected}")
    print("  failures:", [f"{r['tier']}/{r['name']}" for r in results if not r["success"]])
    print(f"  -> {OUT}")


if __name__ == "__main__":
    main()
