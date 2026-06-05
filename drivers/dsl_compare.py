#!/usr/bin/env python3
"""Slice-2 measurement: direct NL->DSL  vs  NL->intent->DSL, head to head.

The intent path has exactly ONE LLM stage (parse: NL -> structured intent); everything after is
deterministic — intent.resolve fills gaps with provenance, to_program lowers to DSL, the kernel builds it
(/intent/resolve {build:true}). The hypothesis: emitting a high-level INTENT (state only what's mentioned,
defaults filled deterministically) is more reliable than emitting low-level DSL directly, and it resolves
ambiguity canonically + declines OOV at the resolve stage. We measure both paths on the same cases.

  DEEPSEEK_API_KEY=... python3 drivers/dsl_compare.py [N]
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dsl_experiment import CASES, cadkit, compare, deepseek, generate  # noqa: E402  (direct-path reuse)

PARSE_SYSTEM = (
    "Translate a part/assembly description into a structured INTENT (NOT geometry). Output ONLY JSON: "
    '{"entities":[{"id":str,"kind":str,"requirements":{param:value,...},"designation"?:str}],'
    '"interfaces":[{"a":id,"b":id,"relation":str}]}. '
    "kind = a part type you know (spacer, spur_gear, plate, shaft, washer, nut, bolt, panel, link, housing, "
    "sheet_bracket) OR, if you have NO matching type, the real name of the thing (synchronizer, helical_gear, "
    "tapered_bearing, shift_fork) — do NOT substitute a different part. "
    "requirements: include ONLY explicitly-stated values, OMIT anything not stated (defaults are filled "
    "downstream). Use generator param names: spacer{outer_d,bore_d,height}, spur_gear{module,teeth,width,"
    "bore_d}, plate{width,depth,thick,boss_d,boss_h,bolt_d,bolt_circle,bolt_count}, shaft{diameter,length}, "
    "washer{outer_d,bore_d,thick}, nut{af,height,bore_d}, bolt{shank_d,length,head_d,head_h}, "
    "panel{width,depth,height}, link{length,width,thickness}, sheet_bracket{thickness,base_length,"
    "flange_length,width}. interfaces relation in [seat_on,insert,fasten,mesh]. designation for standards "
    "('M6x1'). "
    "RULES: (1) List the FIXED BASE entity FIRST — the part others attach to (a plate, a housing, the "
    "ground/largest part). (2) In an interface, 'a' is that base/target and 'b' is the part placed onto it "
    "(plate is 'a', the gear/bolt/washer seated on it is 'b'; for fasten, the holed part like the plate is "
    "'a' and the fastener is 'b'). (3) Capture EVERY stated number, and encode negations/explicit values: "
    "'no bore'/'solid' -> bore_d:0. Output JSON only, no prose."
)
PARSE_FEWSHOT = [
    {"role": "user", "content": "a gear"},
    {"role": "assistant", "content": json.dumps({"entities": [{"id": "g", "kind": "spur_gear", "requirements": {}}], "interfaces": []})},
    {"role": "user", "content": "a module-2 spur gear, 40 teeth, 10mm wide, 14mm bore"},
    {"role": "assistant", "content": json.dumps({"entities": [{"id": "g", "kind": "spur_gear",
        "requirements": {"module": 2, "teeth": 40, "width": 10, "bore_d": 14}}], "interfaces": []})},
    {"role": "user", "content": "a 24-tooth m1 gear seated on a plate's boss"},
    {"role": "assistant", "content": json.dumps({"entities": [
        {"id": "plate", "kind": "plate", "requirements": {}},
        {"id": "g", "kind": "spur_gear", "requirements": {"module": 1, "teeth": 24}}],
        "interfaces": [{"a": "plate", "b": "g", "relation": "seat_on"}]})},
    {"role": "user", "content": "a solid spacer, 20mm OD, 10mm tall, no bore"},
    {"role": "assistant", "content": json.dumps({"entities": [{"id": "s", "kind": "spacer",
        "requirements": {"outer_d": 20, "bore_d": 0, "height": 10}}], "interfaces": []})},
    {"role": "user", "content": "a bolt fastened into the first hole of a plate"},
    {"role": "assistant", "content": json.dumps({"entities": [           # base (plate) FIRST; a=plate, b=bolt
        {"id": "plate", "kind": "plate", "requirements": {}},
        {"id": "b", "kind": "bolt", "requirements": {}}],
        "interfaces": [{"a": "plate", "b": "b", "relation": "fasten"}]})},
    {"role": "user", "content": "a synchronizer ring"},
    {"role": "assistant", "content": json.dumps({"entities": [{"id": "s", "kind": "synchronizer", "requirements": {}}], "interfaces": []})},
]


def parse_intent(desc):
    msgs = [{"role": "system", "content": PARSE_SYSTEM}, *PARSE_FEWSHOT, {"role": "user", "content": desc}]
    for _ in range(2):
        try:
            return json.loads(deepseek(msgs))
        except json.JSONDecodeError:
            msgs.append({"role": "user", "content": "Resend ONLY valid JSON."})
    return {"entities": []}


def generate_via_intent(desc):
    """One LLM stage (parse) -> deterministic resolve+build. Returns (program, signature, status)."""
    intent = parse_intent(desc)
    r = cadkit("/intent/resolve", {"intent": intent, "build": True})
    resolved = r.get("resolved", {})
    if resolved.get("declined"):
        return None, None, "declined"
    if not r.get("check", {}).get("ok"):
        return r.get("program"), None, "build-failed"
    return r.get("program"), r.get("signature"), None


def main():
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("DEEPSEEK_API_KEY not set"); return
    from collections import defaultdict
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    score = {"direct": defaultdict(lambda: [0, 0]), "intent": defaultdict(lambda: [0, 0])}
    oov = {"direct": defaultdict(int), "intent": defaultdict(int)}
    for cat, desc, ref in CASES:
        refsig = None
        if ref is not None:
            refsig = cadkit("/dsl/signature", {"program": ref}).get("signature")
        for _ in range(N):
            # direct path
            dprog, drounds, dstatus, dsig = generate(desc)
            # intent path
            iprog, isig, istatus = generate_via_intent(desc)
            if cat == "oov":
                oov["direct"]["declined" if dstatus == "declined" else "substituted"] += 1
                oov["intent"]["declined" if istatus == "declined" else "substituted"] += 1
                continue
            for path, sig in (("direct", dsig), ("intent", isig)):
                score[path][cat][1] += 1
                if sig is not None and not compare(sig, refsig):
                    score[path][cat][0] += 1
    print(f"\n=== direct NL->DSL  vs  NL->intent->DSL  (N={N} per case) ===")
    cats = sorted(set(score["direct"]) | set(score["intent"]))
    print(f"  {'category':10} {'direct':>10}   {'intent':>10}")
    for c in cats:
        d, i = score["direct"][c], score["intent"][c]
        print(f"  {c:10} {f'{d[0]}/{d[1]}':>10}   {f'{i[0]}/{i[1]}':>10}")
    print("  --- OOV honesty (declined is good) ---")
    print(f"  direct: {dict(oov['direct'])}")
    print(f"  intent: {dict(oov['intent'])}")


if __name__ == "__main__":
    main()
