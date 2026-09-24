"""OpenAI-compatible chat client shared by the planner, vision QA, and spec extraction.

Defaults to DeepSeek's hosted API (deepseek-v4-pro plans, deepseek-flash handles images). Point
LLM_BASE_URL (or PLANNER_URL / VLM_URL) at a local vLLM / llama.cpp server to self-host instead.
DeepSeek only accepts response_format json_object, so a json_schema request is downgraded to
json_object with the schema spelled out in the system prompt; the caller still parses + validates.
"""
from __future__ import annotations

import json
import os

import httpx

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_PLANNER_MODEL = "deepseek-v4-pro"
DEFAULT_VLM_MODEL = "deepseek-flash"  # the DeepSeek model that accepts image input


def base_url(role: str | None = None) -> str:
    """role 'planner' honors PLANNER_URL, 'vlm' honors VLM_URL; both fall back to LLM_BASE_URL."""
    specific = {"planner": "PLANNER_URL", "vlm": "VLM_URL"}.get(role or "")
    url = (specific and os.environ.get(specific)) or os.environ.get(
        "LLM_BASE_URL", os.environ.get("QWEN_BASE_URL", DEFAULT_BASE_URL))
    return url.rstrip("/")


def planner_model() -> str:
    return os.environ.get("MODEL_PLANNER", os.environ.get("LLM_MODEL", DEFAULT_PLANNER_MODEL))


def vlm_model() -> str:
    return os.environ.get("MODEL_VLM", os.environ.get("LLM_VISION_MODEL", DEFAULT_VLM_MODEL))


def _is_deepseek(url: str) -> bool:
    return "api.deepseek.com" in url


def _adapt_for_deepseek(body: dict) -> dict:
    body = dict(body)
    body.pop("chat_template_kwargs", None)
    body["thinking"] = {"type": "disabled"}
    rf = body.get("response_format") or {}
    if rf.get("type") == "json_schema":
        schema = rf.get("json_schema", {}).get("schema", {})
        note = ("Output a single json object that validates against this JSON Schema:\n"
                + json.dumps(schema, separators=(",", ":")))
        msgs = list(body["messages"])
        if msgs and msgs[0]["role"] == "system":
            msgs[0] = {**msgs[0], "content": msgs[0]["content"] + "\n\n" + note}
        else:
            msgs.insert(0, {"role": "system", "content": note})
        body["messages"] = msgs
        body["response_format"] = {"type": "json_object"}
    return body


def chat(body: dict, *, url: str, timeout: float = 120.0) -> dict:
    """POST {url}/chat/completions and return the raw response JSON. Raises on HTTP errors."""
    url = url.rstrip("/")
    headers = {}
    key = os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if _is_deepseek(url):
        body = _adapt_for_deepseek(body)
    r = httpx.post(f"{url}/chat/completions", json=body, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()
