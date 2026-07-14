"""Documents API router — upload, list, detail, delete."""
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, BackgroundTasks
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import get_db
from app.db.models import Document, Workspace
from app.ingestion.parsers import detect_file_type
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
        file_type = detect_file_type(file.filename or "", file.content_type)
        if not file_type:
            raise HTTPException(
                status_code=422,
                detail=f"Unsupported file type for '{file.filename}'. Accepted: PDF, DOCX, PPTX.",
            )

        unique_name = f"{uuid.uuid4().hex}_{file.filename}"
        file_path = UPLOAD_DIR / unique_name
        content = await file.read()
        file_path.write_bytes(content)

        doc = Document(
            workspace_id=workspace_id,
            filename=str(file_path.absolute()),
            file_type=file_type,
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
    db.delete(doc)
    db.commit()
