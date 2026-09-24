#!/usr/bin/env python3
"""Score face_off.jsonl in cadkit: per generator, the census admission standard —
parse -> dsl.check -> build -> overlap gate; {"unsupported"} = honest decline. Side-by-side report."""
from __future__ import annotations

import json
import os
from collections import Counter

from constraint_kit import dsl

ROOT = os.environ.get("CK_WORK_DIR", "/tmp/constraint-kit")


def classify(text):
    try:
        prog = json.loads(text)
    except Exception:  # noqa: BLE001
        return "format-fail"
    if not isinstance(prog, dict):
        return "format-fail"
    if "unsupported" in prog:
        return "declined"
    if not prog.get("parts"):
        return "format-fail"
    chk = dsl.check(prog)
    if not chk["ok"]:
        return "check-fail"
    try:
        if len(prog["parts"]) > 1 and dsl.overlap_fraction(prog) > 0.5:
            return "piled"
        dsl.program_signature(prog)
        return "built-ok"
    except Exception:  # noqa: BLE001
        return "build-fail"


def main():
    rows = [json.loads(l) for l in open(f"{ROOT}/cadkit/output/face_off.jsonl")]
    res = {"deepseek": Counter(), "local": Counter()}
    detail = []
    for r in rows:
        d = classify(r["deepseek"])
        l = classify(r["local"])
        res["deepseek"][d] += 1
        res["local"][l] += 1
        detail.append({"request": r["request"], "deepseek": d, "local": l})
    with open(f"{ROOT}/cadkit/output/face_off_scored.jsonl", "w") as fh:
        for d in detail:
            fh.write(json.dumps(d) + "\n")
    n = len(rows)
    print(f"\n=== FACE-OFF: local trained 12B vs prompted DeepSeek ({n} fresh requests) ===")
    print(f"{'outcome':<14}{'deepseek':>10}{'local-12B':>12}")
    for k in ("built-ok", "declined", "check-fail", "build-fail", "piled", "format-fail"):
        print(f"{k:<14}{res['deepseek'][k]:>10}{res['local'][k]:>12}")
    both_built = sum(1 for d in detail if d["deepseek"] == d["local"] == "built-ok")
    disagree = [d for d in detail if (d["deepseek"] == "built-ok") != (d["local"] == "built-ok")]
    print(f"\nboth built: {both_built} | disagreements: {len(disagree)}")
    for d in disagree[:8]:
        print(f"  [{d['deepseek']:>9} vs {d['local']:<9}] {d['request'][:70]}")


if __name__ == "__main__":
    main()
