"""Documents API router — upload, list, detail, delete."""
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, BackgroundTasks
from sqlalchemy.orm import Session

from app.config import settings
from app.core.upload_validation import validate_upload
from app.db.session import get_db
from app.db.models import Document, Workspace
from app.graph.memory_manager import graph_memory_manager
from app.ingestion.pipeline import run_ingestion_pipeline
from app.schemas import DocumentOut, DocumentDetail

router = APIRouter(tags=["documents"])

UPLOAD_DIR = Path(settings.upload_dir)


def _ensure_upload_dir():
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@router.post("/workspaces/{workspace_id}/documents", response_model=list[DocumentOut], status_code=202)
async def upload_documents(
    workspace_id: int,
    files: list[UploadFile] = File(...),
    background_tasks: BackgroundTasks = BackgroundTasks(),
    db: Session = Depends(get_db),
):
    ws = db.get(Workspace, workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found.")

    _ensure_upload_dir()
    created = []

    for file in files:
        validated = await validate_upload(file)

        unique_name = f"{uuid.uuid4().hex}_{file.filename}"
        file_path = UPLOAD_DIR / unique_name
        file_path.write_bytes(validated.content)

        doc = Document(
            workspace_id=workspace_id,
            filename=str(file_path.absolute()),
            file_type=validated.file_type,
            title=Path(file.filename or "").stem,
            upload_status="pending",
        )
        db.add(doc)
        db.commit()
        db.refresh(doc)
        created.append(doc)

        background_tasks.add_task(run_ingestion_pipeline, doc.id)

    return [DocumentOut.model_validate(d) for d in created]


@router.get("/workspaces/{workspace_id}/documents", response_model=list[DocumentOut])
def list_documents(workspace_id: int, db: Session = Depends(get_db)):
    ws = db.get(Workspace, workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found.")
    docs = db.query(Document).filter(Document.workspace_id == workspace_id).all()
    return [DocumentOut.model_validate(d) for d in docs]


@router.get("/documents/{document_id}", response_model=DocumentDetail)
def get_document(document_id: int, db: Session = Depends(get_db)):
    doc = db.get(Document, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")
    return DocumentDetail.model_validate(doc)


@router.delete("/documents/{document_id}", status_code=204)
def delete_document(document_id: int, db: Session = Depends(get_db)):
    doc = db.get(Document, document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found.")
    workspace_id = doc.workspace_id
    graph_memory_manager.remove_document_contributions(workspace_id, doc.id)
    db.delete(doc)
    db.commit()
