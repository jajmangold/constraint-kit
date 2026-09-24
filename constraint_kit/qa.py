"""Vision QA -- asks the vision model (deepseek-flash by default, see llm.py) about a render.

Geometry is ground truth (mass/bbox/interference come from the kernel); the VLM is a categorical
second opinion ("is the gear seated on the boss / anything floating / obviously wrong?"). Thinking is
disabled, or the answer lands in `reasoning` and content is null.
"""
from __future__ import annotations

import base64

from . import llm

DEFAULT_Q = ("This is a render of a CAD assembly: a mounting plate with a spur gear seated on its "
             "central boss. Is the gear actually sitting on the plate's boss (not floating, not "
             "intersecting the plate body)? Answer YES or NO first, then one sentence why.")


def ask(image_path: str, question: str = DEFAULT_Q, *,
        url: str | None = None, model: str | None = None, timeout: float = 120.0) -> str:
    url = url or llm.base_url("vlm")
    model = model or llm.vlm_model()
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
    return (llm.chat(body, url=url, timeout=timeout)["choices"][0]["message"].get("content") or "").strip()
