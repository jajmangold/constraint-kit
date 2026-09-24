# Training — Gemma 4 12B on the constraint-kit dataset (vast.ai)

**Released model:** [jajmangold/gemma-4-12b-constraintkit-GGUF](https://huggingface.co/jajmangold/gemma-4-12b-constraintkit-GGUF) (GRPO Q6_K GGUF + LoRA adapter + training system prompt).

The dataset teaches: NL → reasoning → exact DSL program; honest DECLINE; diagnostic-driven
self-correction (bad attempts loss-masked); **and the multimodal moat — render-image → program pairs +
visual-correction trajectories** that only this stack can mint (verified geometry + own renderer + ground-
truth programs).

## Rungs
1. `--text-only` — recipe validation (cheap, any 24GB card)
2. full multimodal — image→CAD + visual self-correction (the strategic run)
3. GRPO afterwards — Unsloth GRPO, colocated vLLM; reward = `constraint_kit.dsl` verifier **as a library**
   (`pip install cadquery`, no services): graded check-pass + builds + signature-proximity + exact bonus.

## Setup
1. Rent a GPU instance (Blackwell RTX PRO 6000, A100-80GB, or 24GB+ for `--text-only`).
2. Use a PyTorch CUDA image and install dependencies:
   ```bash
   pip install -q unsloth unsloth_zoo trl peft bitsandbytes tensorboard
   pip install git+https://github.com/huggingface/transformers.git  # Gemma 4 support
   ```
3. `git clone` this repo and copy `cadkit/output/dataset/` to the training box.
4. Run `python train_gemma4.py --inspect` first to verify label boundaries.
5. Run `python train_gemma4.py --text-only` (rung 1) then the full multimodal run (rung 2).

## Rung-1 RESULT (2026-06-07, geometric ground truth on 90 held-out probes)
Gemma 4 12B LoRA r=16, 2 epochs, ~40k text rows, final loss ~0.22-0.25, ~10.5h on RTX PRO 6000 (~$7):
**GEOMETRY-VERIFIED 56/60 build probes (93%)** (vol+bbox within 2% of held-out targets) ·
**decline-honest 26/30 (87%)** · 0 wrong-declines · 0 format failures · 0 build errors —
first-shot, no prompt scaffold, no correction loop. In prompted-DeepSeek's band (90-97%) from a local
12B with auditable reasoning traces. Artifacts (not in git): training/lora_rung1/ (251MB adapter) + Q6_K GGUF.


## GRPO RESULT (stage 3, 2026-06-07) — KEEP
Signal-dense GRPO (700 decline / 300 hard-build, 150 steps, verifier reward) on the rung-1 adapter.
Held-out A/B (16 unseen probes), rung-1 -> GRPO:
- silent-variant-builds (the target failure): 2/10 -> **1/10** (halved)
- declines correct: 8/10 -> **9/10** · builds: **6/6 -> 6/6** (no regression, no over-decline)
Training reward drifted down (1.07->0.74) but that was the harder-prompt mix, NOT overcooking —
held-out geometry confirmed improvement on target. Artifacts: training/lora_grpo/ +
gemma4-12b-constraintkit-grpo-Q6_K.gguf, both published at [Hugging Face](https://huggingface.co/jajmangold/gemma-4-12b-constraintkit-GGUF). Bugs killed en route (each a committed lesson): GRPO eos
(rollouts ran to max-len), TRL strips special tokens (reward extracts trailing JSON), prompt mix
(90%% zero-variance easy builds -> rebalanced to the failure mass).


## RUNG-2 (multimodal image->CAD): NEGATIVE (2026-06-08)
Two LoRA vision-SFT runs (Gemma-4-12b, ~1325 distinct renders x8, answer-free bbox-only prompts).
Held-out, images VERIFIED fed: image+bbox 52% vs bbox-only 47%; feature-rich (bbox non-discriminating)
16/27 == 16/27. The model ignores the render — bbox+priors carry everything. image->CAD via vision-LoRA
at this data scale does not work. Path forward (untested): 10-100x more distinct renders + full
vision-tower FT. Eval gotcha: use the PROCESSOR (proc.apply_chat_template(...,return_dict=True)) — not
the inner text tokenizer, which silently drops images (no pixel_values).

## Eval (back home)
Serve the LoRA (vLLM `--enable-lora` or merged) and point the existing harness at it: the gauntlet
(`drivers/target_gauntlet.py`), census (`drivers/census.py`), and `/dsl/verify` measure built%, decline
honesty, and signature-match — same benchmarks, new generator. Compare against the prompted-DeepSeek
baseline (90-97% on generation) before scaling anything.

## Notes
- Dataset format: STRUCTURED messages (`reasoning` field + per-message `train` flags). This script owns
  template application + label masking — no custom collator hidden in a framework.
- Known Unsloth/Gemma-4 gotchas (their docs): grad-accum loss inflation (fixed in current unsloth),
  `use_cache=False` gibberish (fixed), 26B/31B `num_kv_shared_layers` IndexError (fixed) — pin a recent
  unsloth.
- License: Gemma 4 is Apache-2.0, and so is the released fine-tune.
