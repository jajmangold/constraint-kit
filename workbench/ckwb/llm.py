# -*- coding: utf-8 -*-
"""A minimal OpenAI-compatible chat client, on the standard library only.

`urllib.request`, not `requests` or `httpx`: this runs inside FreeCAD's interpreter, where
whatever the host environment has pip-installed is not available and adding a dependency to a
CAD application to make one HTTP POST is a poor trade. One function, no session state.

Works against anything that speaks the chat-completions shape — DeepSeek, OpenAI, OpenRouter, or
a local vLLM / llama.cpp / Ollama / LM Studio server, which is the case worth optimising for
since it needs no key at all.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 180.0


class LLMError(RuntimeError):
    """The endpoint could not be reached, or did not answer in the expected shape.

    Always carries what to do about it: a key problem, a URL problem and a model-name problem all
    look the same from the call site and need different fixes.
    """


def chat(messages: list, *, base_url: str, api_key: str = "", model: str,
         temperature: float = 0.0, json_object: bool = True,
         timeout: float = DEFAULT_TIMEOUT) -> str:
    """One chat completion. Returns the assistant's message content as text.

    `temperature=0` by default: this is used to produce a program that must validate, and
    sampling variety buys nothing but irreproducibility.

    `json_object` asks for JSON mode where the server supports it. It is a hint, not a guarantee
    — plenty of local servers ignore it — so the caller must still cope with a fenced or chatty
    reply rather than relying on this.
    """
    if not base_url:
        raise LLMError(
            "no model endpoint configured. Set one in Settings: a hosted URL such as "
            "https://api.deepseek.com, or a local server such as http://127.0.0.1:11434/v1 "
            "(Ollama) or http://127.0.0.1:1234/v1 (LM Studio), which need no API key.")
    if not model:
        raise LLMError("no model name configured; set one in Settings")

    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        if not url.endswith("/v1"):
            url += "/v1"
        url += "/chat/completions"

    payload = {"model": model, "messages": messages, "temperature": float(temperature)}
    if json_object:
        payload["response_format"] = {"type": "json_object"}

    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
        except Exception:  # noqa: BLE001
            pass
        hint = {401: " — the API key is missing or rejected",
                403: " — the key is not permitted to use this model",
                404: f" — no such endpoint or model at {url}",
                429: " — rate limited"}.get(exc.code, "")
        raise LLMError(f"{url} returned HTTP {exc.code}{hint}. {detail}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(
            f"cannot reach {url}: {exc.reason}. If this is a local server, check it is running; "
            "if it is hosted, check the URL and your network.") from exc
    except json.JSONDecodeError as exc:
        raise LLMError(f"{url} did not return JSON: {exc}") from exc

    try:
        return raw["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(
            f"unexpected response shape from {url}: {json.dumps(raw)[:400]}") from exc


def probe(base_url: str, api_key: str = "", timeout: float = 10.0) -> list:
    """List the models an endpoint offers, for the Settings dialog's connection test."""
    url = base_url.rstrip("/")
    if not url.endswith("/models"):
        if not url.endswith("/v1"):
            url += "/v1"
        url += "/models"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise LLMError(f"cannot list models at {url}: {exc}") from exc
    return [m.get("id") for m in raw.get("data", []) if m.get("id")]
