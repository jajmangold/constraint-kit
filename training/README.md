# Training — Gemma 4 12B on the constraint-kit dataset (vast.ai)

The dataset teaches: NL → reasoning → exact DSL program; honest DECLINE; diagnostic-driven
self-correction (bad attempts loss-masked); **and the multimodal moat — render-image → program pairs +
visual-correction trajectories** that only this stack can mint (verified geometry + own renderer + ground-
truth programs).

## Rungs
1. `--text-only` — recipe validation (cheap, any 24GB card)
2. full multimodal — image→CAD + visual self-correction (the strategic run)
3. GRPO afterwards — Unsloth GRPO, colocated vLLM; reward = `constraint_kit.dsl` verifier **as a library**
   (`pip install cadquery`, no services): graded check-pass + builds + signature-proximity + exact bonus.

## vast.ai recipe
1. Rent: 1× A100-80GB or H100 (12B bf16 LoRA + activations is comfortable; 24GB works for `--text-only`).
   Image: `unsloth/unsloth` (official). 100GB disk.
2. `git clone` this repo; copy `cadkit/output/dataset/` (train_g4/val_g4.jsonl + the S7 PNGs it references —
   `rsync -a cadkit/output/dataset/ <box>:.../dataset/` plus `cadkit/output/s7_*png`; image paths in rows are
   absolute `/srv/nvme-data/...` — keep the same mount path or sed the prefix).
3. **`python train_gemma4.py --inspect` FIRST** — eyeball the printed label boundaries: system/user/bad-turn
   tokens must be unmarked, assistant reasoning+program marked. The gemma-4-thinking template is days old;
   if the reasoning channel renders empty, fix `to_template_messages()` (one place).
4. `python train_gemma4.py --text-only` (rung 1) → eval → full multimodal run (rung 2).

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
- Gemma license: read it once before shipping anything external (not Apache-2.0).
