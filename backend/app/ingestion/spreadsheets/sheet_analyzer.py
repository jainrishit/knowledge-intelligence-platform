"""
Sheet analyser — classifies each worksheet by its apparent content type
and extracts a structured summary used to guide concept extraction.

Classification is heuristic-first (header pattern matching), with no LLM
call required at this stage. The richer classification hints are passed to
the concept extractor so the LLM can apply domain-appropriate prompting.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.ingestion.spreadsheets.excel_parser import SheetData

# Known sheet types and their canonical header keyword sets.
# Order matters: more specific patterns are checked first.
_SHEET_TYPE_PATTERNS: list[tuple[str, set[str]]] = [
    ("requirements",  {"requirement", "req id", "req_id", "requirement id", "description", "priority", "status", "domain", "owner", "functional", "non-functional"}),
    ("risk_register",  {"risk", "risk id", "likelihood", "impact", "mitigation", "status", "owner", "control", "residual"}),
    ("issue_log",      {"issue", "issue id", "raised by", "raised date", "resolution", "status", "priority", "assignee"}),
    ("dependency_map", {"dependency", "depends on", "predecessor", "successor", "linked", "impact"}),
    ("data_dictionary",{"field", "column", "data type", "description", "nullable", "primary key", "foreign key", "format", "example"}),
    ("capability_catalog", {"capability", "capability id", "maturity", "owner", "status", "business unit", "level"}),
    ("process_inventory", {"process", "process id", "activity", "actor", "system", "trigger", "outcome", "step"}),
    ("control_inventory",  {"control", "control id", "objective", "frequency", "type", "automated", "manual", "regulation"}),
    ("mapping_sheet",  {"source", "target", "mapping", "transformation", "rule", "direction", "iso 20022", "mt message", "mx message"}),
    ("glossary",       {"term", "definition", "abbreviation", "acronym", "category", "source"}),
    ("raid_log",       {"raid", "assumption", "issue", "dependency", "description", "owner", "status", "due date"}),
]


@dataclass
class SheetAnalysis:
    """Analysis result for a single worksheet."""
    sheet_name: str
    sheet_type: str                        # e.g. "requirements", "risk_register", "generic"
    confidence: float                      # 0.0–1.0 type classification confidence
    key_columns: list[str]                 # columns most likely to carry concept names
    domain_hints: list[str]               # payments-domain terms detected in headers/values
    row_count: int
    is_useful: bool                        # False for empty or purely numeric sheets


def analyse_sheet(sheet: SheetData) -> SheetAnalysis:
    """
    Classify a sheet and identify its most informative columns.
    """
    if sheet.is_empty or sheet.row_count == 0:
        return SheetAnalysis(
            sheet_name=sheet.name, sheet_type="empty", confidence=1.0,
            key_columns=[], domain_hints=[], row_count=0, is_useful=False,
        )

    header_lower = {h.lower().strip() for h in sheet.headers}
    sheet_type, type_confidence = _classify_sheet_type(header_lower)
    key_columns = _identify_key_columns(sheet.headers, sheet_type)
    domain_hints = _detect_domain_hints(sheet)

    return SheetAnalysis(
        sheet_name=sheet.name,
        sheet_type=sheet_type,
        confidence=type_confidence,
        key_columns=key_columns,
        domain_hints=domain_hints,
        row_count=sheet.row_count,
        is_useful=True,
    )


def analyse_workbook(sheets: list[SheetData]) -> list[SheetAnalysis]:
    """Analyse all sheets in a workbook and return per-sheet analysis results."""
    return [analyse_sheet(s) for s in sheets]




def _classify_sheet_type(header_lower: set[str]) -> tuple[str, float]:
    best_type = "generic"
    best_score = 0.0
    for sheet_type, keywords in _SHEET_TYPE_PATTERNS:
        overlap = len(header_lower & keywords)
        if overlap == 0:
            continue
        score = overlap / max(len(keywords) * 0.3, 1)   # normalise; partial match is fine
        score = min(score, 1.0)
        if score > best_score:
            best_score = score
            best_type = sheet_type
    return best_type, round(best_score, 3)


def _identify_key_columns(headers: list[str], sheet_type: str) -> list[str]:
    """
    Return the column names most likely to carry meaningful concept names.
    For a requirements sheet, 'Description' and 'Domain' are key.
    For a data dictionary, 'Field' and 'Description' are key.
    """
    key_patterns = {
        "requirements":      ["description", "requirement", "domain", "name", "title"],
        "risk_register":     ["risk", "description", "mitigation", "control"],
        "issue_log":         ["issue", "description", "resolution"],
        "dependency_map":    ["dependency", "description", "depends on"],
        "data_dictionary":   ["field", "column", "description", "data type"],
        "capability_catalog":["capability", "description", "business unit"],
        "process_inventory": ["process", "activity", "description"],
        "control_inventory": ["control", "objective", "regulation"],
        "mapping_sheet":     ["source", "target", "rule", "description"],
        "glossary":          ["term", "definition"],
        "raid_log":          ["description", "assumption", "issue", "dependency"],
        "generic":           ["name", "description", "title", "label", "value"],
    }
    patterns = key_patterns.get(sheet_type, key_patterns["generic"])
    key: list[str] = []
    for h in headers:
        h_lower = h.lower().strip()
        for pattern in patterns:
            if pattern in h_lower:
                key.append(h)
                break
    return key or headers[:3]  # fallback to first 3 if nothing matches


# Payments-domain terminology to detect in sheet content
_PAYMENTS_TERMS = {
    "swift", "sepa", "fedwire", "fednow", "rtp", "chips", "chaps", "bacs",
    "iso 20022", "cbpr+", "mx message", "mt message", "pain.", "camt.", "pacs.",
    "settlement", "clearing", "netting", "nostro", "vostro", "correspondent",
    "liquidity", "treasury", "intraday", "daylight overdraft",
    "aml", "kyc", "sanctions", "fraud", "pep", "ofac",
    "cbdc", "tokenized", "tokenised", "digital asset", "stablecoin",
    "open banking", "psd2", "api banking",
}


def _detect_domain_hints(sheet: SheetData) -> list[str]:
    """
    Scan headers and a sample of cell values for known payments-domain terms.
    Returns a deduplicated list of detected terms.
    """
    detected: set[str] = set()
    candidate_text = " ".join(sheet.headers).lower()
    for row in sheet.rows[:50]:
        candidate_text += " " + " ".join(str(v) for v in row.values()).lower()

    for term in _PAYMENTS_TERMS:
        if term in candidate_text:
            detected.add(term)
    return sorted(detected)
