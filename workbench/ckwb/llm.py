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

# Per-SOCKET-OPERATION, not a total deadline — urllib has no such thing. A server that accepts
# the connection and then sends nothing will stall for this long per read, so keep it short
# enough that a human waiting on it does not conclude the application has died. Measured against
# a real endpoint that answered /models in 0.5 s and then never answered a chat completion.
DEFAULT_TIMEOUT = 60.0


class LLMError(RuntimeError):
    """The endpoint could not be reached, or did not answer in the expected shape.

    Always carries what to do about it: a key problem, a URL problem and a model-name problem all
    look the same from the call site and need different fixes.
    """


def _mentions_json(messages: list) -> bool:
    """Does any message contain the word "json"? Some servers require it for JSON mode."""
    return any("json" in str(m.get("content", "")).lower() for m in messages)


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
    if json_object and _mentions_json(messages):
        # Only when the prompt actually says "json". DeepSeek rejects response_format outright
        # otherwise: HTTP 400 "Prompt must contain the word 'json' in some form to use
        # 'response_format' of type 'json_object'". Relying on the caller's prompt happening to
        # contain the word is luck, not design, so it is checked.
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
    except TimeoutError as exc:
        raise LLMError(
            f"{url} accepted the connection but sent no response within {timeout:.0f}s. The "
            "endpoint is reachable, so this is not a URL or key problem — the model is queued, "
            "overloaded, or the account cannot currently serve completions. Check the provider's "
            "status and balance, or point Settings at a local server.") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError):
            raise LLMError(
                f"{url} sent no response within {timeout:.0f}s. The endpoint is reachable, so "
                "this is not a URL or key problem — check the provider's status and "
                "balance.") from exc
        raise LLMError(
            f"cannot reach {url}: {reason}. If this is a local server, check it is running; "
            "if it is hosted, check the URL and your network.") from exc
    except json.JSONDecodeError as exc:
        raise LLMError(f"{url} did not return JSON: {exc}") from exc

    try:
        choice = raw["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError(
            f"unexpected response shape from {url}: {json.dumps(raw)[:400]}") from exc

    content = message.get("content") or ""
    if content.strip():
        return content

    # An empty answer with HTTP 200. The cause is knowable here and nowhere downstream, so it is
    # named here rather than handed on as "the model did not return valid JSON" -- which is true
    # of an empty string and tells nobody anything.
    reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
    finish = choice.get("finish_reason")
    if reasoning:
        if finish == "length":
            raise LLMError(
                f"{model} spent its entire output budget on reasoning and returned no answer "
                f"({len(reasoning)} characters of reasoning, finish_reason=length). It is a "
                "reasoning model; either raise its output limit or configure a model that does "
                "not think before answering.")
        raise LLMError(
            f"{model} returned {len(reasoning)} characters of reasoning and an empty answer. It "
            "is a reasoning model and its reply never reached the answer field; try a "
            "non-reasoning model, or the same model with thinking disabled.")
    raise LLMError(
        f"{model} returned an empty message (finish_reason={finish!r}). The endpoint and key are "
        "working, so this is the model or the request: check the model name is one this endpoint "
        "actually serves.")


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
