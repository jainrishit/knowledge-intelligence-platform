"""Documents API router — upload, list, detail, delete."""
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, BackgroundTasks
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.core.upload_validation import validate_upload
from app.db.session import get_db
from app.db.models import Concept, ConsultingPattern, Document, Relationship, Workspace
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
    did = doc.id

    # ── Cascade: permanently remove this document's knowledge contribution ─────
    # Concepts carry a single source_document_id, so any concept sourced from this
    # document has no other evidence and must be deleted (not left orphaned with a
    # NULL source, which previously let it re-enter the graph on rebuild). If
    # multi-source provenance is added later, delete only those whose remaining
    # evidence count reaches 0.
    concept_ids = [
        cid for (cid,) in db.query(Concept.id).filter(
            Concept.workspace_id == workspace_id,
            Concept.source_document_id == did,
        ).all()
    ]
    # Relationships: delete those sourced from this doc OR touching a deleted concept
    # (otherwise they dangle, referencing a now-missing concept).
    db.query(Relationship).filter(
        Relationship.workspace_id == workspace_id,
        or_(
            Relationship.source_document_id == did,
            Relationship.source_concept_id.in_(concept_ids or [-1]),
            Relationship.target_concept_id.in_(concept_ids or [-1]),
        ),
    ).delete(synchronize_session=False)
    if concept_ids:
        db.query(Concept).filter(
            Concept.id.in_(concept_ids)
        ).delete(synchronize_session=False)

    # Consulting patterns: drop this doc from provenance and any deleted-concept
    # references; delete a pattern only once it has no remaining source document.
    concept_id_set = set(concept_ids)
    for p in db.query(ConsultingPattern).filter(
        ConsultingPattern.workspace_id == workspace_id
    ).all():
        remaining_docs = [x for x in (p.source_document_ids or []) if int(x) != did]
        if not remaining_docs:
            db.delete(p)
        else:
            p.source_document_ids = remaining_docs
            p.related_concept_ids = [
                x for x in (p.related_concept_ids or []) if int(x) not in concept_id_set
            ]

    # Prune the live in-memory graph, delete the document row, commit.
    graph_memory_manager.remove_document_contributions(workspace_id, did)
    db.delete(doc)
    db.commit()

    # Make the deletion durable: evict the cached graph (next access rebuilds from
    # the now-authoritative DB) and bump graph_version — so the removed document can
    # never re-enter the graph after a cache eviction or restart.
    graph_memory_manager.invalidate(workspace_id)
    graph_memory_manager._bump_graph_version(db, workspace_id)
