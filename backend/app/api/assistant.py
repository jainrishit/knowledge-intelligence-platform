"""Assistant / chat API router."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.db.models import Workspace, ChatMessage
from app.retrieval.qa_service import ask_workspace
from app.schemas import AskRequest, AskResponse, ChatMessageOut

router = APIRouter(tags=["assistant"])


@router.post("/workspaces/{workspace_id}/ask", response_model=AskResponse)
def ask(workspace_id: int, body: AskRequest, db: Session = Depends(get_db)):
    ws = db.get(Workspace, workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    return ask_workspace(db, workspace_id, body.question)


@router.get("/workspaces/{workspace_id}/chat-history", response_model=list[ChatMessageOut])
def get_chat_history(workspace_id: int, db: Session = Depends(get_db)):
    ws = db.get(Workspace, workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.workspace_id == workspace_id)
        .order_by(ChatMessage.created_at.asc())
        .all()
    )
    return [ChatMessageOut.model_validate(m) for m in messages]
