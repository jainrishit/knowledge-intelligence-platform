"""Deliverables API router — generate, list, detail, update, export."""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.db.models import Deliverable, Workspace
from app.generation.deliverable_service import generate_deliverable, export_as_docx
from app.schemas import DeliverableCreate, DeliverableUpdate, DeliverableOut, DeliverableResponse

router = APIRouter(tags=["deliverables"])


@router.post("/workspaces/{workspace_id}/deliverables", response_model=DeliverableResponse, status_code=201)
def create_deliverable(workspace_id: int, body: DeliverableCreate, db: Session = Depends(get_db)):
    ws = db.get(Workspace, workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    try:
        return generate_deliverable(db, workspace_id, body.type, body.topic, body.audience)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/workspaces/{workspace_id}/deliverables", response_model=list[DeliverableOut])
def list_deliverables(workspace_id: int, db: Session = Depends(get_db)):
    ws = db.get(Workspace, workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    deliverables = db.query(Deliverable).filter(Deliverable.workspace_id == workspace_id).all()
    return [DeliverableOut.model_validate(d) for d in deliverables]


@router.get("/deliverables/{deliverable_id}", response_model=DeliverableOut)
def get_deliverable(deliverable_id: int, db: Session = Depends(get_db)):
    d = db.get(Deliverable, deliverable_id)
    if not d:
        raise HTTPException(status_code=404, detail="Deliverable not found.")
    return DeliverableOut.model_validate(d)


@router.put("/deliverables/{deliverable_id}", response_model=DeliverableOut)
def update_deliverable(deliverable_id: int, body: DeliverableUpdate, db: Session = Depends(get_db)):
    d = db.get(Deliverable, deliverable_id)
    if not d:
        raise HTTPException(status_code=404, detail="Deliverable not found.")
    d.content_markdown = body.content_markdown
    db.commit()
    db.refresh(d)
    return DeliverableOut.model_validate(d)


@router.get("/deliverables/{deliverable_id}/export")
def export_deliverable(deliverable_id: int, format: str = "md", db: Session = Depends(get_db)):
    d = db.get(Deliverable, deliverable_id)
    if not d:
        raise HTTPException(status_code=404, detail="Deliverable not found.")
    content = d.content_markdown or ""
    if format == "md":
        return Response(
            content=content,
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="{d.title}.md"'},
        )
    elif format == "docx":
        docx_bytes = export_as_docx(content, d.title)
        return Response(
            content=docx_bytes,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f'attachment; filename="{d.title}.docx"'},
        )
    else:
        raise HTTPException(status_code=422, detail="Invalid format. Use 'md' or 'docx'.")
