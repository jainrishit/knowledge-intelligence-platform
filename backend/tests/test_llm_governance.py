"""
LLM governance tests — secret provider abstraction, budget controls,
cost tracking, circuit breaker, and rate limiting.

All tests are self-contained with no LLM calls or real DB writes required
beyond what is explicitly set up in each test.
"""
from __future__ import annotations

import time
import threading

import pytest
from unittest.mock import MagicMock, patch
from sqlalchemy import create_engine, StaticPool
from sqlalchemy.orm import sessionmaker
from fastapi import HTTPException

from app.db.models import Base, LLMBudget, LLMUsage


# ── Shared DB factory ─────────────────────────────────────────────────

def _make_session(name: str):
    engine = create_engine(
        f"sqlite:///file:{name}?mode=memory&cache=shared&uri=true",
        connect_args={"check_same_thread": False, "uri": True},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    return engine, Session


def _teardown(engine):
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


# ══════════════════════════════════════════════════════════════════════
# SECRET MANAGEMENT
# ══════════════════════════════════════════════════════════════════════

def test_environment_secret_provider_returns_value(monkeypatch):
    """EnvironmentSecretProvider must return the value from the environment."""
    from app.security.secrets import EnvironmentSecretProvider
    monkeypatch.setenv("TEST_SECRET_XYZ", "super-secret-value")
    provider = EnvironmentSecretProvider()
    assert provider.get_secret("TEST_SECRET_XYZ") == "super-secret-value"


def test_environment_secret_provider_raises_for_missing(monkeypatch):
    """EnvironmentSecretProvider must raise KeyError for an unknown secret."""
    from app.security.secrets import EnvironmentSecretProvider
    monkeypatch.delenv("NONEXISTENT_SECRET_ABC", raising=False)
    provider = EnvironmentSecretProvider()
    with pytest.raises(KeyError, match="NONEXISTENT_SECRET_ABC"):
        provider.get_secret("NONEXISTENT_SECRET_ABC")


def test_secret_provider_is_abstract():
    """SecretProvider cannot be instantiated directly — it is an ABC."""
    from app.security.secrets import SecretProvider
    with pytest.raises(TypeError):
        SecretProvider()


def test_get_secret_provider_returns_singleton():
    """get_secret_provider() must return the same instance on every call."""
    from app.security.secrets import get_secret_provider
    get_secret_provider.cache_clear()
    a = get_secret_provider()
    b = get_secret_provider()
    assert a is b


def test_llm_client_uses_secret_provider(monkeypatch):
    """_get_client() must obtain the API key through SecretProvider, not settings."""
    from app.security.secrets import get_secret_provider

    monkeypatch.setenv("CLAUDE_API_KEY", "test-key-via-provider")
    get_secret_provider.cache_clear()

    import app.llm_client as llm_module
    llm_module._get_client.cache_clear()

    created_keys = []

    class FakeAnthropic:
        # Accept all kwargs so the test is forward-compatible with new client params
        def __init__(self, api_key, **kwargs):
            created_keys.append(api_key)

    with patch("app.llm_client.anthropic.Anthropic", FakeAnthropic):
        llm_module._get_client()

    assert created_keys == ["test-key-via-provider"]
    llm_module._get_client.cache_clear()
    get_secret_provider.cache_clear()


# ══════════════════════════════════════════════════════════════════════
# BUDGET ENFORCEMENT
# ══════════════════════════════════════════════════════════════════════

def test_budget_check_passes_when_no_budget_row():
    """check_budget() must be a no-op when no LLMBudget row exists."""
    from app.llm.governance import check_budget
    engine, Session = _make_session("budget_no_row")
    db = Session()
    try:
        check_budget(db, workspace_id=1)  # must not raise
    finally:
        db.close()
        _teardown(engine)


def test_budget_check_passes_within_token_limit():
    """check_budget() must not raise when token usage is below the limit."""
    from app.llm.governance import check_budget
    engine, Session = _make_session("budget_ok_tokens")
    db = Session()
    try:
        budget = LLMBudget(workspace_id=1, monthly_token_limit=1000, current_token_usage=500)
        db.add(budget)
        db.commit()
        check_budget(db, workspace_id=1)  # must not raise
    finally:
        db.close()
        _teardown(engine)


def test_budget_check_rejects_when_token_limit_exceeded():
    """check_budget() must raise HTTP 429 when token usage equals or exceeds the limit."""
    from app.llm.governance import check_budget
    engine, Session = _make_session("budget_exceed_tokens")
    db = Session()
    try:
        budget = LLMBudget(workspace_id=1, monthly_token_limit=1000, current_token_usage=1000)
        db.add(budget)
        db.commit()
        with pytest.raises(HTTPException) as exc_info:
            check_budget(db, workspace_id=1)
        assert exc_info.value.status_code == 429
        assert "token budget exceeded" in exc_info.value.detail.lower()
    finally:
        db.close()
        _teardown(engine)


def test_budget_check_rejects_when_cost_limit_exceeded():
    """check_budget() must raise HTTP 429 when cost usage equals or exceeds the limit."""
    from app.llm.governance import check_budget
    engine, Session = _make_session("budget_exceed_cost")
    db = Session()
    try:
        budget = LLMBudget(workspace_id=1, monthly_cost_limit=1.0, current_cost_usage=1.0)
        db.add(budget)
        db.commit()
        with pytest.raises(HTTPException) as exc_info:
            check_budget(db, workspace_id=1)
        assert exc_info.value.status_code == 429
        assert "cost budget exceeded" in exc_info.value.detail.lower()
    finally:
        db.close()
        _teardown(engine)


def test_unlimited_budget_never_blocks():
    """Limits of 0 mean unlimited — check_budget() must never block."""
    from app.llm.governance import check_budget
    engine, Session = _make_session("budget_unlimited")
    db = Session()
    try:
        budget = LLMBudget(
            workspace_id=1,
            monthly_token_limit=0,
            monthly_cost_limit=0.0,
            current_token_usage=9_999_999,
            current_cost_usage=9999.0,
        )
        db.add(budget)
        db.commit()
        check_budget(db, workspace_id=1)  # must not raise
    finally:
        db.close()
        _teardown(engine)


# ══════════════════════════════════════════════════════════════════════
# COST TRACKING
# ══════════════════════════════════════════════════════════════════════

def test_estimate_cost_calculation():
    """estimate_cost() must return correct USD amount based on config prices."""
    from app.llm.usage_tracker import estimate_cost
    from app.config import settings

    cost = estimate_cost(prompt_tokens=1000, completion_tokens=1000)
    expected = (
        1000 / 1000.0 * settings.llm_cost_per_1k_input_tokens +
        1000 / 1000.0 * settings.llm_cost_per_1k_output_tokens
    )
    assert abs(cost - expected) < 1e-9


def test_record_usage_persists_row():
    """record_usage() must create an LLMUsage row in the database."""
    from app.llm.usage_tracker import record_usage
    engine, Session = _make_session("usage_persist")
    db = Session()
    try:
        record_usage(
            db, workspace_id=1, operation_type="workspace_chat",
            model_name="claude-sonnet-4-5",
            prompt_tokens=500, completion_tokens=200,
        )
        rows = db.query(LLMUsage).all()
        assert len(rows) == 1
        assert rows[0].operation_type == "workspace_chat"
        assert rows[0].prompt_tokens == 500
        assert rows[0].completion_tokens == 200
        assert rows[0].estimated_cost_usd > 0
    finally:
        db.close()
        _teardown(engine)


def test_record_usage_creates_budget_row():
    """record_usage() must upsert an LLMBudget row and accumulate totals."""
    from app.llm.usage_tracker import record_usage
    engine, Session = _make_session("usage_budget_upsert")
    db = Session()
    try:
        record_usage(db, workspace_id=5, operation_type="concept_extraction",
                     model_name="claude-sonnet-4-5",
                     prompt_tokens=300, completion_tokens=100)
        record_usage(db, workspace_id=5, operation_type="concept_extraction",
                     model_name="claude-sonnet-4-5",
                     prompt_tokens=200, completion_tokens=50)

        db2 = Session()
        budget = db2.query(LLMBudget).filter(LLMBudget.workspace_id == 5).first()
        assert budget is not None
        assert budget.current_token_usage == 650  # 400 + 250
        db2.close()
    finally:
        db.close()
        _teardown(engine)


# ══════════════════════════════════════════════════════════════════════
# CIRCUIT BREAKER
# ══════════════════════════════════════════════════════════════════════

def _fresh_breaker(threshold: int = 3, timeout: float = 60.0):
    from app.llm.circuit_breaker import CircuitBreaker
    return CircuitBreaker(failure_threshold=threshold, recovery_timeout=timeout)


def test_breaker_starts_closed():
    from app.llm.circuit_breaker import BreakerState
    cb = _fresh_breaker()
    assert cb.state == BreakerState.CLOSED


def test_breaker_opens_after_threshold_failures():
    from app.llm.circuit_breaker import BreakerState
    cb = _fresh_breaker(threshold=3)
    for _ in range(3):
        cb.record_failure()
    assert cb.state == BreakerState.OPEN


def test_breaker_rejects_when_open():
    cb = _fresh_breaker(threshold=2)
    cb.record_failure()
    cb.record_failure()
    with pytest.raises(HTTPException) as exc_info:
        with cb:
            pass
    assert exc_info.value.status_code == 503


def test_breaker_resets_after_success():
    from app.llm.circuit_breaker import BreakerState
    cb = _fresh_breaker(threshold=3)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    assert cb.state == BreakerState.CLOSED
    assert cb._failures == 0


def test_breaker_transitions_to_half_open_after_timeout():
    from app.llm.circuit_breaker import BreakerState
    cb = _fresh_breaker(threshold=1, timeout=0.05)
    cb.record_failure()
    assert cb.state == BreakerState.OPEN
    time.sleep(0.1)
    assert cb.state == BreakerState.HALF_OPEN


def test_breaker_closes_after_successful_half_open_probe():
    from app.llm.circuit_breaker import BreakerState
    cb = _fresh_breaker(threshold=1, timeout=0.05)
    cb.record_failure()
    time.sleep(0.1)
    # State is now HALF_OPEN; a successful call should close it
    with cb:
        pass  # simulates a successful LLM call
    assert cb.state == BreakerState.CLOSED


def test_breaker_reopens_on_failed_half_open_probe():
    from app.llm.circuit_breaker import BreakerState
    cb = _fresh_breaker(threshold=1, timeout=0.05)
    cb.record_failure()
    time.sleep(0.1)
    assert cb.state == BreakerState.HALF_OPEN
    # Simulate a failure during the probe
    cb.record_failure()
    assert cb.state == BreakerState.OPEN


def test_breaker_is_thread_safe():
    """Multiple threads hitting record_failure() must not corrupt state."""
    cb = _fresh_breaker(threshold=100)
    errors = []

    def fail_once():
        try:
            cb.record_failure()
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=fail_once) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert cb._failures == 50


# ══════════════════════════════════════════════════════════════════════
# RATE LIMITING
# ══════════════════════════════════════════════════════════════════════

def test_rate_limit_allows_requests_under_threshold(monkeypatch):
    """Requests within the window limit must all pass."""
    from app.llm import governance
    # Isolate this test's rate window from other tests
    monkeypatch.setattr(governance.settings, "max_llm_requests_per_minute", 5)
    monkeypatch.setattr(governance.settings, "llm_rate_window_seconds", 60.0)
    # Use a unique workspace ID to avoid state leakage from other tests
    ws_id = 88001
    governance._rate_windows.pop(ws_id, None)
    for _ in range(5):
        governance.check_rate_limit(ws_id)  # must not raise
    governance._rate_windows.pop(ws_id, None)


def test_rate_limit_rejects_above_threshold(monkeypatch):
    """The (limit+1)th request in the window must raise HTTP 429."""
    from app.llm import governance
    monkeypatch.setattr(governance.settings, "max_llm_requests_per_minute", 3)
    monkeypatch.setattr(governance.settings, "llm_rate_window_seconds", 60.0)
    ws_id = 88002
    governance._rate_windows.pop(ws_id, None)
    for _ in range(3):
        governance.check_rate_limit(ws_id)
    with pytest.raises(HTTPException) as exc_info:
        governance.check_rate_limit(ws_id)
    assert exc_info.value.status_code == 429
    assert "rate limit" in exc_info.value.detail.lower()
    governance._rate_windows.pop(ws_id, None)


def test_rate_limit_resets_after_window_expires(monkeypatch):
    """Requests made after the window expires should be allowed again."""
    from app.llm import governance
    monkeypatch.setattr(governance.settings, "max_llm_requests_per_minute", 2)
    monkeypatch.setattr(governance.settings, "llm_rate_window_seconds", 0.1)
    ws_id = 88003
    governance._rate_windows.pop(ws_id, None)
    governance.check_rate_limit(ws_id)
    governance.check_rate_limit(ws_id)
    time.sleep(0.15)  # window expires
    governance.check_rate_limit(ws_id)  # must not raise — old entries expired
    governance._rate_windows.pop(ws_id, None)
