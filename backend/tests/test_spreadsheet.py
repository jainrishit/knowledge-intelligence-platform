"""
Spreadsheet intelligence pipeline tests.

Covers:
  - Excel parser (xlsx, xls)
  - CSV parser
  - Sheet analyser
  - Table detector
  - Schema extractor
  - Upload validation — xlsx, xls, csv allowed; corrupt/unsupported rejected
  - Ingestion pipeline routing (xlsx/xls/csv → spreadsheet path)
  - Concept extraction (mocked LLM)
  - Graph memory integration
  - Document deletion provenance cleanup
  - Admin ingestion-run metrics endpoint

All LLM calls are mocked.  No real API calls are made.
"""
from __future__ import annotations

import csv
import io
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.db.models import (
    Base, Workspace, Document, Concept, Relationship, SpreadsheetIngestionRun,
)
from app.db.session import get_db

# ── Test DB ────────────────────────────────────────────────────────────

TEST_DB_URL = "sqlite:///file:spreadsheet_test?mode=memory&cache=shared&uri=true"
test_engine = create_engine(
    TEST_DB_URL,
    connect_args={"check_same_thread": False, "uri": True},
)
TestSession = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
Base.metadata.create_all(bind=test_engine)


def override_get_db():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(scope="module", autouse=True)
def install_db_override():
    app.dependency_overrides[get_db] = override_get_db
    yield
    app.dependency_overrides.pop(get_db, None)


client = TestClient(app, raise_server_exceptions=True)


@pytest.fixture(autouse=True)
def reset_db():
    Base.metadata.drop_all(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)
    from app.graph.memory_manager import graph_memory_manager
    graph_memory_manager._store.clear()
    yield


# ── Helpers ────────────────────────────────────────────────────────────

def make_xlsx_bytes(sheets: dict[str, list[list[str]]]) -> bytes:
    """Build a minimal .xlsx file from a {sheet_name: rows} dict."""
    import openpyxl
    wb = openpyxl.Workbook()
    first = True
    for sheet_name, rows in sheets.items():
        if first:
            ws = wb.active
            ws.title = sheet_name
            first = False
        else:
            ws = wb.create_sheet(title=sheet_name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def make_csv_bytes(rows: list[list[str]], delimiter: str = ",") -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter)
    for row in rows:
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")


def make_xls_bytes(rows: list[list[str]]) -> bytes:
    """Build a minimal .xls file using xlwt (if available) or xlrd-compatible stub."""
    try:
        import xlwt
        wb = xlwt.Workbook()
        ws = wb.add_sheet("Sheet1")
        for r_idx, row in enumerate(rows):
            for c_idx, val in enumerate(row):
                ws.write(r_idx, c_idx, val)
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()
    except ImportError:
        pytest.skip("xlwt not installed; skipping XLS write test")


def _create_workspace(name="Test WS"):
    resp = client.post("/workspaces", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()


# ══════════════════════════════════════════════════════════════════════
# UNIT: excel_parser
# ══════════════════════════════════════════════════════════════════════

class TestExcelParser:
    def test_parse_xlsx_single_sheet(self):
        from app.ingestion.spreadsheets.excel_parser import parse_excel
        data = make_xlsx_bytes({"Sheet1": [
            ["Requirement ID", "Description", "Priority"],
            ["REQ-001", "ISO 20022 migration required", "High"],
            ["REQ-002", "SWIFT connectivity maintained", "Medium"],
        ]})
        wb = parse_excel(data, "reqs.xlsx")
        assert wb.sheet_count == 1
        assert wb.sheets[0].row_count == 2
        assert wb.sheets[0].headers == ["Requirement ID", "Description", "Priority"]
        assert wb.sheets[0].has_headers is True

    def test_parse_xlsx_multiple_sheets(self):
        from app.ingestion.spreadsheets.excel_parser import parse_excel
        data = make_xlsx_bytes({
            "Requirements": [["ID", "Desc"], ["R1", "Something"]],
            "Risks": [["Risk ID", "Risk"], ["K1", "Settlement failure"]],
        })
        wb = parse_excel(data, "workbook.xlsx")
        assert wb.sheet_count == 2
        assert {s.name for s in wb.sheets} == {"Requirements", "Risks"}

    def test_parse_xlsx_empty_sheet_is_marked(self):
        from app.ingestion.spreadsheets.excel_parser import parse_excel
        data = make_xlsx_bytes({"Empty": []})
        wb = parse_excel(data, "empty.xlsx")
        assert wb.sheets[0].is_empty is True

    def test_parse_xlsx_synthesises_raw_text(self):
        from app.ingestion.spreadsheets.excel_parser import parse_excel
        data = make_xlsx_bytes({"Sheet1": [
            ["Term", "Definition"],
            ["ISO 20022", "Financial messaging standard"],
        ]})
        wb = parse_excel(data, "glossary.xlsx")
        assert "ISO 20022" in wb.raw_text
        assert "Financial messaging standard" in wb.raw_text

    def test_parse_corrupt_xlsx_raises(self):
        from app.ingestion.spreadsheets.excel_parser import parse_excel
        with pytest.raises(ValueError, match="Could not parse"):
            parse_excel(b"not an excel file", "bad.xlsx")

    def test_parse_xlsx_header_detection_numeric_row(self):
        from app.ingestion.spreadsheets.excel_parser import _looks_like_headers
        # All-numeric first row should NOT be detected as headers
        assert _looks_like_headers(["1", "2", "3", "4"]) is False

    def test_parse_xlsx_header_detection_text_row(self):
        from app.ingestion.spreadsheets.excel_parser import _looks_like_headers
        assert _looks_like_headers(["Requirement ID", "Description", "Priority"]) is True


# ══════════════════════════════════════════════════════════════════════
# UNIT: csv_parser
# ══════════════════════════════════════════════════════════════════════

class TestCsvParser:
    def test_parse_csv_basic(self):
        from app.ingestion.spreadsheets.csv_parser import parse_csv
        data = make_csv_bytes([
            ["Risk ID", "Risk Description", "Owner"],
            ["R001", "Settlement failure", "Treasury"],
            ["R002", "AML regulatory breach", "Compliance"],
        ])
        wb = parse_csv(data, "risks.csv")
        assert wb.sheet_count == 1
        assert wb.sheets[0].row_count == 2
        assert "Risk ID" in wb.sheets[0].headers

    def test_parse_csv_tab_delimited(self):
        from app.ingestion.spreadsheets.csv_parser import parse_csv
        data = make_csv_bytes([
            ["Field", "Type", "Description"],
            ["account_id", "VARCHAR", "Primary key"],
        ], delimiter="\t")
        wb = parse_csv(data, "dict.csv")
        assert wb.sheets[0].row_count == 1

    def test_parse_csv_bom_encoding(self):
        from app.ingestion.spreadsheets.csv_parser import parse_csv
        content = "\ufeffName,Value\nISO 20022,Standard\n"
        data = content.encode("utf-8-sig")
        wb = parse_csv(data, "bom.csv")
        assert "Name" in wb.sheets[0].headers
        assert wb.sheets[0].rows[0]["Name"] == "ISO 20022"

    def test_parse_csv_empty_returns_empty_sheet(self):
        from app.ingestion.spreadsheets.csv_parser import parse_csv
        wb = parse_csv(b"", "empty.csv")
        assert wb.sheets[0].is_empty is True

    def test_parse_csv_synthesises_raw_text(self):
        from app.ingestion.spreadsheets.csv_parser import parse_csv
        data = make_csv_bytes([["Term", "Definition"], ["SWIFT", "Society for Worldwide Interbank Financial Telecommunication"]])
        wb = parse_csv(data, "g.csv")
        assert "SWIFT" in wb.raw_text


# ══════════════════════════════════════════════════════════════════════
# UNIT: sheet_analyser
# ══════════════════════════════════════════════════════════════════════

class TestSheetAnalyser:
    def _make_sheet(self, name, headers, rows=None):
        from app.ingestion.spreadsheets.excel_parser import SheetData
        rows = rows or [{"col": "val"}]
        return SheetData(
            name=name, headers=headers, rows=rows,
            row_count=len(rows), col_count=len(headers),
            is_empty=False, has_headers=True,
        )

    def test_classifies_requirements_sheet(self):
        from app.ingestion.spreadsheets.sheet_analyzer import analyse_sheet
        sheet = self._make_sheet(
            "Reqs",
            ["Requirement ID", "Description", "Priority", "Domain", "Status"],
            rows=[{"Requirement ID": "R1", "Description": "SWIFT migration", "Priority": "High", "Domain": "Payments", "Status": "Open"}],
        )
        analysis = analyse_sheet(sheet)
        assert analysis.sheet_type == "requirements"
        assert analysis.is_useful is True

    def test_classifies_risk_register(self):
        from app.ingestion.spreadsheets.sheet_analyzer import analyse_sheet
        sheet = self._make_sheet(
            "Risks",
            ["Risk ID", "Risk", "Likelihood", "Impact", "Mitigation", "Owner"],
            rows=[{"Risk ID": "R1", "Risk": "Settlement delay", "Likelihood": "High", "Impact": "High", "Mitigation": "Hedge", "Owner": "Treasury"}],
        )
        analysis = analyse_sheet(sheet)
        assert analysis.sheet_type == "risk_register"

    def test_classifies_mapping_sheet(self):
        from app.ingestion.spreadsheets.sheet_analyzer import analyse_sheet
        sheet = self._make_sheet(
            "Mappings",
            ["Source", "Target", "Rule", "ISO 20022 message"],
            rows=[{"Source": "MT103", "Target": "pacs.008", "Rule": "Direct", "ISO 20022 message": "pacs.008"}],
        )
        analysis = analyse_sheet(sheet)
        assert analysis.sheet_type == "mapping_sheet"

    def test_detects_payments_domain_hints(self):
        from app.ingestion.spreadsheets.sheet_analyzer import analyse_sheet
        sheet = self._make_sheet(
            "Payments",
            ["Standard", "Rail"],
            rows=[{"Standard": "ISO 20022", "Rail": "SWIFT"}],
        )
        analysis = analyse_sheet(sheet)
        assert "swift" in analysis.domain_hints or "iso 20022" in analysis.domain_hints

    def test_empty_sheet_not_useful(self):
        from app.ingestion.spreadsheets.excel_parser import SheetData
        from app.ingestion.spreadsheets.sheet_analyzer import analyse_sheet
        sheet = SheetData(name="Empty", headers=[], rows=[], row_count=0, col_count=0, is_empty=True, has_headers=False)
        analysis = analyse_sheet(sheet)
        assert analysis.is_useful is False


# ══════════════════════════════════════════════════════════════════════
# UNIT: table_detector
# ══════════════════════════════════════════════════════════════════════

class TestTableDetector:
    def test_detect_tables_from_useful_sheet(self):
        from app.ingestion.spreadsheets.excel_parser import SheetData
        from app.ingestion.spreadsheets.sheet_analyzer import SheetAnalysis
        from app.ingestion.spreadsheets.table_detector import detect_tables

        sheet = SheetData(
            name="Reqs", headers=["ID", "Description"],
            rows=[{"ID": "R1", "Description": "Settlement required"}],
            row_count=1, col_count=2, is_empty=False, has_headers=True,
        )
        analysis = SheetAnalysis(
            sheet_name="Reqs", sheet_type="requirements", confidence=0.8,
            key_columns=["Description"], domain_hints=[], row_count=1, is_useful=True,
        )
        tables = detect_tables(sheet, analysis)
        assert len(tables) == 1
        assert tables[0].row_count == 1
        assert "requirements" in tables[0].table_label.lower()

    def test_empty_sheet_yields_no_tables(self):
        from app.ingestion.spreadsheets.excel_parser import SheetData
        from app.ingestion.spreadsheets.sheet_analyzer import SheetAnalysis
        from app.ingestion.spreadsheets.table_detector import detect_tables

        sheet = SheetData(name="E", headers=[], rows=[], row_count=0, col_count=0, is_empty=True, has_headers=False)
        analysis = SheetAnalysis(
            sheet_name="E", sheet_type="empty", confidence=1.0,
            key_columns=[], domain_hints=[], row_count=0, is_useful=False,
        )
        assert detect_tables(sheet, analysis) == []

    def test_table_to_text_block_includes_headers_and_rows(self):
        from app.ingestion.spreadsheets.table_detector import DetectedTable, table_to_text_block
        table = DetectedTable(
            sheet_name="S", table_label="Requirements (Sheet: S)",
            headers=["ID", "Desc"], rows=[{"ID": "R1", "Desc": "ISO 20022 required"}],
            row_count=1, key_columns=["Desc"],
        )
        text = table_to_text_block(table)
        assert "ISO 20022 required" in text
        assert "Requirements" in text


# ══════════════════════════════════════════════════════════════════════
# UNIT: schema_extractor
# ══════════════════════════════════════════════════════════════════════

class TestSchemaExtractor:
    def test_extract_schema_identifies_entity_types(self):
        from app.ingestion.spreadsheets.excel_parser import SheetData, WorkbookData
        from app.ingestion.spreadsheets.sheet_analyzer import SheetAnalysis
        from app.ingestion.spreadsheets.schema_extractor import extract_schema

        sheet = SheetData(
            name="Risks", headers=["Risk ID", "Description", "Owner"],
            rows=[{"Risk ID": "R1", "Description": "Settlement risk", "Owner": "Treasury"}],
            row_count=1, col_count=3, is_empty=False, has_headers=True,
        )
        wb = WorkbookData(name="wb.xlsx", sheet_count=1, sheets=[sheet], raw_text="", metadata={})
        analysis = SheetAnalysis(
            sheet_name="Risks", sheet_type="risk_register", confidence=0.9,
            key_columns=["Description"], domain_hints=[], row_count=1, is_useful=True,
        )
        schema = extract_schema(wb, [analysis])
        assert schema.sheets[0].entity_type == "Risk"

    def test_schema_to_context_is_nonempty_string(self):
        from app.ingestion.spreadsheets.excel_parser import SheetData, WorkbookData
        from app.ingestion.spreadsheets.sheet_analyzer import SheetAnalysis
        from app.ingestion.spreadsheets.schema_extractor import extract_schema, schema_to_context

        sheet = SheetData(
            name="Glossary", headers=["Term", "Definition"],
            rows=[{"Term": "KYC", "Definition": "Know Your Customer"}],
            row_count=1, col_count=2, is_empty=False, has_headers=True,
        )
        wb = WorkbookData(name="g.xlsx", sheet_count=1, sheets=[sheet], raw_text="", metadata={})
        analysis = SheetAnalysis(
            sheet_name="Glossary", sheet_type="glossary", confidence=0.85,
            key_columns=["Term"], domain_hints=[], row_count=1, is_useful=True,
        )
        schema = extract_schema(wb, [analysis])
        ctx = schema_to_context(schema)
        assert isinstance(ctx, str)
        assert len(ctx) > 0


# ══════════════════════════════════════════════════════════════════════
# UPLOAD VALIDATION — spreadsheet types
# ══════════════════════════════════════════════════════════════════════

class TestSpreadsheetUploadValidation:
    def test_xlsx_accepted_by_magic_bytes(self):
        xlsx = make_xlsx_bytes({"S": [["A", "B"], ["1", "2"]]})
        ws = _create_workspace("xlsx_ws")
        with patch("app.api.documents.run_ingestion_pipeline"):
            r = client.post(
                f"/workspaces/{ws['id']}/documents",
                files=[("files", ("test.xlsx", xlsx, "application/octet-stream"))],
            )
        assert r.status_code == 202
        assert r.json()[0]["file_type"] == "xlsx"

    def test_csv_accepted_by_extension(self):
        csv_data = make_csv_bytes([["ID", "Name"], ["1", "ISO 20022"]])
        ws = _create_workspace("csv_ws")
        with patch("app.api.documents.run_ingestion_pipeline"):
            r = client.post(
                f"/workspaces/{ws['id']}/documents",
                files=[("files", ("data.csv", csv_data, "text/plain"))],
            )
        assert r.status_code == 202
        assert r.json()[0]["file_type"] == "csv"

    def test_unsupported_extension_rejected(self):
        ws = _create_workspace("bad_ws")
        with patch("app.api.documents.run_ingestion_pipeline"):
            r = client.post(
                f"/workspaces/{ws['id']}/documents",
                files=[("files", ("data.zip", b"PK\x03\x04fake", "application/zip"))],
            )
        assert r.status_code == 422

    def test_oversized_xlsx_rejected(self):
        from app.config import settings
        xlsx = make_xlsx_bytes({"S": [["A"], ["1"]]})
        ws = _create_workspace("big_ws")
        original = settings.upload_max_bytes
        settings.upload_max_bytes = 1
        try:
            with patch("app.api.documents.run_ingestion_pipeline"):
                r = client.post(
                    f"/workspaces/{ws['id']}/documents",
                    files=[("files", ("big.xlsx", xlsx, "application/octet-stream"))],
                )
            assert r.status_code == 413
        finally:
            settings.upload_max_bytes = original


# ══════════════════════════════════════════════════════════════════════
# INGESTION PIPELINE — routing
# ══════════════════════════════════════════════════════════════════════

class TestPipelineRouting:
    def test_spreadsheet_routes_to_spreadsheet_pipeline(self):
        """Uploading xlsx triggers the spreadsheet pipeline, not the document pipeline."""
        ws = _create_workspace("route_ws")
        xlsx = make_xlsx_bytes({"Reqs": [
            ["ID", "Description", "Priority"],
            ["R1", "ISO 20022 migration", "High"],
            ["R2", "SWIFT connectivity", "Medium"],
        ]})

        with patch("app.api.documents.run_ingestion_pipeline"):
            r = client.post(
                f"/workspaces/{ws['id']}/documents",
                files=[("files", ("reqs.xlsx", xlsx, "application/octet-stream"))],
            )
        assert r.status_code == 202

        # Run the pipeline synchronously using the test DB
        doc_id = r.json()[0]["id"]
        db = TestSession()
        try:
            doc = db.get(Document, doc_id)
            doc.upload_status = "processing"
            db.commit()

            from app.ingestion.pipeline import _run_spreadsheet_pipeline
            from app.db.models import IngestionAudit
            audit = IngestionAudit(workspace_id=doc.workspace_id, document_id=doc.id, status="processing")
            db.add(audit)
            db.commit()
            with patch("app.extraction.spreadsheet_concept_agent.chat", return_value="[]"), \
                 patch("app.extraction.relationship_agent.chat", return_value="[]"):
                _run_spreadsheet_pipeline(db, doc, audit)

            db.refresh(doc)
            assert doc.raw_text is not None
            assert "ISO 20022" in doc.raw_text or "ID" in doc.raw_text
        finally:
            db.close()

    def test_pdf_does_not_route_to_spreadsheet_pipeline(self):
        from app.ingestion.pipeline import _SPREADSHEET_TYPES
        assert "pdf" not in _SPREADSHEET_TYPES
        assert "xlsx" in _SPREADSHEET_TYPES
        assert "xls" in _SPREADSHEET_TYPES
        assert "csv" in _SPREADSHEET_TYPES


# ══════════════════════════════════════════════════════════════════════
# CONCEPT EXTRACTION — spreadsheet agent
# ══════════════════════════════════════════════════════════════════════

class TestSpreadsheetConceptExtractor:
    def _make_doc(self, db, ws_id: int, file_type: str = "xlsx") -> Document:
        doc = Document(
            workspace_id=ws_id, filename="/tmp/test.xlsx",
            file_type=file_type, title="Test WB", upload_status="processing",
        )
        db.add(doc)
        db.commit()
        db.refresh(doc)
        return doc

    def test_extracts_concepts_from_table(self):
        from app.ingestion.spreadsheets.table_detector import DetectedTable
        from app.extraction.spreadsheet_concept_agent import extract_spreadsheet_concepts

        db = TestSession()
        ws_resp = _create_workspace("ext_ws")
        ws = db.get(Workspace, ws_resp["id"])
        doc = self._make_doc(db, ws.id)

        mock_response = '''[
            {"name": "ISO 20022", "type": "Standard", "confidence": 0.95,
             "description": "Financial messaging standard", "source_excerpt": "Row: ISO 20022 required"},
            {"name": "SWIFT Migration", "type": "Requirement", "confidence": 0.9,
             "description": "Requirement to migrate to SWIFT", "source_excerpt": "REQ-001: SWIFT migration"}
        ]'''

        table = DetectedTable(
            sheet_name="Reqs",
            table_label="Requirements (Sheet: Reqs)",
            headers=["ID", "Description"],
            rows=[{"ID": "REQ-001", "Description": "SWIFT migration to ISO 20022"}],
            row_count=1,
            key_columns=["Description"],
        )

        with patch("app.extraction.spreadsheet_concept_agent.chat", return_value=mock_response):
            concepts = extract_spreadsheet_concepts(
                db=db, doc=doc,
                table_text="[Table: Reqs]\nID | Description\n  REQ-001 | SWIFT migration to ISO 20022",
                schema_context="Workbook: test.xlsx",
                sheet_type="requirements",
                domain_hints=["swift", "iso 20022"],
                table=table,
            )

        assert len(concepts) == 2
        names = {c.name for c in concepts}
        assert "ISO 20022" in names
        assert "SWIFT Migration" in names
        assert all(c.source_document_id == doc.id for c in concepts)
        db.close()

    def test_low_confidence_concept_excluded(self):
        from app.ingestion.spreadsheets.table_detector import DetectedTable
        from app.extraction.spreadsheet_concept_agent import extract_spreadsheet_concepts

        db = TestSession()
        ws_resp = _create_workspace("low_conf_ws")
        ws = db.get(Workspace, ws_resp["id"])
        doc = self._make_doc(db, ws.id)

        mock_response = '''[
            {"name": "Low Conf", "type": "General", "confidence": 0.3,
             "description": "low", "source_excerpt": "row"}
        ]'''

        table = DetectedTable(
            sheet_name="S", table_label="Generic (Sheet: S)",
            headers=["Col"], rows=[{"Col": "val"}], row_count=1, key_columns=["Col"],
        )
        with patch("app.extraction.spreadsheet_concept_agent.chat", return_value=mock_response):
            concepts = extract_spreadsheet_concepts(
                db=db, doc=doc, table_text="x",
                schema_context="x", sheet_type="generic",
                domain_hints=[], table=table,
            )
        assert len(concepts) == 0
        db.close()

    def test_duplicate_concept_not_created_twice(self):
        from app.ingestion.spreadsheets.table_detector import DetectedTable
        from app.extraction.spreadsheet_concept_agent import extract_spreadsheet_concepts

        db = TestSession()
        ws_resp = _create_workspace("dup_ws")
        ws = db.get(Workspace, ws_resp["id"])
        doc = self._make_doc(db, ws.id)

        # Pre-create an existing concept
        existing = Concept(
            workspace_id=ws.id, name="ISO 20022", type="Standard",
            description="Existing", source_document_id=doc.id,
            source_excerpt="existing", confidence=0.9,
        )
        db.add(existing)
        db.commit()

        mock_response = '''[
            {"name": "ISO 20022", "type": "Standard", "confidence": 0.95,
             "description": "dup", "source_excerpt": "row"}
        ]'''
        table = DetectedTable(
            sheet_name="S", table_label="L", headers=[], rows=[], row_count=0, key_columns=[],
        )
        with patch("app.extraction.spreadsheet_concept_agent.chat", return_value=mock_response):
            new_concepts = extract_spreadsheet_concepts(
                db=db, doc=doc, table_text="x", schema_context="x",
                sheet_type="generic", domain_hints=[], table=table,
            )
        assert len(new_concepts) == 0   # duplicate not created
        db.close()


# ══════════════════════════════════════════════════════════════════════
# GRAPH MEMORY INTEGRATION
# ══════════════════════════════════════════════════════════════════════

class TestSpreadsheetGraphMemory:
    def test_spreadsheet_concepts_appear_in_graph(self):
        """Concepts extracted from a spreadsheet must appear in the workspace graph."""
        ws_resp = _create_workspace("graph_ws")
        ws_id = ws_resp["id"]

        db = TestSession()
        ws = db.get(Workspace, ws_id)
        doc = Document(
            workspace_id=ws_id, filename="/tmp/x.xlsx",
            file_type="xlsx", title="Test", upload_status="processing",
        )
        db.add(doc)
        db.commit()
        db.refresh(doc)

        concept = Concept(
            workspace_id=ws_id, name="Settlement Risk",
            type="Risk", description="Risk of settlement failure",
            source_document_id=doc.id,
            source_excerpt="Row: Settlement Risk | High",
            confidence=0.92,
        )
        db.add(concept)
        db.commit()

        from app.graph.memory_manager import graph_memory_manager
        graph_memory_manager.add_document_contributions(ws_id, doc.id, db)
        G = graph_memory_manager.get_workspace_graph(ws_id, db)
        assert G.has_node(concept.id)
        db.close()

    def test_spreadsheet_delete_removes_from_graph(self):
        """Deleting a spreadsheet document removes its concepts from the graph."""
        ws_resp = _create_workspace("del_ws")
        ws_id = ws_resp["id"]

        db = TestSession()
        doc = Document(
            workspace_id=ws_id, filename="/tmp/y.xlsx",
            file_type="xlsx", title="Risks", upload_status="processing",
        )
        db.add(doc)
        db.commit()
        db.refresh(doc)

        concept = Concept(
            workspace_id=ws_id, name="AML Control",
            type="Control", description="Anti-money laundering control",
            source_document_id=doc.id,
            source_excerpt="Row: AML | Mandatory",
            confidence=0.88,
        )
        db.add(concept)
        db.commit()

        from app.graph.memory_manager import graph_memory_manager
        graph_memory_manager.add_document_contributions(ws_id, doc.id, db)
        G = graph_memory_manager.get_workspace_graph(ws_id, db)
        assert G.has_node(concept.id)

        graph_memory_manager.remove_document_contributions(ws_id, doc.id)
        G = graph_memory_manager.get_workspace_graph(ws_id, db)
        assert not G.has_node(concept.id)
        db.close()


# ══════════════════════════════════════════════════════════════════════
# SpreadsheetIngestionRun model
# ══════════════════════════════════════════════════════════════════════

class TestSpreadsheetIngestionRunModel:
    def test_ingestion_run_recorded(self):
        from datetime import datetime, timezone
        db = TestSession()
        ws_resp = _create_workspace("run_ws")
        ws_id = ws_resp["id"]
        doc = Document(
            workspace_id=ws_id, filename="/tmp/r.xlsx",
            file_type="xlsx", title="Run", upload_status="complete",
        )
        db.add(doc)
        db.commit()
        db.refresh(doc)

        now = datetime.now(timezone.utc)
        run = SpreadsheetIngestionRun(
            workspace_id=ws_id,
            document_id=doc.id,
            file_type="xlsx",
            started_at=now,
            completed_at=now,
            sheets_processed=2,
            tables_detected=3,
            concepts_extracted=10,
            relationships_extracted=5,
            status="complete",
        )
        db.add(run)
        db.commit()

        loaded = db.query(SpreadsheetIngestionRun).filter(
            SpreadsheetIngestionRun.document_id == doc.id
        ).first()
        assert loaded is not None
        assert loaded.concepts_extracted == 10
        assert loaded.status == "complete"
        db.close()


# ══════════════════════════════════════════════════════════════════════
# ADMIN API — ingestion runs endpoint
# ══════════════════════════════════════════════════════════════════════

class TestAdminSpreadsheetEndpoint:
    def test_ingestion_runs_endpoint_returns_ok(self):
        r = client.get("/admin/spreadsheet/ingestion-runs")
        assert r.status_code == 200
        body = r.json()
        assert "runs" in body
        assert "total_runs" in body
        assert "generated_at" in body

    def test_ingestion_runs_workspace_filter(self):
        r = client.get("/admin/spreadsheet/ingestion-runs?workspace_id=999")
        assert r.status_code == 200
        assert r.json()["total_runs"] == 0


# ══════════════════════════════════════════════════════════════════════
# SPREADSHEET CONCEPT TYPES
# ══════════════════════════════════════════════════════════════════════

class TestSpreadsheetConceptTypes:
    def test_required_types_present(self):
        from app.db.models import SPREADSHEET_CONCEPT_TYPES
        required = {"Requirement", "Risk", "Issue", "Dependency", "Process",
                    "Capability", "Data Element", "Control", "Business Rule"}
        assert required.issubset(SPREADSHEET_CONCEPT_TYPES)

    def test_normalise_type_requirement(self):
        from app.extraction.spreadsheet_concept_agent import _normalise_type
        assert _normalise_type("requirement") == "Requirement"
        assert _normalise_type("Risk") == "Risk"
        assert _normalise_type("unknown_xyz") == "General"


# ══════════════════════════════════════════════════════════════════════
# PAYMENTS-SPECIFIC DOMAIN DETECTION
# ══════════════════════════════════════════════════════════════════════

class TestPaymentsDomainDetection:
    def test_iso_20022_detected_in_headers(self):
        from app.ingestion.spreadsheets.excel_parser import SheetData
        from app.ingestion.spreadsheets.sheet_analyzer import analyse_sheet
        sheet = SheetData(
            name="ISO Mapping",
            headers=["MT Message", "ISO 20022 MX", "Transformation Rule"],
            rows=[{"MT Message": "MT103", "ISO 20022 MX": "pacs.008", "Transformation Rule": "Direct"}],
            row_count=1, col_count=3, is_empty=False, has_headers=True,
        )
        analysis = analyse_sheet(sheet)
        assert "iso 20022" in analysis.domain_hints

    def test_swift_detected_in_cell_values(self):
        from app.ingestion.spreadsheets.excel_parser import SheetData
        from app.ingestion.spreadsheets.sheet_analyzer import analyse_sheet
        sheet = SheetData(
            name="Rails",
            headers=["Rail", "Protocol"],
            rows=[{"Rail": "SWIFT", "Protocol": "CBPR+"}],
            row_count=1, col_count=2, is_empty=False, has_headers=True,
        )
        analysis = analyse_sheet(sheet)
        assert "swift" in analysis.domain_hints

    def test_multiple_payment_terms_detected(self):
        from app.ingestion.spreadsheets.excel_parser import SheetData
        from app.ingestion.spreadsheets.sheet_analyzer import analyse_sheet
        sheet = SheetData(
            name="Compliance",
            headers=["Control", "Regulation", "Standard"],
            rows=[{"Control": "AML check", "Regulation": "KYC", "Standard": "SEPA compliance"}],
            row_count=1, col_count=3, is_empty=False, has_headers=True,
        )
        analysis = analyse_sheet(sheet)
        assert "aml" in analysis.domain_hints
        assert "kyc" in analysis.domain_hints
