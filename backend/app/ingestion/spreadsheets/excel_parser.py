"""
Excel workbook parser — extracts structured sheet data from .xlsx and .xls files.

Returns a WorkbookData object that preserves the workbook's sheet-level
structure (headers, data rows, merged regions) for downstream analysis.
Raw text is also synthesised for compatibility with the existing concept
extraction pipeline.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class SheetData:
    """Structured representation of a single worksheet."""
    name: str
    headers: list[str]
    rows: list[dict[str, str]]          # list of {header: cell_value} dicts
    row_count: int
    col_count: int
    is_empty: bool
    has_headers: bool


@dataclass
class WorkbookData:
    """Structured representation of a parsed workbook."""
    name: str
    sheet_count: int
    sheets: list[SheetData]
    raw_text: str                        # synthesised plain-text for LLM context
    metadata: dict = field(default_factory=dict)


def parse_excel(file_bytes: bytes, filename: str = "workbook.xlsx") -> WorkbookData:
    """
    Parse an Excel workbook (.xlsx or .xls) from raw bytes.

    Raises ValueError on corrupt or unreadable files.
    Supports both .xlsx (openpyxl) and .xls (xlrd) via format sniffing.
    """
    # Sniff format: xlsx starts with PK (zip), xls starts with D0CF (OLE compound)
    is_xls = len(file_bytes) >= 8 and file_bytes[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

    try:
        if is_xls:
            return _parse_xls(file_bytes, filename)
        return _parse_xlsx(file_bytes, filename)
    except Exception as exc:
        logger.error("Excel parsing failed for '%s': %s", filename, exc)
        raise ValueError(f"Could not parse Excel file '{filename}': {exc}") from exc




def _parse_xlsx(file_bytes: bytes, filename: str) -> WorkbookData:
    import io
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    sheets: list[SheetData] = []

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        sheet = _extract_sheet_openpyxl(ws, sheet_name)
        sheets.append(sheet)
        logger.debug("Parsed xlsx sheet '%s': %d rows × %d cols", sheet_name, sheet.row_count, sheet.col_count)

    wb.close()
    raw_text = _synthesise_text(sheets)
    return WorkbookData(
        name=filename,
        sheet_count=len(sheets),
        sheets=sheets,
        raw_text=raw_text,
        metadata={"source_format": "xlsx", "sheet_names": [s.name for s in sheets]},
    )


def _extract_sheet_openpyxl(ws, sheet_name: str) -> SheetData:
    all_rows: list[list[str]] = []
    for row in ws.iter_rows(values_only=True):
        str_row = [str(cell) if cell is not None else "" for cell in row]
        all_rows.append(str_row)

    if not all_rows:
        return SheetData(
            name=sheet_name, headers=[], rows=[], row_count=0, col_count=0,
            is_empty=True, has_headers=False,
        )

    # Trim completely empty trailing rows
    while all_rows and all(c == "" for c in all_rows[-1]):
        all_rows.pop()

    if not all_rows:
        return SheetData(
            name=sheet_name, headers=[], rows=[], row_count=0, col_count=0,
            is_empty=True, has_headers=False,
        )

    col_count = max(len(r) for r in all_rows)
    has_headers = _looks_like_headers(all_rows[0])
    headers = [c.strip() for c in all_rows[0]] if has_headers else [f"Col{i+1}" for i in range(col_count)]
    data_rows_raw = all_rows[1:] if has_headers else all_rows

    # Build dicts — pad short rows with empty strings
    rows: list[dict[str, str]] = []
    for raw in data_rows_raw:
        padded = raw + [""] * (len(headers) - len(raw))
        row_dict = {h: str(v).strip() for h, v in zip(headers, padded) if h}
        if any(v for v in row_dict.values()):   # skip fully empty rows
            rows.append(row_dict)

    return SheetData(
        name=sheet_name,
        headers=headers,
        rows=rows,
        row_count=len(rows),
        col_count=col_count,
        is_empty=(len(rows) == 0),
        has_headers=has_headers,
    )




def _parse_xls(file_bytes: bytes, filename: str) -> WorkbookData:
    import xlrd

    wb = xlrd.open_workbook(file_contents=file_bytes)
    sheets: list[SheetData] = []

    for sheet_name in wb.sheet_names():
        ws = wb.sheet_by_name(sheet_name)
        sheet = _extract_sheet_xlrd(ws, sheet_name)
        sheets.append(sheet)
        logger.debug("Parsed xls sheet '%s': %d rows × %d cols", sheet_name, sheet.row_count, sheet.col_count)

    raw_text = _synthesise_text(sheets)
    return WorkbookData(
        name=filename,
        sheet_count=len(sheets),
        sheets=sheets,
        raw_text=raw_text,
        metadata={"source_format": "xls", "sheet_names": [s.name for s in sheets]},
    )


def _extract_sheet_xlrd(ws, sheet_name: str) -> SheetData:
    all_rows: list[list[str]] = []
    for row_idx in range(ws.nrows):
        row = [str(ws.cell(row_idx, col_idx).value).strip() for col_idx in range(ws.ncols)]
        all_rows.append(row)

    if not all_rows:
        return SheetData(
            name=sheet_name, headers=[], rows=[], row_count=0, col_count=0,
            is_empty=True, has_headers=False,
        )

    col_count = max(len(r) for r in all_rows)
    has_headers = _looks_like_headers(all_rows[0])
    headers = [c.strip() for c in all_rows[0]] if has_headers else [f"Col{i+1}" for i in range(col_count)]
    data_rows_raw = all_rows[1:] if has_headers else all_rows

    rows: list[dict[str, str]] = []
    for raw in data_rows_raw:
        padded = raw + [""] * (len(headers) - len(raw))
        row_dict = {h: str(v).strip() for h, v in zip(headers, padded) if h}
        if any(v for v in row_dict.values()):
            rows.append(row_dict)

    return SheetData(
        name=sheet_name,
        headers=headers,
        rows=rows,
        row_count=len(rows),
        col_count=col_count,
        is_empty=(len(rows) == 0),
        has_headers=has_headers,
    )




def _looks_like_headers(row: list[str]) -> bool:
    """
    Heuristic: a row is a header row when the majority of cells are non-empty
    short strings (≤ 80 chars) and the row contains no purely numeric cells.
    """
    if not row or all(c == "" for c in row):
        return False
    non_empty = [c for c in row if c.strip()]
    if not non_empty:
        return False
    numeric_count = sum(1 for c in non_empty if _is_numeric(c))
    short_text_count = sum(1 for c in non_empty if len(c) <= 80 and not _is_numeric(c))
    return short_text_count >= len(non_empty) * 0.6 and numeric_count < len(non_empty) * 0.4


def _is_numeric(value: str) -> bool:
    try:
        float(value.replace(",", "").replace("%", ""))
        return True
    except ValueError:
        return False


def _synthesise_text(sheets: list[SheetData]) -> str:
    """
    Produce a human-readable plain-text representation of all sheets.
    Used as fallback context for the LLM when structured extraction is insufficient.
    """
    parts: list[str] = []
    for sheet in sheets:
        if sheet.is_empty:
            continue
        parts.append(f"[Sheet: {sheet.name}]")
        if sheet.headers:
            parts.append("Headers: " + " | ".join(h for h in sheet.headers if h))
        for i, row in enumerate(sheet.rows[:200]):   # cap at 200 rows for text synthesis
            row_text = " | ".join(f"{k}: {v}" for k, v in row.items() if v)
            if row_text:
                parts.append(f"  Row {i+1}: {row_text}")
        if len(sheet.rows) > 200:
            parts.append(f"  ... ({len(sheet.rows) - 200} additional rows not shown)")
        parts.append("")
    return "\n".join(parts)
