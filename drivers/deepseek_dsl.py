#!/usr/bin/env python3
"""DeepSeek V4-flash → constraint-kit DSL, corrected by the checker, verified by geometry → training pairs.

Closes the loop on a small scale: for each reference part, ask `deepseek-v4-flash` (THINKING DISABLED — cheap,
fast, deterministic) to emit a DSL program from a text description; validate via cadkit `/dsl/check` and feed
the diagnostics back to self-correct; then `/dsl/verify` the program's EXACT compiled volume against the known
reference. Keep matches as (description, program) verified pairs in JSONL — the seed of an RL/SFT corpus.

This is the *mechanism*; scaling it to "a ton" of pairs is a generation campaign (V4-flash does it cheaply at
high concurrency). RL on a ~30B model is a separate, GPU-heavy research step that consumes this corpus.

  DEEPSEEK_API_KEY=... python3 drivers/deepseek_dsl.py
"""
from __future__ import annotations

import json
import os
import urllib.request

DEEPSEEK = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-v4-flash"
KEY = os.environ.get("DEEPSEEK_API_KEY")
CADKIT = os.environ.get("CADKIT_URL", "http://127.0.0.1:8195")
OUT = "/srv/nvme-data/containers/constraint-kit/cadkit/output/dsl_corpus.jsonl"

SYSTEM = (
    "You translate a plain-English part description into a constraint-kit DSL program. Output ONLY JSON, "
    'shape: {"parts":[{"id":str,"type":str,"material":str,"params":{...}}],"mates":[]}. '
    "Known part types and params: "
    "spacer{outer_d,bore_d,height}; spur_gear{module,teeth,width,bore_d}; plate{width,depth,thick,boss_d,"
    "boss_h,bolt_d,bolt_circle,bolt_count}; shaft{diameter,length}; washer{outer_d,bore_d,thick}; "
    "nut{af,height,bore_d}; bolt{shank_d,length,head_d,head_h}; panel{width,depth,height}; "
    "link{length,width,thickness}. Use millimetres. One part unless asked otherwise. No prose, JSON only."
)
FEWSHOT = [
    {"role": "user", "content": "a 14mm OD steel spacer, 6mm bore, 10mm tall"},
    {"role": "assistant", "content": json.dumps(
        {"parts": [{"id": "sp", "type": "spacer", "material": "steel",
                    "params": {"outer_d": 14, "bore_d": 6, "height": 10}}], "mates": []})},
    {"role": "user", "content": "an involute spur gear, module 2, 30 teeth, 8mm face width, 10mm bore"},
    {"role": "assistant", "content": json.dumps(
        {"parts": [{"id": "g", "type": "spur_gear", "material": "steel",
                    "params": {"module": 2, "teeth": 30, "width": 8, "bore_d": 10}}], "mates": []})},
]

REFERENCES = [
    {"desc": "a 20mm-diameter solid aluminum spacer, 10mm tall (no bore)",
     "ref": {"type": "spacer", "params": {"outer_d": 20, "bore_d": 0, "height": 10}}},
    {"desc": "an involute spur gear, module 1, 24 teeth, 6mm wide, 8mm bore",
     "ref": {"type": "spur_gear", "params": {"module": 1, "teeth": 24, "width": 6, "bore_d": 8}}},
    {"desc": "a flat washer, 18mm outer diameter, 8mm hole, 2mm thick",
     "ref": {"type": "washer", "params": {"outer_d": 18, "bore_d": 8, "thick": 2}}},
    {"desc": "a round shaft, 12mm diameter, 80mm long",
     "ref": {"type": "shaft", "params": {"diameter": 12, "length": 80}}},
]


def _post(url, payload, headers, timeout=120):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def deepseek(messages):
    body = {"model": MODEL, "messages": messages, "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"}}            # minimize thinking (V4): cheap, fast, no CoT
    r = _post(DEEPSEEK, body, {"Authorization": f"Bearer {KEY}"})
    return r["choices"][0]["message"]["content"]


def cadkit(path, payload):
    return _post(f"{CADKIT}{path}", payload, {})


def generate_corrected(desc, max_rounds=3):
    """Generate a DSL program and self-correct using cadkit /dsl/check diagnostics (the LSP loop)."""
    msgs = [{"role": "system", "content": SYSTEM}, *FEWSHOT, {"role": "user", "content": desc}]
    prog, rounds, res = None, 0, {"diagnostics": []}
    for rounds in range(1, max_rounds + 1):
        out = deepseek(msgs)
        try:
            prog = json.loads(out)
        except json.JSONDecodeError:
            msgs += [{"role": "assistant", "content": out},
                     {"role": "user", "content": "That was not valid JSON. Resend ONLY the JSON program."}]
            continue
        res = cadkit("/dsl/check", {"program": prog})
        if res["ok"]:
            return prog, rounds, []
        msgs += [{"role": "assistant", "content": json.dumps(prog)},
                 {"role": "user", "content": "The checker reported these errors — fix ALL and resend the "
                  "full JSON program:\n" + json.dumps([d for d in res["diagnostics"]
                                                       if d["severity"] == "error"])}]
    return prog, rounds, res.get("diagnostics", [])


def main():
    if not KEY:
        print("DEEPSEEK_API_KEY not set"); return
    kept = 0
    with open(OUT, "a") as fh:
        for r in REFERENCES:
            prog, rounds, _diags = generate_corrected(r["desc"])
            v = cadkit("/dsl/verify", {"program": prog, "reference": r["ref"]})
            ok = bool(v.get("match"))
            why = "" if ok else " fails=" + ",".join(f["field"] for f in v.get("fails", [])) or v.get("reason", "")
            print(f"[{'MATCH' if ok else 'no-match'}] rounds={rounds}{why}  {r['desc'][:54]}")
            if ok:
                fh.write(json.dumps({"description": r["desc"], "program": prog,
                                     "signature": v["signature"]}) + "\n")
                kept += 1
    print(f"\nverified pairs kept: {kept}/{len(REFERENCES)} -> {OUT}")


if __name__ == "__main__":
    main()
