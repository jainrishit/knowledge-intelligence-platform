"""
SQLAlchemy ORM models. Database-agnostic: change DATABASE_URL to switch
between SQLite (default) and PostgreSQL without any code changes.
All cross-workspace joins go through the workspace_id foreign key.
"""
import json
from datetime import datetime

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
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

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
    uploaded_at = Column(DateTime, default=datetime.utcnow, nullable=False)

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
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

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
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

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
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    workspace = relationship("Workspace", back_populates="patterns")


Index("ix_patterns_workspace_id", ConsultingPattern.workspace_id)


class Deliverable(Base):
    __tablename__ = "deliverables"

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    type = Column(String(50), nullable=False)
    title = Column(String(512), nullable=False)
    content_markdown = Column(Text, nullable=True)
    source_concept_ids = Column(_JSONField, nullable=True, default=list)
    source_document_ids = Column(_JSONField, nullable=True, default=list)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    workspace = relationship("Workspace", back_populates="deliverables")


Index("ix_deliverables_workspace_id", Deliverable.workspace_id)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    workspace_id = Column(Integer, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    role = Column(String(20), nullable=False)
    content = Column(Text, nullable=False)
    source_document_ids = Column(_JSONField, nullable=True, default=list)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    workspace = relationship("Workspace", back_populates="chat_messages")


Index("ix_chat_messages_workspace_id", ChatMessage.workspace_id)
