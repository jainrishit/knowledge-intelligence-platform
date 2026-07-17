"""
Centralised upload validation layer.

Validation order for every uploaded file:
  1. Read the first 8 KB for magic-byte detection.
  2. Identify the true MIME type from magic bytes — extension and Content-Type
     headers are attacker-controlled and are not trusted.
  3. Reject if the MIME type is not in the allow-list.
  4. Read the remainder and enforce the MAX_UPLOAD_BYTES size limit.
"""
from __future__ import annotations

import logging
import os
from typing import NamedTuple

import filetype
from fastapi import HTTPException, UploadFile

from app.config import settings

logger = logging.getLogger(__name__)

_ALLOWED_MIME_TYPES: dict[str, str] = {
    "application/pdf":   "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document":    "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation":  "pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":          "xlsx",
    "application/vnd.ms-excel":                                                    "xls",
    "text/plain":                                                                   "csv",
    "text/csv":                                                                     "csv",
}

# CSV files carry no binary magic bytes; they are identified by file extension.
_CSV_EXTENSIONS = {".csv"}

_MAGIC_READ_BYTES = 8192


class ValidationResult(NamedTuple):
    file_type: str   # 'pdf' | 'docx' | 'pptx' | 'xlsx' | 'xls' | 'csv'
    content: bytes   # full file bytes, ready for the parser


async def validate_upload(file: UploadFile) -> ValidationResult:
    """
    Validate a single uploaded file and return its content.
    Raises HTTPException (413 or 422) on any violation.
    """
    filename = file.filename or "(unknown)"

    first_chunk = await file.read(_MAGIC_READ_BYTES)
    if not first_chunk:
        raise HTTPException(status_code=422, detail=f"'{filename}' is empty.")

    ext = os.path.splitext(filename)[1].lower()
    kind = filetype.guess(first_chunk)
    detected_mime = kind.mime if kind else None

    if detected_mime is None and ext in _CSV_EXTENSIONS:
        detected_mime = "text/plain"

    file_type = _ALLOWED_MIME_TYPES.get(detected_mime or "")
    if file_type is None:
        logger.warning(
            "Upload rejected — unsupported magic-byte MIME '%s' for file '%s'",
            detected_mime, filename,
        )
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unsupported file type detected for '{filename}'. "
                "Accepted formats: PDF, DOCX, PPTX, XLSX, XLS, CSV. "
                f"(Detected: {detected_mime or 'unknown'})"
            ),
        )

    max_bytes = settings.upload_max_bytes
    remaining = await file.read(max_bytes)
    full_content = first_chunk + remaining

    # Check size: if the full content already exceeds the limit, or if there is
    # more data still in the stream, reject immediately.
    if len(full_content) > max_bytes:
        max_mb = max_bytes / (1024 * 1024)
        raise HTTPException(
            status_code=413,
            detail=f"'{filename}' exceeds the maximum upload size of {max_mb:.0f} MB.",
        )
    overflow = await file.read(1)
    if overflow:
        max_mb = max_bytes / (1024 * 1024)
        raise HTTPException(
            status_code=413,
            detail=f"'{filename}' exceeds the maximum upload size of {max_mb:.0f} MB.",
        )

    logger.debug(
        "Upload validated: '%s' — type=%s size=%d bytes",
        filename, file_type, len(full_content),
    )
    return ValidationResult(file_type=file_type, content=full_content)
