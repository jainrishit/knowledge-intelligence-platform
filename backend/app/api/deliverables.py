"""Deliverables API router — generate client materials as PowerPoint presentations."""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.db.models import Deliverable, Workspace
from app.generation.deliverable_service import generate_client_material
from app.schemas import DeliverableCreate, DeliverableOut, DeliverablePptxResponse

router = APIRouter(tags=["deliverables"])

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


@router.post("/workspaces/{workspace_id}/deliverables", status_code=201)
def create_deliverable(workspace_id: int, body: DeliverableCreate, db: Session = Depends(get_db)):
    """
    Generate a client material PowerPoint and return it as a file download.

    Supported types: client_101, client_201, executive_summary.
    focus_area is only used for executive_summary.
    """
    ws = db.get(Workspace, workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    try:
        result: DeliverablePptxResponse = generate_client_material(
            db=db,
            workspace_id=workspace_id,
            deliverable_type=body.type,
            focus_area=body.focus_area,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return Response(
        content=result.pptx_bytes,
        media_type=PPTX_MIME,
        status_code=201,
        headers={
            "Content-Disposition": f'attachment; filename="{result.filename}"',
            # Pass metadata back as custom headers for the frontend
            "X-Deliverable-Id": str(result.deliverable.id),
            "X-Deliverable-Title": result.deliverable.title,
            "X-Source-Count": str(len(result.sources)),
        },
    )


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


@router.get("/deliverables/{deliverable_id}/export")
def export_deliverable(deliverable_id: int, db: Session = Depends(get_db)):
    """
    Re-generate and return the PPTX for a saved deliverable record.
    Re-runs generation using the current workspace knowledge.
    """
    d = db.get(Deliverable, deliverable_id)
    if not d:
        raise HTTPException(status_code=404, detail="Deliverable not found.")

    try:
        result: DeliverablePptxResponse = generate_client_material(
            db=db,
            workspace_id=d.workspace_id,
            deliverable_type=d.type,
            focus_area=None,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return Response(
        content=result.pptx_bytes,
        media_type=PPTX_MIME,
        headers={
            "Content-Disposition": f'attachment; filename="{result.filename}"',
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )


@router.delete("/deliverables/{deliverable_id}", status_code=204)
def delete_deliverable(deliverable_id: int, db: Session = Depends(get_db)):
    """Delete a saved deliverable record."""
    d = db.get(Deliverable, deliverable_id)
    if not d:
        raise HTTPException(status_code=404, detail="Deliverable not found.")
    db.delete(d)
    db.commit()
