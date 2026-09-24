#!/usr/bin/env python3
"""Reconstruction data factory (Stage 3/4 seed): mint VERIFIED (shape -> construct.py) pairs at volume.

The Stage-0 probe proved the loop works for prismatic parts (~75%). This scales it across the parametric
catalog x param grids: build each part (the target, with known signature) -> DeepSeek best-of-N constructive
reconstruction (threaded I/O) -> build+signature-match (SERIAL, OCC not thread-safe) -> keep verified.

Each kept row carries BOTH the parametric program AND a verified construct.py program for the same shape
(a parametric<->constructive translation pair) -> the seed corpus for teaching the model the constructive
tier (Stage 4). HONEST: text+bbox conditioning -> prismatic-biased; curved/lofted parts need image
conditioning (rung-2), so they're under-represented here by design, not by accident.

  DEEPSEEK_API_KEY=... python3 drivers/recon_factory.py [n_targets]
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from constraint_kit import construct, dsl
from constraint_kit.parts import PART_GENS
from recon_probe import SYS, matches  # reuse the proven prompt + match criterion

KEY = os.environ.get("DEEPSEEK_API_KEY")
OUT = os.path.join(os.environ.get("CK_WORK_DIR", "/tmp/constraint-kit"), "cadkit/output/constructive_corpus.jsonl")
N_CAND = 3


def ds(user, temp):
    body = {"model": "deepseek-v4-flash", "temperature": temp, "max_tokens": 900,
            "response_format": {"type": "json_object"}, "thinking": {"type": "disabled"},
            "messages": [{"role": "system", "content": SYS}, {"role": "user", "content": user}]}
    req = urllib.request.Request("https://api.deepseek.com/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    return json.loads(urllib.request.urlopen(req, timeout=180).read())["choices"][0]["message"]["content"]


def grid(n):
    """Prismatic-friendly catalog x params (what text+bbox reconstruction can actually hit). Each entry:
    (name, kwargs, description)."""
    T = []
    for od in (12, 18, 24, 30, 40):
        for br in (0.0, 0.35, 0.5):
            for h in (4, 10, 20, 30):
                bore = round(od * br, 1)
                T.append(("spacer", {"outer_d": od, "bore_d": bore, "height": h},
                          f"a round {'solid disc' if bore==0 else 'spacer/sleeve with a through bore'}"))
    for od in (14, 20, 28):
        for th in (1.5, 3):
            T.append(("washer", {"outer_d": od, "bore_d": round(od*0.45, 1), "thick": th},
                      "a flat round washer with a center hole"))
    for d in (5, 8, 12, 20):
        for ln in (20, 60, 120):
            T.append(("shaft", {"diameter": d, "length": ln}, "a plain cylindrical shaft/pin"))
    for w in (40, 60, 90):
        for dp in (30, 50):
            for hh in (4, 10, 20):
                T.append(("panel", {"width": w, "depth": dp, "height": hh}, "a rectangular plate/panel block"))
    for w in (50, 70):
        for t in (4, 6):
            for nb in (4, 6):
                T.append(("plate", {"width": w, "depth": w, "thick": t, "boss_d": 0, "boss_h": 0,
                                    "bolt_d": 5, "bolt_circle": w-16, "bolt_count": nb},
                          f"a square plate with {nb} bolt holes evenly on a circle, no boss"))
    for w in (60, 80):
        T.append(("plate", {"width": w, "depth": w, "thick": 6, "boss_d": 16, "boss_h": 5,
                            "bolt_d": 5, "bolt_circle": w-16, "bolt_count": 4},
                  "a square mounting plate with a central round boss and 4 bolt holes on a circle"))
    import random
    rng = random.Random(11); rng.shuffle(T)
    return T[:n]


def main():
    if not KEY:
        print("DEEPSEEK_API_KEY not set"); return
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 250
    targets = grid(n)

    # serial geometry: build each target -> signature + hint (OCC not thread-safe)
    built = []
    for name, params, desc in targets:
        try:
            wp, _ = PART_GENS[name](**params)
            ref = dsl.signature(wp)
            bb = ref["bbox_sorted"]
            hint = (f"{desc}. Measured bounding box (sorted) = [{bb[0]:.1f},{bb[1]:.1f},{bb[2]:.1f}] mm; "
                    f"volume = {ref['volume']:.0f} mm^3.")
            built.append((name, params, desc, ref, hint))
        except Exception:  # noqa: BLE001
            continue
    print(f"{len(built)} targets built")

    # threaded I/O: N constructive candidates per target
    def cands(item):
        _n, _p, _d, _ref, hint = item
        out = []
        for i in range(N_CAND):
            try:
                out.append(ds(hint, 0.2 if i == 0 else 0.7))
            except Exception:  # noqa: BLE001
                pass
        return out
    with ThreadPoolExecutor(max_workers=16) as ex:
        all_cands = list(ex.map(cands, built))

    # serial build + score; keep first verified match
    kept = 0
    from collections import Counter
    by = Counter(); tot = Counter()
    with open(OUT, "w") as fh:
        for (name, params, desc, ref, _hint), cs in zip(built, all_cands):
            tot[name] += 1
            param_prog = {"parts": [{"id": "p", "type": name, "material": "steel", "params": params}], "mates": []}
            for c in cs:
                try:
                    prog = json.loads(c)
                    wp2 = construct.compile_construct(prog)
                    v = construct.validity_report(wp2)
                    if v["ok"] and matches(v.get("signature"), ref):
                        fh.write(json.dumps({"description": desc, "params": params, "name": name,
                                             "parametric_program": param_prog, "construct_program": prog,
                                             "signature": ref}) + "\n")
                        kept += 1; by[name] += 1
                        break
                except Exception:  # noqa: BLE001
                    continue
    print(f"=== constructive corpus: {kept}/{len(built)} verified ({100*kept//max(len(built),1)}%) ===")
    for nm in sorted(tot):
        print(f"  {nm:8} {by[nm]}/{tot[nm]}")
    print(f"  -> {OUT}")


if __name__ == "__main__":
    main()
