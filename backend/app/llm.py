"""
Shared LLM client — IBM Consulting Advantage, Claude Code endpoint.

Wraps the Anthropic SDK with a custom base_url pointing at the ICA endpoint.
All credentials are read from environment variables via app.config.Settings.

Usage
-----
from app.llm import chat

response_text = chat(
    system="You are Bob ...",
    user="What is ISO 20022?",
    max_tokens=2048,
)
"""
from __future__ import annotations

import logging
from functools import lru_cache

import anthropic

from app.config import settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _get_client() -> anthropic.Anthropic:
    """Build (and cache) the Anthropic client pointed at the ICA Claude Code endpoint."""
    return anthropic.Anthropic(
        api_key=settings.claude_api_key,
        base_url=settings.claude_base_url,
    )


def chat(
    system: str,
    user: str,
    max_tokens: int = 2048,
    temperature: float = 0.2,
) -> str:
    """
    Send a single chat completion via Claude Code and return the text response.
    Raises on network/auth error; caller handles retries.
    """
    client = _get_client()

    message = client.messages.create(
        model=settings.claude_model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system,
        messages=[{"role": "user", "content": user}],
    )

    return message.content[0].text or ""
