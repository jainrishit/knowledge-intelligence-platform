"""
Schema extractor — derives structured schema facts from a spreadsheet without
relying solely on LLM inference.

Heuristics extract:
  - Entity types from column names (e.g. "Requirement ID" → entity type Requirement)
  - Column taxonomies (identifier, descriptor, status, owner, domain, reference)
  - Cross-sheet relationships when columns share domain vocabulary
  - Business objects implied by header patterns

Results feed the concept extractor to enrich the LLM prompt with structured
prior knowledge rather than asking the LLM to infer everything from raw text.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.ingestion.spreadsheets.excel_parser import WorkbookData, SheetData
from app.ingestion.spreadsheets.sheet_analyzer import SheetAnalysis

# Column role taxonomy patterns
_IDENTIFIER_PATTERNS  = {"id", "identifier", "ref", "reference", "number", "code", "key"}
_DESCRIPTOR_PATTERNS  = {"name", "title", "description", "summary", "detail", "notes", "comment"}
_STATUS_PATTERNS      = {"status", "state", "stage", "phase", "progress"}
_OWNER_PATTERNS       = {"owner", "assignee", "responsible", "accountable", "team", "author"}
_DOMAIN_PATTERNS      = {"domain", "area", "category", "type", "classification", "workstream"}
_REFERENCE_PATTERNS   = {"related", "linked", "maps to", "references", "dependency", "parent"}


@dataclass
class ColumnSchema:
    name: str
    role: str       # "identifier" | "descriptor" | "status" | "owner" | "domain" | "reference" | "value"
    sample_values: list[str] = field(default_factory=list)


@dataclass
class SheetSchema:
    sheet_name: str
    sheet_type: str
    entity_type: str          # the primary entity this sheet represents (e.g. "Requirement")
    columns: list[ColumnSchema]
    cross_sheet_refs: list[str]   # other sheet names this sheet references


@dataclass
class WorkbookSchema:
    workbook_name: str
    sheets: list[SheetSchema]
    shared_entity_types: list[str]   # entity types appearing in ≥2 sheets → candidate cross-sheet rels


def extract_schema(workbook: WorkbookData, analyses: list[SheetAnalysis]) -> WorkbookSchema:
    """Derive the structural schema of a workbook from parsed data + analysis results."""
    sheet_schemas: list[SheetSchema] = []
    analysis_by_name = {a.sheet_name: a for a in analyses}

    for sheet in workbook.sheets:
        analysis = analysis_by_name.get(sheet.name)
        if analysis is None or not analysis.is_useful:
            continue
        schema = _extract_sheet_schema(sheet, analysis)
        sheet_schemas.append(schema)

    entity_type_counts: dict[str, int] = {}
    for ss in sheet_schemas:
        entity_type_counts[ss.entity_type] = entity_type_counts.get(ss.entity_type, 0) + 1
    shared = [et for et, count in entity_type_counts.items() if count >= 2]

    return WorkbookSchema(
        workbook_name=workbook.name,
        sheets=sheet_schemas,
        shared_entity_types=shared,
    )


def schema_to_context(schema: WorkbookSchema) -> str:
    """
    Render the WorkbookSchema as a concise text block for inclusion in the
    LLM extraction prompt — gives the model structural prior knowledge.
    """
    lines = [f"Workbook: {schema.workbook_name}"]
    if schema.shared_entity_types:
        lines.append(f"Cross-sheet entities: {', '.join(schema.shared_entity_types)}")
    for ss in schema.sheets:
        identifiers = [c.name for c in ss.columns if c.role == "identifier"]
        descriptors = [c.name for c in ss.columns if c.role == "descriptor"]
        lines.append(
            f"  Sheet '{ss.sheet_name}' ({ss.sheet_type}): entity={ss.entity_type}"
            + (f", IDs=[{', '.join(identifiers)}]" if identifiers else "")
            + (f", descriptions=[{', '.join(descriptors)}]" if descriptors else "")
        )
    return "\n".join(lines)




def _extract_sheet_schema(sheet: SheetData, analysis: SheetAnalysis) -> SheetSchema:
    columns = [_classify_column(h, sheet) for h in sheet.headers if h]
    entity_type = _infer_entity_type(analysis.sheet_type, sheet.headers)
    cross_refs = _find_cross_sheet_refs(sheet)
    return SheetSchema(
        sheet_name=sheet.name,
        sheet_type=analysis.sheet_type,
        entity_type=entity_type,
        columns=columns,
        cross_sheet_refs=cross_refs,
    )


def _classify_column(header: str, sheet: SheetData) -> ColumnSchema:
    h_lower = header.lower().strip()
    role = "value"
    for pattern in _IDENTIFIER_PATTERNS:
        if pattern in h_lower:
            role = "identifier"; break
    if role == "value":
        for pattern in _DESCRIPTOR_PATTERNS:
            if pattern in h_lower:
                role = "descriptor"; break
    if role == "value":
        for pattern in _STATUS_PATTERNS:
            if pattern in h_lower:
                role = "status"; break
    if role == "value":
        for pattern in _OWNER_PATTERNS:
            if pattern in h_lower:
                role = "owner"; break
    if role == "value":
        for pattern in _DOMAIN_PATTERNS:
            if pattern in h_lower:
                role = "domain"; break
    if role == "value":
        for pattern in _REFERENCE_PATTERNS:
            if pattern in h_lower:
                role = "reference"; break

    samples = [row[header] for row in sheet.rows[:5] if header in row and row[header]]
    return ColumnSchema(name=header, role=role, sample_values=samples)


_ENTITY_TYPE_MAP: dict[str, str] = {
    "requirements":       "Requirement",
    "risk_register":      "Risk",
    "issue_log":          "Issue",
    "dependency_map":     "Dependency",
    "data_dictionary":    "Data Element",
    "capability_catalog": "Capability",
    "process_inventory":  "Process",
    "control_inventory":  "Control",
    "mapping_sheet":      "Mapping",
    "glossary":           "Business Term",
    "raid_log":           "RAID Item",
    "generic":            "General",
}


def _infer_entity_type(sheet_type: str, headers: list[str]) -> str:
    mapped = _ENTITY_TYPE_MAP.get(sheet_type)
    if mapped:
        return mapped
    # Fallback: look for a singular noun in the first header
    if headers:
        return headers[0].strip().title() or "General"
    return "General"


def _find_cross_sheet_refs(sheet: SheetData) -> list[str]:
    """Detect column names that suggest references to other sheets (e.g. 'Related Requirements')."""
    refs: list[str] = []
    for header in sheet.headers:
        h_lower = header.lower()
        for pattern in _REFERENCE_PATTERNS:
            if pattern in h_lower:
                refs.append(header)
                break
    return refs
