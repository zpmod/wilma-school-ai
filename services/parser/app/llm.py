"""Ollama chat client with one JSON-retry on parse failure."""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from .prompt import RETRY_REMINDER, SYSTEM_PROMPT, build_user_prompt

log = logging.getLogger(__name__)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL = os.environ.get(
    "WILMA_PARSER_MODEL",
    "hf.co/mradermacher/Llama-Poro-2-8B-Instruct-GGUF:Q4_K_M",
)
NUM_CTX = int(os.environ.get("WILMA_PARSER_NUM_CTX", "8192"))
NUM_PREDICT = int(os.environ.get("WILMA_PARSER_NUM_PREDICT", "2000"))
TIMEOUT_S = float(os.environ.get("WILMA_PARSER_TIMEOUT_S", "1800"))


class LLMError(Exception):
    pass


async def _chat(messages: list[dict[str, str]]) -> str:
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        r = await client.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": MODEL,
                "stream": False,
                "format": "json",
                "options": {
                    "temperature": 0.0,
                    "num_ctx": NUM_CTX,
                    "num_predict": NUM_PREDICT,
                },
                "messages": messages,
            },
        )
    r.raise_for_status()
    return r.json()["message"]["content"]


async def extract_events(
    *, sent: str, sender: str, subject: str, body: str, today: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return (events, debug). Retries once on JSON parse failure."""
    user = build_user_prompt(sent=sent, sender=sender, subject=subject, body=body, today=today)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
    raw1 = await _chat(messages)
    try:
        data = json.loads(raw1)
        return data.get("events", []), {"attempts": 1, "raw": raw1}
    except json.JSONDecodeError as e:
        log.warning("JSON parse failure on attempt 1: %s — retrying", e)

    # Retry: feed assistant's bad output back + reminder.
    messages.append({"role": "assistant", "content": raw1})
    messages.append({"role": "user", "content": RETRY_REMINDER})
    raw2 = await _chat(messages)
    try:
        data = json.loads(raw2)
        return data.get("events", []), {"attempts": 2, "raw": raw2}
    except json.JSONDecodeError as e:
        raise LLMError(f"JSON parse failed twice: {e}; last raw: {raw2[:300]!r}") from e


async def healthcheck() -> dict[str, Any]:
    """Confirm ollama is reachable and the chosen model is loaded."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            r = await client.get(f"{OLLAMA_URL}/api/tags")
            r.raise_for_status()
        except httpx.HTTPError as e:
            return {"ok": False, "ollama": False, "error": str(e)}
        models = [m["name"] for m in r.json().get("models", [])]
        return {
            "ok": MODEL in models,
            "ollama": True,
            "model": MODEL,
            "model_loaded": MODEL in models,
            "available": models,
        }
