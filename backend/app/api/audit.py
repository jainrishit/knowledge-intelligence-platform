"""
Memory Audit API router.

Endpoints:
  GET  /workspaces/{id}/audit           — full workspace audit report
  GET  /workspaces/{id}/audit/score     — headline Memory Confidence Score only
  GET  /documents/{id}/audit            — single-document audit record
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.graph.audit_service import get_document_audit, get_workspace_audit

logger = logging.getLogger(__name__)

router = APIRouter(tags=["audit"])


def _dataclass_to_dict(obj: Any) -> Any:
    """Recursively convert dataclasses (and nested ones) to plain dicts."""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _dataclass_to_dict(v) for k, v in asdict(obj).items()}
    if isinstance(obj, list):
        return [_dataclass_to_dict(i) for i in obj]
    return obj


@router.get("/workspaces/{workspace_id}/audit")
def workspace_audit(workspace_id: int, db: Session = Depends(get_db)):
    """
    Full memory audit report for a workspace.

    Returns all sub-scores, per-document breakdowns, graph integrity
    summary, deliverable readiness flags, and the headline
    Memory Confidence Score (0–100).
    """
    try:
        report = get_workspace_audit(db, workspace_id)
    except Exception as exc:
        logger.error(
            "[audit ws=%d] get_workspace_audit failed: %s: %s",
            workspace_id, type(exc).__name__, exc,
        )
        raise HTTPException(
            status_code=500,
            detail="Failed to generate audit report. Please try again.",
        )

    if report is None:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    return _dataclass_to_dict(report)


@router.get("/workspaces/{workspace_id}/audit/score")
def workspace_audit_score(workspace_id: int, db: Session = Depends(get_db)):
    """
    Lightweight endpoint — returns only the headline scores and readiness flags.
    Suitable for polling from the workspace header or dashboard.
    """
    try:
        report = get_workspace_audit(db, workspace_id)
    except Exception as exc:
        logger.error(
            "[audit ws=%d] audit_score failed: %s: %s",
            workspace_id, type(exc).__name__, exc,
        )
        raise HTTPException(
            status_code=500,
            detail="Failed to compute audit scores. Please try again.",
        )

    if report is None:
        raise HTTPException(status_code=404, detail="Workspace not found.")

    return {
        "workspace_id": report.workspace_id,
        "graph_version": report.graph_version,
        "memory_confidence_score": report.memory_confidence_score,
        "ingestion_score": report.ingestion_score,
        "extraction_density_score": report.extraction_density_score,
        "relationship_density_score": report.relationship_density_score,
        "evidence_coverage_score": report.evidence_coverage_score,
        "graph_integrity_score": report.graph_integrity_score,
        "pattern_coverage_score": report.pattern_coverage_score,
        "consulting_readiness_score": report.consulting_readiness_score,
        "client_101_ready": report.client_101_ready,
        "client_201_ready": report.client_201_ready,
        "executive_summary_ready": report.executive_summary_ready,
        "total_documents": report.total_documents,
        "complete_documents": report.complete_documents,
        "failed_documents": report.failed_documents,
        "total_concepts": report.total_concepts,
        "total_relationships": report.total_relationships,
        "total_patterns": report.total_patterns,
        "evidence_coverage_pct": report.evidence_coverage_pct,
        "generated_at": report.generated_at.isoformat(),
    }


@router.get("/documents/{document_id}/audit")
def document_audit(document_id: int, db: Session = Depends(get_db)):
    """
    Audit record for a single document — parse metrics, extraction counts,
    evidence coverage, ingestion timing, and a per-document health score.
    """
    try:
        record = get_document_audit(db, document_id)
    except Exception as exc:
        logger.error(
            "[audit doc=%d] get_document_audit failed: %s: %s",
            document_id, type(exc).__name__, exc,
        )
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve document audit record. Please try again.",
        )

    if record is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    return _dataclass_to_dict(record)
