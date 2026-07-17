"""
Upload security tests — file size limits, magic-byte MIME validation,
spoofing detection, parser hardening, and memory-safety ordering.

All tests are self-contained and require no live API or LLM calls.
"""
from __future__ import annotations
import io
import zipfile

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import UploadFile


# ── Helpers ───────────────────────────────────────────────────────────

def _make_pdf_bytes(text: str = "ISO 20022 is a payment messaging standard.") -> bytes:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 72), text)
    return doc.tobytes()


def _make_docx_bytes() -> bytes:
    from docx import Document
    buf = io.BytesIO()
    Document().save(buf)
    return buf.getvalue()


def _make_pptx_bytes() -> bytes:
    from pptx import Presentation
    buf = io.BytesIO()
    Presentation().save(buf)
    return buf.getvalue()


def _make_zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("evil.txt", "not a real document")
    return buf.getvalue()


def _make_exe_bytes() -> bytes:
    """Minimal MZ (Windows PE) header — classic executable signature."""
    return b"MZ" + b"\x00" * 100


def _upload_file(content: bytes, filename: str = "test.pdf") -> UploadFile:
    """Build a minimal UploadFile whose async read() returns content in two calls."""
    CHUNK = 8192
    first = content[:CHUNK]
    rest  = content[CHUNK:]
    # simulate the three sequential reads: first_chunk, remaining, overflow check
    uf = MagicMock(spec=UploadFile)
    uf.filename = filename
    uf.read = AsyncMock(side_effect=[first, rest, b""])
    return uf


def _oversize_upload_file(size_bytes: int, filename: str = "big.pdf") -> UploadFile:
    """
    Simulate a file larger than size_bytes. The third read() returns b"\x01"
    to signal that there is still data beyond the limit — triggering the 413.
    """
    pdf_header = _make_pdf_bytes()  # valid magic bytes in the first chunk
    CHUNK = 8192
    first = pdf_header[:CHUNK]
    rest  = b"\x00" * (size_bytes - CHUNK)
    uf = MagicMock(spec=UploadFile)
    uf.filename = filename
    # read calls: first_chunk, bulk_read, overflow_check
    uf.read = AsyncMock(side_effect=[first, rest, b"\x01"])
    return uf


# ══════════════════════════════════════════════════════════════════════
# FILE SIZE VALIDATION
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_small_file_accepted():
    """1 MB PDF must pass size and type validation."""
    from app.core.upload_validation import validate_upload
    content = _make_pdf_bytes("small file test")
    uf = _upload_file(content, "small.pdf")
    result = await validate_upload(uf)
    assert result.file_type == "pdf"
    assert result.content == content


@pytest.mark.asyncio
async def test_file_at_limit_accepted():
    """A file exactly at the 25 MB limit must be accepted."""
    from app.core.upload_validation import validate_upload
    from app.config import settings

    pdf_header = _make_pdf_bytes("at limit")
    CHUNK = 8192
    first = pdf_header[:CHUNK]
    # remaining = exactly max_bytes - CHUNK bytes (no overflow)
    remaining = b"\x00" * (settings.upload_max_bytes - CHUNK)

    uf = MagicMock(spec=UploadFile)
    uf.filename = "at_limit.pdf"
    uf.read = AsyncMock(side_effect=[first, remaining, b""])
    result = await validate_upload(uf)
    assert result.file_type == "pdf"


@pytest.mark.asyncio
async def test_oversized_file_rejected():
    """A file exceeding 25 MB must return HTTP 413."""
    from app.core.upload_validation import validate_upload
    from app.config import settings
    from fastapi import HTTPException

    uf = _oversize_upload_file(settings.upload_max_bytes + 1)
    with pytest.raises(HTTPException) as exc_info:
        await validate_upload(uf)
    assert exc_info.value.status_code == 413
    assert "exceeds the maximum upload size" in exc_info.value.detail


# ══════════════════════════════════════════════════════════════════════
# VALID FILE TYPES
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_valid_pdf_accepted():
    from app.core.upload_validation import validate_upload
    content = _make_pdf_bytes("ISO 20022 payment standard")
    uf = _upload_file(content, "report.pdf")
    result = await validate_upload(uf)
    assert result.file_type == "pdf"


@pytest.mark.asyncio
async def test_valid_docx_accepted():
    from app.core.upload_validation import validate_upload
    content = _make_docx_bytes()
    uf = _upload_file(content, "spec.docx")
    result = await validate_upload(uf)
    assert result.file_type == "docx"


@pytest.mark.asyncio
async def test_valid_pptx_accepted():
    from app.core.upload_validation import validate_upload
    content = _make_pptx_bytes()
    uf = _upload_file(content, "deck.pptx")
    result = await validate_upload(uf)
    assert result.file_type == "pptx"


# ══════════════════════════════════════════════════════════════════════
# SPOOFING DETECTION — magic bytes override extension and Content-Type
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_zip_renamed_to_pdf_rejected():
    """A ZIP file with a .pdf extension must be rejected via magic-byte check."""
    from app.core.upload_validation import validate_upload
    from fastapi import HTTPException

    content = _make_zip_bytes()
    uf = _upload_file(content, "evil.pdf")
    with pytest.raises(HTTPException) as exc_info:
        await validate_upload(uf)
    assert exc_info.value.status_code == 422
    assert "Unsupported file type detected" in exc_info.value.detail


@pytest.mark.asyncio
async def test_exe_renamed_to_docx_rejected():
    """An executable (MZ header) with a .docx extension must be rejected."""
    from app.core.upload_validation import validate_upload
    from fastapi import HTTPException

    content = _make_exe_bytes()
    uf = _upload_file(content, "malware.docx")
    with pytest.raises(HTTPException) as exc_info:
        await validate_upload(uf)
    assert exc_info.value.status_code == 422
    assert "Unsupported file type detected" in exc_info.value.detail


@pytest.mark.asyncio
async def test_exe_renamed_to_pptx_rejected():
    """An executable (MZ header) with a .pptx extension must be rejected."""
    from app.core.upload_validation import validate_upload
    from fastapi import HTTPException

    content = _make_exe_bytes()
    uf = _upload_file(content, "malware.pptx")
    with pytest.raises(HTTPException) as exc_info:
        await validate_upload(uf)
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_empty_file_rejected():
    """An empty file must be rejected with 422."""
    from app.core.upload_validation import validate_upload
    from fastapi import HTTPException

    uf = MagicMock(spec=UploadFile)
    uf.filename = "empty.pdf"
    uf.read = AsyncMock(return_value=b"")
    with pytest.raises(HTTPException) as exc_info:
        await validate_upload(uf)
    assert exc_info.value.status_code == 422


# ══════════════════════════════════════════════════════════════════════
# PARSER HARDENING — corrupt files must not leak stack traces
# ══════════════════════════════════════════════════════════════════════

def test_corrupt_pdf_raises_value_error():
    """parse_pdf() must raise ValueError, not an unhandled library exception."""
    from app.ingestion.parsers import parse_pdf
    with pytest.raises(ValueError, match="Could not parse PDF"):
        parse_pdf(b"not a pdf at all")


def test_corrupt_docx_raises_value_error():
    """parse_docx() must raise ValueError on bad input."""
    from app.ingestion.parsers import parse_docx
    with pytest.raises(ValueError, match="Could not parse DOCX"):
        parse_docx(b"not a docx at all")


def test_corrupt_pptx_raises_value_error():
    """parse_pptx() must raise ValueError on bad input."""
    from app.ingestion.parsers import parse_pptx
    with pytest.raises(ValueError, match="Could not parse PPTX"):
        parse_pptx(b"not a pptx at all")


def test_corrupt_pdf_sets_failed_status_in_pipeline():
    """
    The ingestion pipeline must catch parse errors and mark the document
    as 'failed' with the error message — never crash silently.
    """
    from unittest.mock import patch as upatch, MagicMock, call
    from sqlalchemy import create_engine, StaticPool
    from sqlalchemy.orm import sessionmaker
    import tempfile, os

    from app.db.models import Base, Workspace, Document

    engine = create_engine(
        "sqlite:///file:corrupt_pdf_test?mode=memory&cache=shared&uri=true",
        connect_args={"check_same_thread": False, "uri": True},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    tmp_path = None
    try:
        ws = Workspace(name="Corrupt WS")
        db.add(ws)
        db.commit()

        # Write corrupt bytes to a temp file
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"this is not a valid pdf")
            tmp_path = f.name

        doc = Document(
            workspace_id=ws.id, filename=tmp_path,
            file_type="pdf", title="Corrupt", upload_status="pending",
        )
        db.add(doc)
        db.commit()
        doc_id = doc.id
        db.close()

        from app.ingestion.pipeline import run_ingestion_pipeline
        with upatch("app.ingestion.pipeline.SessionLocal", Session):
            run_ingestion_pipeline(doc_id)

        db2 = Session()
        doc2 = db2.get(Document, doc_id)
        assert doc2.upload_status == "failed"
        assert doc2.error_message is not None
        db2.close()
    finally:
        if tmp_path:
            os.unlink(tmp_path)
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


# ══════════════════════════════════════════════════════════════════════
# MEMORY SAFETY — validation occurs before full read
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_validation_reads_chunk_before_full_content():
    """
    validate_upload() must call file.read() with a small chunk size first,
    not read the entire file in a single call.
    """
    from app.core.upload_validation import validate_upload, _MAGIC_READ_BYTES

    content = _make_pdf_bytes("memory safety test")
    uf = _upload_file(content, "safe.pdf")
    await validate_upload(uf)

    # The first read call must request exactly _MAGIC_READ_BYTES, not a huge number
    first_call_arg = uf.read.call_args_list[0][0][0]
    assert first_call_arg == _MAGIC_READ_BYTES, (
        f"First read should request {_MAGIC_READ_BYTES} bytes, got {first_call_arg}"
    )


@pytest.mark.asyncio
async def test_oversized_file_never_fully_buffered():
    """
    For an oversized file, validate_upload() must raise 413 before the full
    content is assembled — i.e. the overflow check fires.
    """
    from app.core.upload_validation import validate_upload
    from app.config import settings
    from fastapi import HTTPException

    uf = _oversize_upload_file(settings.upload_max_bytes + 100)
    with pytest.raises(HTTPException) as exc_info:
        await validate_upload(uf)
    assert exc_info.value.status_code == 413
    # Three reads must have been called: first_chunk, bulk_read, overflow_check
    assert uf.read.call_count == 3
