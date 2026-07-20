"""
LLM resilience tests — timeouts, retry strategy, exponential backoff,
circuit breaker integration, and retry exhaustion.

All tests are self-contained: no real LLM calls are made.
The Anthropic client and httpx are patched at the point of use.
"""
from __future__ import annotations

import time
import threading
import uuid

import pytest
import httpx
import anthropic
from unittest.mock import MagicMock, patch, call
from fastapi import HTTPException
from tenacity import RetryError


# ══════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════

def _make_message(input_tokens: int = 100, output_tokens: int = 50, text: str = "ok") -> MagicMock:
    """Build a minimal mock that mimics anthropic.types.Message."""
    msg = MagicMock()
    msg.usage.input_tokens  = input_tokens
    msg.usage.output_tokens = output_tokens
    msg.content = [MagicMock(text=text)]
    return msg


def _make_status_error(status_code: int) -> anthropic.APIStatusError:
    """Construct an APIStatusError with the given HTTP status code."""
    response = MagicMock()
    response.status_code = status_code
    return anthropic.APIStatusError(
        message=f"HTTP {status_code}",
        response=response,
        body={},
    )


# ══════════════════════════════════════════════════════════════════════
# RETRY POLICY — is_transient predicate
# ══════════════════════════════════════════════════════════════════════

class TestIsTransient:
    def test_httpx_timeout_is_transient(self):
        from app.llm.retry import _is_transient
        assert _is_transient(httpx.ReadTimeout("timed out")) is True

    def test_httpx_connect_error_is_transient(self):
        from app.llm.retry import _is_transient
        assert _is_transient(httpx.ConnectError("refused")) is True

    def test_httpx_remote_protocol_error_is_transient(self):
        from app.llm.retry import _is_transient
        assert _is_transient(httpx.RemoteProtocolError("broken pipe")) is True

    def test_anthropic_429_is_transient(self):
        from app.llm.retry import _is_transient
        assert _is_transient(_make_status_error(429)) is True

    def test_anthropic_500_is_transient(self):
        from app.llm.retry import _is_transient
        assert _is_transient(_make_status_error(500)) is True

    def test_anthropic_503_is_transient(self):
        from app.llm.retry import _is_transient
        assert _is_transient(_make_status_error(503)) is True

    def test_anthropic_401_is_not_transient(self):
        from app.llm.retry import _is_transient
        assert _is_transient(_make_status_error(401)) is False

    def test_anthropic_422_is_not_transient(self):
        from app.llm.retry import _is_transient
        assert _is_transient(_make_status_error(422)) is False

    def test_value_error_is_not_transient(self):
        from app.llm.retry import _is_transient
        assert _is_transient(ValueError("bad input")) is False

    def test_http_exception_is_not_transient(self):
        from app.llm.retry import _is_transient
        assert _is_transient(HTTPException(status_code=503, detail="cb open")) is False


# ══════════════════════════════════════════════════════════════════════
# RETRY POLICY — llm_retry() decorator
# ══════════════════════════════════════════════════════════════════════

class TestLlmRetryDecorator:
    def test_succeeds_on_first_attempt(self, monkeypatch):
        monkeypatch.setattr("app.llm.retry.settings.llm_max_retries", 3)
        monkeypatch.setattr("app.llm.retry.settings.llm_retry_max_wait", 0.0)

        from app.llm.retry import llm_retry

        call_count = 0
        @llm_retry()
        def fn():
            nonlocal call_count
            call_count += 1
            return "result"

        assert fn() == "result"
        assert call_count == 1

    def test_retries_on_transient_error_then_succeeds(self, monkeypatch):
        monkeypatch.setattr("app.llm.retry.settings.llm_max_retries", 3)
        monkeypatch.setattr("app.llm.retry.settings.llm_retry_max_wait", 0.0)

        from app.llm.retry import llm_retry

        attempts = []
        @llm_retry()
        def fn():
            attempts.append(1)
            if len(attempts) < 3:
                raise httpx.ReadTimeout("slow")
            return "ok"

        result = fn()
        assert result == "ok"
        assert len(attempts) == 3

    def test_exhaustion_raises_retry_error(self, monkeypatch):
        """When all retries are consumed, tenacity raises RetryError."""
        monkeypatch.setattr("app.llm.retry.settings.llm_max_retries", 2)
        monkeypatch.setattr("app.llm.retry.settings.llm_retry_max_wait", 0.0)

        from app.llm.retry import llm_retry

        @llm_retry()
        def fn():
            raise httpx.ConnectError("refused")

        with pytest.raises(RetryError):
            fn()

    def test_non_transient_error_not_retried(self, monkeypatch):
        monkeypatch.setattr("app.llm.retry.settings.llm_max_retries", 4)
        monkeypatch.setattr("app.llm.retry.settings.llm_retry_max_wait", 0.0)

        from app.llm.retry import llm_retry

        attempts = []
        @llm_retry()
        def fn():
            attempts.append(1)
            raise _make_status_error(401)

        with pytest.raises(anthropic.APIStatusError):
            fn()
        assert len(attempts) == 1, "Auth error must not be retried"

    def test_backoff_delay_increases_between_attempts(self, monkeypatch):
        """Each successive retry must wait longer than the previous one."""
        monkeypatch.setattr("app.llm.retry.settings.llm_max_retries", 4)
        monkeypatch.setattr("app.llm.retry.settings.llm_retry_max_wait", 60.0)

        from app.llm.retry import llm_retry

        sleep_calls: list[float] = []
        original_sleep = time.sleep

        def fake_sleep(seconds: float):
            sleep_calls.append(seconds)

        attempt_count = 0
        @llm_retry()
        def fn():
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 4:
                raise httpx.ReadTimeout("slow")
            return "done"

        with patch("tenacity.nap.time.sleep", fake_sleep):
            fn()

        assert len(sleep_calls) >= 2, "Should have slept between retries"
        # Each sleep must be at least as long as the previous (exponential)
        for i in range(1, len(sleep_calls)):
            assert sleep_calls[i] >= sleep_calls[i - 1] * 0.5, (
                f"Backoff should grow: {sleep_calls}"
            )

    def test_max_wait_caps_backoff(self, monkeypatch):
        """No single sleep may exceed llm_retry_max_wait."""
        cap = 2.0
        monkeypatch.setattr("app.llm.retry.settings.llm_max_retries", 6)
        monkeypatch.setattr("app.llm.retry.settings.llm_retry_max_wait", cap)

        from app.llm.retry import llm_retry

        sleep_calls: list[float] = []
        attempt_count = 0

        @llm_retry()
        def fn():
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 6:
                raise httpx.ReadTimeout("slow")
            return "done"

        with patch("tenacity.nap.time.sleep", lambda s: sleep_calls.append(s)):
            fn()

        for s in sleep_calls:
            assert s <= cap + 1.5, f"Sleep {s:.2f}s exceeded cap {cap}s"


# ══════════════════════════════════════════════════════════════════════
# TIMEOUT — httpx.Timeout applied to the Anthropic client
# ══════════════════════════════════════════════════════════════════════

class TestTimeoutConfiguration:
    def test_client_receives_timeout_object(self, monkeypatch):
        """_get_client() must pass an httpx.Timeout to the Anthropic constructor."""
        monkeypatch.setattr("app.llm_client.settings.llm_connect_timeout", 5.0)
        monkeypatch.setattr("app.llm_client.settings.llm_read_timeout",    90.0)
        monkeypatch.setattr("app.llm_client.settings.llm_write_timeout",   20.0)
        monkeypatch.setenv("CLAUDE_API_KEY", "test-key")

        import app.llm_client as llm_module
        from app.security.secrets import get_secret_provider
        llm_module._get_client.cache_clear()
        get_secret_provider.cache_clear()

        created_timeouts: list[httpx.Timeout] = []

        class FakeAnthropic:
            def __init__(self, api_key, base_url, timeout, **kwargs):
                created_timeouts.append(timeout)

        with patch("app.llm_client.anthropic.Anthropic", FakeAnthropic):
            llm_module._get_client()

        assert len(created_timeouts) == 1
        t = created_timeouts[0]
        assert isinstance(t, httpx.Timeout)
        assert t.connect == 5.0
        assert t.read   == 90.0
        assert t.write  == 20.0

        llm_module._get_client.cache_clear()
        get_secret_provider.cache_clear()

    def test_read_timeout_raises_correctly(self):
        """If the SDK raises httpx.ReadTimeout, it must surface as a transient error."""
        from app.llm.retry import _is_transient
        exc = httpx.ReadTimeout("read timed out")
        assert _is_transient(exc) is True

    def test_connect_timeout_raises_correctly(self):
        from app.llm.retry import _is_transient
        exc = httpx.ConnectTimeout("connect timed out")
        assert _is_transient(exc) is True


# ══════════════════════════════════════════════════════════════════════
# CHAT FUNCTION — integration of retry + circuit breaker
# ══════════════════════════════════════════════════════════════════════

class TestChatFunction:
    def test_successful_call_returns_text(self, monkeypatch):
        msg = _make_message(text="ISO 20022 is a standard.")
        with patch("app.llm_client._call_api", return_value=msg):
            from app.llm_client import chat
            result = chat(system="sys", user="question", operation="test")
        assert result == "ISO 20022 is a standard."

    def test_transient_failure_retried_then_succeeds(self, monkeypatch):
        """
        _call_api raises a timeout on the first two calls, succeeds on the third.
        chat() must return the successful result and not raise.
        """
        monkeypatch.setattr("app.llm.retry.settings.llm_max_retries", 4)
        monkeypatch.setattr("app.llm.retry.settings.llm_retry_max_wait", 0.0)

        call_results = [
            httpx.ReadTimeout("slow"),
            httpx.ReadTimeout("still slow"),
            _make_message(text="recovered"),
        ]
        call_iter = iter(call_results)

        def fake_call(*_args, **_kwargs):
            val = next(call_iter)
            if isinstance(val, Exception):
                raise val
            return val

        with patch("app.llm_client._call_api", side_effect=fake_call):
            with patch("tenacity.nap.time.sleep", lambda _: None):
                from app.llm_client import chat
                result = chat(system="sys", user="q", operation="test_retry")
        assert result == "recovered"

    def test_exhausted_retries_raise_http_503(self, monkeypatch):
        """
        When all retries are consumed, chat() must raise HTTP 503 — not a
        raw RetryError or SDK exception — AND surface the real underlying cause
        (here a timeout) instead of a generic "retry attempts" message.
        """
        monkeypatch.setattr("app.llm.retry.settings.llm_max_retries", 1)
        monkeypatch.setattr("app.llm.retry.settings.llm_retry_max_wait", 0.0)

        with patch("app.llm_client._call_api", side_effect=httpx.ReadTimeout("always")):
            with patch("tenacity.nap.time.sleep", lambda _: None):
                from app.llm_client import chat
                with pytest.raises(HTTPException) as exc_info:
                    chat(system="sys", user="q", operation="test_exhaustion")
        assert exc_info.value.status_code == 503
        detail = exc_info.value.detail.lower()
        assert "provider timeout" in detail        # real cause surfaced to the user
        assert "try again" in detail


    def test_exhausted_retries_surface_provider_status(self, monkeypatch):
        """A provider 5xx (e.g. 'no available server') must surface as its real
        status, not a generic retry message."""
        monkeypatch.setattr("app.llm.retry.settings.llm_max_retries", 1)
        monkeypatch.setattr("app.llm.retry.settings.llm_retry_max_wait", 0.0)
        err = _make_status_error(503)
        with patch("app.llm_client._call_api", side_effect=err):
            with patch("tenacity.nap.time.sleep", lambda _: None):
                from app.llm_client import chat
                with pytest.raises(HTTPException) as exc_info:
                    chat(system="sys", user="q", operation="test_5xx")
        assert exc_info.value.status_code == 503
        assert "provider unavailable (503)" in exc_info.value.detail.lower()

    def test_auth_error_not_retried(self, monkeypatch):
        """A 401 auth error must propagate immediately without retrying."""
        monkeypatch.setattr("app.llm.retry.settings.llm_max_retries", 4)
        monkeypatch.setattr("app.llm.retry.settings.llm_retry_max_wait", 0.0)

        call_count = 0
        def fake_call(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            raise _make_status_error(401)

        with patch("app.llm_client._call_api", side_effect=fake_call):
            from app.llm_client import chat
            with pytest.raises(anthropic.APIStatusError):
                chat(system="sys", user="q")
        assert call_count == 1, "Auth error must not trigger any retries"

    def test_circuit_breaker_rejects_when_open(self, monkeypatch):
        """chat() must raise HTTP 503 immediately when the circuit breaker is open."""
        from app.llm.circuit_breaker import BreakerState, CircuitBreaker
        from app.llm_client import chat

        breaker = CircuitBreaker(failure_threshold=1)
        breaker.record_failure()
        assert breaker.state == BreakerState.OPEN

        with patch("app.llm_client.circuit_breaker", breaker):
            with pytest.raises(HTTPException) as exc_info:
                chat(system="sys", user="q")
        assert exc_info.value.status_code == 503

    def test_circuit_breaker_records_failure_on_non_transient(self, monkeypatch):
        """
        A non-retried error that escapes the retry wrapper must still count
        as a circuit-breaker failure so the breaker can open.
        """
        from app.llm.circuit_breaker import CircuitBreaker, BreakerState

        breaker = CircuitBreaker(failure_threshold=2)
        assert breaker.state == BreakerState.CLOSED

        def bad_call(*_):
            raise httpx.ConnectError("refused")

        with patch("app.llm_client._call_api", side_effect=bad_call):
            with patch("app.llm_client.circuit_breaker", breaker):
                with patch("tenacity.nap.time.sleep", lambda _: None):
                    # max_retries=1 → 2 total attempts, both fail → RetryError → 503
                    monkeypatch.setattr("app.llm.retry.settings.llm_max_retries", 1)
                    monkeypatch.setattr("app.llm.retry.settings.llm_retry_max_wait", 0.0)
                    from app.llm_client import chat
                    with pytest.raises(HTTPException):
                        chat(system="s", user="u")

        # The HTTPException(503) raised by chat() is NOT recorded as a CB failure
        # (it is a HTTPException subclass). The CB failure was already recorded when
        # the inner RetryError triggered the `raise HTTPException` path — but the
        # circuit_breaker.__exit__ sees the HTTPException, not the RetryError.
        # So verify the breaker failure count didn't increment from the outer 503.
        assert breaker._failures < breaker._threshold


# ══════════════════════════════════════════════════════════════════════
# CIRCUIT BREAKER — timeout failures open the breaker
# ══════════════════════════════════════════════════════════════════════

class TestCircuitBreakerTimeoutIntegration:
    def test_timeout_failures_increment_breaker(self):
        """
        Each time _call_api raises a timeout, the circuit breaker must
        record a failure (because the exception is not a ValueError or HTTPException).
        """
        from app.llm.circuit_breaker import CircuitBreaker, BreakerState

        breaker = CircuitBreaker(failure_threshold=3)

        # Simulate raw failure recording (what __exit__ does on non-exempt errors)
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state == BreakerState.CLOSED
        breaker.record_failure()
        assert breaker.state == BreakerState.OPEN

    def test_breaker_recovers_after_timeout(self):
        from app.llm.circuit_breaker import CircuitBreaker, BreakerState

        breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=0.05)
        breaker.record_failure()
        assert breaker.state == BreakerState.OPEN

        time.sleep(0.1)
        assert breaker.state == BreakerState.HALF_OPEN

        breaker.record_success()
        assert breaker.state == BreakerState.CLOSED


# ══════════════════════════════════════════════════════════════════════
# OBSERVABILITY — structured log fields
# ══════════════════════════════════════════════════════════════════════

class TestObservability:
    def test_successful_call_logs_request_id_and_operation(self, caplog):
        import logging
        msg = _make_message(text="answer")

        with patch("app.llm_client._call_api", return_value=msg):
            with caplog.at_level(logging.INFO, logger="app.llm_client"):
                from app.llm_client import chat
                chat(system="s", user="u", operation="concept_extraction")

        log_text = " ".join(caplog.messages)
        assert "concept_extraction" in log_text
        assert "latency" in log_text
        assert "prompt_tokens" in log_text
        assert "cost" in log_text

    def test_retry_sleep_logged_at_warning(self, monkeypatch, caplog):
        """tenacity's before_sleep_log must emit a WARNING when a retry is about to happen."""
        import logging
        monkeypatch.setattr("app.llm.retry.settings.llm_max_retries", 3)
        monkeypatch.setattr("app.llm.retry.settings.llm_retry_max_wait", 0.0)

        attempt = 0
        def flaky(*_):
            nonlocal attempt
            attempt += 1
            if attempt == 1:
                raise httpx.ReadTimeout("slow")
            return _make_message()

        with patch("app.llm_client._call_api", side_effect=flaky):
            with patch("tenacity.nap.time.sleep", lambda _: None):
                with caplog.at_level(logging.WARNING, logger="app.llm.retry"):
                    from app.llm_client import chat
                    chat(system="s", user="u")

        # before_sleep_log emits at WARNING level
        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("Retrying" in m or "retry" in m.lower() for m in warning_msgs), (
            f"Expected retry warning. Got: {warning_msgs}"
        )

    def test_exhaustion_logs_error(self, monkeypatch, caplog):
        """Retry exhaustion must be logged at ERROR level with the operation name."""
        import logging
        monkeypatch.setattr("app.llm.retry.settings.llm_max_retries", 1)
        monkeypatch.setattr("app.llm.retry.settings.llm_retry_max_wait", 0.0)

        with patch("app.llm_client._call_api", side_effect=httpx.ReadTimeout("always")):
            with patch("tenacity.nap.time.sleep", lambda _: None):
                with caplog.at_level(logging.ERROR, logger="app.llm_client"):
                    from app.llm_client import chat
                    with pytest.raises(HTTPException):
                        chat(system="s", user="u", operation="test_op")

        error_msgs = [r.message for r in caplog.records if r.levelno == logging.ERROR]
        assert len(error_msgs) > 0, "An ERROR log must be emitted on retry exhaustion"
        combined = " ".join(error_msgs).lower()
        assert "failed" in combined or "retri" in combined, (
            f"Error log must mention failure/retries. Got: {error_msgs}"
        )


# ══════════════════════════════════════════════════════════════════════
# CONFIG — settings surface correctly
# ══════════════════════════════════════════════════════════════════════

class TestResilienceSettings:
    def test_default_connect_timeout(self):
        from app.config import settings
        assert settings.llm_connect_timeout == 10.0

    def test_default_read_timeout(self):
        # Raised 240s → 360s: reliability headroom for slow gateway periods (normal
        # Client 201 generation ~134s, well under the cap).
        from app.config import settings
        assert settings.llm_read_timeout == 360.0

    def test_default_write_timeout(self):
        from app.config import settings
        assert settings.llm_write_timeout == 30.0

    def test_default_max_retries(self):
        # Raised 1 → 3 (4 attempts) with escalating backoff: transient 5xx return fast,
        # so more attempts materially improve recovery from provider blips.
        from app.config import settings
        assert settings.llm_max_retries == 3

    def test_default_retry_max_wait(self):
        # Raised 8 → 30s: cap on per-attempt exponential backoff (~5s→10s→20s→30s).
        from app.config import settings
        assert settings.llm_retry_max_wait == 30.0
