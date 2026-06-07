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

## vast.ai recipe (battle-tested 2026-06-06 — every step below was a real failure first)
0. **SSH key is `~/.ssh/vast_gpu_falcon`** — every ssh/rsync needs `-i ~/.ssh/vast_gpu_falcon`.
   Endpoint: `vastai ssh-url <id>`. Destroy needs `yes | vastai destroy instance <id>` (else "Aborted.").
1. Rent: Blackwell RTX PRO 6000 (97.9GB, often ~$0.69/hr) or A100-80GB; 24GB works for `--text-only`.
   Image: `pytorch/pytorch:2.7.1-cuda12.8-cudnn9-devel` — **NOT `unsloth/unsloth` (its sshd is broken
   under vast ssh-mode)** — with onstart:
   `pip install -q unsloth unsloth_zoo trl peft bitsandbytes tensorboard; touch /root/PROVISIONED`.
   After boot: fix DNS if name resolution fails (`echo 'nameserver 8.8.8.8' > /etc/resolv.conf`), and
   install transformers from git main (Gemma 4 needs `gemma4_unified`, absent from releases ≤5.5.0):
   `pip install git+https://github.com/huggingface/transformers.git`.
2. `git clone` this repo; copy `cadkit/output/dataset/` (train_g4/val_g4.jsonl + the S7 PNGs it references —
   `rsync -a cadkit/output/dataset/ <box>:.../dataset/` plus `cadkit/output/s7_*png`; image paths in rows are
   absolute `/srv/nvme-data/...` — keep the same mount path or sed the prefix).
3. **`python train_gemma4.py --inspect` FIRST** — eyeball the printed label boundaries: system/user/bad-turn
   tokens must be unmarked, assistant reasoning+program marked. The gemma-4-thinking template is days old;
   if the reasoning channel renders empty, fix `to_template_messages()` (one place).
4. `python train_gemma4.py --text-only` (rung 1) → eval → full multimodal run (rung 2).

## Rung-1 RESULT (2026-06-07, geometric ground truth on 90 held-out probes)
Gemma 4 12B LoRA r=16, 2 epochs, ~40k text rows, final loss ~0.22-0.25, ~10.5h on RTX PRO 6000 (~$7):
**GEOMETRY-VERIFIED 56/60 build probes (93%)** (vol+bbox within 2% of held-out targets) ·
**decline-honest 26/30 (87%)** · 0 wrong-declines · 0 format failures · 0 build errors —
first-shot, no prompt scaffold, no correction loop. In prompted-DeepSeek's band (90-97%) from a local
12B with auditable reasoning traces. Artifacts: training/lora_rung1/ (251MB adapter) + Q6_K_XL GGUF.

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
