"""
Knowledge Health Audit Service — measures, scores, and reports on the
reliability of the Knowledge Graph and Agentic Memory layer.

All computations are deterministic SQL queries — no LLM calls, no async,
no graph loading required.

═══════════════════════════════════════════════════════
CONCEPT CLASSIFICATION MODEL
═══════════════════════════════════════════════════════

Every concept in the workspace is classified as one of:

  CONNECTED (category 2)
      Participates in ≥1 relationship.
      Examples: Ripple, ODL, Cross-Border Payments, ISO 20022.

  STANDALONE (category 1)
      Has no relationships, but does NOT have other closely-related
      concepts in the workspace that would imply a missing link.
      Examples: XRP (informational reference), MiCA (regulation
      named but not connected to compliance concepts).
      This is NOT a failure — standalone concepts still have value.

  UNCONNECTED_CANDIDATE (category 3)
      Has no relationships, but co-exists with concepts that should
      logically connect to it based on domain proximity (same document,
      similar type, or adjacent topic).
      This IS a graph-quality issue — the relationship likely exists
      but was not extracted.

Classification heuristic for UNCONNECTED_CANDIDATE:
  A standalone concept is elevated to unconnected_candidate if it
  appears in the same source document as ≥2 other concepts that are
  themselves connected (have relationships). The assumption is that
  concepts extracted from a rich, well-connected document should also
  be participating in relationships.

═══════════════════════════════════════════════════════
SCORE METHODOLOGY
═══════════════════════════════════════════════════════

ingestion_score
    Fraction of documents with status="complete".
    Penalised by documents with status="failed".

extraction_density_score
    Average concepts-per-document across completed docs.
    Benchmarked against a threshold (5/doc min, 20/doc ideal).

relationship_density_score
    Average relationships per connected concept (not per total concept).
    Standalone concepts are excluded from the denominator — they are
    not failures. Benchmarked at 1.0 rel/connected-concept min, 3.0 ideal.

relationship_coverage_score
    Fraction of non-standalone concepts that participate in ≥1
    relationship. Unconnected candidates penalise this score.
    Formula: connected / (connected + unconnected_candidates)

evidence_coverage_score
    Fraction of concepts with a non-empty source_excerpt.

graph_connectivity_score  (replaces graph_integrity_score)
    Measures relationship quality, NOT orphan count:
      0.5 * relationship_coverage + 0.3 * type_diversity + 0.2 * avg_strength
    type_diversity = unique relationship types used / 10 (normalised).
    avg_strength   = average strength across all relationships.

    Does NOT penalise standalone concepts.

pattern_coverage_score
    Whether pattern extraction has run (≥1 pattern per 10 concepts).

consulting_readiness_score
    Composite: evidence × relationship_density × (1 if patterns>0 else 0.6).

memory_confidence_score  (headline)
    Weighted average:
      ingestion               20%
      extraction_density      15%
      relationship_density    15%
      evidence_coverage       20%
      graph_connectivity      15%
      pattern_coverage        15%

═══════════════════════════════════════════════════════
DELIVERABLE READINESS THRESHOLDS
═══════════════════════════════════════════════════════

Client 101 — basics only; standalone workspaces acceptable
    ≥1 complete doc, ≥5 concepts, evidence ≥50%

Client 201 — requires relationship depth
    ≥2 complete docs, ≥15 concepts, ≥8 relationships,
    relationship_coverage ≥30%, evidence ≥60%

Executive Summary — needs evidence + minimal connectivity
    ≥1 complete doc, ≥5 concepts, evidence ≥50%,
    relationship_density ≥20%
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models import (
    Concept, ConsultingPattern, Document, IngestionAudit,
    Relationship, Workspace,
)

# ── Benchmarks ────────────────────────────────────────────────────────────────

_MIN_CONCEPTS_PER_DOC     = 5
_IDEAL_CONCEPTS_PER_DOC   = 20
_MIN_RELS_PER_CONNECTED   = 1.0   # per connected concept
_IDEAL_RELS_PER_CONNECTED = 3.0
_TOTAL_REL_TYPES          = 10    # denominator for type-diversity normalisation


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class DocumentAuditRecord:
    document_id: int
    document_title: str
    file_type: str
    upload_status: str
    char_count: int
    chunk_count: int
    page_count: int
    concepts_extracted: int
    relationships_extracted: int
    concepts_with_evidence: int
    evidence_coverage_pct: float
    ingestion_started_at: Optional[datetime]
    ingestion_completed_at: Optional[datetime]
    ingestion_duration_seconds: Optional[float]
    error_detail: Optional[str]
    document_health_score: float


@dataclass
class ConceptConnectivitySummary:
    """
    Classifies concepts into three categories.

    connected           — participates in ≥1 relationship
    standalone          — no relationships; no strong expectation of one
    unconnected_candidate — no relationships; likely has missing links
    relationship_coverage_pct — connected / (connected + unconnected_candidates) × 100
    """
    connected: int
    standalone: int
    unconnected_candidates: int
    relationship_coverage_pct: float


@dataclass
class GraphIntegritySummary:
    """
    Renamed fields retain the old API for backward compat:
      orphan_concepts     — now means unconnected_candidates
      orphan_pct          — now means unconnected_candidates / total × 100
    New fields added for the richer model:
    """
    total_concepts: int
    total_relationships: int
    # Legacy fields (kept for API compat) — now represent unconnected candidates
    orphan_concepts: int
    orphan_pct: float
    # New classification fields
    connected_concepts: int
    standalone_concepts: int
    unconnected_candidates: int
    relationship_coverage_pct: float
    # Graph quality metrics
    unique_relationship_types: int
    avg_relationship_strength: float
    # Existing
    multi_doc_concepts: int
    multi_doc_pct: float
    avg_confidence: float
    low_confidence_concepts: int


@dataclass
class WorkspaceAuditReport:
    workspace_id: int
    workspace_name: str
    graph_version: int
    generated_at: datetime

    total_documents: int
    complete_documents: int
    failed_documents: int
    pending_processing_documents: int

    total_chars_ingested: int
    total_chunks_processed: int
    total_pages_processed: int

    total_concepts: int
    total_relationships: int
    total_patterns: int
    concepts_with_evidence: int
    evidence_coverage_pct: float

    integrity: GraphIntegritySummary

    # Sub-scores (0–100)
    ingestion_score: float
    extraction_density_score: float
    relationship_density_score: float
    evidence_coverage_score: float
    graph_integrity_score: float          # ← now graph_connectivity_score internally
    pattern_coverage_score: float
    consulting_readiness_score: float
    relationship_coverage_score: float    # NEW

    memory_confidence_score: float

    client_101_ready: bool
    client_201_ready: bool
    executive_summary_ready: bool

    documents: list[DocumentAuditRecord] = field(default_factory=list)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _pct(numerator: int | float, denominator: int | float) -> float:
    if not denominator:
        return 0.0
    return round(100.0 * numerator / denominator, 1)


def _score_to_100(value: float) -> float:
    return round(max(0.0, min(1.0, value)) * 100, 1)


def _ingestion_score(complete: int, total: int, failed: int) -> float:
    if total == 0:
        return 0.0
    base = complete / total
    penalty = 0.1 * failed / total
    return max(0.0, min(1.0, base - penalty))


def _extraction_density_score(total_concepts: int, complete_docs: int) -> float:
    if complete_docs == 0:
        return 0.0
    avg = total_concepts / complete_docs
    return min(1.0, avg / _IDEAL_CONCEPTS_PER_DOC)


def _relationship_density_score(total_rels: int, connected_concepts: int) -> float:
    """
    Rels per CONNECTED concept — standalone concepts excluded from denominator.
    A workspace with 80 standalone + 20 connected concepts and 40 rels has a
    ratio of 40/20 = 2.0, not 40/100 = 0.4.  This is the correct signal.
    """
    if connected_concepts == 0:
        return 0.0
    ratio = total_rels / connected_concepts
    return min(1.0, ratio / _IDEAL_RELS_PER_CONNECTED)


def _relationship_coverage_score(connected: int, unconnected_candidates: int) -> float:
    """
    What fraction of concepts that SHOULD be connected ARE connected?
    Standalone concepts (those not expected to have relationships) are excluded.
    Returns 0.0 when both are 0 (empty workspace — no knowledge to measure).
    Returns 1.0 when there are connected concepts but no unconnected candidates.
    """
    denominator = connected + unconnected_candidates
    if denominator == 0:
        return 0.0  # empty workspace — no concepts at all → no credit
    return connected / denominator


def _evidence_coverage_score(concepts_with_evidence: int, total_concepts: int) -> float:
    if total_concepts == 0:
        return 0.0
    return concepts_with_evidence / total_concepts


def _graph_connectivity_score(
    relationship_coverage: float,
    unique_rel_types: int,
    avg_strength: float,
) -> float:
    """
    Measures connectivity quality — NOT orphan fraction.

    Components:
      50% — relationship coverage (what fraction of candidates are connected)
      30% — type diversity (breadth of relationship vocabulary used)
      20% — average strength (quality of relationships that exist)
    """
    type_diversity = min(1.0, unique_rel_types / _TOTAL_REL_TYPES)
    return (
        0.5 * relationship_coverage
        + 0.3 * type_diversity
        + 0.2 * avg_strength
    )


def _pattern_coverage_score(total_patterns: int, total_concepts: int) -> float:
    if total_concepts == 0:
        return 0.0
    if total_patterns == 0:
        return 0.0
    ratio = total_patterns / max(1, total_concepts / 10)
    return min(1.0, ratio)


def _consulting_readiness(
    evidence_score: float,
    rel_density_score: float,
    pattern_score: float,
) -> float:
    pattern_factor = 1.0 if pattern_score > 0 else 0.6
    return evidence_score * rel_density_score * pattern_factor


def _memory_confidence(
    ingestion: float,
    extraction: float,
    rel_density: float,
    evidence: float,
    connectivity: float,
    pattern: float,
) -> float:
    return (
        0.20 * ingestion
        + 0.15 * extraction
        + 0.15 * rel_density
        + 0.20 * evidence
        + 0.15 * connectivity
        + 0.15 * pattern
    )


def _document_health(status: str, concepts: int, evidence_pct: float) -> float:
    if status == "failed":
        return 0.0
    if status != "complete":
        return 10.0
    concept_score  = min(1.0, concepts / _IDEAL_CONCEPTS_PER_DOC)
    evidence_score = evidence_pct / 100.0
    raw = 0.5 * concept_score + 0.5 * evidence_score
    return _score_to_100(raw)


# ── SQL queries ───────────────────────────────────────────────────────────────

def _connected_concept_ids(db: Session, workspace_id: int) -> set[int]:
    """Concepts that appear in at least one relationship (source or target)."""
    sources: set[int] = {
        row[0] for row in
        db.query(Relationship.source_concept_id)
        .filter(Relationship.workspace_id == workspace_id)
        .distinct()
        .all()
    }
    targets: set[int] = {
        row[0] for row in
        db.query(Relationship.target_concept_id)
        .filter(Relationship.workspace_id == workspace_id)
        .distinct()
        .all()
    }
    return sources | targets


def _classify_concepts(
    db: Session,
    workspace_id: int,
    connected_ids: set[int],
) -> tuple[int, int, int]:
    """
    Classify all concepts as connected / standalone / unconnected_candidate.

    Returns (connected_count, standalone_count, unconnected_candidate_count).

    Unconnected candidate heuristic:
        A concept is an unconnected_candidate if it has no relationships BUT
        its source document also contains ≥2 OTHER concepts that ARE connected.
        This signals that the concept was extracted from a relationship-rich
        context and should itself have relationships.
    """
    all_concepts = (
        db.query(Concept)
        .filter(Concept.workspace_id == workspace_id)
        .all()
    )

    # For each document, count how many of its concepts are connected
    doc_connected_count: dict[int, int] = {}
    for c in all_concepts:
        if c.source_document_id and c.id in connected_ids:
            doc_connected_count[c.source_document_id] = (
                doc_connected_count.get(c.source_document_id, 0) + 1
            )

    connected = 0
    standalone = 0
    unconnected_candidates = 0

    for c in all_concepts:
        if c.id in connected_ids:
            connected += 1
        else:
            # Unconnected — decide if it's standalone or a candidate
            doc_id = c.source_document_id
            if doc_id and doc_connected_count.get(doc_id, 0) >= 2:
                # Its document has ≥2 other connected concepts → expected to participate
                unconnected_candidates += 1
            else:
                standalone += 1

    return connected, standalone, unconnected_candidates


# ── Public API ────────────────────────────────────────────────────────────────

def get_document_audit(db: Session, document_id: int) -> Optional[DocumentAuditRecord]:
    doc = db.get(Document, document_id)
    if doc is None:
        return None

    ia = db.query(IngestionAudit).filter(IngestionAudit.document_id == document_id).first()

    char_count   = ia.char_count if ia else 0
    chunk_count  = ia.chunk_count if ia else 0
    page_count   = ia.page_count if ia else 0
    error_detail = ia.error_detail if ia else None
    started_at   = ia.started_at if ia else None
    completed_at = ia.completed_at if ia else None

    duration: Optional[float] = None
    if started_at and completed_at:
        duration = (completed_at - started_at).total_seconds()

    concepts_extracted = (
        db.query(func.count(Concept.id))
        .filter(Concept.source_document_id == document_id)
        .scalar() or 0
    )
    rels_extracted = (
        db.query(func.count(Relationship.id))
        .filter(Relationship.source_document_id == document_id)
        .scalar() or 0
    )
    concepts_with_evidence = (
        db.query(func.count(Concept.id))
        .filter(
            Concept.source_document_id == document_id,
            Concept.source_excerpt.isnot(None),
            Concept.source_excerpt != "",
        )
        .scalar() or 0
    )
    evid_pct = _pct(concepts_with_evidence, concepts_extracted)
    health   = _document_health(doc.upload_status, concepts_extracted, evid_pct)

    return DocumentAuditRecord(
        document_id=document_id,
        document_title=doc.title or doc.filename,
        file_type=doc.file_type,
        upload_status=doc.upload_status,
        char_count=char_count,
        chunk_count=chunk_count,
        page_count=page_count,
        concepts_extracted=concepts_extracted,
        relationships_extracted=rels_extracted,
        concepts_with_evidence=concepts_with_evidence,
        evidence_coverage_pct=evid_pct,
        ingestion_started_at=started_at,
        ingestion_completed_at=completed_at,
        ingestion_duration_seconds=duration,
        error_detail=error_detail,
        document_health_score=health,
    )


def get_workspace_audit(db: Session, workspace_id: int) -> Optional[WorkspaceAuditReport]:
    ws = db.get(Workspace, workspace_id)
    if ws is None:
        return None

    # ── Document counts ───────────────────────────────────────────────────────
    docs = db.query(Document).filter(Document.workspace_id == workspace_id).all()
    total_docs    = len(docs)
    complete_docs = sum(1 for d in docs if d.upload_status == "complete")
    failed_docs   = sum(1 for d in docs if d.upload_status == "failed")
    pending_docs  = total_docs - complete_docs - failed_docs

    # ── Aggregate ingestion metrics ───────────────────────────────────────────
    ia_rows = (
        db.query(IngestionAudit)
        .filter(IngestionAudit.workspace_id == workspace_id)
        .all()
    )
    total_chars  = sum(r.char_count  for r in ia_rows)
    total_chunks = sum(r.chunk_count for r in ia_rows)
    total_pages  = sum(r.page_count  for r in ia_rows)

    # ── Extraction totals ─────────────────────────────────────────────────────
    total_concepts = (
        db.query(func.count(Concept.id))
        .filter(Concept.workspace_id == workspace_id)
        .scalar() or 0
    )
    total_rels = (
        db.query(func.count(Relationship.id))
        .filter(Relationship.workspace_id == workspace_id)
        .scalar() or 0
    )
    total_patterns = (
        db.query(func.count(ConsultingPattern.id))
        .filter(ConsultingPattern.workspace_id == workspace_id)
        .scalar() or 0
    )

    # ── Evidence coverage ─────────────────────────────────────────────────────
    concepts_with_evidence = (
        db.query(func.count(Concept.id))
        .filter(
            Concept.workspace_id == workspace_id,
            Concept.source_excerpt.isnot(None),
            Concept.source_excerpt != "",
        )
        .scalar() or 0
    )
    evid_pct = _pct(concepts_with_evidence, total_concepts)

    # ── Concept connectivity classification ───────────────────────────────────
    connected_ids = _connected_concept_ids(db, workspace_id)
    connected_count, standalone_count, unconnected_count = _classify_concepts(
        db, workspace_id, connected_ids
    )

    # Backward compat: orphan_concepts = unconnected_candidates (the real issue)
    orphan_count = unconnected_count
    rel_coverage_pct = _pct(connected_count, connected_count + unconnected_count)

    # ── Relationship quality metrics ──────────────────────────────────────────
    unique_rel_types = (
        db.query(func.count(Relationship.relationship_type.distinct()))
        .filter(Relationship.workspace_id == workspace_id)
        .scalar() or 0
    )
    avg_strength_row = (
        db.query(func.avg(Relationship.strength))
        .filter(Relationship.workspace_id == workspace_id)
        .scalar()
    )
    avg_strength = float(avg_strength_row or 0.0)

    # ── Multi-doc and confidence ──────────────────────────────────────────────
    multi_doc_concepts = (
        db.query(func.count())
        .select_from(
            db.query(Concept.id)
            .filter(
                Concept.workspace_id == workspace_id,
                Concept.source_document_id.isnot(None),
            )
            .group_by(Concept.name)
            .having(func.count(Concept.id) > 1)
            .subquery()
        )
        .scalar() or 0
    )
    avg_conf_row = (
        db.query(func.avg(Concept.confidence))
        .filter(Concept.workspace_id == workspace_id)
        .scalar()
    )
    avg_confidence = float(avg_conf_row or 0.0)
    low_confidence_concepts = (
        db.query(func.count(Concept.id))
        .filter(Concept.workspace_id == workspace_id, Concept.confidence < 0.6)
        .scalar() or 0
    )

    integrity = GraphIntegritySummary(
        total_concepts=total_concepts,
        total_relationships=total_rels,
        # Legacy fields — now represent unconnected_candidates
        orphan_concepts=orphan_count,
        orphan_pct=_pct(orphan_count, total_concepts),
        # New classification
        connected_concepts=connected_count,
        standalone_concepts=standalone_count,
        unconnected_candidates=unconnected_count,
        relationship_coverage_pct=rel_coverage_pct,
        unique_relationship_types=unique_rel_types,
        avg_relationship_strength=round(avg_strength, 3),
        # Existing
        multi_doc_concepts=multi_doc_concepts,
        multi_doc_pct=_pct(multi_doc_concepts, total_concepts),
        avg_confidence=round(avg_confidence, 3),
        low_confidence_concepts=low_confidence_concepts,
    )

    # ── Sub-scores (0–1) ──────────────────────────────────────────────────────
    ing_s  = _ingestion_score(complete_docs, total_docs, failed_docs)
    ext_s  = _extraction_density_score(total_concepts, complete_docs)
    rel_s  = _relationship_density_score(total_rels, connected_count)
    rel_cov_s = _relationship_coverage_score(connected_count, unconnected_count)
    evid_s = _evidence_coverage_score(concepts_with_evidence, total_concepts)
    conn_s = _graph_connectivity_score(rel_cov_s, unique_rel_types, avg_strength)
    pat_s  = _pattern_coverage_score(total_patterns, total_concepts)
    cons_s = _consulting_readiness(evid_s, rel_s, pat_s)
    conf_s = _memory_confidence(ing_s, ext_s, rel_s, evid_s, conn_s, pat_s)

    # ── Deliverable readiness ─────────────────────────────────────────────────
    # Client 101 — basics only; standalone concepts are acceptable
    client_101_ready = (
        complete_docs >= 1
        and total_concepts >= 5
        and _score_to_100(evid_s) >= 50
    )
    # Client 201 — requires relationship depth; standalone workspaces may not qualify
    client_201_ready = (
        complete_docs >= 2
        and total_concepts >= 15
        and total_rels >= 8
        and rel_coverage_pct >= 30.0
        and _score_to_100(evid_s) >= 60
    )
    # Executive Summary — needs evidence + minimal connectivity
    executive_summary_ready = (
        complete_docs >= 1
        and total_concepts >= 5
        and _score_to_100(evid_s) >= 50
        and _score_to_100(rel_s) >= 20
    )

    # ── Per-document records ──────────────────────────────────────────────────
    doc_records = []
    for doc in docs:
        dr = get_document_audit(db, doc.id)
        if dr:
            doc_records.append(dr)

    return WorkspaceAuditReport(
        workspace_id=workspace_id,
        workspace_name=ws.name,
        graph_version=ws.graph_version or 0,
        generated_at=datetime.now(timezone.utc),

        total_documents=total_docs,
        complete_documents=complete_docs,
        failed_documents=failed_docs,
        pending_processing_documents=pending_docs,

        total_chars_ingested=total_chars,
        total_chunks_processed=total_chunks,
        total_pages_processed=total_pages,

        total_concepts=total_concepts,
        total_relationships=total_rels,
        total_patterns=total_patterns,
        concepts_with_evidence=concepts_with_evidence,
        evidence_coverage_pct=evid_pct,

        integrity=integrity,

        ingestion_score=_score_to_100(ing_s),
        extraction_density_score=_score_to_100(ext_s),
        relationship_density_score=_score_to_100(rel_s),
        evidence_coverage_score=_score_to_100(evid_s),
        graph_integrity_score=_score_to_100(conn_s),    # field name kept for API compat
        pattern_coverage_score=_score_to_100(pat_s),
        consulting_readiness_score=_score_to_100(cons_s),
        relationship_coverage_score=_score_to_100(rel_cov_s),

        memory_confidence_score=_score_to_100(conf_s),

        client_101_ready=client_101_ready,
        client_201_ready=client_201_ready,
        executive_summary_ready=executive_summary_ready,

        documents=doc_records,
    )
