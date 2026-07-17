"""
Pydantic schemas for all API request/response models.

The `SourcedResponse` mixin enforces that every AI-generated response
carries a non-empty sources list (or an explicit "not found" answer).
"""
from __future__ import annotations
from datetime import datetime
from typing import Any, Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator




class SourceRef(BaseModel):
    document_id: int
    document_name: str
    excerpt: str = ""


class SourcedResponseMixin(BaseModel):
    """Mixin that forces AI responses to carry source citations."""
    sources: list[SourceRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_sources_when_answered(self) -> "SourcedResponseMixin":
        # If subclass has an 'answer' field and it's not the not-found sentinel, sources must be non-empty
        answer = getattr(self, "answer", None)
        NOT_FOUND_SIGNALS = ("not found in the uploaded knowledge", "could not find", "cannot find", "outside the documents")
        if answer and not any(sig in answer.lower() for sig in NOT_FOUND_SIGNALS) and not self.sources:
            raise ValueError("AI response must include at least one source citation when an answer is provided.")
        return self




class WorkspaceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None


class WorkspaceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: Optional[str]
    created_at: datetime
    document_count: int = 0
    concept_count: int = 0
    relationship_count: int = 0
    pattern_count: int = 0
    graph_version: int = 0
    graph_last_updated: Optional[datetime] = None




class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    workspace_id: int
    filename: str
    file_type: str
    title: Optional[str]
    industry: Optional[str]
    topics: list[str] = []
    upload_status: str
    error_message: Optional[str] = None
    uploaded_at: datetime


class DocumentDetail(DocumentOut):
    raw_text: Optional[str] = None




class ConceptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    workspace_id: int
    name: str
    type: Optional[str]
    description: Optional[str]
    source_document_id: Optional[int]
    source_excerpt: Optional[str]
    created_at: datetime




class RelationshipOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    workspace_id: int
    source_concept_id: int
    target_concept_id: int
    relationship_type: str
    source_document_id: Optional[int]
    created_at: datetime




class ConsultingPatternOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    workspace_id: int
    name: str
    problem_statement: Optional[str]
    ibm_approach: list[str] = []
    related_concept_ids: list[int] = []
    source_document_ids: list[int] = []
    created_at: datetime




class GraphNode(BaseModel):
    id: str  # React Flow expects string IDs
    data: dict[str, Any]
    position: dict[str, float] = Field(default_factory=lambda: {"x": 0, "y": 0})
    type: str = "default"


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    label: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class GraphOut(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]


class NodeNeighbourhood(BaseModel):
    node: ConceptOut
    neighbours: list[ConceptOut]
    edges: list[RelationshipOut]
    source_document: Optional[DocumentOut] = None




class AskRequest(BaseModel):
    question: str = Field(..., min_length=1)


class AskResponse(SourcedResponseMixin):
    answer: str
    sources: list[SourceRef] = Field(default_factory=list)


class ChatMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    workspace_id: int
    role: str
    content: str
    source_document_ids: list[int] = []
    created_at: datetime




class DeliverableCreate(BaseModel):
    type: str = Field(..., pattern="^(client_101|client_201|executive_summary)$")
    # focus_area is only meaningful for executive_summary; ignored for client_101/201
    focus_area: Optional[str] = None


class DeliverableOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    workspace_id: int
    type: str
    title: str
    content_markdown: Optional[str]
    source_concept_ids: list[int] = []
    source_document_ids: list[int] = []
    created_at: datetime


class DeliverablePptxResponse(SourcedResponseMixin):
    deliverable: DeliverableOut
    sources: list[SourceRef] = Field(default_factory=list)
    # Raw PPTX bytes — not serialised to JSON; consumed directly by the API layer
    pptx_bytes: bytes = Field(default=b"", exclude=True)
    filename: str = ""
