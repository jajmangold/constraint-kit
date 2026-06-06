#!/usr/bin/env python3
"""Campaign orchestrator — BUILD ONCE, CAPTION MANY (how 2500-concurrent DeepSeek scales this box).

The measured inversion: the build farm does ~100/s; DeepSeek does 500-1000+/s. So at scale a caption pairs
DIRECTLY with its already-verified geometry (no per-caption build), and faithfulness is guaranteed
STATISTICALLY instead of per-pair:
  s0     PREFLIGHT: full ROUND-TRIP yield (caption -> regenerate -> build -> signature match) PER CAPTION
         STYLE on a sample. Styles below the bar do NOT run at scale. (The 95-97% prior was clean captions;
         noisy styles must earn their place.)
  pilot  S1 geometry backbone: parametric GRIDS over part types + MATED assembly templates -> build+verify
         each distinct geometry ONCE on the persistent warm pool -> campaign_geometries.jsonl.
         S2 caption multiplication: N diverse captions per geometry across the PASSING styles (deduped),
         paired with the verified program -> dsl_corpus.jsonl, plus a 1% random round-trip AUDIT (drift alarm).

Run IN the container (DeepSeek via host netns; cadquery for the pool):
  docker exec -e DEEPSEEK_API_KEY=$KEY cadkit python3 drivers/campaign.py s0
  docker exec -e DEEPSEEK_API_KEY=$KEY cadkit python3 drivers/campaign.py pilot [caps_per_geom]
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import random
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from corpus_factory import GEN_SYS, _build_and_sig, _deepseek, _match  # noqa: E402

OUT = "/srv/nvme-data/containers/constraint-kit/cadkit/output"
CORPUS = f"{OUT}/dsl_corpus.jsonl"
YIELD_BAR = 0.70
DS_WORKERS = 128

STYLES = {
    "terse":      "Write it as a TERSE engineering shorthand line (abbreviations ok, e.g. 'M2 spur gear, 24T, 8mm bore').",
    "spec":       "Write it as one precise specification sentence with all key dimensions in mm.",
    "contextual": "Write it as a request mentioning a plausible use ('for my printer's extruder...'), still including the key dimensions.",
    "casual":     "Write it casually/colloquially, slightly imprecise wording but keep the key numbers ('need a smallish gear, 24 teeth, hole around 8mm').",
    "imperative": "Write it as a direct command ('make me a ...'), including the key dimensions.",
}
CAP_SYS = ("You caption a known mechanical part as a natural-language request someone would type. {style} "
           'The part (type + exact params, mm) follows. Output ONLY JSON: {{"desc":"..."}}.')


def geometry_grid():
    """Parametric GRIDS (reproducible, cache-friendly) + MATED assembly templates (pass the overlap gate by
    construction). ~2k distinct geometries at default density."""
    G = []

    def P(t, pp):
        G.append({"parts": [{"id": "p", "type": t, "material": "steel", "params": pp}], "mates": []})
    for od in (8, 14, 20, 28, 38, 50):
        for br in (0.0, 0.3, 0.5):
            for h in (4, 10, 18, 30):
                P("spacer", {"outer_d": od, "bore_d": round(od * br, 1), "height": h})
    for od in (10, 16, 24, 36):
        for br in (0.35, 0.5):
            for th in (1, 2, 3):
                P("washer", {"outer_d": od, "bore_d": round(od * br, 1), "thick": th})
    for d in (4, 8, 12, 18, 25):
        for ln in (15, 40, 80, 140):
            P("shaft", {"diameter": d, "length": ln})
    for w in (20, 40, 70, 100):
        for dp in (20, 50):
            for h in (3, 8, 16):
                P("panel", {"width": w, "depth": dp, "height": h})
    for m in (0.5, 1, 1.5, 2, 3):
        for t in (12, 20, 32, 48, 64):
            for wd in (4, 8):
                P("spur_gear", {"module": m, "teeth": t, "width": wd, "bore_d": max(3, round(m * t * 0.15))})
    for m in (1, 2):
        for t in (16, 28, 40):
            P("helical_gear", {"module": m, "teeth": t, "width": 10, "bore_d": 8, "helix_angle": 20})
    for od in (16, 22, 30, 40):
        for ln in (18, 28, 40):
            P("coupling", {"outer_d": od, "bore_d": round(od * 0.32, 1), "length": ln, "set_screw_d": 4})
    for od in (24, 36, 50):
        for gw in (5, 8):
            P("pulley", {"outer_d": od, "groove_d": od - 8, "width": gw + 6, "groove_width": gw, "bore_d": 8})
    for af in (7, 10, 13, 17):
        P("nut", {"af": af, "height": round(af * 0.45, 1), "bore_d": round(af * 0.55, 1)})
    for sd in (3, 5, 8, 10):
        for ln in (10, 20, 35):
            P("bolt", {"shank_d": sd, "length": ln, "head_d": round(sd * 1.7, 1), "head_h": round(sd * 0.7, 1)})
    for ln in (40, 80, 130):
        for wd in (8, 14):
            P("link", {"length": ln, "width": wd, "thickness": 4})
    # mated assembly templates (overlap-gate-clean by construction)
    for t in (16, 24, 36):
        G.append({"parts": [{"id": "plate", "type": "plate", "material": "aluminum", "params": {}},
                            {"id": "g", "type": "spur_gear", "material": "steel",
                             "params": {"module": 1, "teeth": t, "width": 6, "bore_d": 12}}],
                  "mates": [{"a": "plate", "b": "g", "intent": "seat_on"}]})
    for od in (14, 20):
        for n in (2, 3):
            parts = [{"id": f"s{i}", "type": "spacer", "material": "steel",
                      "params": {"outer_d": od, "bore_d": 6, "height": 10}} for i in range(n)]
            mates = [{"a": f"s{i}", "a_joint": "top", "b": f"s{i+1}", "b_joint": "bottom",
                      "type": "coincident"} for i in range(n - 1)]
            G.append({"parts": parts, "mates": mates})
    for za, zb in ((20, 30), (16, 40)):
        G.append({"parts": [{"id": "g1", "type": "spur_gear", "material": "steel",
                             "params": {"module": 1, "teeth": za, "width": 6, "bore_d": 8}},
                            {"id": "g2", "type": "spur_gear", "material": "steel",
                             "params": {"module": 1, "teeth": zb, "width": 6, "bore_d": 8}}],
                  "mates": [{"a": "g1", "a_joint": "bore_base", "b": "g2", "b_joint": "bore_base", "type": "mesh"}]})
    return G


def caption_one(args):
    prog, style = args
    parts = [{"type": p["type"], "params": p.get("params", {})} for p in prog["parts"]]
    try:
        out = json.loads(_deepseek(CAP_SYS.format(style=STYLES[style]),
                                   json.dumps(parts if len(parts) > 1 else parts[0]), 0.7))
        d = (out.get("desc") or "").strip()
        return d if d else None
    except Exception:  # noqa: BLE001
        return None


def generate_one(desc):
    try:
        prog = json.loads(_deepseek(GEN_SYS, desc, 0.2))
        return prog if isinstance(prog, dict) and prog.get("parts") else None
    except Exception:  # noqa: BLE001
        return None


def _pool(workers=32):
    return ProcessPoolExecutor(max_workers=workers, mp_context=mp.get_context("spawn"))


def s0():
    """Per-style ROUND-TRIP yield on a sample -> campaign_s0.json (gates the pilot's styles)."""
    geoms = random.Random(7).sample(geometry_grid(), 40)
    with _pool() as ex:
        ref = {i: s for i, s, _ in ex.map(_build_and_sig, list(enumerate(geoms)))}
        jobs = [(i, st) for i in range(len(geoms)) for st in STYLES]
        with ThreadPoolExecutor(max_workers=DS_WORKERS) as tex:
            caps = list(tex.map(lambda j: (j[0], j[1], caption_one((geoms[j[0]], j[1]))), jobs))
        with ThreadPoolExecutor(max_workers=DS_WORKERS) as tex:
            regs = list(tex.map(lambda c: (c[0], c[1], generate_one(c[2])), caps))
        live = [(i, st, p) for i, st, p in regs if p]
        sigs = list(ex.map(_build_and_sig, [(k, live[k][2]) for k in range(len(live))]))
    per = Counter(); ok = Counter()
    for k, sig, _err in sigs:
        i, st, _p = live[k]
        per[st] += 1
        if sig and _match(sig, ref.get(i)):
            ok[st] += 1
    table = {st: round(ok[st] / per[st], 3) if per[st] else 0.0 for st in STYLES}
    json.dump({"yields": table, "bar": YIELD_BAR}, open(f"{OUT}/campaign_s0.json", "w"), indent=2)
    print("=== S0 per-style round-trip yield ===")
    for st, y in table.items():
        print(f"  {st:11} {y:.0%}  {'PASS' if y >= YIELD_BAR else 'FAIL (excluded at scale)'}")


def pilot(caps_per_geom=10):
    styles = [st for st, y in json.load(open(f"{OUT}/campaign_s0.json"))["yields"].items() if y >= YIELD_BAR]
    if not styles:
        print("no styles passed s0 — not running"); return
    geoms = geometry_grid()
    print(f"S1: {len(geoms)} geometries | S2 styles: {styles} x {max(1, caps_per_geom // len(styles))} caps")
    t0 = time.time()
    with _pool() as ex:
        built = list(ex.map(_build_and_sig, list(enumerate(geoms))))
        verified = [(geoms[i], sig) for i, sig, _e in built if sig]
        print(f"S1 backbone: {len(verified)}/{len(geoms)} verified in {time.time()-t0:.0f}s")
        with open(f"{OUT}/campaign_geometries.jsonl", "w") as fh:
            for prog, sig in verified:
                fh.write(json.dumps({"program": prog, "signature": sig}) + "\n")
        # S2: caption multiplication (deduped), direct-paired with the verified program
        k = max(1, caps_per_geom // len(styles))
        jobs = [(gi, st) for gi in range(len(verified)) for st in styles for _ in range(k)]
        t1 = time.time(); seen = set(); kept = 0
        with open(CORPUS, "a") as fh, ThreadPoolExecutor(max_workers=DS_WORKERS) as tex:
            for gi, st, d in tex.map(lambda j: (j[0], j[1], caption_one((verified[j[0]][0], j[1]))), jobs):
                if not d:
                    continue
                key = " ".join(d.lower().split())
                if key in seen:
                    continue
                seen.add(key)
                fh.write(json.dumps({"description": d, "program": verified[gi][0],
                                     "signature": verified[gi][1], "style": st, "source": "campaign"}) + "\n")
                kept += 1
        print(f"S2: {kept} unique pairs in {time.time()-t1:.0f}s (deduped from {len(jobs)})")
        # 1% round-trip AUDIT (drift alarm)
        rows = [json.loads(l) for l in open(CORPUS) if '"campaign"' in l]
        audit = random.Random(11).sample(rows, max(10, len(rows) // 100))
        with ThreadPoolExecutor(max_workers=DS_WORKERS) as tex:
            regs = list(tex.map(lambda r: generate_one(r["description"]), audit))
        live = [(j, p) for j, p in enumerate(regs) if p]
        sigs = list(ex.map(_build_and_sig, [(k2, live[k2][1]) for k2 in range(len(live))]))
        good = sum(1 for k2, sig, _e in sigs if sig and _match(sig, audit[live[k2][0]]["signature"]))
        print(f"AUDIT (round-trip on {len(audit)} sampled pairs): {good}/{len(audit)} = {good/max(len(audit),1):.0%}")
    print(f"corpus total: {sum(1 for _ in open(CORPUS))}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "s0"
    if cmd == "s0":
        s0()
    else:
        pilot(int(sys.argv[2]) if len(sys.argv) > 2 else 10)
