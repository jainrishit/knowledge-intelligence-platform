"""
Spreadsheet concept extractor — structure-aware LLM-assisted extraction.

Unlike the document concept agent (which operates on free text), this agent
is table-aware: it receives structured table blocks (header + rows) so the
LLM can reason about entity types, column roles, and row-level evidence.

Separate system prompts are used for:
  - Generic structured tables
  - Requirements sheets
  - Risk/control sheets
  - Mapping sheets (payments-specific: ISO 20022 / SWIFT / etc.)
  - Data dictionaries / glossaries

For each prompt a source_excerpt is synthesised from the originating row
so every extracted concept carries traceable evidence.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Concept, Document, SPREADSHEET_CONCEPT_TYPES
from app.llm_client import chat

logger = logging.getLogger(__name__)



_BASE_RULES = """\
Rules:
- Extract ONLY concepts explicitly present in the table rows provided.
- source_excerpt MUST be a verbatim row value or column header quote. Never fabricate.
- confidence < 0.6 → exclude entirely.
- Deduplicate by name (case-insensitive).
- Return [] if the table contains no extractable consulting knowledge.
- Return ONLY a JSON array — no prose, no markdown fences.
"""

_GENERIC_SYSTEM_PROMPT = f"""\
You are an AI knowledge compiler for IBM Consulting.
Extract ALL significant named consulting concepts from the structured table provided.
You work strictly from the table content — never introduce external knowledge.

A concept is any NAMED, REUSABLE entity: standards, technologies, processes,
capabilities, regulations, methodologies, frameworks, business terms.

For each concept return:
{{
  "name": "concept name",
  "type": "one of: Requirement, Risk, Issue, Dependency, Process, Capability, Data Element, Control, Business Rule, Standard, Technology, Methodology, Framework, Regulation, Pattern, Tool, General",
  "confidence": 0.0–1.0,
  "description": "1-2 sentence description from the table content",
  "source_excerpt": "verbatim cell content or column header that supports this concept"
}}

{_BASE_RULES}"""

_REQUIREMENTS_SYSTEM_PROMPT = f"""\
You are an AI knowledge compiler for IBM Consulting.
Extract ALL named business and functional requirements from the requirements table provided.

For each row that represents a meaningful requirement return:
{{
  "name": "requirement name (from Name/Title/ID+short description)",
  "type": "Requirement",
  "confidence": 0.0–1.0,
  "description": "full requirement description from the Description column",
  "source_excerpt": "verbatim row content including ID and description"
}}

Also extract any domain concepts, regulations, or systems mentioned in the requirement text.

{_BASE_RULES}"""

_RISK_SYSTEM_PROMPT = f"""\
You are an AI knowledge compiler for IBM Consulting.
Extract ALL named risks, controls, and mitigations from the risk register table provided.

For each row return:
{{
  "name": "risk or control name",
  "type": "Risk or Control",
  "confidence": 0.0–1.0,
  "description": "risk description and mitigation from the table",
  "source_excerpt": "verbatim row content"
}}

Also extract regulations, standards, and business processes referenced as risk drivers or controls.

{_BASE_RULES}"""

_MAPPING_SYSTEM_PROMPT = f"""\
You are an AI knowledge compiler for IBM Consulting, specialising in payments and financial messaging.
Extract ALL named standards, message types, payment rails, and transformation rules from the mapping table.

Focus on:
- ISO 20022 message types (pacs., pain., camt., remt.)
- SWIFT MT/MX messages
- Payment rails (SEPA, FedNow, RTP, CHIPS, FEDWIRE, CHAPS)
- Data elements and field mappings
- Transformation rules and business logic

For each return:
{{
  "name": "standard / message type / payment rail / rule name",
  "type": "Standard, Process, Business Rule, Data Element, or General",
  "confidence": 0.0–1.0,
  "description": "what this element represents in the mapping context",
  "source_excerpt": "verbatim source→target mapping or rule from the table"
}}

{_BASE_RULES}"""

_GLOSSARY_SYSTEM_PROMPT = f"""\
You are an AI knowledge compiler for IBM Consulting.
Extract ALL business terms and definitions from the glossary or data dictionary table.

For each term return:
{{
  "name": "term or field name",
  "type": "Business Term or Data Element",
  "confidence": 0.0–1.0,
  "description": "definition from the Definition/Description column",
  "source_excerpt": "verbatim term + definition from the table"
}}

{_BASE_RULES}"""

_PROMPT_BY_SHEET_TYPE: dict[str, str] = {
    "requirements":       _REQUIREMENTS_SYSTEM_PROMPT,
    "risk_register":      _RISK_SYSTEM_PROMPT,
    "control_inventory":  _RISK_SYSTEM_PROMPT,
    "mapping_sheet":      _MAPPING_SYSTEM_PROMPT,
    "glossary":           _GLOSSARY_SYSTEM_PROMPT,
    "data_dictionary":    _GLOSSARY_SYSTEM_PROMPT,
}




class SpreadsheetConceptItem(BaseModel):
    name: str = Field(..., min_length=1)
    type: str = Field(default="General")
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    description: str = Field(default="")
    source_excerpt: str = Field(default="", min_length=1)




def extract_spreadsheet_concepts(
    db: Session,
    doc: Document,
    table_text: str,
    schema_context: str,
    sheet_type: str,
    domain_hints: list[str],
    table: Any,
) -> list[Concept]:
    """
    Extract concepts from a single table block using a sheet-type-appropriate
    LLM prompt, then persist to the database.

    Parameters
    ----------
    table_text    : structured text rendered by table_detector.table_to_text_block()
    schema_context: workbook schema summary from schema_extractor.schema_to_context()
    sheet_type    : analysis classification (e.g. "requirements", "risk_register")
    domain_hints  : payments-domain terms detected in the sheet
    table         : DetectedTable object (for building evidence excerpts)
    """
    system_prompt = _PROMPT_BY_SHEET_TYPE.get(sheet_type, _GENERIC_SYSTEM_PROMPT)

    domain_note = ""
    if domain_hints:
        domain_note = f"\nDetected payments-domain context: {', '.join(domain_hints)}\n"

    user_content = (
        f"Workbook schema context:\n{schema_context}\n\n"
        f"{domain_note}"
        f"Table to extract from:\n{table_text}"
    )

    raw_items = _call_llm(system_prompt, user_content, doc.id)
    if not raw_items:
        return []

    existing_names = {
        c.name.lower()
        for c in db.query(Concept).filter(Concept.workspace_id == doc.workspace_id).all()
    }

    created: list[Concept] = []
    seen_names: set[str] = set()

    for raw in raw_items:
        try:
            item = SpreadsheetConceptItem(**raw)
        except ValidationError as ve:
            logger.warning("[doc=%d] Rejected spreadsheet concept: %s — %s", doc.id, raw, ve)
            continue

        if item.confidence < settings.concept_confidence_min:
            continue

        # Normalise concept type against allowed spreadsheet types
        item.type = _normalise_type(item.type)

        name_lower = item.name.lower().strip()
        if name_lower in seen_names or name_lower in existing_names:
            continue

        excerpt = item.source_excerpt[:500] if item.source_excerpt else f"[{table.sheet_name}] {item.name}"

        concept = Concept(
            workspace_id=doc.workspace_id,
            name=item.name.strip(),
            type=item.type,
            description=item.description,
            source_document_id=doc.id,
            source_excerpt=excerpt,
            confidence=item.confidence,
        )
        db.add(concept)
        seen_names.add(name_lower)
        existing_names.add(name_lower)
        created.append(concept)

    db.commit()
    logger.info(
        "[doc=%d] Spreadsheet table '%s': extracted %d concepts",
        doc.id, getattr(table, "table_label", "?"), len(created),
    )
    return created




def _call_llm(system: str, user: str, doc_id: int) -> list[dict[str, Any]]:
    """Call LLM and parse JSON. Retries once on parse failure."""
    for attempt in range(2):
        try:
            raw = chat(system=system, user=user, max_tokens=4096)
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw)
        except (json.JSONDecodeError, IndexError) as e:
            if attempt == 0:
                logger.warning("[doc=%d] Spreadsheet concept JSON parse failed (attempt 1): %s", doc_id, e)
            else:
                logger.error("[doc=%d] Spreadsheet concept JSON parse failed after retry: %s", doc_id, e)
                return []
    return []


def _normalise_type(raw_type: str) -> str:
    """Map LLM-returned type string to the canonical SPREADSHEET_CONCEPT_TYPES set."""
    t = raw_type.strip().title()
    if t in SPREADSHEET_CONCEPT_TYPES:
        return t
    # Fuzzy fallback
    t_lower = raw_type.lower()
    if "req" in t_lower:
        return "Requirement"
    if "risk" in t_lower:
        return "Risk"
    if "control" in t_lower:
        return "Control"
    if "process" in t_lower:
        return "Process"
    if "capabilit" in t_lower:
        return "Capability"
    if "data" in t_lower or "field" in t_lower or "element" in t_lower:
        return "Data Element"
    if "rule" in t_lower or "business rule" in t_lower:
        return "Business Rule"
    if "depend" in t_lower:
        return "Dependency"
    if "issue" in t_lower:
        return "Issue"
    if "term" in t_lower or "glossar" in t_lower:
        return "Business Term"
    if "standard" in t_lower:
        return "Standard"
    if "technolog" in t_lower:
        return "Technology"
    return "General"
