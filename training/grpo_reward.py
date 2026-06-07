#!/usr/bin/env python3
"""GRPO reward — deterministic, decline-aware, geometry-graded. Importable on the GPU box (needs
cadquery + constraint_kit installed) AND testable in cadkit. Scale ≈ [-1, +1.5].

Terms (from the measured failure mass):
  decline-expected prompts (unexpressible markers): decline=+1.0, build=-1.0   <- the 4% silent-drop killer
  buildable prompts: over-decline=-0.5; then graded ladder:
    parses +0.2 · check passes +0.2 · builds +0.3 · not piled (overlap<=0.5) else -0.3
    + geometric PROXIMITY vs reference when present: up to +0.8 (volume + bbox closeness)
Format garbage: -1.0. Every term is computable without any model judgment.
"""
from __future__ import annotations

import json


def strip_thought(text: str) -> str:
    if "<channel|>" in text:
        text = text.split("<channel|>")[-1]
    return text.split("<turn|>")[0].strip()


def score(completion: str, decline_expected: bool, ref: dict | None) -> float:
    ans = strip_thought(completion)
    try:
        obj = json.loads(ans)
        assert isinstance(obj, dict)
    except Exception:  # noqa: BLE001
        return -1.0
    declined = "unsupported" in obj
    if decline_expected:
        return 1.0 if declined else -1.0
    if declined:
        return -0.5
    if not obj.get("parts"):
        return -0.8
    r = 0.2                                                   # parsed as a program
    from constraint_kit import dsl
    chk = dsl.check(obj)
    if not chk["ok"]:
        return r - 0.5
    r += 0.2
    try:
        sig = dsl.program_signature(obj)
    except Exception:  # noqa: BLE001
        return r - 0.3
    r += 0.3
    try:
        if len(obj.get("parts", [])) > 1 and dsl.overlap_fraction(obj) > 0.5:
            return r - 0.3
    except Exception:  # noqa: BLE001
        pass
    if ref:                                                   # graded geometric proximity
        dv = abs(sig["volume"] - ref["volume"]) / max(ref["volume"], 1e-9)
        db = max(abs(a - b) / max(b, 1.0) for a, b in zip(sig["bbox_sorted"], ref["bbox_sorted"]))
        prox = max(0.0, 1.0 - (dv + db))                      # 1.0 = exact, 0 = 100% combined error
        r += 0.8 * prox
    return r


def reward_fn(completions, decline_expected=None, ref=None, **kwargs):
    """TRL GRPOTrainer adapter: extra dataset columns arrive as parallel lists."""
    out = []
    for i, c in enumerate(completions):
        text = c if isinstance(c, str) else c[0].get("content", "")
        de = bool(decline_expected[i]) if decline_expected else False
        rf = ref[i] if ref else None
        if isinstance(rf, str):
            rf = json.loads(rf) if rf else None
        try:
            out.append(score(text, de, rf))
        except Exception:  # noqa: BLE001
            out.append(-1.0)
    return out
