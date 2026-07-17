"""
LLM governance layer — budget enforcement and workspace-level rate limiting.

Budget enforcement
------------------
Before every LLM call, check_budget() queries the workspace's LLMBudget row.
  - If monthly_token_limit > 0 and current_token_usage >= limit → HTTP 429
  - If monthly_cost_limit  > 0 and current_cost_usage  >= limit → HTTP 429
  - Limits of 0 mean unlimited (default for new workspaces).

Rate limiting
-------------
A simple in-process sliding-window counter per workspace.
  - Window: 60 seconds (configurable via LLM_RATE_WINDOW_SECONDS)
  - Limit:  MAX_LLM_REQUESTS_PER_MINUTE per workspace (default 60)
  - Returns HTTP 429 when the window is full.

Note: this in-process rate limiter is suitable for a single-process deployment.
For a multi-process/multi-node deployment replace with a Redis-backed counter.
"""
from __future__ import annotations

import collections
import logging
import threading
import time

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import LLMBudget

logger = logging.getLogger(__name__)




def check_budget(db: Session, workspace_id: int) -> None:
    """
    Raise HTTP 429 if the workspace has exceeded its monthly token or cost budget.
    No-op if no budget row exists (unlimited by default).
    """
    budget = (
        db.query(LLMBudget)
        .filter(LLMBudget.workspace_id == workspace_id)
        .first()
    )
    if budget is None:
        return

    if budget.monthly_token_limit > 0:
        if budget.current_token_usage >= budget.monthly_token_limit:
            logger.warning(
                "[ws=%d] Token budget exceeded: %d / %d",
                workspace_id, budget.current_token_usage, budget.monthly_token_limit,
            )
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Workspace LLM token budget exceeded "
                    f"({budget.current_token_usage:,} / {budget.monthly_token_limit:,} tokens used this month)."
                ),
            )

    if budget.monthly_cost_limit > 0.0:
        if budget.current_cost_usage >= budget.monthly_cost_limit:
            logger.warning(
                "[ws=%d] Cost budget exceeded: $%.4f / $%.4f",
                workspace_id, budget.current_cost_usage, budget.monthly_cost_limit,
            )
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Workspace LLM cost budget exceeded "
                    f"(${budget.current_cost_usage:.4f} / ${budget.monthly_cost_limit:.4f} spent this month)."
                ),
            )




_rate_lock   = threading.Lock()
# workspace_id → deque of request timestamps (float, monotonic)
_rate_windows: dict[int, collections.deque] = collections.defaultdict(
    lambda: collections.deque()
)


def check_rate_limit(workspace_id: int) -> None:
    """
    Raise HTTP 429 if the workspace has made more than
    MAX_LLM_REQUESTS_PER_MINUTE LLM calls in the last LLM_RATE_WINDOW_SECONDS.
    """
    limit  = settings.max_llm_requests_per_minute
    window = settings.llm_rate_window_seconds
    now    = time.monotonic()
    cutoff = now - window

    with _rate_lock:
        dq = _rate_windows[workspace_id]
        # Remove timestamps outside the current window
        while dq and dq[0] < cutoff:
            dq.popleft()

        if len(dq) >= limit:
            logger.warning(
                "[ws=%d] LLM rate limit hit: %d requests in %.0fs window.",
                workspace_id, len(dq), window,
            )
            raise HTTPException(
                status_code=429,
                detail=(
                    f"LLM request rate limit exceeded "
                    f"({limit} requests per {window:.0f}s). Please slow down."
                ),
            )

        dq.append(now)
