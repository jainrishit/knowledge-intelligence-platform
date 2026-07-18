"""
Presentation Plan API router — plan → review → approve → generate workflow.

Endpoints:
  POST   /workspaces/{id}/presentation-plans         — generate a new plan
  GET    /workspaces/{id}/presentation-plans         — list plans for workspace
  GET    /presentation-plans/{id}                    — get single plan
  PATCH  /presentation-plans/{id}/revise             — revise plan with user instruction
  POST   /presentation-plans/{id}/generate           — approve plan & generate PPTX
  DELETE /presentation-plans/{id}                    — delete a draft plan
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.db.models import PresentationPlan, Workspace
from app.generation.plan_service import (
    approve_and_generate, generate_plan, revise_plan, update_plan_slides,
)
from app.schemas import (
    PlanRevisionRequest,
    PlanSlidesUpdate,
    PresentationPlanCreate,
    PresentationPlanOut,
    DeliverablePptxResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["presentation-plans"])

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def _plan_out(plan: PresentationPlan) -> PresentationPlanOut:
    """Helper — import plan_service._plan_to_out without circular issues."""
    from app.generation.plan_service import _plan_to_out
    return _plan_to_out(plan)


# ─────────────────────────────────────────────────────────────────────────────
# Generate new plan
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/workspaces/{workspace_id}/presentation-plans", status_code=201,
             response_model=PresentationPlanOut)
def create_presentation_plan(
    workspace_id: int,
    body: PresentationPlanCreate,
    db: Session = Depends(get_db),
):
    """
    Step 1: generate a presentation plan from the workspace knowledge graph.

    Returns a structured plan (slide-by-slide) for user review.
    Does NOT generate a PPTX.
    """
    ws = db.get(Workspace, workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    try:
        return generate_plan(
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
        logger.error("[plans ws=%d] generate_plan RuntimeError: %s", workspace_id, e)
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error(
            "[plans ws=%d] generate_plan unexpected error: %s: %s",
            workspace_id, type(e).__name__, e,
        )
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred during plan generation. Please try again.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# List plans for a workspace
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/workspaces/{workspace_id}/presentation-plans",
            response_model=list[PresentationPlanOut])
def list_presentation_plans(workspace_id: int, db: Session = Depends(get_db)):
    ws = db.get(Workspace, workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    try:
        plans = (
            db.query(PresentationPlan)
            .filter(PresentationPlan.workspace_id == workspace_id)
            .order_by(PresentationPlan.created_at.desc())
            .all()
        )
        return [_plan_out(p) for p in plans]
    except Exception as exc:
        logger.error("[plans ws=%d] list_plans failed: %s: %s", workspace_id, type(exc).__name__, exc)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Get single plan
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/presentation-plans/{plan_id}", response_model=PresentationPlanOut)
def get_presentation_plan(plan_id: int, db: Session = Depends(get_db)):
    plan = db.get(PresentationPlan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Presentation plan not found.")
    try:
        return _plan_out(plan)
    except Exception as exc:
        logger.error("[plans id=%d] get_plan failed: %s: %s", plan_id, type(exc).__name__, exc)
        raise HTTPException(status_code=500, detail="Failed to retrieve plan.")


# ─────────────────────────────────────────────────────────────────────────────
# Persist local slide edits (reorder / remove)
# ─────────────────────────────────────────────────────────────────────────────

@router.patch("/presentation-plans/{plan_id}/slides", response_model=PresentationPlanOut)
def update_presentation_plan_slides(
    plan_id: int,
    body: PlanSlidesUpdate,
    db: Session = Depends(get_db),
):
    """
    Persist the user's local slide edits (reorder / remove) without calling Claude.

    Replaces the stored blueprint slide array with the provided list, renumbered
    from 1.  Must be called before revise_plan or approve_and_generate to ensure
    local edits are not discarded.
    """
    plan = db.get(PresentationPlan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Presentation plan not found.")
    try:
        return update_plan_slides(db=db, plan_id=plan_id, body=body)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error(
            "[plans id=%d] update_slides unexpected error: %s: %s",
            plan_id, type(e).__name__, e,
        )
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while saving slide edits.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Revise plan
# ─────────────────────────────────────────────────────────────────────────────

@router.patch("/presentation-plans/{plan_id}/revise", response_model=PresentationPlanOut)
def revise_presentation_plan(
    plan_id: int,
    body: PlanRevisionRequest,
    db: Session = Depends(get_db),
):
    """
    Step 2 (optional, repeatable): apply a user revision instruction to the plan.

    Claude applies the instruction and returns the updated plan.
    This can be called multiple times until the user is satisfied.
    """
    plan = db.get(PresentationPlan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Presentation plan not found.")
    try:
        return revise_plan(db=db, plan_id=plan_id, instruction=body.instruction)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except RuntimeError as e:
        logger.error("[plans id=%d] revise_plan RuntimeError: %s", plan_id, e)
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error(
            "[plans id=%d] revise_plan unexpected error: %s: %s",
            plan_id, type(e).__name__, e,
        )
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred during plan revision. Please try again.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Approve plan → generate PPTX
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/presentation-plans/{plan_id}/generate")
def approve_and_generate_pptx(plan_id: int, db: Session = Depends(get_db)):
    """
    Step 3: approve the plan and generate the PPTX.

    Normalises the approved blueprint and renders the PowerPoint file.
    Returns the PPTX as a file download.
    """
    plan = db.get(PresentationPlan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Presentation plan not found.")
    try:
        result: DeliverablePptxResponse = approve_and_generate(db=db, plan_id=plan_id)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except RuntimeError as e:
        logger.error("[plans id=%d] approve_and_generate RuntimeError: %s", plan_id, e)
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error(
            "[plans id=%d] approve_and_generate unexpected error: %s: %s",
            plan_id, type(e).__name__, e,
        )
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred during PPTX generation. Please try again.",
        )

    return Response(
        content=result.pptx_bytes,
        media_type=PPTX_MIME,
        status_code=200,
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


# ─────────────────────────────────────────────────────────────────────────────
# Delete plan
# ─────────────────────────────────────────────────────────────────────────────

@router.delete("/presentation-plans/{plan_id}", status_code=204)
def delete_presentation_plan(plan_id: int, db: Session = Depends(get_db)):
    """Delete a draft presentation plan."""
    plan = db.get(PresentationPlan, plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="Presentation plan not found.")
    db.delete(plan)
    db.commit()
