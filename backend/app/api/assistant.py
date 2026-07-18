"""Assistant / chat API router."""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.db.models import Workspace, ChatMessage
from app.retrieval.qa_service import ask_workspace
from app.schemas import AskRequest, AskResponse, ChatMessageOut

logger = logging.getLogger(__name__)

router = APIRouter(tags=["assistant"])


@router.post("/workspaces/{workspace_id}/ask", response_model=AskResponse)
def ask(workspace_id: int, body: AskRequest, db: Session = Depends(get_db)):
    ws = db.get(Workspace, workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    try:
        return ask_workspace(db, workspace_id, body.question)
    except HTTPException:
        # Propagate 503 (circuit breaker / retry exhaustion) as-is
        raise
    except Exception as exc:
        logger.error(
            "[assistant ws=%d] ask_workspace failed: %s: %s",
            workspace_id, type(exc).__name__, exc,
        )
        # Return a graceful degradation response instead of crashing
        answer = (
            "The assistant encountered an error while processing your question. "
            "Please try again. If the problem persists, verify that the workspace "
            "has completed documents."
        )
        return AskResponse(answer=answer, sources=[])


@router.get("/workspaces/{workspace_id}/chat-history", response_model=list[ChatMessageOut])
def get_chat_history(workspace_id: int, db: Session = Depends(get_db)):
    ws = db.get(Workspace, workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    try:
        messages = (
            db.query(ChatMessage)
            .filter(ChatMessage.workspace_id == workspace_id)
            .order_by(ChatMessage.created_at.asc())
            .all()
        )
        return [ChatMessageOut.model_validate(m) for m in messages]
    except Exception as exc:
        logger.error(
            "[assistant ws=%d] chat_history failed: %s: %s",
            workspace_id, type(exc).__name__, exc,
        )
        return []
