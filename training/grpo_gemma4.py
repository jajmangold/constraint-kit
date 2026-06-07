#!/usr/bin/env python3
"""GRPO on the rung-1 LoRA: rollouts scored by the DETERMINISTIC verifier reward (grpo_reward.py —
decline-aware + geometry-graded). Continues training the SAME adapter. Run on the GPU box:

  python grpo_gemma4.py [--steps 400] [--lora /root/ckpt_rung1/lora_final]

Box needs: unsloth (+transformers git main), trl, cadquery + cq_gears, and the constraint_kit package
on PYTHONPATH (rsync the repo) — the reward BUILDS every rollout's geometry on the box's CPUs.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = "/srv/nvme-data/containers/constraint-kit"
sys.path.insert(0, REPO)
sys.path.insert(0, f"{REPO}/training")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lora", default="/root/ckpt_rung1/lora_final")
    ap.add_argument("--prompts", default=f"{REPO}/training/grpo_prompts.jsonl")
    ap.add_argument("--out", default="/root/ckpt_grpo")
    ap.add_argument("--steps", type=int, default=400)
    ap.add_argument("--gens", type=int, default=6)
    ap.add_argument("--lr", type=float, default=5e-6)
    ap.add_argument("--preflight", action="store_true", help="generate+score 3 samples, print, exit")
    args = ap.parse_args()

    from grpo_reward import reward_fn, score  # noqa: F401  (verifier reward; sanity import early)
    from constraint_kit import dsl  # noqa: F401  -- fail FAST if cadquery/constraint_kit missing

    from unsloth import FastVisionModel
    model, tokenizer = FastVisionModel.from_pretrained(
        args.lora, max_seq_length=2600, load_in_4bit=False, dtype=None)
    chat_tok = getattr(tokenizer, "tokenizer", tokenizer)

    # STOP at turn end — without this, rollouts run past the program into max-length babble,
    # every completion fails parse (-1.0), groups have zero variance, and GRPO learns nothing
    # (observed: 123 wasted steps at mean reward -1.0, frac_zero_std 0.9-1.0).
    turn_id = chat_tok.convert_tokens_to_ids("<turn|>")
    eos_ids = sorted({i for i in (chat_tok.eos_token_id, turn_id) if isinstance(i, int) and i >= 0})
    model.generation_config.eos_token_id = eos_ids
    print(f"eos ids: {eos_ids} (turn_id={turn_id})")

    from datasets import Dataset
    rows = [json.loads(l) for l in open(args.prompts)]
    ds = Dataset.from_list(rows)
    print(f"{len(ds)} prompts ({sum(r['decline_expected'] for r in rows)} decline-expected)")

    if args.preflight:
        import torch
        for r in rows[:3]:
            ids = chat_tok(r["prompt"], return_tensors="pt").to("cuda")
            with torch.no_grad():
                out = model.generate(**ids, max_new_tokens=900, temperature=0.8, do_sample=True)
            text = chat_tok.decode(out[0][ids["input_ids"].shape[1]:], skip_special_tokens=False)
            rf = json.loads(r["ref"]) if r["ref"] else None
            print(f"REWARD={score(text, r['decline_expected'], rf):+.2f} | len={len(text)} | tail: ...{text[-160:]!r}")
        return

    from trl import GRPOConfig, GRPOTrainer
    cfg = GRPOConfig(
        output_dir=args.out, max_steps=args.steps, learning_rate=args.lr,
        per_device_train_batch_size=args.gens, num_generations=args.gens,
        gradient_accumulation_steps=2, max_prompt_length=1200, max_completion_length=1100,
        temperature=0.7, bf16=True, optim="adamw_8bit", logging_steps=5,
        save_steps=100, report_to="tensorboard", remove_unused_columns=False)
    trainer = GRPOTrainer(model=model, processing_class=chat_tok, args=cfg,
                          train_dataset=ds, reward_funcs=[reward_fn])
    trainer.train()
    model.save_pretrained(os.path.join(args.out, "lora_grpo_final"))
    print("done ->", os.path.join(args.out, "lora_grpo_final"))


if __name__ == "__main__":
    main()
