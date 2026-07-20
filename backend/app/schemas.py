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
    strength: float = 0.7
    reasoning: Optional[str] = None
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




# ─────────────────────────────────────────────────────────────────────────────
# Presentation Plan schemas (plan → review → approve workflow)
# ─────────────────────────────────────────────────────────────────────────────

class PlanSlide(BaseModel):
    """One slide entry inside a PresentationPlan."""
    slide_number: int
    title: str
    layout: str = "title_content"
    purpose: Optional[str] = None
    section: Optional[str] = None
    bullets: list[str] = Field(default_factory=list)
    columns: Optional[list[list[str]]] = None
    col_heads: Optional[list[str]] = None
    boxes: Optional[list[str]] = None
    stats: Optional[list[dict]] = None
    notes: Optional[str] = None
    visual_recommendation: Optional[str] = None
    # Plan-phase annotation fields — populated by Claude during blueprint generation.
    # These make the plan review rich: show exactly which graph intelligence each slide uses.
    # key_insights: the "so what" consulting takeaways the user sees before approving.
    key_insights: list[str] = Field(default_factory=list)
    graph_concepts: list[str] = Field(default_factory=list)
    relationships_used: list[str] = Field(default_factory=list)
    patterns_used: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class PresentationPlanCreate(BaseModel):
    """Request body for generating a new presentation plan."""
    type: str = Field(..., pattern="^(client_101|client_201|executive_summary)$")
    focus_area: Optional[str] = None


class GraphCoverage(BaseModel):
    """Deck-level knowledge-graph coverage — shown so the UI communicates that the
    graph is actively used (the plan strips per-slide attribution for token
    efficiency, which previously made the UI display 0)."""
    concepts_available: int = 0
    relationships_analyzed: int = 0
    patterns_available: int = 0
    source_documents: int = 0
    concepts_selected: int = 0


class PresentationPlanOut(BaseModel):
    """Serialised plan returned to the frontend."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    workspace_id: int
    deliverable_type: str
    focus_area: Optional[str]
    # Deck-level graph coverage (None for older plans generated before this field)
    graph_coverage: Optional[GraphCoverage] = None
    # Parsed slide list for the frontend
    slides: list[PlanSlide] = Field(default_factory=list)
    # Governing messages / storyline from the blueprint
    governing_messages: list[str] = Field(default_factory=list)
    storyline_summary: Optional[str] = None
    deck_title: Optional[str] = None
    revision_history: list[dict] = Field(default_factory=list)
    status: str
    deliverable_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime


class PlanRevisionRequest(BaseModel):
    """User instruction to revise the plan."""
    instruction: str = Field(..., min_length=1, max_length=2000)


class PlanSlidesUpdate(BaseModel):
    """
    Payload to persist the user's local slide edits (reorder / remove).

    Contains the COMPLETE ordered list of slides after the user's edits.
    The backend replaces the blueprint's slide array with this list and
    renumbers slide_number fields sequentially from 1.
    """
    slides: list[PlanSlide] = Field(..., min_length=1)


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


class ValidationSummary(BaseModel):
    """Stats from the deterministic validation layer that runs before PPTX render."""
    slides_removed: int = 0
    slides_fixed: int = 0
    slide_count_before: int = 0
    slide_count_after: int = 0


class DeliverablePptxResponse(SourcedResponseMixin):
    deliverable: DeliverableOut
    sources: list[SourceRef] = Field(default_factory=list)
    # Raw PPTX bytes — not serialised to JSON; consumed directly by the API layer
    pptx_bytes: bytes = Field(default=b"", exclude=True)
    filename: str = ""
    # Validation layer outcome — exposed as response headers for transparency
    validation: ValidationSummary = Field(default_factory=ValidationSummary)
