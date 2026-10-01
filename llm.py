"""Optional LLM client (off unless LLM_API_KEY is set).

Used only for free-form merchant questions in /v1/reply. Every output is validated
(no URLs, no numbers absent from the grounding facts, length cap) and the caller
falls back to a deterministic template on any failure or timeout. Responses are
cached by prompt hash, so the same input always yields the same output.

Env:
  LLM_PROVIDER  anthropic | openai        (openai = any OpenAI-compatible endpoint)
  LLM_API_KEY   provider key
  LLM_MODEL     default: claude-sonnet-5 (anthropic) / gpt-4o-mini (openai)
  LLM_BASE_URL  for OpenAI-compatible providers (DeepSeek, Groq, OpenRouter, ...)
  LLM_TIMEOUT   seconds, default 7
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Optional
from urllib import error as urlerror, request as urlrequest

PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").strip().lower()
API_KEY = os.getenv("LLM_API_KEY", "").strip()
MODEL = os.getenv("LLM_MODEL", "").strip() or ("claude-sonnet-5" if PROVIDER == "anthropic" else "gpt-4o-mini")
BASE_URL = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
TIMEOUT = float(os.getenv("LLM_TIMEOUT", "7"))

_cache: dict[str, Optional[str]] = {}
_lock = threading.Lock()


def enabled() -> bool:
    return bool(API_KEY)


def model_name() -> str:
    return f"{PROVIDER}:{MODEL}" if enabled() else "deterministic-templates (no LLM)"


def _post(url: str, body: dict, headers: dict) -> dict:
    req = urlrequest.Request(url, data=json.dumps(body).encode("utf-8"),
                             headers={"Content-Type": "application/json", **headers})
    with urlrequest.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _anthropic(system: str, user: str, max_tokens: int) -> str:
    body = {"model": MODEL, "max_tokens": max_tokens, "system": system,
            "messages": [{"role": "user", "content": user}], "temperature": 0}
    headers = {"x-api-key": API_KEY, "anthropic-version": "2023-06-01"}
    try:
        data = _post("https://api.anthropic.com/v1/messages", body, headers)
    except urlerror.HTTPError as e:
        if e.code == 400 and "temperature" in e.read().decode("utf-8", "ignore").lower():
            body.pop("temperature", None)  # some models fix sampling params
            data = _post("https://api.anthropic.com/v1/messages", body, headers)
        else:
            raise
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def _openai(system: str, user: str, max_tokens: int) -> str:
    body = {"model": MODEL, "max_tokens": max_tokens, "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    data = _post(f"{BASE_URL}/chat/completions", body, {"Authorization": f"Bearer {API_KEY}"})
    return data["choices"][0]["message"]["content"]


def complete(system: str, user: str, max_tokens: int = 350) -> Optional[str]:
    """Text or None (disabled / error / timeout). Never raises."""
    if not enabled():
        return None
    key = hashlib.sha256(f"{MODEL}\n{system}\n{user}".encode()).hexdigest()
    with _lock:
        if key in _cache:
            return _cache[key]
    try:
        out = (_anthropic if PROVIDER == "anthropic" else _openai)(system, user, max_tokens).strip()
    except Exception:
        return None  # not cached: a transient failure may succeed next time
    with _lock:
        _cache[key] = out
    return out
