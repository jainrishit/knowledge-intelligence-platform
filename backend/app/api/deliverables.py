"""Deliverables API router — generate client materials as PowerPoint presentations."""
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.db.models import Deliverable, Workspace
from app.generation.deliverable_service import generate_client_material
from app.schemas import DeliverableCreate, DeliverableOut, DeliverablePptxResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["deliverables"])

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


@router.post("/workspaces/{workspace_id}/deliverables", status_code=201)
def create_deliverable(
    workspace_id: int,
    body: DeliverableCreate,
    db: Session = Depends(get_db),
):
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
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except RuntimeError as e:
        logger.error("[deliverables ws=%d] RuntimeError: %s", workspace_id, e)
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error(
            "[deliverables ws=%d] Unexpected error during generation: %s: %s",
            workspace_id, type(e).__name__, e,
        )
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred during presentation generation. Please try again.",
        )

    return Response(
        content=result.pptx_bytes,
        media_type=PPTX_MIME,
        status_code=201,
        headers={
            "Content-Disposition": f'attachment; filename="{result.filename}"',
            "Access-Control-Expose-Headers": (
                "Content-Disposition, X-Deliverable-Id, X-Deliverable-Title, X-Source-Count"
            ),
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
    try:
        deliverables = db.query(Deliverable).filter(Deliverable.workspace_id == workspace_id).all()
        return [DeliverableOut.model_validate(d) for d in deliverables]
    except Exception as exc:
        logger.error("[deliverables ws=%d] list_deliverables failed: %s: %s", workspace_id, type(exc).__name__, exc)
        return []


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
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except RuntimeError as e:
        logger.error("[deliverables id=%d] re-export RuntimeError: %s", deliverable_id, e)
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error(
            "[deliverables id=%d] Unexpected error during re-export: %s: %s",
            deliverable_id, type(e).__name__, e,
        )
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred during re-export. Please try again.",
        )

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
