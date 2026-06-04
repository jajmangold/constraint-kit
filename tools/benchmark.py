#!/usr/bin/env python3
"""Scale benchmark harness (T3.4) — build an N-part assembly and time build + interference, then record a
CkBenchmark breadcrumb to atlas. Exercises the spatial-grid prefilter (T3.2) at scale. Run:

    docker exec cadkit python3 tools/benchmark.py [grid_k]      # n_parts = k*k spacers in a grid
"""
from __future__ import annotations

import sys
import time

from constraint_kit import assembly, store, validate


def _grid_defs(k: int, spacing: float = 40.0) -> tuple:
    """A k×k grid of identical spacers placed via hierarchy ports (well-separated -> grid prunes hard)."""
    defs = {"cell": {"parts": [{"id": "s", "type": "spacer",
                               "params": {"outer_d": 20, "bore_d": 8, "height": 10}}],
                     "ports": {"p": {"origin": [0, 0, 0]}}},
            "grid": {"children": [
                {"instance": f"c{i}_{j}", "ref": "cell",
                 "place": {"port": "p", "at": [i * spacing, j * spacing, 0]}}
                for i in range(k) for j in range(k)]}}
    return defs, "grid"


def bench(k: int = 5) -> dict:
    defs, root = _grid_defs(k)
    t0 = time.time()
    res = assembly.build_tree(root, defs)
    build_s = time.time() - t0
    t1 = time.time()
    inter = validate.interference(res["cq_assembly"])
    interference_s = time.time() - t1
    out = {"n_parts": res["part_count"], "build_s": round(build_s, 4),
           "interference_s": round(interference_s, 4),
           "pairs_total": inter["pairs_total"], "pairs_candidate": inter["pairs_candidate"]}
    store.record_benchmark(out)        # fail-soft atlas breadcrumb
    return out


def main():
    k = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    r = bench(k)
    print(f"n_parts={r['n_parts']}  build={r['build_s']}s  interference={r['interference_s']}s  "
          f"pairs {r['pairs_candidate']}/{r['pairs_total']} (grid-pruned)")


if __name__ == "__main__":
    main()
