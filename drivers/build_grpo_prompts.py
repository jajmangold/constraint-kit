#!/usr/bin/env python3
"""Build the GRPO prompt set (run IN cadkit) -> training/grpo_prompts.jsonl.

Mix (from the face-off evidence): ~1200 referenced build captions (graded proximity reward),
~400 decline probes incl. the MEASURED silent-drop classes (keyway/shoulder/head-styles weighted up).
Prompts are RAW serialized strings (training-exact: system opens with <|think|>), using TRAIN_SYS_V2 —
the COMPLETE part list (rung-1's prompt omitted half the vocabulary; the model out-learned it, but
GRPO rollouts should see the honest prompt)."""
from __future__ import annotations

import json
import random
import sys

sys.path.insert(0, "/srv/nvme-data/containers/constraint-kit/drivers")
from dataset_factory import TRAIN_SYS  # noqa: E402

ROOT = "/srv/nvme-data/containers/constraint-kit"

TRAIN_SYS_V2 = TRAIN_SYS + (
    " ALSO AVAILABLE: ring_gear{module,teeth,width,rim_width} (internal gear); "
    "helical_gear{module,teeth,width,bore_d,helix_angle}; coupling{outer_d,bore_d,length,set_screw_d} "
    "(RIGID only); pulley{outer_d,groove_d,width,groove_width,bore_d} (smooth flanged only); "
    "bearing{outer_d,bore_d,width} (simplified envelope); spacer/washer/shaft as above.")

# the measured silent-drop classes, weighted UP in the decline probes
EXTRA_DECLINES = [
    ("a 20mm shaft, 150mm long, keyed at both ends", "keyway feature not modeled"),
    ("hollow shaft 40mm OD 25mm ID with a 6mm keyway", "keyway feature not modeled"),
    ("Phillips pan head screw M5x0.8, 20mm", "head-style/drive variants not modeled"),
    ("countersunk flat head screw M4, 16mm long", "head-style variants not modeled"),
    ("torx button head screw M6", "head-style variants not modeled"),
    ("shoulder bolt M6 thread with 10mm shoulder", "shoulder feature not modeled"),
    ("a splined shaft, 25mm, DIN 5480", "splines not modeled"),
    ("split ring lock washer M10", "spring/lock washer variants not modeled"),
]


def serialize(user_text):
    return (f"<|turn>system\n<|think|>\n{TRAIN_SYS_V2}<turn|>\n"
            f"<|turn>user\n{user_text}<turn|>\n<|turn>model\n")


def main():
    # SIGNAL-DENSE mix for GRPO (measure-and-fix: the v1 mix was 90% zero-variance easy builds — no
    # gradient). RL wants prompts where the model is UNCERTAIN: the measured failure mass (declines +
    # silent-drop classes) and only HARD builds (multi-part assemblies + gears, where params/mates drift).
    rng = random.Random(7)
    rows = []
    from constraint_kit import dsl
    traces = [json.loads(l) for l in open(f"{ROOT}/cadkit/output/dataset/traces.jsonl")]
    # HARD builds only: multi-part programs or gears (zero-variance easy single parts excluded)
    hard = [t for t in traces if t["tier"] in ("gold", "backfill", "ambiguous") and "program" in t
            and (len(t["program"].get("parts", [])) > 1
                 or any(p.get("type", "").endswith("gear") for p in t["program"].get("parts", [])))]
    rng.shuffle(hard)
    kept = 0
    for t in hard:
        if kept >= 300:
            break
        try:
            sig = dsl.program_signature(t["program"])
        except Exception:  # noqa: BLE001
            continue
        rows.append({"prompt": serialize(t["caption"]), "decline_expected": False,
                     "ref": json.dumps({"volume": sig["volume"], "bbox_sorted": sig["bbox_sorted"]})})
        kept += 1
    # DECLINES dominate (the failure mass): full taxonomy + the measured silent-drop classes weighted hard
    decls = [json.loads(l) for l in open(f"{ROOT}/cadkit/output/dataset/declines.jsonl")]
    rng.shuffle(decls)
    for d in decls[:500]:
        rows.append({"prompt": serialize(d["caption"]), "decline_expected": True, "ref": ""})
    for cap, _why in EXTRA_DECLINES * 25:                     # the keyway/shoulder/head-style silent drops
        rows.append({"prompt": serialize(cap), "decline_expected": True, "ref": ""})
    rng.shuffle(rows)
    with open(f"{ROOT}/training/grpo_prompts.jsonl", "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    n_d = sum(1 for r in rows if r["decline_expected"])
    print(f"grpo_prompts.jsonl: {len(rows)} prompts ({len(rows)-n_d} build w/ refs, {n_d} decline)")


if __name__ == "__main__":
    main()
