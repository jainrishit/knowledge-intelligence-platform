"""
CSV parser — wraps a single CSV file as a single-sheet WorkbookData so that
the rest of the spreadsheet pipeline treats CSV identically to Excel.
Handles common delimiters (comma, tab, semicolon, pipe) via sniffing.
"""
from __future__ import annotations

import csv
import io
import logging

from app.ingestion.spreadsheets.excel_parser import SheetData, WorkbookData, _looks_like_headers

logger = logging.getLogger(__name__)

# Maximum rows read per CSV — prevents memory exhaustion on extremely large files.
_MAX_ROWS = 10_000


def parse_csv(file_bytes: bytes, filename: str = "data.csv") -> WorkbookData:
    """
    Parse a CSV file from raw bytes.

    Raises ValueError on unreadable or non-tabular input.
    Encoding is detected via UTF-8 with BOM, then latin-1 fallback.
    The UTF-8 BOM (U+FEFF) is stripped so column names are clean.
    """
    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = file_bytes.decode("latin-1")
        except UnicodeDecodeError as exc:
            raise ValueError(f"Could not decode CSV '{filename}': {exc}") from exc
    # Strip any residual BOM that may survive codec-specific decoding
    text = text.lstrip("\ufeff")

    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel  # type: ignore[assignment]

    reader = csv.reader(io.StringIO(text), dialect)
    all_rows: list[list[str]] = []
    for i, row in enumerate(reader):
        if i >= _MAX_ROWS:
            logger.warning("CSV '%s' truncated at %d rows.", filename, _MAX_ROWS)
            break
        all_rows.append([c.strip() for c in row])

    if not all_rows:
        sheet = SheetData(
            name="Sheet1", headers=[], rows=[], row_count=0, col_count=0,
            is_empty=True, has_headers=False,
        )
        return WorkbookData(
            name=filename, sheet_count=1, sheets=[sheet], raw_text="",
            metadata={"source_format": "csv"},
        )

    col_count = max(len(r) for r in all_rows)
    has_headers = _looks_like_headers(all_rows[0])
    headers = [c.strip() for c in all_rows[0]] if has_headers else [f"Col{i+1}" for i in range(col_count)]
    data_rows_raw = all_rows[1:] if has_headers else all_rows

    rows: list[dict[str, str]] = []
    for raw in data_rows_raw:
        padded = raw + [""] * (len(headers) - len(raw))
        row_dict = {h: v for h, v in zip(headers, padded) if h and v}
        if row_dict:
            rows.append(row_dict)

    # Synthesise readable text
    text_parts = [f"[Sheet: {filename}]"]
    if headers:
        text_parts.append("Headers: " + " | ".join(h for h in headers if h))
    for i, row in enumerate(rows[:200]):
        row_str = " | ".join(f"{k}: {v}" for k, v in row.items())
        if row_str:
            text_parts.append(f"  Row {i+1}: {row_str}")
    if len(rows) > 200:
        text_parts.append(f"  ... ({len(rows) - 200} additional rows not shown)")

    sheet = SheetData(
        name=filename,
        headers=headers,
        rows=rows,
        row_count=len(rows),
        col_count=col_count,
        is_empty=(len(rows) == 0),
        has_headers=has_headers,
    )

    return WorkbookData(
        name=filename,
        sheet_count=1,
        sheets=[sheet],
        raw_text="\n".join(text_parts),
        metadata={"source_format": "csv", "sheet_names": [filename]},
    )
