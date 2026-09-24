#!/usr/bin/env python3
"""Quant-parity check: the SAME 90 held-out probes through the SERVED Q6_K GGUF (llama.cpp), scored
identically to the on-box bf16 eval (structural param-match + decline honesty). The bf16 adapter scored
93%/87%; this measures what the 6-bit artifact actually serves. RAW /completion prompts mirroring the
training serialization EXACTLY (system gets the <|think|> mode token; stop at <turn|>).

  python3 drivers/quant_parity.py [n_parallel=2]
"""
from __future__ import annotations

import json
import sys
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

ROOT = os.environ.get("CK_WORK_DIR", "/tmp/constraint-kit")
SERVER = "http://127.0.0.1:8086"
TRAIN_SYS = open(f"{ROOT}/cadkit/output/train_sys.txt").read()


def gen(user_text):
    prompt = (f"<|turn>system\n<|think|>\n{TRAIN_SYS}<turn|>\n"
              f"<|turn>user\n{user_text}<turn|>\n<|turn>model\n")
    body = {"prompt": prompt, "n_predict": 1400, "temperature": 0.2, "top_p": 0.95,
            "stop": ["<turn|>"], "cache_prompt": True}
    req = urllib.request.Request(f"{SERVER}/completion", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    out = json.loads(urllib.request.urlopen(req, timeout=600).read())
    return out.get("content", "")


def strip_thought(text):
    if "<channel|>" in text:
        text = text.split("<channel|>")[-1]
    return text.split("<turn|>")[0].strip()


def param_match(gen_text, ref_text):
    try:
        g, r = json.loads(gen_text), json.loads(ref_text)
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
            if all(abs(float(p.get("params", {}).get(k, -1e9)) - float(v)) <= 0.015 * max(abs(float(v)), 1)
                   for k, v in c.items() if isinstance(v, (int, float))):
                ok = True
                cands.remove(c)
                break
        if not ok:
            return "param-mismatch"
    if len(g.get("mates", [])) != len(r.get("mates", [])):
        return "mate-count-mismatch"
    return "match"


def main():
    par = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    probes = [json.loads(l) for l in open(f"{ROOT}/cadkit/output/eval_generations.jsonl")]
    score = Counter()
    rows = []

    def run(p):
        try:
            raw = gen(p["user"])
            ans = strip_thought(raw)
            return {**p, "generated_q6": ans, "verdict_q6": param_match(ans, p["target"])}
        except Exception as exc:  # noqa: BLE001
            return {**p, "generated_q6": "", "verdict_q6": f"error:{type(exc).__name__}"}

    with ThreadPoolExecutor(max_workers=par) as ex:
        for i, r in enumerate(ex.map(run, probes)):
            rows.append(r)
            score[r["verdict_q6"]] += 1
            if (i + 1) % 10 == 0:
                print(f"  {i+1}/{len(probes)} {dict(score)}", flush=True)
    with open(f"{ROOT}/cadkit/output/quant_parity.jsonl", "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    total = sum(score.values())
    flips = sum(1 for r in rows if (r["verdict_q6"] == "match") != (r["verdict"] == "match"))
    print(f"\n=== Q6_K parity ({total} probes) ===")
    for k, v in score.most_common():
        print(f"  {k}: {v} ({100*v//total}%)")
    print(f"bf16 baseline: match 82/90 (91%) | verdict flips vs bf16: {flips}")


if __name__ == "__main__":
    main()
