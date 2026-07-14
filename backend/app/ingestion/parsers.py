"""
Document parsers — extract raw text and metadata from PDF, DOCX, PPTX.
Each parser returns (raw_text: str, metadata: dict).

chunk_text_hierarchical() splits at section/header boundaries first,
then at paragraph boundaries for oversized sections, and adds overlap
between adjacent chunks so no context is lost at chunk edges.
"""
from __future__ import annotations
import io
import re
from pathlib import Path
from typing import Optional


def parse_pdf(file_bytes: bytes) -> tuple[str, dict]:
    """Extract text from PDF using PyMuPDF."""
    import fitz  # PyMuPDF

    doc = fitz.open(stream=file_bytes, filetype="pdf")
    pages = []
    for page in doc:
        pages.append(page.get_text("text"))
    raw_text = "\n\n".join(pages)
    meta = doc.metadata or {}
    metadata = {
        "title": meta.get("title") or "",
        "author": meta.get("author") or "",
        "page_count": doc.page_count,
    }
    doc.close()
    return raw_text, metadata


def parse_docx(file_bytes: bytes) -> tuple[str, dict]:
    """Extract text from DOCX using python-docx."""
    from docx import Document

    doc = Document(io.BytesIO(file_bytes))
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                if cell.text.strip():
                    paragraphs.append(cell.text.strip())

    raw_text = "\n\n".join(paragraphs)
    core_props = doc.core_properties
    metadata = {
        "title": core_props.title or "",
        "author": core_props.author or "",
    }
    return raw_text, metadata


def parse_pptx(file_bytes: bytes) -> tuple[str, dict]:
    """Extract text from PPTX using python-pptx."""
    from pptx import Presentation

    prs = Presentation(io.BytesIO(file_bytes))
    slides_text = []
    for i, slide in enumerate(prs.slides):
        parts = []
        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text.strip():
                parts.append(shape.text.strip())
        if parts:
            slides_text.append(f"[Slide {i + 1}]\n" + "\n".join(parts))

    raw_text = "\n\n".join(slides_text)
    core_props = prs.core_properties
    metadata = {
        "title": core_props.title or "",
        "author": core_props.author or "",
        "slide_count": len(prs.slides),
    }
    return raw_text, metadata


def detect_file_type(filename: str, content_type: Optional[str] = None) -> Optional[str]:
    """Return 'pdf', 'docx', 'pptx' or None for unsupported types."""
    ext = Path(filename).suffix.lower()
    mapping = {".pdf": "pdf", ".docx": "docx", ".pptx": "pptx"}
    mime_mapping = {
        "application/pdf": "pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    }
    return mapping.get(ext) or (mime_mapping.get(content_type or "") if content_type else None)


def parse_document(file_bytes: bytes, file_type: str) -> tuple[str, dict]:
    """Route to the correct parser based on file_type."""
    parsers = {"pdf": parse_pdf, "docx": parse_docx, "pptx": parse_pptx}
    if file_type not in parsers:
        raise ValueError(f"Unsupported file type: {file_type}")
    return parsers[file_type](file_bytes)


# ── Chunking ──────────────────────────────────────────────────────────────────

# Matches Markdown/document headers: #, ##, ### or ALL-CAPS lines ≥ 4 chars
_HEADER_RE = re.compile(r"^(#{1,4}\s+.+|[A-Z][A-Z0-9 \-:]{3,})$", re.MULTILINE)


def chunk_text(text: str, max_chars: int = 12000) -> list[str]:
    """Paragraph-aware chunker. For new code prefer chunk_text_hierarchical()."""
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    paragraphs = text.split("\n\n")
    current: list[str] = []
    current_len = 0
    for para in paragraphs:
        if current_len + len(para) + 2 > max_chars and current:
            chunks.append("\n\n".join(current))
            current = []
            current_len = 0
        current.append(para)
        current_len += len(para) + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def chunk_text_hierarchical(
    text: str,
    max_chars: int = 12000,
    overlap_chars: int = 200,
) -> list[str]:
    """
    Hierarchical chunker that splits at section/header boundaries, then
    at paragraph boundaries for oversized sections, and prepends
    overlap_chars of the previous chunk to the next to avoid losing
    context at boundaries. Returns a flat list of text chunks.
    """
    if not text or len(text.strip()) == 0:
        return []

    if len(text) <= max_chars:
        return [text]

    # Step 1: split at header boundaries
    header_positions = [m.start() for m in _HEADER_RE.finditer(text)]

    if not header_positions:
        return _split_by_paragraphs(text, max_chars, overlap_chars)

    section_texts: list[str] = []
    boundaries = header_positions + [len(text)]
    for i in range(len(header_positions)):
        section = text[boundaries[i]:boundaries[i + 1]].strip()
        if section:
            section_texts.append(section)

    # Step 2: split oversized sections by paragraphs
    fine_chunks: list[str] = []
    for section in section_texts:
        if len(section) <= max_chars:
            fine_chunks.append(section)
        else:
            fine_chunks.extend(_split_by_paragraphs(section, max_chars, overlap_chars))

    # Step 3: add overlap between adjacent chunks
    if overlap_chars <= 0 or len(fine_chunks) <= 1:
        return fine_chunks

    overlapped: list[str] = [fine_chunks[0]]
    for i in range(1, len(fine_chunks)):
        prev_tail = fine_chunks[i - 1][-overlap_chars:]
        overlapped.append(prev_tail + "\n\n" + fine_chunks[i])

    return overlapped


def _split_by_paragraphs(
    text: str,
    max_chars: int,
    overlap_chars: int,
) -> list[str]:
    """Split text at paragraph boundaries with overlap."""
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    prev_tail = ""

    for para in paragraphs:
        para_len = len(para) + 2
        if current_len + para_len > max_chars and current:
            chunk = "\n\n".join(current)
            chunks.append((prev_tail + "\n\n" + chunk).strip() if prev_tail else chunk)
            prev_tail = chunk[-overlap_chars:] if overlap_chars > 0 else ""
            current = []
            current_len = 0
        current.append(para)
        current_len += para_len

    if current:
        chunk = "\n\n".join(current)
        chunks.append((prev_tail + "\n\n" + chunk).strip() if prev_tail else chunk)

    return chunks if chunks else [text]
