"""
Parser unit tests — PDF/DOCX/PPTX extraction, hierarchical chunker, overlap,
MIME/extension detection, edge cases, and chunker stress tests.
"""
import io
import pytest


# ── Fixtures ──────────────────────────────────────────────────────────

def make_pdf_bytes(text: str) -> bytes:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 72), text)
    return doc.tobytes()


def make_multipage_pdf_bytes(pages: list) -> bytes:
    import fitz
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((50, 72), text)
    return doc.tobytes()


def make_docx_bytes(paragraphs: list) -> bytes:
    from docx import Document
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def make_pptx_bytes(slide_texts: list) -> bytes:
    from pptx import Presentation
    from pptx.util import Inches
    prs = Presentation()
    blank = prs.slide_layouts[6]
    for text in slide_texts:
        slide = prs.slides.add_slide(blank)
        tb = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(2))
        tb.text_frame.text = text
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════════════
# PDF
# ══════════════════════════════════════════════════════════════════════

def test_parse_pdf_extracts_text():
    from app.ingestion.parsers import parse_pdf
    text, _ = parse_pdf(make_pdf_bytes("ISO 20022 is a payment messaging standard."))
    assert "ISO 20022" in text


def test_parse_pdf_multipage():
    from app.ingestion.parsers import parse_pdf
    pages = ["SWIFT connects banks.", "ISO 20022 is the new standard."]
    text, meta = parse_pdf(make_multipage_pdf_bytes(pages))
    assert "SWIFT" in text
    assert "ISO 20022" in text
    assert meta["page_count"] == 2


def test_parse_pdf_metadata_keys():
    from app.ingestion.parsers import parse_pdf
    _, meta = parse_pdf(make_pdf_bytes("test"))
    assert "page_count" in meta
    assert "title" in meta
    assert "author" in meta
    assert meta["page_count"] >= 1


def test_parse_pdf_empty_page():
    from app.ingestion.parsers import parse_pdf
    text, meta = parse_pdf(make_pdf_bytes(""))
    assert isinstance(text, str)
    assert meta["page_count"] >= 1


# ══════════════════════════════════════════════════════════════════════
# DOCX
# ══════════════════════════════════════════════════════════════════════

def test_parse_docx_extracts_paragraphs():
    from app.ingestion.parsers import parse_docx
    text, _ = parse_docx(make_docx_bytes(["SWIFT is the global network.", "ISO 20022 is the standard."]))
    assert "SWIFT" in text
    assert "ISO 20022" in text


def test_parse_docx_multiple_paragraphs_joined():
    from app.ingestion.parsers import parse_docx
    paras = [f"Paragraph {i}" for i in range(10)]
    text, _ = parse_docx(make_docx_bytes(paras))
    for i in range(10):
        assert f"Paragraph {i}" in text


def test_parse_docx_metadata_keys():
    from app.ingestion.parsers import parse_docx
    _, meta = parse_docx(make_docx_bytes(["test"]))
    assert "title" in meta
    assert "author" in meta


def test_parse_docx_empty_doc():
    from app.ingestion.parsers import parse_docx
    text, meta = parse_docx(make_docx_bytes([]))
    assert isinstance(text, str)


# ══════════════════════════════════════════════════════════════════════
# PPTX
# ══════════════════════════════════════════════════════════════════════

def test_parse_pptx_extracts_text():
    from app.ingestion.parsers import parse_pptx
    text, _ = parse_pptx(make_pptx_bytes(["Tokenized deposits use blockchain.", "CBDCs are digital currencies."]))
    assert "Tokenized" in text
    assert "CBDC" in text


def test_parse_pptx_slide_count():
    from app.ingestion.parsers import parse_pptx
    _, meta = parse_pptx(make_pptx_bytes(["a", "b", "c"]))
    assert meta["slide_count"] == 3


def test_parse_pptx_slide_markers():
    """Each slide is prefixed with [Slide N]."""
    from app.ingestion.parsers import parse_pptx
    text, _ = parse_pptx(make_pptx_bytes(["Slide one content.", "Slide two content."]))
    assert "[Slide 1]" in text
    assert "[Slide 2]" in text


def test_parse_pptx_empty():
    from app.ingestion.parsers import parse_pptx
    text, meta = parse_pptx(make_pptx_bytes([]))
    assert isinstance(text, str)
    assert meta["slide_count"] == 0


def test_parse_document_router():
    from app.ingestion.parsers import parse_document
    text, _ = parse_document(make_pdf_bytes("ISO 20022"), "pdf")
    assert "ISO 20022" in text


def test_parse_document_unknown_type_raises():
    from app.ingestion.parsers import parse_document
    with pytest.raises(ValueError):
        parse_document(b"data", "xlsx")


# ══════════════════════════════════════════════════════════════════════
# chunk_text_hierarchical
# ══════════════════════════════════════════════════════════════════════

def test_hierarchical_short_text_not_split():
    from app.ingestion.parsers import chunk_text_hierarchical
    text = "A short consulting document."
    chunks = chunk_text_hierarchical(text, max_chars=10000)
    assert chunks == [text]


def test_hierarchical_empty_returns_empty():
    from app.ingestion.parsers import chunk_text_hierarchical
    assert chunk_text_hierarchical("", max_chars=1000) == []
    assert chunk_text_hierarchical("   ", max_chars=1000) == []


def test_hierarchical_splits_on_headers():
    from app.ingestion.parsers import chunk_text_hierarchical
    text = (
        "## Section One\n" + "Content one. " * 100 + "\n\n"
        "## Section Two\n" + "Content two. " * 100
    )
    chunks = chunk_text_hierarchical(text, max_chars=500)
    assert len(chunks) >= 2
    # Sections should stay together where possible
    assert any("Section One" in c for c in chunks)
    assert any("Section Two" in c for c in chunks)


def test_hierarchical_overlap_prepended():
    from app.ingestion.parsers import chunk_text_hierarchical
    # Force two chunks with 50-char overlap
    text = "## Alpha\n" + "X" * 600 + "\n\n## Beta\n" + "Y" * 600
    chunks = chunk_text_hierarchical(text, max_chars=700, overlap_chars=50)
    assert len(chunks) >= 2
    # Second chunk should contain tail of first chunk as overlap
    tail = chunks[0][-50:]
    assert tail in chunks[1]


def test_hierarchical_no_headers_falls_back_to_paragraphs():
    from app.ingestion.parsers import chunk_text_hierarchical
    # No headers — should split at paragraph boundaries
    text = "\n\n".join(["paragraph " + str(i) + " " + "w" * 200 for i in range(10)])
    chunks = chunk_text_hierarchical(text, max_chars=500)
    assert len(chunks) > 1


def test_hierarchical_all_chunks_non_empty():
    from app.ingestion.parsers import chunk_text_hierarchical
    text = "## Intro\n" + "A" * 2000 + "\n\n## Main\n" + "B" * 2000
    chunks = chunk_text_hierarchical(text, max_chars=500)
    for c in chunks:
        assert c.strip() != ""


def test_hierarchical_zero_overlap():
    from app.ingestion.parsers import chunk_text_hierarchical
    text = "## Section\n" + "X" * 2000 + "\n\n## Section2\n" + "Y" * 2000
    chunks = chunk_text_hierarchical(text, max_chars=1000, overlap_chars=0)
    assert len(chunks) >= 2


# ══════════════════════════════════════════════════════════════════════
# STRESS — CHUNKER
# ══════════════════════════════════════════════════════════════════════

def test_chunker_large_document_no_crash():
    """50 000-character document must chunk without error."""
    from app.ingestion.parsers import chunk_text_hierarchical
    sections = ["## Section %d\n" % i + "Word content here. " * 100 for i in range(25)]
    text = "\n\n".join(sections)
    assert len(text) > 40000
    chunks = chunk_text_hierarchical(text, max_chars=3000, overlap_chars=200)
    assert len(chunks) > 1
    for c in chunks:
        assert isinstance(c, str)
        assert len(c) > 0


def test_chunker_preserves_all_content():
    """Union of chunks must contain all original words (with overlap, may have more)."""
    from app.ingestion.parsers import chunk_text_hierarchical
    keywords = ["SWIFT", "ISO20022", "CBDC", "tokenization", "settlement"]
    text = "## Finance\n" + " ".join(keywords) + " " + ("filler " * 500)
    chunks = chunk_text_hierarchical(text, max_chars=500)
    combined = " ".join(chunks)
    for kw in keywords:
        assert kw in combined
