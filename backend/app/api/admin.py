"""Admin API router — LLM usage, budget visibility, and spreadsheet ingestion metrics."""
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.db.session import get_db
from app.db.models import LLMUsage, LLMBudget, SpreadsheetIngestionRun
from app.llm.circuit_breaker import circuit_breaker

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/llm/usage")
def get_llm_usage(db: Session = Depends(get_db)) -> dict[str, Any]:
    """
    Return per-workspace LLM usage aggregated by operation type.
    Includes total tokens consumed and estimated spend.
    """
    rows = (
        db.query(
            LLMUsage.workspace_id,
            LLMUsage.operation_type,
            func.sum(LLMUsage.prompt_tokens).label("prompt_tokens"),
            func.sum(LLMUsage.completion_tokens).label("completion_tokens"),
            func.sum(LLMUsage.estimated_cost_usd).label("estimated_cost_usd"),
            func.count(LLMUsage.id).label("call_count"),
        )
        .group_by(LLMUsage.workspace_id, LLMUsage.operation_type)
        .all()
    )

    by_workspace: dict[str, Any] = {}
    for r in rows:
        ws_key = str(r.workspace_id or "global")
        by_workspace.setdefault(ws_key, {
            "workspace_id": r.workspace_id,
            "total_tokens": 0,
            "total_cost_usd": 0.0,
            "operations": [],
        })
        entry = by_workspace[ws_key]
        total = (r.prompt_tokens or 0) + (r.completion_tokens or 0)
        entry["total_tokens"]   += total
        entry["total_cost_usd"] += r.estimated_cost_usd or 0.0
        entry["operations"].append({
            "operation_type":    r.operation_type,
            "call_count":        r.call_count,
            "prompt_tokens":     r.prompt_tokens,
            "completion_tokens": r.completion_tokens,
            "estimated_cost_usd": round(r.estimated_cost_usd or 0.0, 6),
        })

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workspaces": list(by_workspace.values()),
    }


@router.get("/llm/budgets")
def get_llm_budgets(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Return all workspace budgets with remaining allowance and circuit-breaker state."""
    budgets = db.query(LLMBudget).all()

    result = []
    for b in budgets:
        token_remaining = (
            max(0, b.monthly_token_limit - b.current_token_usage)
            if b.monthly_token_limit > 0 else None
        )
        cost_remaining = (
            round(max(0.0, b.monthly_cost_limit - b.current_cost_usage), 6)
            if b.monthly_cost_limit > 0.0 else None
        )
        result.append({
            "workspace_id":         b.workspace_id,
            "monthly_token_limit":  b.monthly_token_limit or "unlimited",
            "monthly_cost_limit":   b.monthly_cost_limit or "unlimited",
            "current_token_usage":  b.current_token_usage,
            "current_cost_usage":   round(b.current_cost_usage, 6),
            "token_remaining":      token_remaining,
            "cost_remaining_usd":   cost_remaining,
            "budget_period_start":  b.budget_period_start.isoformat(),
            "updated_at":           b.updated_at.isoformat(),
        })

    return {
        "generated_at":     datetime.now(timezone.utc).isoformat(),
        "circuit_breaker":  circuit_breaker.state.value,
        "budgets":          result,
    }


@router.get("/spreadsheet/ingestion-runs")
def get_spreadsheet_ingestion_runs(
    workspace_id: Optional[int] = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return recent spreadsheet ingestion run metrics, optionally filtered by workspace."""
    query = db.query(SpreadsheetIngestionRun)
    if workspace_id is not None:
        query = query.filter(SpreadsheetIngestionRun.workspace_id == workspace_id)
    runs = query.order_by(SpreadsheetIngestionRun.started_at.desc()).limit(100).all()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_runs": len(runs),
        "runs": [
            {
                "id":                    r.id,
                "workspace_id":          r.workspace_id,
                "document_id":           r.document_id,
                "file_type":             r.file_type,
                "started_at":            r.started_at.isoformat(),
                "completed_at":          r.completed_at.isoformat() if r.completed_at else None,
                "sheets_processed":      r.sheets_processed,
                "tables_detected":       r.tables_detected,
                "concepts_extracted":    r.concepts_extracted,
                "relationships_extracted": r.relationships_extracted,
                "status":                r.status,
            }
            for r in runs
        ],
    }
