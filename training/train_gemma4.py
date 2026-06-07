#!/usr/bin/env python3
"""Gemma 4 12B SFT on the constraint-kit dataset (Unsloth, vast.ai single GPU).

Dataset rows (cadkit/output/dataset/{train,val}_g4.jsonl) are STRUCTURED: messages carry a separate
`reasoning` field and per-message `train` flags; THIS script owns the template application, building
labels with -100 over every non-trained segment (system, user, masked bad-attempt assistant turns).
Multimodal rows carry content arrays with {"type":"image","image":path}.

First run on a new box:  python train_gemma4.py --inspect     # prints one tokenized example with label
boundaries — visually verify masking BEFORE burning GPU hours (the gemma-4-thinking template is 3 days
old; trust nothing blind).

Train:  python train_gemma4.py --data dataset/ --out ckpt/ [--model unsloth/gemma-4-12b-it] [--text-only]
"""
from __future__ import annotations

import argparse
import json
import os


def build_example(tokenizer, row, text_only=False):
    """Apply the chat template turn by turn, masking labels (-100) for non-trained segments.
    Returns dict(input_ids, labels, images). Trained segments: assistant turns with train!=False —
    reasoning (thinking block) AND the final content are both trained."""
    input_ids, labels, images = [], [], []
    msgs = row["messages"]
    for i, m in enumerate(msgs):
        content = m.get("content", "")
        if isinstance(content, list):                        # multimodal content array
            if text_only:
                content = " ".join(p.get("text", "[image]") for p in content)
            else:
                for part in content:
                    if part.get("type") == "image":
                        images.append(part["image"])
        if m["role"] == "assistant":
            # MANUAL assistant-turn rendering: both available templates only STRIP provided reasoning
            # (serving templates) — so we serialize the model family's canonical thought-channel form
            # ourselves (verbatim from the official template's serialization branch).
            text = "<|turn>model\n"
            if m.get("reasoning"):
                text += "<|channel>thought\n" + m["reasoning"] + "\n<channel|>\n"
            ctext = content if isinstance(content, str) else " ".join(
                p.get("text", "") for p in m["content"] if p.get("type") == "text")
            text += ctext + "<turn|>\n"
            seg = tokenizer(text, add_special_tokens=False).input_ids
        else:
            # system/user turns: incremental template rendering (suffix of the templated prefix)
            kw = {"tokenize": True, "enable_thinking": True}
            prefix = tokenizer.apply_chat_template(msgs[: i], **kw) if i else []
            upto = tokenizer.apply_chat_template(msgs[: i + 1], **kw)
            seg = upto[len(prefix):]
        train_seg = m["role"] == "assistant" and m.get("train", True)
        input_ids += seg
        labels += seg if train_seg else [-100] * len(seg)
    return {"input_ids": input_ids, "labels": labels, "images": images}


def to_template_messages(row):
    """Convert our structured rows -> the form the gemma-4-thinking template expects: reasoning goes in
    the template's thinking channel (verified by --inspect; adjust ONE place here if the template wants
    a different key)."""
    out = []
    for m in row["messages"]:
        mm = {"role": m["role"], "content": m.get("content", "")}
        if m.get("reasoning"):
            mm["reasoning_content"] = m["reasoning"]          # gemma-4-thinking reasoning channel
        out.append(mm)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="dataset")
    ap.add_argument("--out", default="ckpt")
    ap.add_argument("--model", default="unsloth/gemma-4-12b-it")
    ap.add_argument("--max-seq", type=int, default=6144)
    ap.add_argument("--epochs", type=float, default=2.0)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--text-only", action="store_true", help="skip image parts (rung-1 recipe check)")
    ap.add_argument("--inspect", action="store_true", help="print one tokenized example + label boundaries")
    args = ap.parse_args()

    from unsloth import FastVisionModel
    model, tokenizer = FastVisionModel.from_pretrained(
        args.model, max_seq_length=args.max_seq, load_in_4bit=False, dtype=None)
    chat_tok = getattr(tokenizer, "tokenizer", tokenizer)
    if hasattr(chat_tok, "chat_template") and "think" not in str(chat_tok.chat_template):
        print("WARNING: tokenizer template has no thinking channel — set chat_template='gemma-4-thinking'")

    rows = [json.loads(l) for l in open(os.path.join(args.data, "train_g4.jsonl"))]
    if args.text_only:
        rows = [r for r in rows if not any(isinstance(m.get("content"), list) for m in r["messages"])]
    print(f"{len(rows)} rows ({'text-only' if args.text_only else 'multimodal'})")

    if args.inspect:
        for kind in ("trajectory", "reasoning", "decline"):
            row = next((r for r in rows if r["meta"]["kind"] == kind), None)
            if not row:
                continue
            ex = build_example(chat_tok, row, args.text_only)
            toks = chat_tok.convert_ids_to_tokens(ex["input_ids"])
            print(f"\n=== {kind} ({len(toks)} tokens) — '|' marks trained segments ===")
            trained = sum(1 for l in ex["labels"] if l != -100)
            out = "".join(("|" + t) if l != -100 else t for t, l in zip(toks, ex["labels"]))
            print(f"trained tokens: {trained}/{len(toks)} ({100*trained//max(len(toks),1)}%)")
            print("--- tail (assistant turns) ---")
            print(out[-2200:])
        return

    model = FastVisionModel.get_peft_model(
        model, r=args.rank, lora_alpha=args.rank, lora_dropout=0,
        finetune_vision_layers=not args.text_only, finetune_language_layers=True)

    from torch.utils.data import Dataset

    class DS(Dataset):
        def __len__(self):
            return len(rows)

        def __getitem__(self, i):
            r = rows[i]
            return build_example(chat_tok, r, args.text_only)

    from trl import SFTConfig, SFTTrainer
    trainer = SFTTrainer(
        model=model, processing_class=tokenizer, train_dataset=DS(),
        args=SFTConfig(output_dir=args.out, num_train_epochs=args.epochs, learning_rate=args.lr,
                       per_device_train_batch_size=1, gradient_accumulation_steps=8,
                       lr_scheduler_type="cosine", optim="adamw_8bit", bf16=True,
                       logging_steps=20, save_steps=500, dataset_kwargs={"skip_prepare_dataset": True},
                       remove_unused_columns=False, max_length=args.max_seq))
    trainer.train()
    model.save_pretrained(os.path.join(args.out, "lora_final"))
    print("done ->", os.path.join(args.out, "lora_final"))


if __name__ == "__main__":
    main()
