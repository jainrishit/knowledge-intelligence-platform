"""
LLM usage tracker — persists one row per API call to the llm_usage table.

Token counts come from the Anthropic response object's usage field.
Cost estimates use configurable per-token prices; updating prices requires
only a config change, not a code change.

Override pricing defaults via environment variables:
  LLM_COST_PER_1K_INPUT_TOKENS
  LLM_COST_PER_1K_OUTPUT_TOKENS
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import LLMBudget, LLMUsage

logger = logging.getLogger(__name__)

_INPUT_COST_PER_1K  = settings.llm_cost_per_1k_input_tokens
_OUTPUT_COST_PER_1K = settings.llm_cost_per_1k_output_tokens


def estimate_cost(prompt_tokens: int, completion_tokens: int) -> float:
    """Return estimated USD cost for a single API call."""
    return (
        prompt_tokens  / 1000.0 * _INPUT_COST_PER_1K +
        completion_tokens / 1000.0 * _OUTPUT_COST_PER_1K
    )


def record_usage(
    db: Session,
    workspace_id: int | None,
    operation_type: str,
    model_name: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> LLMUsage:
    """
    Persist one LLMUsage row and update the workspace's running budget totals.
    Re-raises on failure after logging — callers decide whether to absorb the error.
    """
    cost = estimate_cost(prompt_tokens, completion_tokens)

    try:
        usage = LLMUsage(
            workspace_id=workspace_id,
            operation_type=operation_type,
            model_name=model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            estimated_cost_usd=cost,
            request_timestamp=datetime.now(timezone.utc),
        )
        db.add(usage)

        if workspace_id is not None:
            _update_budget(db, workspace_id, prompt_tokens + completion_tokens, cost)

        db.commit()

        logger.info(
            "[ws=%s] LLM call recorded: op=%s model=%s tokens=%d+%d cost=$%.6f",
            workspace_id, operation_type, model_name,
            prompt_tokens, completion_tokens, cost,
        )
        return usage

    except Exception as exc:
        logger.error("Failed to record LLM usage: %s", exc)
        db.rollback()
        raise


def _update_budget(db: Session, workspace_id: int, total_tokens: int, cost: float) -> None:
    """Create or update the LLMBudget row for this workspace."""
    budget = (
        db.query(LLMBudget)
        .filter(LLMBudget.workspace_id == workspace_id)
        .first()
    )
    if budget is None:
        budget = LLMBudget(workspace_id=workspace_id)
        db.add(budget)

    budget.current_token_usage = (budget.current_token_usage or 0) + total_tokens
    budget.current_cost_usage  = (budget.current_cost_usage  or 0.0) + cost
    budget.updated_at = datetime.now(timezone.utc)
