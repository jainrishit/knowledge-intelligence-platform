"""
SQLAlchemy ORM models. Database-agnostic: change DATABASE_URL to switch
between SQLite (default) and PostgreSQL without any code changes.
All cross-workspace joins go through the workspace_id foreign key.
"""
import json
from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    Index,
    types as sa_types,
)
from sqlalchemy.orm import DeclarativeBase, relationship

# All recognised concept type values across both the document and spreadsheet pipelines.
# The type field is a free string on the Concept model — no migration is required to add values.
SPREADSHEET_CONCEPT_TYPES: set[str] = {
    "Requirement",
    "Risk",
    "Issue",
    "Dependency",
    "Process",
    "Capability",
    "Data Element",
    "Control",
    "Business Rule",
    "Business Term",
    "Standard",
    "Technology",
    "Methodology",
    "Framework",
    "Regulation",
    "Pattern",
    "Tool",
    "General",
}


class Base(DeclarativeBase):
    pass


class _JSONField(sa_types.TypeDecorator):
    """Stores a Python list/dict as JSON text; transparent on get/set."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return "[]"
        return json.dumps(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return []
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return []


class Workspace(Base):
    __tablename__ = "workspaces"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    # Knowledge evolution tracking — updated by GraphMemoryManager and pattern extraction
    graph_version = Column(Integer, nullable=False, default=0)
    graph_last_updated = Column(DateTime, nullable=True)
    last_pattern_extraction_at = Column(DateTime, nullable=True)
    concept_count_at_last_pattern_run = Column(Integer, nullable=False, default=0)
    relationship_count_at_last_pattern_run = Column(Integer, nullable=False, default=0)

    documents = relationship("Document", back_populates="workspace", cascade="all, delete-orphan")
    concepts = relationship("Concept", back_populates="workspace", cascade="all, delete-orphan")
    relationships = relationship("Relationship", back_populates="workspace", cascade="all, delete-orphan")
    patterns = relationship("ConsultingPattern", back_populates="workspace", cascade="all, delete-orphan")
    deliverables = relationship("Deliverable", back_populates="workspace", cascade="all, delete-orphan")
    chat_messages = relationship("ChatMessage", back_populates="workspace", cascade="all, delete-orphan")


class Document(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    filename = Column(String(512), nullable=False)
    file_type = Column(String(10), nullable=False)
    title = Column(String(512), nullable=True)
    industry = Column(String(200), nullable=True)
    topics = Column(_JSONField, nullable=True, default=list)
    raw_text = Column(Text, nullable=True)
    upload_status = Column(String(20), nullable=False, default="pending")
    error_message = Column(Text, nullable=True)
    uploaded_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    workspace = relationship("Workspace", back_populates="documents")
    concepts = relationship("Concept", back_populates="source_document")
    relationships = relationship("Relationship", back_populates="source_document")


Index("ix_documents_workspace_id", Document.workspace_id)
Index("ix_documents_upload_status", Document.upload_status)


class Concept(Base):
    __tablename__ = "concepts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(512), nullable=False)
    type = Column(String(200), nullable=True)
    description = Column(Text, nullable=True)
    source_document_id = Column(Integer, ForeignKey("documents.id", ondelete="SET NULL"), nullable=True)
    source_excerpt = Column(Text, nullable=True)
    confidence = Column(sa_types.Float, nullable=False, default=0.8)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    workspace = relationship("Workspace", back_populates="concepts")
    source_document = relationship("Document", back_populates="concepts")
    source_relationships = relationship(
        "Relationship", foreign_keys="Relationship.source_concept_id", back_populates="source_concept", cascade="all, delete-orphan"
    )
    target_relationships = relationship(
        "Relationship", foreign_keys="Relationship.target_concept_id", back_populates="target_concept", cascade="all, delete-orphan"
    )


Index("ix_concepts_workspace_id", Concept.workspace_id)
Index("ix_concepts_source_document_id", Concept.source_document_id)


ALLOWED_RELATIONSHIP_TYPES = {
    "depends_on", "requires", "implements", "extends",
    "contrasts_with", "enables", "is_part_of", "related_to",
}


class Relationship(Base):
    __tablename__ = "relationships"

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    source_concept_id = Column(Integer, ForeignKey("concepts.id", ondelete="CASCADE"), nullable=False)
    target_concept_id = Column(Integer, ForeignKey("concepts.id", ondelete="CASCADE"), nullable=False)
    relationship_type = Column(String(100), nullable=False, default="related_to")
    source_document_id = Column(Integer, ForeignKey("documents.id", ondelete="SET NULL"), nullable=True)
    strength = Column(sa_types.Float, nullable=False, default=0.7)
    reasoning = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    workspace = relationship("Workspace", back_populates="relationships")
    source_concept = relationship("Concept", foreign_keys=[source_concept_id], back_populates="source_relationships")
    target_concept = relationship("Concept", foreign_keys=[target_concept_id], back_populates="target_relationships")
    source_document = relationship("Document", back_populates="relationships")


Index("ix_relationships_workspace_id", Relationship.workspace_id)
Index("ix_relationships_source_concept_id", Relationship.source_concept_id)
Index("ix_relationships_target_concept_id", Relationship.target_concept_id)


class ConsultingPattern(Base):
    __tablename__ = "consulting_patterns"

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(512), nullable=False)
    problem_statement = Column(Text, nullable=True)
    ibm_approach = Column(_JSONField, nullable=True, default=list)
    related_concept_ids = Column(_JSONField, nullable=True, default=list)
    source_document_ids = Column(_JSONField, nullable=True, default=list)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    workspace = relationship("Workspace", back_populates="patterns")


Index("ix_patterns_workspace_id", ConsultingPattern.workspace_id)




class SpreadsheetIngestionRun(Base):
    """
    Audit record for each spreadsheet ingestion run.
    Tracks workbook-level metrics per document per run.
    """
    __tablename__ = "spreadsheet_ingestion_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    document_id = Column(Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    file_type = Column(String(10), nullable=False)
    started_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    completed_at = Column(DateTime, nullable=True)
    sheets_processed = Column(Integer, nullable=False, default=0)
    tables_detected = Column(Integer, nullable=False, default=0)
    concepts_extracted = Column(Integer, nullable=False, default=0)
    relationships_extracted = Column(Integer, nullable=False, default=0)
    # "complete" | "failed"
    status = Column(String(20), nullable=False, default="complete")


Index("ix_spreadsheet_runs_workspace_id", SpreadsheetIngestionRun.workspace_id)
Index("ix_spreadsheet_runs_document_id", SpreadsheetIngestionRun.document_id)


class Deliverable(Base):
    __tablename__ = "deliverables"

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    type = Column(String(50), nullable=False)
    title = Column(String(512), nullable=False)
    content_markdown = Column(Text, nullable=True)
    source_concept_ids = Column(_JSONField, nullable=True, default=list)
    source_document_ids = Column(_JSONField, nullable=True, default=list)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    workspace = relationship("Workspace", back_populates="deliverables")


Index("ix_deliverables_workspace_id", Deliverable.workspace_id)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    source_document_ids = Column(_JSONField, nullable=True, default=list)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    workspace = relationship("Workspace", back_populates="chat_messages")


Index("ix_chat_messages_workspace_id", ChatMessage.workspace_id)




class PatternExtractionRun(Base):
    """
    Audit record for each pattern extraction run.
    Stores what the workspace looked like at the time and what was produced,
    enabling threshold decisions and extraction history queries.
    """
    __tablename__ = "pattern_extraction_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    started_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    completed_at = Column(DateTime, nullable=True)
    concept_count = Column(Integer, nullable=False, default=0)
    relationship_count = Column(Integer, nullable=False, default=0)
    patterns_generated = Column(Integer, nullable=False, default=0)
    graph_version = Column(Integer, nullable=False, default=0)
    # "complete" | "skipped" | "failed"
    status = Column(String(20), nullable=False, default="complete")


Index("ix_pattern_runs_workspace_id", PatternExtractionRun.workspace_id)
Index("ix_pattern_runs_started_at", PatternExtractionRun.started_at)




class LLMUsage(Base):
    """One row per LLM API call — used for cost auditing and quota tracking."""
    __tablename__ = "llm_usage"

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True)
    operation_type = Column(String(50), nullable=False)   # e.g. concept_extraction
    model_name = Column(String(100), nullable=False)
    prompt_tokens = Column(Integer, nullable=False, default=0)
    completion_tokens = Column(Integer, nullable=False, default=0)
    estimated_cost_usd = Column(sa_types.Float, nullable=False, default=0.0)
    request_timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)


Index("ix_llm_usage_workspace_id", LLMUsage.workspace_id)
Index("ix_llm_usage_operation_type", LLMUsage.operation_type)
Index("ix_llm_usage_timestamp", LLMUsage.request_timestamp)


class LLMBudget(Base):
    """
    Per-workspace LLM budget. Created on first use; updated after every call.
    monthly_token_limit = 0 means unlimited.
    monthly_cost_limit  = 0.0 means unlimited.
    """
    __tablename__ = "llm_budgets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, unique=True)
    monthly_token_limit = Column(Integer, nullable=False, default=0)
    monthly_cost_limit = Column(sa_types.Float, nullable=False, default=0.0)
    current_token_usage = Column(Integer, nullable=False, default=0)
    current_cost_usage = Column(sa_types.Float, nullable=False, default=0.0)
    budget_period_start = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)


Index("ix_llm_budgets_workspace_id", LLMBudget.workspace_id)
