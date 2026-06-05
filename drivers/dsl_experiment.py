#!/usr/bin/env python3
"""Measurement, not theory: run the generate->check->verify loop over a diverse case set and report the
pass rate + failure modes per category. The verifier makes every output objectively pass/fail, so the
failure DISTRIBUTION tells us the true binding constraint (model decomposition vs DSL expressiveness vs
verifier gaps vs prompt) instead of us guessing.

Each case has a description + an authored reference PROGRAM; we compile the reference to its signature, ask
deepseek-v4-flash (thinking disabled) for a program from the description, self-correct via /dsl/check, then
compare the candidate's signature to the reference. 'oov' cases have no reference — they probe honest
failure on inexpressible parts (synchros, helical, tapered bearings).

  DEEPSEEK_API_KEY=... python3 drivers/dsl_experiment.py
"""
from __future__ import annotations

import json
import os
import urllib.request

DEEPSEEK = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-v4-flash"
KEY = os.environ.get("DEEPSEEK_API_KEY")
CADKIT = os.environ.get("CADKIT_URL", "http://127.0.0.1:8195")
TOL = 1e-2

SYSTEM = (
    "Translate a plain-English mechanical-part/assembly description into a constraint-kit DSL program. "
    'Output ONLY JSON: {"parts":[{"id","type","material","params":{...}}],"mates":[...]}. '
    "PART TYPES + params (mm): spacer{outer_d,bore_d,height}; spur_gear{module,teeth,width,bore_d}; "
    "plate{width,depth,thick,boss_d,boss_h,bolt_d,bolt_circle,bolt_count}; shaft{diameter,length}; "
    "washer{outer_d,bore_d,thick}; nut{af,height,bore_d}; bolt{shank_d,length,head_d,head_h}; "
    "panel{width,depth,height}; link{length,width,thickness}; housing{width,depth,height,wall,bore_d}; "
    "sheet_bracket{thickness,base_length,flange_length,width}. "
    "MATES connect parts; use either {\"a\",\"b\",\"intent\"} with intent in "
    "[seat_on,insert,fasten,mesh], OR {\"a\",\"a_joint\",\"b\",\"b_joint\",\"type\"} with type in "
    "[coincident,rigid,contact,mesh,revolute]. plate anchors: mount,bolt0..N. spur_gear: bore_base,bore_top. "
    "spacer: top,bottom. The FIRST part is the fixed base. "
    "HONESTY RULE: if the description needs a part type or feature you have NO match for (synchronizer, "
    "helical/bevel/worm gear, spline, tapered/roller/needle bearing, shift fork, threads-as-feature), do "
    'NOT substitute a different part — output exactly {"unsupported":"<what you cannot express>"}. '
    "Output JSON only, no prose."
)
FEWSHOT = [
    {"role": "user", "content": "a steel spacer, 14mm OD, 6mm bore, 10mm tall"},
    {"role": "assistant", "content": json.dumps({"parts": [{"id": "sp", "type": "spacer", "material": "steel",
        "params": {"outer_d": 14, "bore_d": 6, "height": 10}}], "mates": []})},
    {"role": "user", "content": "a 20-tooth module-1 spur gear seated on a plate's boss"},
    {"role": "assistant", "content": json.dumps({"parts": [
        {"id": "plate", "type": "plate", "material": "aluminum", "params": {}},
        {"id": "gear", "type": "spur_gear", "material": "steel", "params": {"module": 1, "teeth": 20, "width": 6, "bore_d": 12}}],
        "mates": [{"a": "plate", "b": "gear", "intent": "seat_on"}]})},
    {"role": "user", "content": "a synchronizer hub with blocker ring and cone clutch"},
    {"role": "assistant", "content": json.dumps({"unsupported": "no synchronizer / cone-clutch / dog-tooth part in the vocabulary"})},
]

def P(parts, mates=None):
    return {"parts": parts, "mates": mates or []}

CASES = [
    # ---- single parts (varied types + params) ----
    ("single", "a solid aluminum spacer, 30mm diameter, 12mm tall, no bore",
     P([{"id": "s", "type": "spacer", "material": "aluminum", "params": {"outer_d": 30, "bore_d": 0, "height": 12}}])),
    ("single", "a steel spacer ring, 25mm OD, 10mm bore, 8mm tall",
     P([{"id": "s", "type": "spacer", "material": "steel", "params": {"outer_d": 25, "bore_d": 10, "height": 8}}])),
    ("single", "a module-2 spur gear with 40 teeth, 10mm face width, 14mm bore",
     P([{"id": "g", "type": "spur_gear", "material": "steel", "params": {"module": 2, "teeth": 40, "width": 10, "bore_d": 14}}])),
    ("single", "an involute spur gear, module 1.5, 18 teeth, 6mm wide, 8mm bore",
     P([{"id": "g", "type": "spur_gear", "material": "steel", "params": {"module": 1.5, "teeth": 18, "width": 6, "bore_d": 8}}])),
    ("single", "a round steel shaft, 16mm diameter, 100mm long",
     P([{"id": "sh", "type": "shaft", "material": "steel", "params": {"diameter": 16, "length": 100}}])),
    ("single", "a flat washer, 24mm outer diameter, 12mm hole, 3mm thick",
     P([{"id": "w", "type": "washer", "material": "steel", "params": {"outer_d": 24, "bore_d": 12, "thick": 3}}])),
    ("single", "a hex nut, 13mm across flats, 6mm tall, 8mm bore",
     P([{"id": "n", "type": "nut", "material": "steel", "params": {"af": 13, "height": 6, "bore_d": 8}}])),
    ("single", "a hex-head bolt, 8mm shank, 30mm long, 13mm head across, 5mm head height",
     P([{"id": "b", "type": "bolt", "material": "steel", "params": {"shank_d": 8, "length": 30, "head_d": 13, "head_h": 5}}])),
    ("single", "a flat panel, 100 x 60 x 18 mm",
     P([{"id": "p", "type": "panel", "material": "pla", "params": {"width": 100, "depth": 60, "height": 18}}])),
    ("single", "a linkage bar 120mm long, 10mm wide, 5mm thick",
     P([{"id": "l", "type": "link", "material": "steel", "params": {"length": 120, "width": 10, "thickness": 5}}])),
    ("single", "a square mounting plate 80x80x6mm with a 12mm boss 4mm tall and 4 bolt holes on a 60mm circle, 5mm holes",
     P([{"id": "p", "type": "plate", "material": "aluminum", "params": {"width": 80, "depth": 80, "thick": 6, "boss_d": 12, "boss_h": 4, "bolt_d": 5, "bolt_circle": 60, "bolt_count": 4}}])),
    ("single", "a 90-degree sheet-metal bracket, 2mm thick, 40mm base, 25mm flange, 30mm wide",
     P([{"id": "br", "type": "sheet_bracket", "material": "steel", "params": {"thickness": 2, "base_length": 40, "flange_length": 25, "width": 30}}])),
    # ---- assemblies (mates / intents) ----
    ("assembly", "a 24-tooth module-1 spur gear (6mm wide, 12mm bore) seated on the boss of a default plate",
     P([{"id": "plate", "type": "plate", "material": "aluminum", "params": {}},
        {"id": "g", "type": "spur_gear", "material": "steel", "params": {"module": 1, "teeth": 24, "width": 6, "bore_d": 12}}],
       [{"a": "plate", "b": "g", "intent": "seat_on"}])),
    ("assembly", "two identical solid spacers (20mm OD, 10mm tall, no bore) stacked one on the other",
     P([{"id": "a", "type": "spacer", "material": "steel", "params": {"outer_d": 20, "bore_d": 0, "height": 10}},
        {"id": "b", "type": "spacer", "material": "steel", "params": {"outer_d": 20, "bore_d": 0, "height": 10}}],
       [{"a": "a", "a_joint": "top", "b": "b", "b_joint": "bottom", "type": "coincident"}])),
    ("assembly", "an M5 hex bolt (5mm shank, 16 long, 9mm head, 4mm head height) fastened into the first bolt hole of a default plate",
     P([{"id": "plate", "type": "plate", "material": "aluminum", "params": {}},
        {"id": "b", "type": "bolt", "material": "steel", "params": {"shank_d": 5, "length": 16, "head_d": 9, "head_h": 4}}],
       [{"a": "plate", "a_joint": "bolt0", "b": "b", "b_joint": "seat", "type": "coincident"}])),
    ("assembly", "a default-plate with a 14mm-OD/6mm-bore/10mm spacer seated on its boss and a 24T m1 gear on top of the spacer",
     P([{"id": "plate", "type": "plate", "material": "aluminum", "params": {}},
        {"id": "sp", "type": "spacer", "material": "steel", "params": {"outer_d": 14, "bore_d": 6, "height": 10}},
        {"id": "g", "type": "spur_gear", "material": "steel", "params": {"module": 1, "teeth": 24, "width": 6, "bore_d": 12}}],
       [{"a": "plate", "a_joint": "mount", "b": "sp", "b_joint": "bottom", "type": "coincident"},
        {"a": "sp", "a_joint": "top", "b": "g", "b_joint": "bore_base", "type": "coincident"}])),
    ("assembly", "a washer (18 OD, 8 hole, 2 thick) sitting on the boss of a default plate",
     P([{"id": "plate", "type": "plate", "material": "aluminum", "params": {}},
        {"id": "w", "type": "washer", "material": "steel", "params": {"outer_d": 18, "bore_d": 8, "thick": 2}}],
       [{"a": "plate", "b": "w", "intent": "seat_on"}])),
    ("assembly", "two meshing module-1 spur gears, 20 and 30 teeth, both 6mm wide, 8mm bores",
     P([{"id": "g1", "type": "spur_gear", "material": "steel", "params": {"module": 1, "teeth": 20, "width": 6, "bore_d": 8}},
        {"id": "g2", "type": "spur_gear", "material": "steel", "params": {"module": 1, "teeth": 30, "width": 6, "bore_d": 8}}],
       [{"a": "g1", "a_joint": "bore_base", "b": "g2", "b_joint": "bore_base", "type": "mesh"}])),
    # ---- ambiguous / under-specified (ref = the most reasonable reading) ----
    ("ambiguous", "a small spacer",
     P([{"id": "s", "type": "spacer", "material": "steel", "params": {"outer_d": 14, "bore_d": 6, "height": 10}}])),
    ("ambiguous", "a gear",
     P([{"id": "g", "type": "spur_gear", "material": "steel", "params": {"module": 1, "teeth": 20, "width": 6, "bore_d": 8}}])),
    # ---- out-of-vocab honesty probes (no reference: should fail honestly, not hallucinate) ----
    ("oov", "a synchronizer blocker ring with cone clutch and dog teeth", None),
    ("oov", "a helical gear, 30 teeth, 20-degree helix angle", None),
    ("oov", "a tapered roller bearing, 30mm bore", None),
    ("oov", "a ball-detent shift fork for a manual gearbox", None),
]


def _post(url, payload, headers, timeout=120):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def deepseek(messages, temperature=0.2):
    body = {"model": MODEL, "messages": messages, "temperature": temperature,
            "response_format": {"type": "json_object"}, "thinking": {"type": "disabled"}}
    return _post(DEEPSEEK, body, {"Authorization": f"Bearer {KEY}"})["choices"][0]["message"]["content"]


def cadkit(path, payload):
    return _post(f"{CADKIT}{path}", payload, {})


def generate(desc, max_rounds=4):
    """Two-tier self-correction: static /dsl/check, THEN compile via /dsl/signature (catches what static
    can't). Returns (prog, rounds, status, candidate_signature). status: None=compiles, 'declined'=honest
    refusal, 'check-failed'/'compile-failed'=exhausted correction rounds."""
    msgs = [{"role": "system", "content": SYSTEM}, *FEWSHOT, {"role": "user", "content": desc}]
    prog, rounds, status = None, 0, "check-failed"
    for rounds in range(1, max_rounds + 1):
        try:
            prog = json.loads(deepseek(msgs))
        except json.JSONDecodeError:
            msgs.append({"role": "user", "content": "Resend ONLY valid JSON."}); continue
        if isinstance(prog, dict) and prog.get("unsupported"):
            return prog, rounds, "declined", None
        chk = cadkit("/dsl/check", {"program": prog})
        if not chk["ok"]:
            status = "check-failed"
            msgs += [{"role": "assistant", "content": json.dumps(prog)},
                     {"role": "user", "content": "Fix these and resend full JSON:\n"
                      + json.dumps([d for d in chk["diagnostics"] if d["severity"] == "error"])}]
            continue
        sig = cadkit("/dsl/signature", {"program": prog})
        if sig.get("ok"):
            return prog, rounds, None, sig["signature"]
        status = "compile-failed"
        msgs += [{"role": "assistant", "content": json.dumps(prog)},
                 {"role": "user", "content": "It passed static checks but FAILED to build: "
                  + str(sig.get("reason") or sig.get("diagnostics")) + ". Fix and resend the full JSON."}]
    return prog, rounds, status, None


def compare(cand_sig, ref_sig):
    fails = []
    for k, rv in ref_sig.items():
        sv = cand_sig.get(k)
        if k in ("n_solids", "n_faces", "n_edges"):
            if sv != rv:
                fails.append(k)
        elif k == "bbox_sorted":
            if sv is None or any(abs(a - b) > TOL * max(abs(b), 1e-9) for a, b in zip(sv, rv)):
                fails.append(k)
        else:
            if sv is None or abs(sv - rv) > TOL * max(abs(rv), 1e-9):
                fails.append(k)
    return fails


def main():
    if not KEY:
        print("DEEPSEEK_API_KEY not set"); return
    import sys
    from collections import defaultdict
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 3     # samples per case (temp>0 -> a real rate)
    tally = defaultdict(lambda: [0, 0])                  # cat -> [pass, total]
    modes = defaultdict(int)
    refcache = {}
    for cat, desc, ref in CASES:
        refsig = None
        if ref is not None:
            key = json.dumps(ref, sort_keys=True)
            refsig = refcache.get(key) or cadkit("/dsl/signature", {"program": ref}).get("signature")
            refcache[key] = refsig
        passes = 0
        for _ in range(N):
            prog, rounds, status, candsig = generate(desc)
            if cat == "oov":                             # honest = declined (refused to fake it)
                declined = status == "declined"
                tally["oov"][1] += 1; tally["oov"][0] += int(declined)
                modes["oov_declined" if declined else "oov_substituted"] += 1
                continue
            tally[cat][1] += 1
            if status == "declined":
                modes["false-decline"] += 1; continue    # refused something it CAN build -> wrong
            if candsig is None:
                modes[status or "uncompilable"] += 1; continue
            fails = compare(candsig, refsig)
            if not fails:
                tally[cat][0] += 1; passes += 1
            else:
                for f in fails:
                    modes[f"mismatch:{f}"] += 1
        if cat != "oov":
            print(f"  [{cat:9}] {passes}/{N}  :: {desc[:52]}")
    print(f"\n=== pass rate by category (N={N} per case) ===")
    for cat, (p, t) in sorted(tally.items()):
        print(f"  {cat:10} {p}/{t}  ({100*p//max(t,1)}%)")
    print("=== failure / behaviour modes ===")
    for m, c in sorted(modes.items(), key=lambda x: -x[1]):
        print(f"  {m}: {c}")


if __name__ == "__main__":
    main()
