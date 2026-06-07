#!/usr/bin/env python3
"""Head-to-head: the LOCAL trained 12B (Q6_K @ :8086) vs prompted DeepSeek on the SAME fresh request
stream. Both emit programs; the cadkit scorer (face_off_score.py) applies the census admission standard:
check -> build -> overlap gate, or an honest decline. The result decides rung-2 vs GRPO.

  DEEPSEEK_API_KEY=... python3 drivers/face_off.py [n_per_category=12]
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from census import CATEGORIES, deepseek, gen_requests, is_build_request  # noqa: E402
from quant_parity import gen as local_gen, strip_thought  # noqa: E402

ROOT = "/srv/nvme-data/containers/constraint-kit"
GEN_SYS = open(f"{ROOT}/cadkit/output/train_sys.txt").read()   # SAME system prompt for both


def deepseek_gen(req):
    try:
        out = deepseek([{"role": "system", "content": GEN_SYS}, {"role": "user", "content": req}],
                       temperature=0.2)
        return out
    except Exception:  # noqa: BLE001
        return ""


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    print(f"generating request stream ({n}/category)...")
    reqs, seen = [], set()
    with ThreadPoolExecutor(max_workers=len(CATEGORIES)) as ex:
        for batch in ex.map(lambda c: gen_requests(c, n), CATEGORIES):
            for r in batch:
                k = r.strip().lower()
                if is_build_request(r) and k not in seen:
                    seen.add(k)
                    reqs.append(r)
    print(f"{len(reqs)} unique build requests")

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=32) as ex:
        ds = list(ex.map(deepseek_gen, reqs))
    t_ds = time.time() - t0
    print(f"deepseek: {len(ds)} generations in {t_ds:.0f}s")

    t0 = time.time()
    def local_one(r):
        try:
            return strip_thought(local_gen(r))
        except Exception:  # noqa: BLE001
            return ""
    with ThreadPoolExecutor(max_workers=2) as ex:
        lc = list(ex.map(local_one, reqs))
    t_lc = time.time() - t0
    print(f"local 12B: {len(lc)} generations in {t_lc:.0f}s")

    with open(f"{ROOT}/cadkit/output/face_off.jsonl", "w") as fh:
        for r, d, l in zip(reqs, ds, lc):
            fh.write(json.dumps({"request": r, "deepseek": d, "local": l}) + "\n")
    print("scoring in cadkit...")
    subprocess.run(["docker", "exec", "cadkit", "python3",
                    f"{ROOT}/drivers/face_off_score.py"], check=False)


if __name__ == "__main__":
    main()
