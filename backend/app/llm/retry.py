"""
LLM retry policy — tenacity-based retry with exponential backoff + jitter.

Transient errors that are retried:
  - httpx.TimeoutException  (connect, read, write, pool timeout)
  - httpx.ConnectError      (DNS failure, TCP refused)
  - httpx.RemoteProtocolError
  - anthropic.APIStatusError with status 429 (rate-limited by upstream)
  - anthropic.APIStatusError with status 5xx (provider-side errors)

Errors that are NOT retried (fail immediately):
  - anthropic.AuthenticationError  (bad API key)
  - anthropic.PermissionDeniedError
  - anthropic.NotFoundError
  - anthropic.UnprocessableEntityError  (invalid prompt / request body)
  - fastapi.HTTPException              (circuit breaker open, budget exceeded)
  - ValueError / TypeError             (programming errors)

Configuration (via settings):
  LLM_MAX_RETRIES      default 4   attempts before giving up (1 original + 3 retries)
  LLM_RETRY_MAX_WAIT   default 30  seconds cap on per-attempt backoff sleep
"""
from __future__ import annotations

import logging

import httpx
import anthropic
from fastapi import HTTPException
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
    before_sleep_log,
    RetryError,
)

from app.config import settings

logger = logging.getLogger(__name__)



def _is_transient(exc: BaseException) -> bool:
    """
    Return True if the exception is a transient failure that is safe to retry.
    Anything returning False will propagate immediately without further attempts.
    """
    # Network-level transients (timeout, connection refused, broken pipe)
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError)):
        return True

    # Upstream HTTP status codes that indicate a temporary condition
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code in (429, 500, 502, 503, 504)

    # Everything else (auth errors, validation errors, circuit-breaker 503s) → fail fast
    return False




def llm_retry():
    """
    Return a tenacity @retry decorator configured from application settings.

    Called as a factory so the decorator always reads the live settings values
    (useful in tests that monkeypatch settings).

    Usage::

        @llm_retry()
        def _call_api(...):
            ...
    """
    return retry(
        retry=retry_if_exception(_is_transient),
        stop=stop_after_attempt(settings.llm_max_retries + 1),   # +1 = original attempt
        wait=wait_exponential_jitter(
            initial=1.0,
            max=settings.llm_retry_max_wait,
            jitter=1.0,
        ),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=False,   # raises tenacity.RetryError on exhaustion — caller normalises it
    )
