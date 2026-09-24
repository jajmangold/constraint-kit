"""Vision QA -- REUSES qwen27b VLM (standing instruction: use the VLM for vision, not built-in).

Geometry is ground truth (mass/bbox/interference come from the kernel); the VLM is a categorical
second opinion ("is the gear seated on the boss / anything floating / obviously wrong?"). qwen3.6 is
a reasoning model -> disable thinking or the answer lands in `reasoning` and content is null.
"""
from __future__ import annotations

import base64
import os

import httpx

DEFAULT_Q = ("This is a render of a CAD assembly: a mounting plate with a spur gear seated on its "
             "central boss. Is the gear actually sitting on the plate's boss (not floating, not "
             "intersecting the plate body)? Answer YES or NO first, then one sentence why.")


def ask(image_path: str, question: str = DEFAULT_Q, *,
        url: str | None = None, model: str | None = None, timeout: float = 120.0) -> str:
    url = (url or os.environ.get("VLM_URL", os.environ.get("QWEN_BASE_URL", "http://localhost:8000/v1"))).rstrip("/")
    model = model or os.environ.get("MODEL_VLM", "qwen27b")
    with open(image_path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode()
    body = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": question},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ],
        }],
        "temperature": 0.0,
        "max_tokens": 400,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    r = httpx.post(f"{url}/chat/completions", json=body, timeout=timeout)
    r.raise_for_status()
    return (r.json()["choices"][0]["message"].get("content") or "").strip()
