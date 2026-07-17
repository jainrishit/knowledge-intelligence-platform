"""
Spreadsheet Intelligence Pipeline.

Parses Excel (.xlsx, .xls) and CSV files as first-class knowledge sources.
Preserves workbook structure — sheets, headers, tables, data regions — so
that LLM extraction operates on structured rows rather than flattened text.

Entry point for all callers:
    from app.ingestion.spreadsheets.spreadsheet_processor import process_spreadsheet
"""
from app.ingestion.spreadsheets.spreadsheet_processor import process_spreadsheet

__all__ = ["process_spreadsheet"]
