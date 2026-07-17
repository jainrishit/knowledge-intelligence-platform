"""
LLM client — wraps the Anthropic SDK with resilience and observability.

Wraps the Anthropic SDK with:
  - Secret retrieval via SecretProvider (no direct os.getenv access)
  - httpx.Timeout: explicit connect / read / write limits — no indefinite hangs
  - Tenacity retry: exponential backoff + jitter for transient failures
  - Circuit breaker: rejects calls fast when the provider is unhealthy
  - Structured observability: latency, token counts, cost, retry count per call

Timeout defaults (all configurable via environment):
  LLM_CONNECT_TIMEOUT   10 s   TCP handshake + TLS
  LLM_READ_TIMEOUT     120 s   time waiting for the first response byte
  LLM_WRITE_TIMEOUT     30 s   time uploading the request body

Retry defaults:
  LLM_MAX_RETRIES        4     attempts after the first (total 5 tries)
  LLM_RETRY_MAX_WAIT    30 s   cap on per-attempt backoff sleep

Caller contract:
  - Raises fastapi.HTTPException(503) when the circuit breaker is open.
  - Raises fastapi.HTTPException(503) when all retry attempts are exhausted.
  - Raises any non-transient anthropic.* / httpx.* exception immediately.
"""
from __future__ import annotations

import logging
import time
import uuid
from functools import lru_cache

import anthropic
import httpx
from fastapi import HTTPException
from tenacity import RetryError

from app.config import settings
from app.llm.circuit_breaker import circuit_breaker
from app.llm.retry import llm_retry, _is_transient
from app.security.secrets import get_secret_provider

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _get_client() -> anthropic.Anthropic:
    """Build and cache the Anthropic SDK client."""
    provider = get_secret_provider()
    api_key = provider.get_secret(settings.claude_secret_name)
    timeout = httpx.Timeout(
        connect=settings.llm_connect_timeout,
        read=settings.llm_read_timeout,
        write=settings.llm_write_timeout,
        pool=settings.llm_connect_timeout,
    )
    return anthropic.Anthropic(
        api_key=api_key,
        base_url=settings.claude_base_url,
        timeout=timeout,
    )


def _call_api(
    system: str,
    user: str,
    max_tokens: int,
    temperature: float,
) -> anthropic.types.Message:
    """Execute one Anthropic messages.create call."""
    client = _get_client()
    return client.messages.create(
        model=settings.claude_model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system,
        messages=[{"role": "user", "content": user}],
    )


def chat(
    system: str,
    user: str,
    max_tokens: int = 2048,
    temperature: float = 0.2,
    operation: str = "llm_call",
) -> str:
    """
    Send a single chat completion via Claude and return the text response.

    Parameters
    ----------
    system      : System prompt text.
    user        : User turn content.
    max_tokens  : Upper bound on generated tokens.
    temperature : Sampling temperature (0.0 for deterministic QA).
    operation   : Logical label for observability logs (e.g. 'concept_extraction').

    Raises
    ------
    fastapi.HTTPException(503) — circuit breaker open, or retries exhausted.
    anthropic.AuthenticationError — bad API key (not retried).
    """
    request_id = uuid.uuid4().hex[:8]
    t0 = time.monotonic()
    retry_count = 0

    # Build a fresh retry decorator each call so monkeypatching settings in
    # tests immediately takes effect without restarting the process.
    _retried_call = llm_retry()(_call_api)

    with circuit_breaker:
        try:
            message = _retried_call(system, user, max_tokens, temperature)

        except RetryError as exc:
            # All attempts exhausted — unwrap and log the root cause.
            cause = exc.last_attempt.exception()
            retry_count = exc.last_attempt.attempt_number - 1
            logger.error(
                "[req=%s op=%s] LLM call failed after %d retries. "
                "Final error: %s %s",
                request_id, operation, retry_count,
                type(cause).__name__, cause,
            )
            raise HTTPException(
                status_code=503,
                detail="LLM request failed after multiple retry attempts. Please try again shortly.",
            ) from exc

        except Exception:
            # Non-transient error (auth failure, budget 429, etc.) — let it propagate.
            raise

    latency_ms = (time.monotonic() - t0) * 1000

    stats = getattr(_retried_call, "statistics", {})
    retry_count = max(0, stats.get("attempt_number", 1) - 1)

    usage = message.usage
    prompt_tokens     = getattr(usage, "input_tokens",  0)
    completion_tokens = getattr(usage, "output_tokens", 0)

    from app.llm.usage_tracker import estimate_cost
    cost = estimate_cost(prompt_tokens, completion_tokens)

    logger.info(
        "[req=%s op=%s] LLM call completed: model=%s latency=%.0fms "
        "prompt_tokens=%d completion_tokens=%d cost=$%.6f retries=%d",
        request_id, operation, settings.claude_model, latency_ms,
        prompt_tokens, completion_tokens, cost, retry_count,
    )

    return message.content[0].text or ""
