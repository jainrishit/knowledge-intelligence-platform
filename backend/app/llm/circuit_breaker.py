"""
Circuit breaker for LLM calls.

Protects the application from cascading failures when the LLM provider is
unavailable, rate-limiting, or timing out.

State machine
-------------
CLOSED   — normal operation; failures are counted
OPEN     — breaker is tripped; all calls are rejected immediately
HALF_OPEN — recovery probe; a single call is allowed through to test health

Transition rules
----------------
CLOSED  → OPEN      when consecutive_failures >= failure_threshold
OPEN    → HALF_OPEN after recovery_timeout_seconds have elapsed
HALF_OPEN → CLOSED  on successful call
HALF_OPEN → OPEN    on failed call (resets the recovery timer)

Configuration (all via environment / Settings):
  LLM_CB_FAILURE_THRESHOLD   default 5   failures before opening
  LLM_CB_RECOVERY_TIMEOUT    default 60  seconds before half-open probe
"""
from __future__ import annotations

import logging
import threading
import time
from enum import Enum

from fastapi import HTTPException

from app.config import settings

logger = logging.getLogger(__name__)


class BreakerState(str, Enum):
    CLOSED    = "closed"
    OPEN      = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """
    Thread-safe circuit breaker.

    Usage::

        breaker = CircuitBreaker()

        with breaker:
            result = call_llm_api()
    """

    def __init__(
        self,
        failure_threshold: int | None = None,
        recovery_timeout: float | None = None,
    ) -> None:
        self._threshold = failure_threshold or settings.llm_cb_failure_threshold
        self._timeout   = recovery_timeout  or settings.llm_cb_recovery_timeout
        self._failures  = 0
        self._state     = BreakerState.CLOSED
        self._opened_at: float | None = None
        self._lock      = threading.Lock()



    @property
    def state(self) -> BreakerState:
        with self._lock:
            return self._evaluated_state()

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._state    = BreakerState.CLOSED
            self._opened_at = None
        logger.debug("Circuit breaker: call succeeded → CLOSED")

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self._threshold:
                self._state     = BreakerState.OPEN
                self._opened_at = time.monotonic()
                logger.warning(
                    "Circuit breaker OPEN after %d consecutive failures.", self._failures
                )

    def __enter__(self) -> "CircuitBreaker":
        with self._lock:
            state = self._evaluated_state()

        if state == BreakerState.OPEN:
            logger.warning("Circuit breaker is OPEN — rejecting LLM request.")
            raise HTTPException(
                status_code=503,
                detail="LLM service temporarily unavailable. Please try again shortly.",
            )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if exc_type is None:
            self.record_success()
        else:
            # Only count genuine provider/network errors, not validation errors.
            if not issubclass(exc_type, (ValueError, HTTPException)):
                self.record_failure()
        return False  # never suppress exceptions



    def _evaluated_state(self) -> BreakerState:
        """
        Transition OPEN → HALF_OPEN if the recovery timeout has elapsed.
        Must be called with self._lock held.
        """
        if self._state == BreakerState.OPEN and self._opened_at is not None:
            elapsed = time.monotonic() - self._opened_at
            if elapsed >= self._timeout:
                self._state = BreakerState.HALF_OPEN
                logger.info(
                    "Circuit breaker: recovery timeout elapsed (%.0fs) → HALF_OPEN",
                    elapsed,
                )
        return self._state


# Application-level singleton — shared across all threads in one process.
circuit_breaker = CircuitBreaker()
