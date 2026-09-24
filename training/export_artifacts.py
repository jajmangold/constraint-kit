#!/usr/bin/env python3
"""Post-eval export (run ON the box, BEFORE destroying it — it has the GPU, disk, and cached base):
1. merge LoRA -> 16-bit standalone model dir
2. GGUF quant, Q6_K_XL-preferred (Unsloth dynamic quant naming varies by release; tries variants in
   order and falls back to plain q6_k), for local serving via llama.cpp/ollama.

  python export_artifacts.py [lora_dir]
"""
from __future__ import annotations

import os
import sys

LORA = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("LORA_DIR", "/tmp/lora_final")
EXPORT_DIR = os.environ.get("EXPORT_DIR", "/tmp/export")
MERGED = os.path.join(EXPORT_DIR, "merged16")
GGUF = os.path.join(EXPORT_DIR, "gguf")


def main():
    from unsloth import FastVisionModel
    model, tokenizer = FastVisionModel.from_pretrained(LORA, max_seq_length=6144, load_in_4bit=False)
    os.makedirs(EXPORT_DIR, exist_ok=True)

    print("=== 1/2 merging LoRA -> 16-bit ===", flush=True)
    try:
        model.save_pretrained_merged(MERGED, tokenizer)
    except TypeError:
        model.save_pretrained_merged(MERGED, tokenizer, save_method="merged_16bit")
    print("merged ->", MERGED, flush=True)

    print("=== 2/2 GGUF quant (Q6_K_XL preferred) ===", flush=True)
    for method in ("ud-q6_k_xl", "q6_k_xl", "UD-Q6_K_XL", "q6_k"):
        try:
            model.save_pretrained_gguf(GGUF, tokenizer, quantization_method=method)
            print(f"GGUF OK with quantization_method={method!r} -> {GGUF}", flush=True)
            break
        except Exception as exc:  # noqa: BLE001
            print(f"  {method!r} failed: {str(exc)[:160]}", flush=True)
    for root, _d, files in os.walk(EXPORT_DIR):
        for f in files:
            p = os.path.join(root, f)
            if os.path.getsize(p) > 50e6:
                print(f"{os.path.getsize(p)/1e9:6.2f} GB  {p}")
    print("PULL HOME: the gguf file(s) + keep lora_final; then destroy the instance.")


if __name__ == "__main__":
    main()
