#!/usr/bin/env python3
"""Rung-1 on-box eval: load the trained LoRA, generate against HELD-OUT val rows, score structurally.

Three probe sets from val_g4.jsonl (geometry-keyed split -> genuinely unseen):
  build   (reasoning/direct rows): generate -> parse -> param-level match vs the target program
          (same part-type multiset, numeric params within 1.5%, same mate count)
  decline rows: the model must answer {"unsupported": ...} — the honesty behavior
  format  : every generation must parse as JSON after the thought channel

Structural match here is the cheap proxy; the GEOMETRIC verdict runs at home in cadkit on
eval_generations.jsonl (pulled back with the LoRA). Run:  python eval_rung1.py [n_build] [n_decline]
"""
from __future__ import annotations

import json
import random
import sys

VAL = "/srv/nvme-data/containers/constraint-kit/cadkit/output/dataset/val_g4.jsonl"
LORA = "/root/ckpt_rung1/lora_final"
OUT = "/root/eval_generations.jsonl"


def strip_thought(text):
    if "<channel|>" in text:
        text = text.split("<channel|>")[-1]
    return text.split("<turn|>")[0].strip()


def param_match(gen, ref):
    try:
        g, r = json.loads(gen), json.loads(ref)
    except Exception:  # noqa: BLE001
        return "format-fail"
    if "unsupported" in r:
        return "match" if "unsupported" in g else "wrong-build"
    if "unsupported" in g:
        return "wrong-decline"
    gp, rp = g.get("parts", []), r.get("parts", [])
    if sorted(p.get("type", "") for p in gp) != sorted(p.get("type", "") for p in rp):
        return "type-mismatch"
    rbyt = {}
    for p in rp:
        rbyt.setdefault(p.get("type"), []).append(p.get("params", {}))
    for p in gp:
        cands = rbyt.get(p.get("type"), [])
        ok = False
        for c in cands:
            num_ok = all(abs(float(p.get("params", {}).get(k, -1e9)) - float(v)) <= 0.015 * max(abs(float(v)), 1)
                         for k, v in c.items() if isinstance(v, (int, float)))
            if num_ok:
                ok = True
                cands.remove(c)
                break
        if not ok:
            return "param-mismatch"
    if len(g.get("mates", [])) != len(r.get("mates", [])):
        return "mate-count-mismatch"
    return "match"


def main():
    n_build = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    n_decl = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    import torch
    from unsloth import FastVisionModel
    model, tokenizer = FastVisionModel.from_pretrained(LORA, max_seq_length=6144, load_in_4bit=False)
    FastVisionModel.for_inference(model)
    chat_tok = getattr(tokenizer, "tokenizer", tokenizer)

    rows = [json.loads(l) for l in open(VAL)]
    text = [r for r in rows if not any(isinstance(m.get("content"), list) for m in r["messages"])]
    build = [r for r in text if r["meta"]["kind"] in ("reasoning", "direct")]
    decls = [r for r in text if r["meta"]["kind"] == "decline"]
    rng = random.Random(3)
    rng.shuffle(build)
    rng.shuffle(decls)
    probes = build[:n_build] + decls[:n_decl]
    print(f"probing {len(probes)} held-out rows ({min(n_build,len(build))} build, {min(n_decl,len(decls))} decline)")

    from collections import Counter
    score = Counter()
    with open(OUT, "w") as fh:
        for i, r in enumerate(probes):
            msgs = [m for m in r["messages"] if m["role"] in ("system", "user")][:2]
            ids = chat_tok.apply_chat_template(msgs, add_generation_prompt=True, enable_thinking=True,
                                               tokenize=True, return_tensors="pt").to("cuda")
            with torch.no_grad():
                out = model.generate(ids, max_new_tokens=1400, temperature=0.6, top_p=0.95, do_sample=True)
            gen = chat_tok.decode(out[0][ids.shape[1]:], skip_special_tokens=False)
            answer = strip_thought(gen)
            verdict = param_match(answer, r["messages"][-1]["content"])
            score[verdict] += 1
            fh.write(json.dumps({"kind": r["meta"]["kind"], "user": msgs[-1]["content"],
                                 "target": r["messages"][-1]["content"], "generated": answer,
                                 "raw": gen[:4000], "verdict": verdict}) + "\n")
            if (i + 1) % 10 == 0:
                print(f"  {i+1}/{len(probes)} {dict(score)}", flush=True)
    total = sum(score.values())
    print(f"\n=== rung-1 structural eval ({total} held-out probes) ===")
    for k, v in score.most_common():
        print(f"  {k}: {v} ({100*v//total}%)")
    print(f"generations -> {OUT} (pull home for the GEOMETRIC verify in cadkit)")


if __name__ == "__main__":
    main()
