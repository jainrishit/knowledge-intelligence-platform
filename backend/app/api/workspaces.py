"""Workspaces API router."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.db.session import get_db
from app.db.models import Workspace, Document, Concept
from app.schemas import WorkspaceCreate, WorkspaceOut

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@router.post("", response_model=WorkspaceOut, status_code=201)
def create_workspace(body: WorkspaceCreate, db: Session = Depends(get_db)):
    ws = Workspace(name=body.name, description=body.description)
    db.add(ws)
    db.commit()
    db.refresh(ws)
    return _enrich(ws, db)


@router.get("", response_model=list[WorkspaceOut])
def list_workspaces(db: Session = Depends(get_db)):
    workspaces = db.query(Workspace).order_by(Workspace.created_at.desc()).all()
    return [_enrich(ws, db) for ws in workspaces]


@router.get("/{workspace_id}", response_model=WorkspaceOut)
def get_workspace(workspace_id: int, db: Session = Depends(get_db)):
    ws = db.get(Workspace, workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    return _enrich(ws, db)


@router.delete("/{workspace_id}", status_code=204)
def delete_workspace(workspace_id: int, db: Session = Depends(get_db)):
    ws = db.get(Workspace, workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    db.delete(ws)
    db.commit()


def _enrich(ws: Workspace, db: Session) -> WorkspaceOut:
    doc_count = db.query(func.count(Document.id)).filter(Document.workspace_id == ws.id).scalar() or 0
    concept_count = db.query(func.count(Concept.id)).filter(Concept.workspace_id == ws.id).scalar() or 0
    return WorkspaceOut(
        id=ws.id,
        name=ws.name,
        description=ws.description,
        created_at=ws.created_at,
        document_count=doc_count,
        concept_count=concept_count,
    )
