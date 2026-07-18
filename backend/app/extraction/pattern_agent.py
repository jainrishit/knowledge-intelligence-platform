"""
Pattern extraction agent — uses the ICA LLM client.
Given a workspace's full concept + relationship context,
identifies recurring consulting patterns.
"""
from __future__ import annotations
import json
import logging
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session
from thefuzz import process as fuzzy_process

from app.db.models import Concept, Document, Relationship, ConsultingPattern
from app.llm_client import chat

logger = logging.getLogger(__name__)

PATTERN_SYSTEM_PROMPT = """\
You are an AI knowledge compiler for IBM Consulting.
Your role is to discover recurring consulting patterns from compiled workspace knowledge.
Given a list of concepts and their relationships extracted from client and practice documents, identify recurring CONSULTING PATTERNS — repeatable approaches IBM uses to solve a class of client problem.
You work strictly from the provided concepts and relationships — no external knowledge, no internet search, no fabrication.

Return ONLY a JSON array — no prose, no markdown fences:
[
  {
    "name": "short pattern name, e.g. 'ISO 20022 Migration Pattern'",
    "problem_statement": "1-2 sentence description of the class of client problem this pattern addresses",
    "ibm_approach": [
      "Step 1: ...",
      "Step 2: ...",
      "Step 3: ..."
    ],
    "related_concept_names": ["concept name 1", "concept name 2"],
    "source_document_ids": []
  }
]

Rules:
- Only extract patterns that are genuinely supported by the concept/relationship data provided.
- ibm_approach must have at least 2 steps.
- related_concept_names must reference concept names from the provided list.
- If no clear patterns emerge, return an empty array [].
"""


class PatternItem(BaseModel):
    name: str = Field(..., min_length=1)
    problem_statement: str = Field(default="")
    ibm_approach: list[str] = Field(default_factory=list)
    related_concept_names: list[str] = Field(default_factory=list)
    source_document_ids: list[int] = Field(default_factory=list)


def _call_llm(context_text: str) -> list[dict[str, Any]]:
    for attempt in range(2):
        try:
            raw = chat(
                system=PATTERN_SYSTEM_PROMPT,
                user=context_text,
                # 8192 — raised from 4096; workspace pattern context can be large
                # and was truncating the JSON array of patterns mid-object.
                max_tokens=8192,
            )
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw)
        except (json.JSONDecodeError, IndexError) as e:
            if attempt == 0:
                logger.warning("Pattern extraction JSON parse failed (attempt 1): %s", e)
            else:
                logger.error("Pattern extraction JSON parse failed after retry: %s", e)
                return []
    return []


# Cap lines sent to LLM to keep context bounded on large workspaces.
# The LLM receives the most recent / most central concepts; older ones are less
# likely to generate novel patterns.
_MAX_CONCEPT_LINES  = 120
_MAX_REL_LINES      = 200


def extract_patterns(db: Session, doc: Document) -> list[ConsultingPattern]:
    """Extract consulting patterns for the workspace after document ingestion."""
    all_concepts = (
        db.query(Concept)
        .filter(Concept.workspace_id == doc.workspace_id)
        .order_by(Concept.id.desc())   # most recent first
        .all()
    )
    if not all_concepts:
        return []

    all_relationships = (
        db.query(Relationship)
        .filter(Relationship.workspace_id == doc.workspace_id)
        .order_by(Relationship.strength.desc())   # strongest first
        .all()
    )

    # Cap to avoid unbounded context on large workspaces
    concept_lines = [
        f"- {c.name} ({c.type}): {c.description}"
        for c in all_concepts[:_MAX_CONCEPT_LINES]
    ]
    concept_id_to_name = {c.id: c.name for c in all_concepts}
    rel_lines = []
    for r in all_relationships[:_MAX_REL_LINES]:
        src = concept_id_to_name.get(r.source_concept_id, "?")
        tgt = concept_id_to_name.get(r.target_concept_id, "?")
        rel_lines.append(f"- {src} {r.relationship_type} {tgt}")

    context = (
        "Workspace Concepts:\n" + "\n".join(concept_lines) +
        "\n\nWorkspace Relationships:\n" + ("\n".join(rel_lines) or "(none yet)") +
        f"\n\nMost recently processed document: {doc.title or doc.filename}"
    )

    raw_items = _call_llm(context)
    name_to_concept = {c.name.lower(): c for c in all_concepts}
    # Pre-compute the keys list once — fuzzy_process.extractOne() would otherwise
    # call list() on the dict keys on every single concept name lookup.
    concept_name_keys = list(name_to_concept.keys())
    created: list[ConsultingPattern] = []

    for raw in raw_items:
        try:
            item = PatternItem(**raw)
        except ValidationError as ve:
            logger.warning("Rejected pattern: %s — %s", raw, ve)
            continue

        if len(item.ibm_approach) < 2:
            continue

        related_ids: list[int] = []
        for cname in item.related_concept_names:
            result = fuzzy_process.extractOne(cname.lower(), concept_name_keys)
            if result and result[1] >= 70:
                related_ids.append(name_to_concept[result[0]].id)

        existing = (
            db.query(ConsultingPattern)
            .filter(
                ConsultingPattern.workspace_id == doc.workspace_id,
                ConsultingPattern.name == item.name,
            )
            .first()
        )

        doc_ids = list({doc.id} | {did for did in item.source_document_ids if isinstance(did, int)})

        if existing:
            existing.problem_statement = item.problem_statement
            existing.ibm_approach = item.ibm_approach
            existing.related_concept_ids = related_ids
            existing.source_document_ids = doc_ids
        else:
            pattern = ConsultingPattern(
                workspace_id=doc.workspace_id,
                name=item.name,
                problem_statement=item.problem_statement,
                ibm_approach=item.ibm_approach,
                related_concept_ids=related_ids,
                source_document_ids=doc_ids,
            )
            db.add(pattern)
            created.append(pattern)

    db.commit()
    logger.info("[doc=%d] Extracted/updated %d patterns.", doc.id, len(created))
    return created
