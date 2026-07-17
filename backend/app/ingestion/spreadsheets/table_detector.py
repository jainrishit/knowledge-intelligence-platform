"""
Table detector — identifies structured tables within a worksheet and extracts
them as typed record sets.

A table is a contiguous region with a header row followed by data rows.
Detection is already handled at parse time (excel_parser.py / csv_parser.py).
This module provides higher-level table labelling and record sampling used
by the concept extractor to build evidence excerpts.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.ingestion.spreadsheets.excel_parser import SheetData
from app.ingestion.spreadsheets.sheet_analyzer import SheetAnalysis


@dataclass
class DetectedTable:
    """A labelled, bounded table extracted from a sheet."""
    sheet_name: str
    table_label: str           # human-readable label, e.g. "Requirements (Sheet: Reqs)"
    headers: list[str]
    rows: list[dict[str, str]]
    row_count: int
    key_columns: list[str]     # columns that carry primary concept names / IDs


def detect_tables(sheet: SheetData, analysis: SheetAnalysis) -> list[DetectedTable]:
    """
    Build a DetectedTable from a SheetData/SheetAnalysis pair.

    For now each non-empty worksheet is treated as a single table.  More
    granular sub-table detection (e.g. multiple logical tables on one sheet,
    separated by blank rows) can be added here without changing the interface.
    """
    if not analysis.is_useful or sheet.is_empty:
        return []

    label = f"{analysis.sheet_type.replace('_', ' ').title()} (Sheet: {sheet.name})"
    return [
        DetectedTable(
            sheet_name=sheet.name,
            table_label=label,
            headers=sheet.headers,
            rows=sheet.rows,
            row_count=sheet.row_count,
            key_columns=analysis.key_columns,
        )
    ]


def table_to_text_block(table: DetectedTable, max_rows: int = 100) -> str:
    """
    Render a DetectedTable as a structured text block suitable for inclusion
    in an LLM prompt.  Includes a header line, column list, and sampled rows.
    """
    lines = [
        f"[Table: {table.table_label}]",
        f"Columns: {' | '.join(table.headers)}",
    ]
    for row in table.rows[:max_rows]:
        row_str = " | ".join(f"{k}: {v}" for k, v in row.items() if v)
        if row_str:
            lines.append(f"  {row_str}")
    if table.row_count > max_rows:
        lines.append(f"  ... ({table.row_count - max_rows} more rows)")
    return "\n".join(lines)


def build_evidence_excerpt(table: DetectedTable, row: dict[str, str]) -> str:
    """
    Build a traceable source excerpt for a single row, linking back to the
    table and sheet.  Used as `source_excerpt` on extracted Concept rows.
    """
    key_parts = [f"{k}: {row[k]}" for k in table.key_columns if k in row and row[k]]
    all_parts = [f"{k}: {v}" for k, v in row.items() if v]
    preview = "; ".join(key_parts or all_parts[:4])
    return f"[{table.table_label}] {preview}"
