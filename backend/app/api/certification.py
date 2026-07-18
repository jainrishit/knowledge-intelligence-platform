"""
Brain Certification API router.

Endpoints
---------
POST  /workspaces/{id}/certification/run       — trigger a new benchmark run
GET   /workspaces/{id}/certification           — get the latest full report
GET   /workspaces/{id}/certification/status    — lightweight poll (status + scores)
GET   /workspaces/{id}/certification/gaps      — knowledge gap list from latest run
GET   /workspaces/{id}/certification/runs      — list all runs (history)
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.models import RetrievalBenchmarkRun, Workspace
from app.db.session import get_db
from app.graph.certification_service import (
    get_certification_status,
    get_knowledge_gaps,
    get_latest_certification,
    run_certification,
)

router = APIRouter(tags=["certification"])


def _dc(obj: Any) -> Any:
    """Recursively convert dataclasses → dicts, datetime → ISO strings."""
    from datetime import datetime
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _dc(v) for k, v in asdict(obj).items()}
    if isinstance(obj, list):
        return [_dc(i) for i in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


# ─────────────────────────────────────────────────────────────────────────────
# Trigger a new certification run
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/workspaces/{workspace_id}/certification/run", status_code=201)
def trigger_certification_run(
    workspace_id: int,
    db: Session = Depends(get_db),
):
    """
    Trigger a full Retrieval Reliability benchmark and Brain Certification run.

    Executes synchronously (typically 2–10 seconds for a medium workspace).
    Returns the complete certification report.
    """
    ws = db.get(Workspace, workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found.")

    try:
        report = run_certification(db, workspace_id)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return _dc(report)


# ─────────────────────────────────────────────────────────────────────────────
# Get latest full report
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/workspaces/{workspace_id}/certification")
def get_certification_report(workspace_id: int, db: Session = Depends(get_db)):
    """
    Return the most recent completed certification report.
    404 if no run has completed yet.
    """
    ws = db.get(Workspace, workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found.")

    report = get_latest_certification(db, workspace_id)
    if report is None:
        raise HTTPException(
            status_code=404,
            detail="No completed certification run found. POST to /certification/run first.",
        )
    return _dc(report)


# ─────────────────────────────────────────────────────────────────────────────
# Lightweight status poll
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/workspaces/{workspace_id}/certification/status")
def get_status(workspace_id: int, db: Session = Depends(get_db)):
    """
    Lightweight certification status — suitable for workspace header polling.
    Returns PENDING if no run has been made.
    """
    ws = db.get(Workspace, workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found.")

    status = get_certification_status(db, workspace_id)
    return _dc(status)


# ─────────────────────────────────────────────────────────────────────────────
# Knowledge gaps
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/workspaces/{workspace_id}/certification/gaps")
def get_gaps(workspace_id: int, db: Session = Depends(get_db)):
    """
    Return the knowledge gap list from the latest completed run.
    Returns [] if no run has completed.
    """
    ws = db.get(Workspace, workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found.")

    gaps = get_knowledge_gaps(db, workspace_id)
    return [_dc(g) for g in gaps]


# ─────────────────────────────────────────────────────────────────────────────
# Run history
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/workspaces/{workspace_id}/certification/runs")
def list_certification_runs(workspace_id: int, db: Session = Depends(get_db)):
    """
    Return a summary list of all certification runs for this workspace,
    newest first.
    """
    ws = db.get(Workspace, workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found.")

    runs = (
        db.query(RetrievalBenchmarkRun)
        .filter(RetrievalBenchmarkRun.workspace_id == workspace_id)
        .order_by(RetrievalBenchmarkRun.started_at.desc())
        .all()
    )
    return [
        {
            "run_id":               r.id,
            "status":               r.status,
            "certification_status": r.certification_status,
            "certification_score":  r.certification_score,
            "recall_score":         r.recall_score,
            "precision_score":      r.precision_score,
            "coverage_score":       r.coverage_score,
            "consistency_score":    r.consistency_score,
            "evidence_fidelity":    r.evidence_fidelity,
            "retrieval_accuracy":   r.retrieval_accuracy,
            "questions_generated":  r.questions_generated,
            "questions_answered":   r.questions_answered,
            "graph_version":        r.graph_version,
            "started_at":           r.started_at.isoformat() if r.started_at else None,
            "completed_at":         r.completed_at.isoformat() if r.completed_at else None,
        }
        for r in runs
    ]
