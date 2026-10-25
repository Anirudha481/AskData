"""
llm_client.py
Thin wrapper around LLM providers so the rest of the app only sees one function:
    response = call_llm(messages, model, temperature, max_tokens)

Supported providers (set LLM_PROVIDER in .env):
  - "openai"     calls OpenAI chat completions REST API directly
  - "google"     calls Google Gemini via google-genai SDK
  - "anthropic"  calls Anthropic messages REST API directly
  - "groq"       calls Groq's OpenAI-compatible chat completions API

All HTTP is done with the `requests` library to avoid any httpx version conflicts.
Adding a new provider: implement a _call_<name> function and add it to PROVIDERS.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import requests

from app.core.config import settings


# ---------------------------------------------------------------------------
# Type alias for a chat message
# ---------------------------------------------------------------------------

Message = dict[str, str]  # {"role": "system"|"user"|"assistant", "content": "..."}


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------

def _call_openai(
    messages: list[Message],
    model: str,
    temperature: float,
    max_tokens: int,
) -> str:
    """
    Call the OpenAI chat completions REST endpoint directly with requests.
    Avoids the openai SDK entirely, so httpx version is irrelevant.
    """
    if not settings.openai_api_key:
        raise ValueError(
            "OPENAI_API_KEY is not set in your .env file.\n"
            "Get a key at https://platform.openai.com/api-keys"
        )

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }

    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers=headers,
        json=payload,
        timeout=60,
    )

    if not resp.ok:
        raise RuntimeError(
            f"OpenAI API error {resp.status_code}: {resp.text[:400]}"
        )

    data = resp.json()
    return data["choices"][0]["message"]["content"] or ""


def _call_groq(
    messages: list[Message],
    model: str,
    temperature: float,
    max_tokens: int,
) -> str:
    """Call Groq's OpenAI-compatible chat completions endpoint with automatic rate limit backoff."""
    if not settings.groq_api_key:
        raise ValueError(
            "GROQ_API_KEY is not set in your .env file.\n"
            "Get a key at https://console.groq.com/keys"
        )

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {settings.groq_api_key}",
        "Content-Type": "application/json",
    }

    max_retries = 3
    for attempt in range(max_retries + 1):
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=60,
        )

        if resp.status_code == 429 and attempt < max_retries:
            match = re.search(r"try again in\s+([0-9]+(?:\.[0-9]+)?)\s*s", resp.text, re.IGNORECASE)
            delay = min(float(match.group(1)) + 0.5, 30.0) if match else (2.0 ** attempt)
            time.sleep(delay)
            continue

        if not resp.ok:
            raise RuntimeError(f"Groq API error {resp.status_code}: {resp.text[:400]}")

        data = resp.json()
        return data["choices"][0]["message"]["content"] or ""

    raise RuntimeError("Groq API error 429: Maximum retries exceeded")


def _call_google(
    messages: list[Message],
    model: str,
    temperature: float,
    max_tokens: int,
) -> str:
    """
    Call Google Gemini using the native google-genai SDK.
    Requires an API key from https://aistudio.google.com/app/apikey
    Key starts with 'AIzaSy...', NOT an OAuth token (AQ.xxx).
    """
    from google import genai                        # type: ignore[import]
    from google.genai import types as genai_types  # type: ignore[import]

    if not settings.google_api_key:
        raise ValueError(
            "GOOGLE_API_KEY is not set.\n"
            "Get a free key at https://aistudio.google.com/app/apikey"
        )

    client = genai.Client(api_key=settings.google_api_key)

    system_instruction: str | None = None
    contents: list[Any] = []

    for m in messages:
        if m["role"] == "system":
            system_instruction = m["content"]
        elif m["role"] == "user":
            contents.append({"role": "user", "parts": [{"text": m["content"]}]})
        elif m["role"] == "assistant":
            contents.append({"role": "model", "parts": [{"text": m["content"]}]})

    config = genai_types.GenerateContentConfig(
        temperature=temperature,
        max_output_tokens=max_tokens,
        system_instruction=system_instruction,
    )

    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=config,
    )
    return response.text


def _call_anthropic(
    messages: list[Message],
    model: str,
    temperature: float,
    max_tokens: int,
) -> str:
    """
    Call Anthropic Claude messages REST endpoint directly with requests.
    """
    if not settings.anthropic_api_key:
        raise ValueError("ANTHROPIC_API_KEY is not set in your .env file.")

    system_msg = ""
    conv_messages: list[dict] = []
    for m in messages:
        if m["role"] == "system":
            system_msg = m["content"]
        else:
            conv_messages.append(m)

    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system_msg,
        "messages": conv_messages,
    }
    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers=headers,
        json=payload,
        timeout=60,
    )

    if not resp.ok:
        raise RuntimeError(
            f"Anthropic API error {resp.status_code}: {resp.text[:400]}"
        )

    return resp.json()["content"][0]["text"]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

PROVIDERS: dict[str, Any] = {
    "openai": _call_openai,
    "google": _call_google,
    "anthropic": _call_anthropic,
    "groq": _call_groq,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def call_llm(
    messages: list[Message],
    *,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    provider: str | None = None,
) -> str:
    """
    Send *messages* to the configured LLM and return the text response.

    All parameters fall back to values in `settings` if not provided,
    making it easy to call with just a message list in most places.
    """
    provider = provider or settings.llm_provider
    model = model or settings.llm_model
    temperature = temperature if temperature is not None else settings.llm_temperature
    max_tokens = max_tokens or settings.llm_max_tokens

    fn = PROVIDERS.get(provider)
    if fn is None:
        raise ValueError(
            f"Unknown LLM_PROVIDER '{provider}'. "
            f"Valid options: {list(PROVIDERS.keys())}"
        )

    return fn(messages, model, temperature, max_tokens)
