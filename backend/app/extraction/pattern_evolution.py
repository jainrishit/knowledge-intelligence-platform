"""
Pattern evolution tracker and threshold engine.

Determines whether pattern extraction should run after a graph update,
based on how much the workspace's knowledge has grown since the last run.
Records each extraction decision as a PatternExtractionRun audit row.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Concept, PatternExtractionRun, Relationship, Workspace

logger = logging.getLogger(__name__)


def should_run_pattern_extraction(db: Session, workspace_id: int) -> bool:
    """
    Return True if the workspace knowledge has grown enough since the last
    pattern extraction run to justify re-running the pattern agent.

    Decision logic
    --------------
    1. If extraction has never run → always run.
    2. Compute growth percentage for both concepts and relationships since
       concept_count_at_last_pattern_run / relationship_count_at_last_pattern_run.
    3. If either metric grew by ≥ PATTERN_EXTRACTION_THRESHOLD_PERCENT → run.
    4. Otherwise skip and log the decision.
    """
    ws = db.get(Workspace, workspace_id)
    if ws is None:
        return False

    current_concept_count = (
        db.query(Concept).filter(Concept.workspace_id == workspace_id).count()
    )
    current_rel_count = (
        db.query(Relationship).filter(Relationship.workspace_id == workspace_id).count()
    )

    prev_concept_count = ws.concept_count_at_last_pattern_run or 0
    prev_rel_count = ws.relationship_count_at_last_pattern_run or 0

    # Always run if extraction has never happened
    if prev_concept_count == 0 and prev_rel_count == 0:
        logger.info(
            "[pattern_evolution] ws=%d — first extraction, running unconditionally "
            "(concepts=%d, relationships=%d).",
            workspace_id, current_concept_count, current_rel_count,
        )
        return True

    threshold = settings.pattern_extraction_threshold_pct

    concept_growth = (
        ((current_concept_count - prev_concept_count) / prev_concept_count * 100)
        if prev_concept_count > 0
        else 100.0
    )
    rel_growth = (
        ((current_rel_count - prev_rel_count) / prev_rel_count * 100)
        if prev_rel_count > 0
        else 0.0
    )

    should_run = concept_growth >= threshold or rel_growth >= threshold

    logger.info(
        "[pattern_evolution] ws=%d — concept growth=%.1f%% (prev=%d, now=%d), "
        "rel growth=%.1f%% (prev=%d, now=%d), threshold=%.1f%% → %s",
        workspace_id,
        concept_growth, prev_concept_count, current_concept_count,
        rel_growth, prev_rel_count, current_rel_count,
        threshold,
        "RUN" if should_run else "SKIP",
    )
    return should_run


def record_pattern_run(
    db: Session,
    workspace_id: int,
    concept_count: int,
    relationship_count: int,
    patterns_generated: int,
    status: str = "complete",
) -> PatternExtractionRun:
    """
    Persist a PatternExtractionRun audit row and update the Workspace
    knowledge-evolution counters so the next threshold check is accurate.
    """
    ws = db.get(Workspace, workspace_id)
    graph_version = ws.graph_version if ws else 0

    now = datetime.now(timezone.utc)
    run = PatternExtractionRun(
        workspace_id=workspace_id,
        started_at=now,
        completed_at=now,
        concept_count=concept_count,
        relationship_count=relationship_count,
        patterns_generated=patterns_generated,
        graph_version=graph_version,
        status=status,
    )
    db.add(run)

    if ws:
        ws.last_pattern_extraction_at = now
        ws.concept_count_at_last_pattern_run = concept_count
        ws.relationship_count_at_last_pattern_run = relationship_count

    db.commit()
    logger.info(
        "[pattern_evolution] ws=%d — run recorded: status=%s concepts=%d rels=%d patterns=%d graph_v=%d",
        workspace_id, status, concept_count, relationship_count, patterns_generated, graph_version,
    )
    return run
