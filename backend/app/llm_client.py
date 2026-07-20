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
  LLM_READ_TIMEOUT     240 s   time waiting for the first response byte
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
    """Build and cache the Anthropic SDK client.

    IMPORTANT: max_retries=0 disables the Anthropic SDK's own built-in retry
    loop.  We own all retry logic through Tenacity in llm_retry().
    Without this, a 502 storm triggers BOTH the SDK's retries AND our Tenacity
    retries, compounding into 200-300 s hangs:
      SDK attempt × 3 (internal) × Tenacity attempt × 3 (our) = 9 real HTTP
      requests, each waiting up to 25 s before the gateway gives up.
    With max_retries=0 the SDK raises immediately on any error, and Tenacity
    controls how many times we retry and how long we wait.
    """
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
        max_retries=0,          # Tenacity owns all retry logic — no SDK double-retry
    )


# Cache the Tenacity retry decorator (not the wrapped function) keyed on
# (max_retries, max_wait) so we don't rebuild the decorator on every chat() call.
# We wrap a thin proxy (_call_api_proxy) rather than _call_api directly so that
# unittest.mock.patch("app.llm_client._call_api") still works in tests — the proxy
# reads the current module-level _call_api on each invocation, which respects patches.
_retry_decorator_cache: dict = {}


def _call_api_proxy(system: str, user: str, max_tokens: int, temperature: float):
    """Thin proxy — always calls the current module-level _call_api.
    Allows unittest.mock.patch("app.llm_client._call_api") to work correctly
    even when the Tenacity wrapper is cached across test runs."""
    return _call_api(system, user, max_tokens, temperature)


def _failure_reason(cause: BaseException | None) -> str:
    """Human-readable reason for a surfaced LLM failure (429 / timeout / 5xx / …)."""
    if isinstance(cause, anthropic.APIStatusError):
        return {
            429: "provider rate limit (429)",
            500: "provider error (500)",
            502: "provider bad gateway (502)",
            503: "provider unavailable (503)",
            504: "provider gateway timeout (504)",
        }.get(getattr(cause, "status_code", None), f"provider error ({getattr(cause, 'status_code', '5xx')})")
    if isinstance(cause, httpx.TimeoutException):
        return "provider timeout"
    if isinstance(cause, (httpx.ConnectError, httpx.RemoteProtocolError)):
        return "provider connection error"
    return type(cause).__name__ if cause else "unknown error"


def _get_retried_call():
    """
    Return a Tenacity-wrapped _call_api_proxy for the current retry settings.

    The key is (llm_max_retries, llm_retry_max_wait).  When tests monkeypatch
    those settings, a new decorator is built automatically.  Patching
    _call_api is still effective because the proxy re-reads it each call.
    """
    key = (settings.llm_max_retries, settings.llm_retry_max_wait)
    if key not in _retry_decorator_cache:
        _retry_decorator_cache[key] = llm_retry()(_call_api_proxy)
    return _retry_decorator_cache[key]


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

    # Use a module-level cached retry wrapper so the Tenacity decorator is not
    # re-instantiated on every call.  The wrapper is rebuilt only when settings
    # change (test monkeypatching forces a cache clear via _reset_retry_cache).
    retried_call = _get_retried_call()

    with circuit_breaker:
        try:
            message = retried_call(system, user, max_tokens, temperature)

        except RetryError as exc:
            # All attempts exhausted — unwrap, log, and SURFACE the real root cause
            # (provider 5xx / rate-limit / timeout) instead of a generic message, so
            # operators and users can tell what actually failed.
            cause = exc.last_attempt.exception()
            retry_count = exc.last_attempt.attempt_number - 1
            reason = _failure_reason(cause)
            logger.error(
                "[req=%s op=%s] LLM call failed after %d retries. "
                "Final error: %s %s",
                request_id, operation, retry_count,
                type(cause).__name__, cause,
            )
            raise HTTPException(
                status_code=503,
                detail=f"Generation failed: {reason} (after {retry_count + 1} attempts). "
                       "This is a temporary provider issue — please try again shortly.",
            ) from exc

        except Exception:
            # Non-transient error (auth failure, budget 429, etc.) — let it propagate.
            raise

    latency_ms = (time.monotonic() - t0) * 1000

    stats = getattr(retried_call, "statistics", {})
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
