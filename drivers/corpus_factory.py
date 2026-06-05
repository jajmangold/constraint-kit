#!/usr/bin/env python3
"""Corpus factory — the self-validating NL<->CAD data engine, first run on a bounded core count.

DeepSeek (concurrent, the LANGUAGE side) CAPTIONS known reference parts into natural requests AND generates
DSL programs from those captions; the spawn PROCESS POOL (the build drain, sized to N cores — the governor)
builds the candidates; the VERIFIER keeps only (caption -> program) pairs whose geometry matches the
reference signature. Self-supervised (our generators are ground truth) + rejection-sampled (verifier filters).

Runs IN the container (network_mode host -> reaches DeepSeek; cadquery for the build pool):
  docker exec -e DEEPSEEK_API_KEY=$KEY cadkit python3 drivers/corpus_factory.py [build_workers]
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import time
import urllib.request
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

DEEPSEEK = "https://api.deepseek.com/chat/completions"
KEY = os.environ.get("DEEPSEEK_API_KEY")
CORPUS = "/srv/nvme-data/containers/constraint-kit/cadkit/output/dsl_corpus.jsonl"

GEN_SYS = (
    "Translate a part request into a constraint-kit DSL program. Output ONLY JSON: "
    '{"parts":[{"id","type","material","params":{...}}],"mates":[]}. Types+params (mm): '
    "spacer{outer_d,bore_d,height}; spur_gear{module,teeth,width,bore_d}; washer{outer_d,bore_d,thick}; "
    "shaft{diameter,length}; panel{width,depth,height}; coupling{outer_d,bore_d,length,set_screw_d}; "
    "pulley{outer_d,groove_d,width,groove_width,bore_d}; nut{af,height,bore_d}; bolt{shank_d,length,head_d,head_h}. "
    "Capture every stated number. JSON only."
)
CAP_SYS = ('Describe this mechanical part as a concise, natural one-line engineering request a person would '
           'type, INCLUDING its key dimensions in mm. Output ONLY JSON: {"desc":"..."}.')


def reference_specs():
    s = []
    for od in (14, 20, 26, 32):
        for h in (5, 10, 15):
            s.append(("spacer", {"outer_d": od, "bore_d": 6, "height": h}))
    for m in (1, 1.5, 2):
        for t in (16, 24, 32, 40):
            s.append(("spur_gear", {"module": m, "teeth": t, "width": 6, "bore_d": 8}))
    for od in (16, 20, 24):
        for th in (2, 3):
            s.append(("washer", {"outer_d": od, "bore_d": 8, "thick": th}))
    for d in (8, 12, 16):
        for ln in (40, 60, 80):
            s.append(("shaft", {"diameter": d, "length": ln}))
    for w in (40, 60, 80):
        s.append(("panel", {"width": w, "depth": 40, "height": 10}))
    for od in (20, 25, 30):
        s.append(("coupling", {"outer_d": od, "bore_d": 8, "length": 25, "set_screw_d": 4}))
    for od in (30, 40):
        s.append(("pulley", {"outer_d": od, "groove_d": od - 8, "width": 12, "groove_width": 6, "bore_d": 8}))
    return s


def _deepseek(system, user, temp):
    body = {"model": "deepseek-v4-flash", "temperature": temp, "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    req = urllib.request.Request(DEEPSEEK, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    return json.loads(urllib.request.urlopen(req, timeout=120).read())["choices"][0]["message"]["content"]


def caption(spec):
    t, p = spec
    try:
        return json.loads(_deepseek(CAP_SYS, json.dumps({"type": t, "params": p}), 0.4)).get("desc")
    except Exception:  # noqa: BLE001
        return None


def generate(desc):
    if not desc:
        return None
    try:
        prog = json.loads(_deepseek(GEN_SYS, desc, 0.2))
        return prog if isinstance(prog, dict) and prog.get("parts") else None
    except Exception:  # noqa: BLE001
        return None


def _build_and_sig(item):
    """Process-pool worker: build a program, return its (cheap) signature or an error string."""
    idx, prog = item
    try:
        from constraint_kit import builder, dsl
        assy, _ = builder.build_assembly(prog)
        return idx, dsl.signature(assy), None
    except Exception as exc:  # noqa: BLE001
        return idx, None, f"{type(exc).__name__}: {exc}"


def _match(a, b, tol=0.02):
    if not a or not b:
        return False
    for k in ("volume", "area"):
        if b.get(k) and abs(a[k] - b[k]) > tol * b[k]:
            return False
    return a.get("n_faces") == b.get("n_faces")     # topology must match -> the SAME part, faithfully reproduced


def _pool_build(programs, workers):
    ctx = mp.get_context("spawn")                    # spawn, never fork (OCC threads already loaded)
    out = [None] * len(programs)
    with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as ex:
        for idx, sig, _err in ex.map(_build_and_sig, list(enumerate(programs))):
            out[idx] = sig
    return out


def main():
    if not KEY:
        print("DEEPSEEK_API_KEY not set"); return
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else 32
    refs = reference_specs()
    print(f"reference parts: {len(refs)} | build workers: {workers}")

    # ground-truth signatures (also measures the build pool)
    t = time.time()
    ref_sigs = _pool_build([{"parts": [{"id": "p", "type": tp, "params": pp}], "mates": []} for tp, pp in refs], workers)
    rt = time.time() - t
    print(f"reference build: {len(refs)} parts in {rt:.1f}s -> {len(refs)/rt:.1f} builds/sec")

    # caption + generate (DeepSeek, the LANGUAGE side, concurrent)
    t = time.time()
    with ThreadPoolExecutor(max_workers=32) as ex:
        caps = list(ex.map(caption, refs))
    with ThreadPoolExecutor(max_workers=32) as ex:
        progs = list(ex.map(generate, caps))
    cand = [(i, caps[i], progs[i]) for i in range(len(refs)) if caps[i] and progs[i]]
    print(f"caption+generate: {len(cand)} candidates in {time.time()-t:.1f}s")

    # build candidates on the pool + verify vs reference signature
    t = time.time()
    cand_sigs = _pool_build([c[2] for c in cand], workers)
    bt = time.time() - t
    kept = 0
    with open(CORPUS, "a") as fh:
        for j, (ri, cap, prog) in enumerate(cand):
            if _match(cand_sigs[j], ref_sigs[ri]):
                fh.write(json.dumps({"description": cap, "program": prog,
                                     "signature": cand_sigs[j], "source": "factory"}) + "\n")
                kept += 1
    print("\n=== factory ===")
    print(f"candidate build: {len(cand)} in {bt:.1f}s -> {len(cand)/bt:.1f} builds/sec ({workers} workers)")
    print(f"verified pairs kept: {kept}/{len(cand)} ({100*kept//max(len(cand),1)}% yield)")
    print(f"corpus total: {sum(1 for _ in open(CORPUS))}")


if __name__ == "__main__":
    main()
